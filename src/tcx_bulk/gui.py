"""Okienko (GUI) do masowych zmian w 3CX - dla osob, ktore nie chca uzywac terminala.

Przeplyw pracy:
  1. Polacz  -> logowanie do PBX, pobranie listy encji
  2. Eksportuj (opcjonalnie) -> aktualny stan do CSV; to jest tez wzor do edycji
  3. Wczytaj CSV -> PODGLAD zmian (nic jeszcze nie jest zmieniane!)
  4. Zastosuj -> potwierdzenie, backup, wykonanie
  5. Cofnij -> przywrocenie starych wartosci z backupu
"""
from __future__ import annotations

import json
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, List, Optional

from . import __version__
from .bulk import (PlanError, build_plan, execute, export_records, format_op, guess_key,
                   read_csv, rollback, summarize, write_csv, Op)
from .client import ApiError, XapiClient

CONFIG = Path.home() / ".tcx-bulk" / "profile.json"
RUNS = Path.home() / "tcx-bulk-runs"
COMMON_ENTITIES = ["Users", "Groups", "Queues", "RingGroups", "Trunks", "Parkings", "Peers", "Receptionists"]


class App(ttk.Frame):
    def __init__(self, master: tk.Tk, client_factory: Optional[Callable[..., XapiClient]] = None,
                 sync: bool = False, confirm: Optional[Callable[[str, str], bool]] = None):
        super().__init__(master, padding=10)
        self.master, self.sync = master, sync  # sync=True: bez watkow (do testow)
        self.client_factory = client_factory or XapiClient
        self.confirm = confirm or (lambda title, msg: messagebox.askyesno(title, msg, icon="warning"))
        self.client: Optional[XapiClient] = None
        self.ops: List[Op] = []
        self.key = ""
        self.csv_path = ""
        self.q: "queue.Queue[Callable[[], None]]" = queue.Queue()
        master.title(f"3CX Bulk Tool {__version__}")
        master.geometry("1020x740")
        self.pack(fill="both", expand=True)
        self._build()
        self._load_profile()
        self.after(100, self._pump)

    # ------------------------------------------------------------ UI
    def _build(self) -> None:
        self.v = {k: tk.StringVar() for k in ("fqdn", "cid", "secret", "ca", "entity", "key")}
        self.insecure = tk.BooleanVar(value=False)
        self.allow_create = tk.BooleanVar(value=False)
        self.allow_delete = tk.BooleanVar(value=False)

        box = ttk.LabelFrame(self, text="1. Połączenie z centralą 3CX", padding=8)
        box.pack(fill="x")
        for i, (label, key, show) in enumerate([
            ("Adres PBX (np. pbx.firma.ie)", "fqdn", ""), ("Client ID", "cid", ""),
            ("Client Secret (API Key)", "secret", "*"), ("Plik CA (opcjonalnie)", "ca", "")]):
            ttk.Label(box, text=label).grid(row=i // 2, column=(i % 2) * 2, sticky="w", padx=4, pady=2)
            ttk.Entry(box, textvariable=self.v[key], show=show, width=26).grid(row=i // 2, column=(i % 2) * 2 + 1, padx=4, pady=2)
        ttk.Checkbutton(box, text="Nie sprawdzaj certyfikatu TLS (niezalecane)", variable=self.insecure).grid(row=2, column=0, columnspan=2, sticky="w")
        self.btn_connect = ttk.Button(box, text="Połącz i sprawdź", command=self.connect)
        self.btn_connect.grid(row=2, column=3, sticky="e", pady=4)
        self.status = ttk.Label(box, text="Niepołączone", foreground="#a00")
        self.status.grid(row=2, column=2, sticky="w")

        box = ttk.LabelFrame(self, text="2. Co zmieniamy", padding=8)
        box.pack(fill="x", pady=6)
        ttk.Label(box, text="Encja (typ danych)").grid(row=0, column=0, sticky="w", padx=4)
        self.cb_entity = ttk.Combobox(box, textvariable=self.v["entity"], values=COMMON_ENTITIES, width=30)
        self.cb_entity.grid(row=0, column=1, padx=4)
        self.v["entity"].set("Users")
        ttk.Label(box, text="Kolumna klucza (puste = auto)").grid(row=0, column=2, sticky="w", padx=4)
        ttk.Entry(box, textvariable=self.v["key"], width=16).grid(row=0, column=3, padx=4)
        ttk.Button(box, text="Eksportuj stan do CSV…", command=self.export).grid(row=0, column=4, padx=8)

        box = ttk.LabelFrame(self, text="3. Import CSV i podgląd", padding=8)
        box.pack(fill="x")
        ttk.Button(box, text="Wczytaj CSV (tylko podgląd)…", command=self.load_csv).pack(side="left")
        ttk.Checkbutton(box, text="Zezwól na tworzenie (action=create)", variable=self.allow_create).pack(side="left", padx=10)
        ttk.Checkbutton(box, text="Zezwól na usuwanie (action=delete)", variable=self.allow_delete).pack(side="left")

        box = ttk.LabelFrame(self, text="4. Wykonanie", padding=8)
        box.pack(fill="x", pady=6)
        self.btn_apply = ttk.Button(box, text="ZASTOSUJ ZMIANY", command=self.apply, state="disabled")
        self.btn_apply.pack(side="left")
        ttk.Button(box, text="Cofnij zmiany z backupu…", command=self.rollback).pack(side="left", padx=10)
        self.progress = ttk.Progressbar(box, mode="determinate", length=220)
        self.progress.pack(side="right")

        self.log = scrolledtext.ScrolledText(self, height=18, font=("Courier", 10), state="disabled")
        self.log.pack(fill="both", expand=True)

    def _load_profile(self) -> None:
        try:
            d = json.loads(CONFIG.read_text(encoding="utf-8"))
            self.v["fqdn"].set(d.get("fqdn", "")); self.v["cid"].set(d.get("cid", "")); self.v["ca"].set(d.get("ca", ""))
        except (OSError, ValueError):
            pass  # brak profilu to nie blad

    def _save_profile(self) -> None:
        try:  # sekretu NIE zapisujemy na dysku
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps({k: self.v[k].get() for k in ("fqdn", "cid", "ca")}), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ pomocnicze
    def say(self, text: str = "") -> None:
        self.q.put(lambda: self._say(text))

    def _say(self, text: str) -> None:
        self.log.configure(state="normal"); self.log.insert("end", text + "\n")
        self.log.see("end"); self.log.configure(state="disabled")

    def _pump(self) -> None:
        while True:
            try:
                self.q.get_nowait()()
            except queue.Empty:
                break
        self.after(100, self._pump)

    def flush(self) -> None:  # do testow
        while not self.q.empty():
            self.q.get_nowait()()

    def work(self, fn: Callable[[], None]) -> None:
        """Wykonuje fn w tle (zeby okno nie 'zamarzalo'); bledy pokazuje w logu."""
        def run() -> None:
            try:
                fn()
            except (ApiError, PlanError) as e:
                self.say(f"\nBŁĄD: {e}")
            except Exception as e:  # noqa: BLE001 - nie chcemy, zeby okno po cichu padlo
                self.say(f"\nNIESPODZIEWANY BŁĄD: {type(e).__name__}: {e}")
        if self.sync:
            run()
            self.flush()
        else:
            threading.Thread(target=run, daemon=True).start()

    def need_client(self) -> bool:
        if self.client is None:
            messagebox.showinfo("Najpierw połącz", "Kliknij 'Połącz i sprawdź'.") if not self.sync else None
            self.say("Najpierw połącz się z centralą.")
            return False
        return True

    # ------------------------------------------------------------ akcje
    def connect(self) -> None:
        fqdn, cid, secret = (self.v[k].get().strip() for k in ("fqdn", "cid", "secret"))
        if not (fqdn and cid and secret):
            self.say("Uzupełnij adres PBX, Client ID i Client Secret."); return
        verify = False if self.insecure.get() else (self.v["ca"].get().strip() or True)
        self._save_profile()

        def job() -> None:
            self.say(f"Łączę z {fqdn} ...")
            c = self.client_factory(fqdn, cid, secret, verify=verify)
            try:
                names = c.entity_sets()
            except ApiError as e:
                if e.status in (401, 403) or "Logowanie" in str(e):
                    raise
                names = []  # $metadata niedostepne - zostaje lista podstawowa
            c.list_all("Users", page=1)  # test logowania + uprawnien
            self.client = c
            def ui() -> None:
                self.status.configure(text="Połączono", foreground="#070")
                self.cb_entity.configure(values=names or COMMON_ENTITIES)
            self.q.put(ui)
            self.say(f"Połączono. Dostępnych encji: {len(names) or 'brak listy (użyję podstawowej)'}.")
        self.work(job)

    def _key_for(self, records) -> str:
        return self.v["key"].get().strip() or guess_key(records)

    def export(self) -> None:
        if not self.need_client(): return
        entity = self.v["entity"].get().strip()
        path = getattr(self, "_test_path", None) or filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=f"export_{entity}.csv", filetypes=[("CSV", "*.csv")])
        if not path: return
        def job() -> None:
            self.say(f"Pobieram {entity} …")
            recs = self.client.list_all(entity)
            headers, rows = export_records(recs)
            write_csv(Path(path), headers, rows)
            self.say(f"Zapisano {len(recs)} rekordów do {path}\n"
                     "UWAGA: plik zawiera dane osobowe (RODO) – nie wysyłaj go mailem.")
        self.work(job)

    def load_csv(self) -> None:
        if not self.need_client(): return
        path = getattr(self, "_test_path", None) or filedialog.askopenfilename(filetypes=[("CSV", "*.csv *.txt"), ("Wszystko", "*.*")])
        if not path: return
        entity = self.v["entity"].get().strip()
        def job() -> None:
            self.ops, self.csv_path = [], path
            self.q.put(lambda: self.btn_apply.configure(state="disabled"))
            headers, rows = read_csv(Path(path))
            if not rows:
                self.say("Plik CSV nie ma wierszy z danymi."); return
            self.say(f"\n=== PODGLĄD ({Path(path).name}) – nic jeszcze nie zmieniono ===")
            recs = self.client.list_all(entity)
            self.key = self._key_for(recs)
            self.say(f"Encja: {entity}, kolumna klucza: {self.key}")
            ops = build_plan(rows, headers, recs, self.key, self.allow_create.get(), self.allow_delete.get())
            for o in ops:
                if o.action != "nochange":
                    self.say(format_op(o))
            s = summarize(ops)
            self.say(f"\nPODSUMOWANIE: zmian={s['update']}, nowych={s['create']}, usunięć={s['delete']}, "
                     f"bez zmian={s['nochange']}, błędów={s['error']}")
            self.ops = ops
            if s["update"] + s["create"] + s["delete"]:
                self.say("Jeśli podgląd jest OK, kliknij ZASTOSUJ ZMIANY." + (
                    "\nWiersze z błędami zostaną POMINIĘTE." if s["error"] else ""))
                self.q.put(lambda: self.btn_apply.configure(state="normal"))
            else:
                self.say("Nic do zrobienia.")
        self.work(job)

    def apply(self) -> None:
        if not self.need_client() or not self.ops: return
        s = summarize(self.ops)
        n = s["update"] + s["create"] + s["delete"]
        msg = (f"Za chwilę zmienię dane w centrali {self.client.base}:\n\n  zmian: {s['update']}\n  nowych: {s['create']}\n"
               f"  USUNIĘĆ: {s['delete']}\n  pominiętych błędów: {s['error']}\n\nPrzed zmianami zapiszę backup. Kontynuować?")
        if not self.confirm("Potwierdź zmiany", msg):
            self.say("Anulowano."); return
        entity = self.v["entity"].get().strip()
        ops = self.ops
        run_dir = RUNS / time.strftime("%Y%m%d-%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        self.q.put(lambda: (self.btn_apply.configure(state="disabled"), self.progress.configure(maximum=n, value=0)))
        def job() -> None:
            self.say(f"\n=== WYKONUJĘ {n} zmian ===")
            def prog(line: str) -> None:
                self.say(line); self.q.put(lambda: self.progress.step(1))
            st = execute(ops, self.client, entity, run_dir, progress=prog)
            self.say(f"\nGOTOWE: ok={st['ok']}, błędy={st['failed']}\nBackup i wyniki: {run_dir}")
            self.say("Cofnięcie: przycisk „Cofnij zmiany z backupu” i wybierz backup.json z tego folderu.")
            self.ops = []
        self.work(job)

    def rollback(self) -> None:
        if not self.need_client(): return
        path = getattr(self, "_test_path", None) or filedialog.askopenfilename(
            initialdir=str(RUNS), filetypes=[("backup.json", "*.json")])
        if not path: return
        if not self.confirm("Cofnąć zmiany?", f"Przywrócę stare wartości z:\n{path}\n\nKontynuować?"):
            self.say("Anulowano."); return
        def job() -> None:
            self.say("\n=== COFANIE ZMIAN ===")
            st = rollback(Path(path), self.client, True, progress=self.say)
            self.say(f"GOTOWE: przywrócono={st['ok']}, błędy={st['failed']}, nieprzywracalne={st['not_restorable']}")
        self.work(job)


def main() -> int:
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
