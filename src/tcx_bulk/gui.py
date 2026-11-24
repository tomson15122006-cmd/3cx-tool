"""Desktop window (GUI) for bulk changes in 3CX - for people who prefer not to use a terminal.

Workflow:
  1. Import your systems from a KeePass CSV export (or type one PBX manually) and pick the systems
  2. Export (optional) -> current state to CSV; this is also a template to edit
  3. Load CSV -> PREVIEW of changes (nothing is changed yet!)
  4. Apply -> confirmation, backup, execution (multi-system: pilot system first, then the rest)
  5. Undo -> restore the old values from the backups
"""
from __future__ import annotations

import json
import queue
import re
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
from .multi import (SystemPlan, check_all, describe, has_system_col, plan_all, resolve_pool,
                    rollback_multi, run_multi, split_rows, totals, write_plan_csv)
from .systems import System, SystemsError, load_systems

CONFIG = Path.home() / ".tcx-bulk" / "profile.json"
RUNS = Path.home() / "tcx-bulk-runs"
COMMON_ENTITIES = ["Users", "Groups", "Queues", "RingGroups", "Trunks", "Parkings", "Peers", "Receptionists"]
KEEPASS_WARNING = ("NOTE: a KeePass CSV export contains passwords in plain text. This tool only reads it and "
                   "never copies or saves the passwords. Keep the file on an encrypted disk and delete it when you are done.")


