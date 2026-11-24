"""Desktop window (GUI) for bulk changes in 3CX - for people who prefer not to use a terminal.

Workflow:
  1. Connect  -> log in to the PBX and fetch the list of entities
  2. Export (optional) -> current state to CSV; this is also a template to edit
  3. Load CSV -> PREVIEW of changes (nothing is changed yet!)
  4. Apply -> confirmation, backup, execution
  5. Undo -> restore the old values from the backup
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
        self.master, self.sync = master, sync  # sync=True: no threads (for tests)
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

        box = ttk.LabelFrame(self, text="1. Connection to the 3CX PBX", padding=8)
        box.pack(fill="x")
        for i, (label, key, show) in enumerate([
            ("PBX address (e.g. pbx.company.ie)", "fqdn", ""), ("Client ID", "cid", ""),
            ("Client Secret (API Key)", "secret", "*"), ("CA file (optional)", "ca", "")]):
            ttk.Label(box, text=label).grid(row=i // 2, column=(i % 2) * 2, sticky="w", padx=4, pady=2)
            ttk.Entry(box, textvariable=self.v[key], show=show, width=26).grid(row=i // 2, column=(i % 2) * 2 + 1, padx=4, pady=2)
        ttk.Checkbutton(box, text="Do not verify TLS certificate (not recommended)", variable=self.insecure).grid(row=2, column=0, columnspan=2, sticky="w")
        self.btn_connect = ttk.Button(box, text="Connect and test", command=self.connect)
        self.btn_connect.grid(row=2, column=3, sticky="e", pady=4)
        self.status = ttk.Label(box, text="Not connected", foreground="#a00")
        self.status.grid(row=2, column=2, sticky="w")

        box = ttk.LabelFrame(self, text="2. What to change", padding=8)
        box.pack(fill="x", pady=6)
        ttk.Label(box, text="Entity (data type)").grid(row=0, column=0, sticky="w", padx=4)
        self.cb_entity = ttk.Combobox(box, textvariable=self.v["entity"], values=COMMON_ENTITIES, width=30)
        self.cb_entity.grid(row=0, column=1, padx=4)
        self.v["entity"].set("Users")
        ttk.Label(box, text="Key column (empty = auto)").grid(row=0, column=2, sticky="w", padx=4)
        ttk.Entry(box, textvariable=self.v["key"], width=16).grid(row=0, column=3, padx=4)
        ttk.Button(box, text="Export current state to CSV…", command=self.export).grid(row=0, column=4, padx=8)

        box = ttk.LabelFrame(self, text="3. Import CSV and preview", padding=8)
        box.pack(fill="x")
        ttk.Button(box, text="Load CSV (preview only)…", command=self.load_csv).pack(side="left")
        ttk.Checkbutton(box, text="Allow creating (action=create)", variable=self.allow_create).pack(side="left", padx=10)
        ttk.Checkbutton(box, text="Allow deleting (action=delete)", variable=self.allow_delete).pack(side="left")

        box = ttk.LabelFrame(self, text="4. Execute", padding=8)
        box.pack(fill="x", pady=6)
        self.btn_apply = ttk.Button(box, text="APPLY CHANGES", command=self.apply, state="disabled")
        self.btn_apply.pack(side="left")
        ttk.Button(box, text="Undo changes from a backup…", command=self.rollback).pack(side="left", padx=10)
        self.progress = ttk.Progressbar(box, mode="determinate", length=220)
        self.progress.pack(side="right")

        self.log = scrolledtext.ScrolledText(self, height=18, font=("Courier", 10), state="disabled")
        self.log.pack(fill="both", expand=True)

    def _load_profile(self) -> None:
        try:
            d = json.loads(CONFIG.read_text(encoding="utf-8"))
            self.v["fqdn"].set(d.get("fqdn", "")); self.v["cid"].set(d.get("cid", "")); self.v["ca"].set(d.get("ca", ""))
        except (OSError, ValueError):
            pass  # no saved profile is fine

    def _save_profile(self) -> None:
        try:  # the secret is NEVER written to disk
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps({k: self.v[k].get() for k in ("fqdn", "cid", "ca")}), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ helpers
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

    def flush(self) -> None:  # for tests
        while not self.q.empty():
            self.q.get_nowait()()

    def work(self, fn: Callable[[], None]) -> None:
        """Run fn in the background (so the window does not freeze); errors go to the log."""
        def run() -> None:
            try:
                fn()
            except (ApiError, PlanError) as e:
                self.say(f"\nERROR: {e}")
            except Exception as e:  # noqa: BLE001 - never let the window die silently
                self.say(f"\nUNEXPECTED ERROR: {type(e).__name__}: {e}")
        if self.sync:
            run()
            self.flush()
        else:
            threading.Thread(target=run, daemon=True).start()

    def need_client(self) -> bool:
        if self.client is None:
            if not self.sync:
                messagebox.showinfo("Connect first", "Click “Connect and test” first.")
            self.say("Connect to the PBX first.")
            return False
        return True

    # ------------------------------------------------------------ actions
    def connect(self) -> None:
        fqdn, cid, secret = (self.v[k].get().strip() for k in ("fqdn", "cid", "secret"))
        if not (fqdn and cid and secret):
            self.say("Fill in the PBX address, Client ID and Client Secret."); return
        verify = False if self.insecure.get() else (self.v["ca"].get().strip() or True)
        self._save_profile()

        def job() -> None:
            self.say(f"Connecting to {fqdn} ...")
            c = self.client_factory(fqdn, cid, secret, verify=verify)
            try:
                names = c.entity_sets()
            except ApiError as e:
                if e.status in (401, 403) or "Login failed" in str(e):
                    raise
                names = []  # $metadata not available - keep the basic list
            c.list_all("Users", page=1)  # tests login + permissions
            self.client = c
            def ui() -> None:
                self.status.configure(text="Connected", foreground="#070")
                self.cb_entity.configure(values=names or COMMON_ENTITIES)
            self.q.put(ui)
            self.say(f"Connected. Entities available: {len(names) or 'no list (using the basic one)'}.")
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
            self.say(f"Fetching {entity} ...")
            recs = self.client.list_all(entity)
            headers, rows = export_records(recs)
            write_csv(Path(path), headers, rows)
            self.say(f"Saved {len(recs)} records to {path}\n"
                     "WARNING: the file contains personal data (GDPR) - do not email it.")
        self.work(job)

    def load_csv(self) -> None:
        if not self.need_client(): return
        path = getattr(self, "_test_path", None) or filedialog.askopenfilename(filetypes=[("CSV", "*.csv *.txt"), ("All files", "*.*")])
        if not path: return
        entity = self.v["entity"].get().strip()
        def job() -> None:
            self.ops, self.csv_path = [], path
            self.q.put(lambda: self.btn_apply.configure(state="disabled"))
            headers, rows = read_csv(Path(path))
            if not rows:
                self.say("The CSV file has no data rows."); return
            self.say(f"\n=== PREVIEW ({Path(path).name}) - nothing has been changed yet ===")
            recs = self.client.list_all(entity)
            self.key = self._key_for(recs)
            self.say(f"Entity: {entity}, key column: {self.key}")
            ops = build_plan(rows, headers, recs, self.key, self.allow_create.get(), self.allow_delete.get())
            for o in ops:
                if o.action != "nochange":
                    self.say(format_op(o))
            s = summarize(ops)
            self.say(f"\nSUMMARY: updates={s['update']}, creates={s['create']}, deletes={s['delete']}, "
                     f"no changes={s['nochange']}, errors={s['error']}")
            self.ops = ops
            if s["update"] + s["create"] + s["delete"]:
                self.say("If the preview looks right, click APPLY CHANGES." + (
                    "\nRows with errors will be SKIPPED." if s["error"] else ""))
                self.q.put(lambda: self.btn_apply.configure(state="normal"))
            else:
                self.say("Nothing to do.")
        self.work(job)

    def apply(self) -> None:
        if not self.need_client() or not self.ops: return
        s = summarize(self.ops)
        n = s["update"] + s["create"] + s["delete"]
        msg = (f"I am about to change data on the PBX {self.client.base}:\n\n  updates: {s['update']}\n  creates: {s['create']}\n"
               f"  DELETES: {s['delete']}\n  rows with errors (skipped): {s['error']}\n\nA backup is saved first. Continue?")
        if not self.confirm("Confirm changes", msg):
            self.say("Cancelled."); return
        entity = self.v["entity"].get().strip()
        ops = self.ops
        run_dir = RUNS / time.strftime("%Y%m%d-%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        self.q.put(lambda: (self.btn_apply.configure(state="disabled"), self.progress.configure(maximum=n, value=0)))
        def job() -> None:
            self.say(f"\n=== APPLYING {n} changes ===")
            def prog(line: str) -> None:
                self.say(line); self.q.put(lambda: self.progress.step(1))
            st = execute(ops, self.client, entity, run_dir, progress=prog)
            self.say(f"\nDONE: ok={st['ok']}, failed={st['failed']}\nBackup and results: {run_dir}")
            self.say("To undo: click “Undo changes from a backup…” and pick backup.json from that folder.")
            self.ops = []
        self.work(job)

    def rollback(self) -> None:
        if not self.need_client(): return
        path = getattr(self, "_test_path", None) or filedialog.askopenfilename(
            initialdir=str(RUNS), filetypes=[("backup.json", "*.json")])
        if not path: return
        if not self.confirm("Undo changes?", f"This restores the old values from:\n{path}\n\nContinue?"):
            self.say("Cancelled."); return
        def job() -> None:
            self.say("\n=== UNDOING CHANGES ===")
            st = rollback(Path(path), self.client, True, progress=self.say)
            self.say(f"DONE: restored={st['ok']}, failed={st['failed']}, not restorable={st['not_restorable']}")
        self.work(job)


def main() -> int:
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