class App(ttk.Frame):
    def __init__(self, master: tk.Tk, client_factory: Optional[Callable[..., XapiClient]] = None,
                 sync: bool = False, confirm: Optional[Callable[[str, str], bool]] = None):
        super().__init__(master, padding=10)
        self.master, self.sync = master, sync  # sync=True: no threads (for tests)
        self.client_factory = client_factory or XapiClient
        self.confirm = confirm or (lambda title, msg: messagebox.askyesno(title, msg, icon="warning"))
        self.client: Optional[XapiClient] = None
        self.systems: List[System] = []
        self.shown: List[System] = []
        self.chosen: set = set()
        self.plans: List[SystemPlan] = []
        self.ops: List[Op] = []
        self.key = ""
        self.q: "queue.Queue[Callable[[], None]]" = queue.Queue()
        master.title(f"3CX Bulk Tool {__version__}")
        master.geometry("1040x900")
        self.pack(fill="both", expand=True)
        self._build()
        self._load_profile()
        self.after(100, self._pump)

    # ------------------------------------------------------------ UI
    def _build(self) -> None:
        self.v = {k: tk.StringVar() for k in ("fqdn", "cid", "secret", "ca", "entity", "key", "filter")}
        self.insecure = tk.BooleanVar(value=False)
        self.allow_create = tk.BooleanVar(value=False)
        self.allow_delete = tk.BooleanVar(value=False)
        self.pilot = tk.BooleanVar(value=True)

        box = ttk.LabelFrame(self, text="1. Systems", padding=8)
        box.pack(fill="x")
        top = ttk.Frame(box); top.pack(fill="x")
        ttk.Button(top, text="Import systems from KeePass CSV…", command=self.import_systems).pack(side="left")
        self.sys_label = ttk.Label(top, text="  No systems imported (or connect to a single PBX below)")
        self.sys_label.pack(side="left")
        row = ttk.Frame(box); row.pack(fill="x", pady=3)
        ttk.Label(row, text="Filter").pack(side="left")
        e = ttk.Entry(row, textvariable=self.v["filter"], width=24); e.pack(side="left", padx=4)
        self.v["filter"].trace_add("write", lambda *_: self._fill_list())
        ttk.Button(row, text="Select all shown", command=self.select_all_shown).pack(side="left", padx=4)
        ttk.Button(row, text="Clear selection", command=self.clear_selection).pack(side="left")
        self.sel_label = ttk.Label(row, text=""); self.sel_label.pack(side="right")
        lf = ttk.Frame(box); lf.pack(fill="x")
        self.lb = tk.Listbox(lf, selectmode="extended", height=5, exportselection=False)
        sb = ttk.Scrollbar(lf, command=self.lb.yview); self.lb.configure(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="x", expand=True); sb.pack(side="right", fill="y")
        self.lb.bind("<<ListboxSelect>>", lambda _e: self._sync_selection())

        sep = ttk.LabelFrame(box, text="Or connect to a single PBX", padding=6)
        sep.pack(fill="x", pady=(6, 0))
        for i, (label, key, show) in enumerate([
            ("PBX address (e.g. pbx.company.ie)", "fqdn", ""), ("Client ID", "cid", ""),
            ("Client Secret (API Key)", "secret", "*"), ("CA file (optional)", "ca", "")]):
            ttk.Label(sep, text=label).grid(row=i // 2, column=(i % 2) * 2, sticky="w", padx=4, pady=2)
            ttk.Entry(sep, textvariable=self.v[key], show=show, width=26).grid(row=i // 2, column=(i % 2) * 2 + 1, padx=4, pady=2)
        ttk.Checkbutton(sep, text="Do not verify TLS certificate (not recommended)", variable=self.insecure).grid(row=2, column=0, columnspan=2, sticky="w")
        self.status = ttk.Label(sep, text="Not connected", foreground="#a00")
        self.status.grid(row=2, column=2, sticky="w")
        self.btn_connect = ttk.Button(sep, text="Connect and test", command=self.connect)
        self.btn_connect.grid(row=2, column=3, sticky="e", pady=4)

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
        ttk.Button(box, text="Undo a whole run…", command=self.rollback_run).pack(side="left")
        ttk.Checkbutton(box, text="Pilot first (multi-system)", variable=self.pilot).pack(side="left", padx=10)
        self.progress = ttk.Progressbar(box, mode="determinate", length=200)
        self.progress.pack(side="right")

        self.log = scrolledtext.ScrolledText(self, height=16, font=("Courier", 10), state="disabled")
        self.log.pack(fill="both", expand=True)

    def _load_profile(self) -> None:
        try:
            d = json.loads(CONFIG.read_text(encoding="utf-8"))
            self.v["fqdn"].set(d.get("fqdn", "")); self.v["cid"].set(d.get("cid", "")); self.v["ca"].set(d.get("ca", ""))
        except (OSError, ValueError):
            pass  # no saved profile is fine

    def _save_profile(self) -> None:
        try:  # secrets are NEVER written to disk
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps({k: self.v[k].get() for k in ("fqdn", "cid", "ca")}), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ helpers
    def say(self, text: str = "") -> None:
        self.q.put(lambda: self._say(text))
        if self.sync:
            self.flush()  # tests: show messages immediately

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
            except (ApiError, PlanError, SystemsError) as e:
                self.say(f"\nERROR: {e}")
            except Exception as e:  # noqa: BLE001 - never let the window die silently
                self.say(f"\nUNEXPECTED ERROR: {type(e).__name__}: {e}")
        if self.sync:
            run()
            self.flush()
        else:
            threading.Thread(target=run, daemon=True).start()

    def ask(self, message: str) -> bool:
        """Ask a yes/no question from a worker thread (dialogs must run in the UI thread)."""
        if self.sync:
            return self.confirm("Continue?", message)
        done, res = threading.Event(), []
        self.q.put(lambda: (res.append(self.confirm("Continue?", message)), done.set()))
        done.wait()
        return res[0]

    def need_client(self) -> bool:
        if self.client is None:
            if not self.sync:
                messagebox.showinfo("Connect first", "Click “Connect and test” first (or import systems and select some).")
            self.say("Connect to the PBX first.")
            return False
        return True

    def verify(self):
        return False if self.insecure.get() else (self.v["ca"].get().strip() or True)

    def factory(self) -> Callable[[System], XapiClient]:
        verify = self.verify()
        return lambda s: self.client_factory(s.fqdn, s.client_id, s.secret, verify=verify)

    # ------------------------------------------------------------ systems list
    def selected_systems(self) -> List[System]:
        return [s for s in self.systems if s.name in self.chosen]

    def _fill_list(self) -> None:
        f = self.v["filter"].get().strip().lower()
        self.shown = [s for s in self.systems if not f or f in s.name.lower() or f in s.fqdn.lower() or f in s.group.lower()]
        self.lb.delete(0, "end")
        for i, s in enumerate(self.shown):
            self.lb.insert("end", f"{s.name}   ({s.fqdn})   {s.group}")
            if s.name in self.chosen:
                self.lb.selection_set(i)
        self._sel_label()

    def _sel_label(self) -> None:
        self.sel_label.configure(text=f"{len(self.chosen)} of {len(self.systems)} selected")

    def _sync_selection(self) -> None:
        picked = set(self.lb.curselection())
        for i, s in enumerate(self.shown):
            (self.chosen.add if i in picked else self.chosen.discard)(s.name)
        self._sel_label()

    def select_all_shown(self) -> None:
        self.lb.selection_set(0, "end")
        self._sync_selection()

    def clear_selection(self) -> None:
        self.lb.selection_clear(0, "end")
        self.chosen.clear()
        self._sel_label()

    def import_systems(self) -> None:
        path = getattr(self, "_test_systems_path", None) or filedialog.askopenfilename(
            title="KeePass CSV export", filetypes=[("CSV", "*.csv *.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            systems, warnings = load_systems(Path(path))
        except (SystemsError, OSError, UnicodeDecodeError) as e:
            self.say(f"ERROR: {e}"); return
        self.systems, self.chosen, self.client = systems, set(), None
        self.sys_label.configure(text=f"  {len(systems)} systems imported from {Path(path).name}")
        self._fill_list()
        self.say(f"Imported {len(systems)} systems ({len(warnings)} entries skipped). Select the ones to work on.")
        for w in warnings[:20]:
            self.say(f"  note: {w}")
        if len(warnings) > 20:
            self.say(f"  ... and {len(warnings) - 20} more skipped entries")
        self.say("Tip: entries that are not PBXs but have a URL (e.g. websites) are also listed - "
                 "do not select them. Exporting only your PBX group from KeePass avoids this.")
        self.say(KEEPASS_WARNING)

    # ------------------------------------------------------------ actions
    def connect(self) -> None:
        sel = self.selected_systems()
        if sel:
            self._connect_many(sel); return
        fqdn, cid, secret = (self.v[k].get().strip() for k in ("fqdn", "cid", "secret"))
        if not (fqdn and cid and secret):
            self.say("Fill in the PBX address, Client ID and Client Secret (or import systems and select some)."); return
        verify = self.verify()
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

    def _connect_many(self, sel: List[System]) -> None:
        fac = self.factory()
        def job() -> None:
            self.say(f"Testing {len(sel)} system(s) ...")
            res = check_all(sel, fac)
            for s, ok, detail in res:
                self.say(f"  {'OK  ' if ok else 'FAIL'} {s.name:<28} {s.fqdn:<32} {'' if ok else detail}")
            good = [s for s, ok, _ in res if ok]
            self.q.put(lambda: self.status.configure(
                text=f"{len(good)}/{len(sel)} systems OK", foreground="#070" if len(good) == len(sel) else "#a60"))
            if good:
                try:
                    names = fac(good[0]).entity_sets()
                    self.q.put(lambda: self.cb_entity.configure(values=names or COMMON_ENTITIES))
                except ApiError:
                    pass
        self.work(job)

    def _key_for(self, records) -> str:
        return self.v["key"].get().strip() or guess_key(records)

    def export(self) -> None:
        sel = self.selected_systems()
        entity = self.v["entity"].get().strip()
        if sel:
            folder = getattr(self, "_test_path", None) or filedialog.askdirectory(title="Folder for the exports")
            if not folder:
                return
            fac = self.factory()
            def job_many() -> None:
                for s in sel:
                    try:
                        recs = fac(s).list_all(entity)
                        headers, rows = export_records(recs)
                        from .multi import safe_name
                        write_csv(Path(folder) / f"{safe_name(s.name)}.csv", headers, rows)
                        self.say(f"  OK   {s.name}: {len(recs)} records")
                    except ApiError as e:
                        self.say(f"  FAIL {s.name}: {e}")
                self.say(f"Saved to {folder}\nWARNING: the files contain personal data (GDPR) - do not email them.")
            self.work(job_many); return
        if not self.need_client(): return
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
        path = getattr(self, "_test_path", None) or filedialog.askopenfilename(filetypes=[("CSV", "*.csv *.txt"), ("All files", "*.*")])
        if not path: return
        entity = self.v["entity"].get().strip()
        try:
            headers, rows = read_csv(Path(path))
        except (OSError, UnicodeDecodeError) as e:
            self.say(f"ERROR: cannot read the file: {e}"); return
        if not rows:
            self.say("The CSV file has no data rows."); return
        sel = self.selected_systems()
        multi = bool(self.systems) and (bool(sel) or has_system_col(headers))
        if not multi and not self.need_client():
            return
        self.ops, self.plans = [], []
        self.btn_apply.configure(state="disabled")

        def job_multi() -> None:
            pool, gone = resolve_pool(self.systems, headers, rows, sel or None)
            h2, by_sys, unknown = split_rows(headers, rows, pool)
            self.say(f"\n=== PREVIEW ({Path(path).name}) - nothing has been changed yet ===")
            self.say(f"Entity: {entity}; planning {len(pool)} system(s) ...")
            if gone:
                self.say(f"WARNING: the CSV mentions systems that are not available/selected: {', '.join(gone)}")
            plans = plan_all(pool, h2, by_sys, entity, self.v["key"].get().strip() or None, self.factory(),
                             self.allow_create.get(), self.allow_delete.get())
            for line in describe(plans):
                self.say(line)
            t = totals(plans)
            self.say(f"\nSUMMARY: {t['systems']} systems ({t['systems_with_changes']} with changes, "
                     f"{t['systems_failed_to_plan']} could not be planned); updates={t['update']}, "
                     f"creates={t['create']}, deletes={t['delete']}, row errors={t['error']}")
            self.plans = plans
            if t["update"] + t["create"] + t["delete"]:
                self.say("If the preview looks right, click APPLY CHANGES." + (
                    "\nSystems/rows with errors will be SKIPPED." if (t["error"] or t["systems_failed_to_plan"]) else ""))
                self.q.put(lambda: self.btn_apply.configure(state="normal"))
            else:
                self.say("Nothing to do.")

        def job_single() -> None:
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
        self.work(job_multi if multi else job_single)

    def apply(self) -> None:
        if self.plans:
            self._apply_multi(); return
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

    def _apply_multi(self) -> None:
        plans = self.plans
        todo = [p for p in plans if not p.error and p.todo]
        n = sum(p.todo for p in todo)
        bad = sum(1 for p in plans if p.error)
        pilot = self.pilot.get() and len(todo) > 1
        msg = (f"I am about to change data on {len(todo)} PBX system(s): {n} change(s) in total.\n"
               f"Systems that could not be planned (skipped): {bad}\n\n"
               + (f"Pilot: first '{todo[0].system.name}' only, then I will ask before continuing.\n\n" if pilot else "")
               + "A backup is saved for every system first. Continue?")
        if not self.confirm("Confirm changes", msg):
            self.say("Cancelled."); return
        entity = self.v["entity"].get().strip()
        run_dir = RUNS / time.strftime("%Y%m%d-%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        write_plan_csv(plans, run_dir / "plan_changes.csv")
        self.btn_apply.configure(state="disabled"); self.progress.configure(maximum=max(n, 1), value=0)
        def job() -> None:
            self.say(f"\n=== APPLYING {n} changes on {len(todo)} systems ===")
            def prog(line: str) -> None:
                self.say(line)
                if re.search(r"\[\d+/\d+\]", line):
                    self.q.put(lambda: self.progress.step(1))
            res = run_multi(plans, entity, run_dir, pilot=self.pilot.get(), confirm=self.ask, progress=prog)
            self.say(f"\nDONE: {sum(int(r['ok']) for r in res)} ok, {sum(int(r['failed']) for r in res)} failed, "
                     f"{sum(1 for r in res if r['status'] == 'not run')} system(s) not run.\n"
                     f"Summary, plan and backups: {run_dir}")
            self.say("To undo everything: “Undo a whole run…” and pick that folder.")
            self.plans = []
        self.work(job)

    def rollback(self) -> None:
        path = getattr(self, "_test_path", None) or filedialog.askopenfilename(
            initialdir=str(RUNS), filetypes=[("backup.json", "*.json")])
        if not path: return
        try:
            system_name = json.loads(Path(path).read_text(encoding="utf-8")).get("system", "")
        except (OSError, ValueError) as e:
            self.say(f"ERROR: cannot read the backup: {e}"); return
        client = self.client
        if system_name:
            match = next((s for s in self.systems if s.name == system_name), None)
            if match is None:
                self.say(f"This backup belongs to system '{system_name}', which is not in the imported systems. "
                         "Import your KeePass CSV first."); return
            client = self.factory()(match)
        elif client is None and not self.need_client():
            return
        if not self.confirm("Undo changes?", f"This restores the old values from:\n{path}\n\nContinue?"):
            self.say("Cancelled."); return
        def job() -> None:
            self.say("\n=== UNDOING CHANGES ===")
            st = rollback(Path(path), client, True, progress=self.say)
            self.say(f"DONE: restored={st['ok']}, failed={st['failed']}, not restorable={st['not_restorable']}")
        self.work(job)

    def rollback_run(self) -> None:
        if not self.systems:
            self.say("Import your KeePass CSV first (needed to log in to each system)."); return
        folder = getattr(self, "_test_path", None) or filedialog.askdirectory(initialdir=str(RUNS), title="Run folder to undo")
        if not folder: return
        if not self.confirm("Undo a whole run?", f"This restores the old values on every system in:\n{folder}\n\nContinue?"):
            self.say("Cancelled."); return
        fac = self.factory()
        def job() -> None:
            self.say("\n=== UNDOING A WHOLE RUN ===")
            st = rollback_multi(Path(folder), self.systems, fac, True, progress=self.say)
            self.say(f"DONE: systems={st['systems']}, restored={st['ok']}, failed={st['failed']}, "
                     f"not restorable={st['not_restorable']}")
        self.work(job)


def main() -> int:
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
