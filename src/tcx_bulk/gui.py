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
import os
import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, List, Optional

try:  # modern look (Windows 11 style); falls back to the default theme if missing
    import sv_ttk
except Exception:  # noqa: BLE001
    sv_ttk = None

from . import __version__, updater
from .bulk import (PlanError, build_plan, execute, export_records, format_op, guess_key,
                   read_csv, rollback, summarize, write_csv, Op)
from .client import ApiError, XapiClient
from .multi import (SystemPlan, check_all, describe, has_system_col, plan_all, resolve_pool,
                    rollback_multi, run_multi, split_rows, totals, write_plan_csv)
from .systems import System, SystemsError, load_systems

PALETTE = {  # colours of the preview rows: light / dark theme
    "light": {"update": "#0f5fb4", "create": "#107c10", "delete": "#c42b1c", "error": "#c42b1c", "muted": "#6b6b6b"},
    "dark": {"update": "#6cb8ff", "create": "#6ccb5f", "delete": "#ff99a4", "error": "#ff99a4", "muted": "#a0a0a0"},
}
CONFIG = Path.home() / ".tcx-bulk" / "profile.json"
RUNS = Path.home() / "tcx-bulk-runs"
COMMON_ENTITIES = ["Users", "Groups", "Queues", "RingGroups", "Trunks", "Parkings", "Peers", "Receptionists"]
KEEPASS_WARNING = ("NOTE: a KeePass CSV export contains passwords in plain text. This tool only reads it and "
                   "never copies or saves the passwords. Keep the file on an encrypted disk and delete it when you are done.")


class App(ttk.Frame):
    def __init__(self, master: tk.Tk, client_factory: Optional[Callable[..., XapiClient]] = None,
                 sync: bool = False, confirm: Optional[Callable[[str, str], bool]] = None):
        super().__init__(master, padding=16)
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
        self.busy = 0  # number of running background jobs (no updating while > 0)
        self.pending_release: Optional[updater.Release] = None
        self.restart = updater.restart  # replaceable in tests
        master.title(f"3CX Bulk Tool {__version__}")
        master.geometry("1240x820")
        master.minsize(1100, 720)
        self.pack(fill="both", expand=True)
        self._build()
        self._load_profile()
        self.after(100, self._pump)
        if not sync and updater.is_frozen() and not os.environ.get("TCX_NO_UPDATE_CHECK"):
            self.after(2000, lambda: self.check_updates(silent=True))  # quietly marks the button if there is news

    # ------------------------------------------------------------ UI
    def _build(self) -> None:
        self.v = {k: tk.StringVar() for k in ("fqdn", "cid", "secret", "ca", "entity", "key", "filter")}
        self.insecure = tk.BooleanVar(value=False)
        self.allow_create = tk.BooleanVar(value=False)
        self.allow_delete = tk.BooleanVar(value=False)
        self.pilot = tk.BooleanVar(value=True)
        self.summary = tk.StringVar(value="Load a CSV file to see a preview of the changes.")
        self.theme = "light"
        if sv_ttk:
            sv_ttk.set_theme(self.theme)
        base = tkfont.nametofont("TkDefaultFont").actual()["family"]
        self.f_title = (base, 20, "bold")
        self.f_sub = (base, 10)
        self.f_head = (base, 11, "bold")
        self.columnconfigure(0, weight=0); self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)

        # ---- header
        hdr = ttk.Frame(self); hdr.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        tt = ttk.Frame(hdr); tt.pack(side="left")
        ttk.Label(tt, text="3CX Bulk Tool", font=self.f_title).pack(anchor="w")
        ttk.Label(tt, text=f"Bulk changes for 3CX through the API  ·  v{__version__}", font=self.f_sub, foreground="#7a7a7a").pack(anchor="w")
        self.btn_theme = ttk.Button(hdr, text="Dark mode", command=self.toggle_theme)
        self.btn_theme.pack(side="right")
        self.btn_update = ttk.Button(hdr, text="Check for updates", command=self.check_updates)
        self.btn_update.pack(side="right", padx=(0, 10))
        self.status = ttk.Label(hdr, text="Not connected", foreground="#c42b1c", font=self.f_head)
        self.status.pack(side="right", padx=16)

        # ---- left column: two tabs (many systems from KeePass / one PBX typed by hand)
        left = ttk.Frame(self); left.grid(row=1, column=0, sticky="ns", padx=(0, 14))
        self.nb = ttk.Notebook(left); self.nb.pack(fill="both", expand=True)
        tab = ttk.Frame(self.nb, padding=12); self.nb.add(tab, text="  1  KeePass systems  ")
        ttk.Button(tab, text="Import from KeePass CSV…", style="Accent.TButton", command=self.import_systems).pack(fill="x")
        self.sys_label = ttk.Label(tab, text="No systems imported", foreground="#7a7a7a")
        self.sys_label.pack(anchor="w", pady=(6, 8))
        ttk.Entry(tab, textvariable=self.v["filter"]).pack(fill="x")
        self.v["filter"].trace_add("write", lambda *_: self._fill_list())
        ttk.Label(tab, text="Filter by name, address or group", font=self.f_sub, foreground="#7a7a7a").pack(anchor="w", pady=(2, 6))
        lf = ttk.Frame(tab); lf.pack(fill="both", expand=True)
        self.lb = tk.Listbox(lf, selectmode="extended", height=10, width=34, exportselection=False,
                             borderwidth=0, highlightthickness=1, activestyle="none", font=(base, 10))
        sb = ttk.Scrollbar(lf, command=self.lb.yview); self.lb.configure(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        self.lb.bind("<<ListboxSelect>>", lambda _e: self._sync_selection())
        row = ttk.Frame(tab); row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Select all shown", command=self.select_all_shown).pack(side="left")
        ttk.Button(row, text="Clear", command=self.clear_selection).pack(side="left", padx=6)
        self.sel_label = ttk.Label(tab, text="", font=self.f_head); self.sel_label.pack(anchor="w", pady=(8, 0))
        self.btn_test_many = ttk.Button(tab, text="Test logins of the selected systems", command=self.connect)
        self.btn_test_many.pack(fill="x", pady=(8, 0))

        one = ttk.Frame(self.nb, padding=12); self.nb.add(one, text="  Single PBX  ")
        for label, key, show in [("PBX address (e.g. pbx.company.ie)", "fqdn", ""), ("Client ID", "cid", ""),
                                 ("Client Secret (API key)", "secret", "*"), ("CA file (optional)", "ca", "")]:
            ttk.Label(one, text=label, font=self.f_sub).pack(anchor="w")
            ttk.Entry(one, textvariable=self.v[key], show=show).pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(one, text="Do not verify TLS certificate", variable=self.insecure).pack(anchor="w")
        self.btn_connect = ttk.Button(one, text="Connect and test", style="Accent.TButton", command=self.connect)
        self.btn_connect.pack(fill="x", pady=(12, 0))

        # ---- right column
        right = ttk.Frame(self); right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1); right.rowconfigure(1, weight=1)
        card = ttk.LabelFrame(right, text=" 2  What to change ", padding=12); card.grid(row=0, column=0, sticky="ew")
        card.columnconfigure(1, weight=1)
        ttk.Label(card, text="Entity").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.cb_entity = ttk.Combobox(card, textvariable=self.v["entity"], values=COMMON_ENTITIES, width=24)
        self.cb_entity.grid(row=0, column=1, sticky="w")
        self.v["entity"].set("Users")
        ttk.Label(card, text="Key column").grid(row=0, column=2, sticky="w", padx=(20, 8))
        ttk.Entry(card, textvariable=self.v["key"], width=14).grid(row=0, column=3)
        ttk.Label(card, text="(empty = auto)", font=self.f_sub, foreground="#7a7a7a").grid(row=0, column=4, padx=6)
        ttk.Button(card, text="Export current state to CSV…", command=self.export).grid(row=0, column=5, padx=(20, 0))
        r2 = ttk.Frame(card); r2.grid(row=1, column=0, columnspan=6, sticky="w", pady=(12, 0))
        ttk.Button(r2, text="Load CSV file…", style="Accent.TButton", command=self.load_csv).pack(side="left")
        ttk.Label(r2, text="  preview only, nothing is changed yet", font=self.f_sub, foreground="#7a7a7a").pack(side="left")
        ttk.Checkbutton(r2, text="Allow creating", variable=self.allow_create).pack(side="left", padx=(24, 8))
        ttk.Checkbutton(r2, text="Allow deleting", variable=self.allow_delete).pack(side="left")

        card = ttk.LabelFrame(right, text=" 3  Preview of the changes ", padding=10)
        card.grid(row=1, column=0, sticky="nsew", pady=12)
        card.columnconfigure(0, weight=1); card.rowconfigure(1, weight=1)
        ttk.Label(card, textvariable=self.summary, font=self.f_head).grid(row=0, column=0, sticky="w", pady=(0, 6))
        cols = ("system", "line", "action", "key", "field", "old", "new")
        self.tree = ttk.Treeview(card, columns=cols, show="headings", height=10)
        for c, w, t in (("system", 120, "System"), ("line", 46, "Row"), ("action", 80, "Action"), ("key", 80, "Key"),
                        ("field", 130, "Field"), ("old", 190, "Current value"), ("new", 220, "New value")):
            self.tree.heading(c, text=t); self.tree.column(c, width=w, anchor="w", stretch=c in ("old", "new"))
        ys = ttk.Scrollbar(card, command=self.tree.yview); self.tree.configure(yscrollcommand=ys.set)
        self.tree.grid(row=1, column=0, sticky="nsew"); ys.grid(row=1, column=1, sticky="ns")
        self.tree.bind("<Double-1>", self._row_details)  # full text of a row goes to the log
        ttk.Label(card, text="Double-click a row to see its full text in the log.", font=self.f_sub,
                  foreground="#7a7a7a").grid(row=2, column=0, sticky="w", pady=(4, 0))

        card = ttk.LabelFrame(right, text=" 4  Apply ", padding=12); card.grid(row=2, column=0, sticky="ew")
        self.btn_apply = ttk.Button(card, text="Apply changes", style="Accent.TButton", command=self.apply, state="disabled")
        self.btn_apply.pack(side="left")
        ttk.Checkbutton(card, text="Pilot first (multi-system)", variable=self.pilot).pack(side="left", padx=16)
        ttk.Button(card, text="Undo from a backup…", command=self.rollback).pack(side="left")
        ttk.Button(card, text="Undo a whole run…", command=self.rollback_run).pack(side="left", padx=8)
        self.progress = ttk.Progressbar(card, mode="determinate", length=180)
        self.progress.pack(side="right")

        # ---- log
        lg = ttk.LabelFrame(self, text=" Log ", padding=6); lg.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.log = scrolledtext.ScrolledText(lg, height=7, font=tkfont.nametofont("TkFixedFont"), state="disabled",
                                             borderwidth=0, highlightthickness=0, wrap="word")
        self.log.pack(fill="both", expand=True)
        self._recolor()

    def _row_details(self, _event=None) -> None:
        for item in self.tree.selection():
            v = self.tree.item(item, "values")
            self._say("  ".join(str(x) for x in v if str(x) != ""))

    def _recolor(self) -> None:
        pal = PALETTE[self.theme]
        for tag in ("update", "create", "delete", "error"):
            self.tree.tag_configure(tag, foreground=pal[tag])
        dark = self.theme == "dark"
        bg, fg = ("#1c1c1c", "#e6e6e6") if dark else ("#ffffff", "#1a1a1a")
        self.log.configure(background=bg, foreground=fg, insertbackground=fg)
        self.lb.configure(background=bg, foreground=fg, selectbackground="#0f6cbd", selectforeground="#ffffff",
                          highlightbackground="#3a3a3a" if dark else "#d0d0d0", highlightcolor="#0f6cbd")

    def toggle_theme(self) -> None:
        self.theme = "dark" if self.theme == "light" else "light"
        if sv_ttk:
            sv_ttk.set_theme(self.theme)
        self.btn_theme.configure(text="Light mode" if self.theme == "dark" else "Dark mode")
        self._recolor()

    def _clear_preview(self, text: str = "") -> None:
        self.tree.delete(*self.tree.get_children())
        if text:
            self.summary.set(text)

    def _add_op(self, system: str, o: Op) -> None:
        if o.action in ("update", "create") and o.changes:
            for f, (old, new) in o.changes.items():
                self.tree.insert("", "end", tags=(o.action,), values=(
                    system, o.line, o.action, o.key, f, "" if old is None else str(old), str(new)))
        elif o.action == "delete":
            self.tree.insert("", "end", tags=("delete",), values=(system, o.line, "delete", o.key, "(whole record)", o.label, ""))
        elif o.action == "error":
            self.tree.insert("", "end", tags=("error",), values=(system, o.line, "ERROR", o.key, "", "", o.message))

    def _show_plans(self, plans: List[SystemPlan]) -> None:
        def ui() -> None:
            self._clear_preview()
            shown = 0
            for p in plans:
                if p.error:
                    self.tree.insert("", "end", tags=("error",), values=(p.system.name, "", "ERROR", "", "", "", p.error))
                for o in p.ops:
                    if shown < 5000:
                        self._add_op(p.system.name, o); shown += 1
        self.q.put(ui)

    def _show_ops(self, ops: List[Op]) -> None:
        def ui() -> None:
            self._clear_preview()
            for o in ops[:5000]:
                self._add_op("", o)
        self.q.put(ui)

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
            self.busy += 1
            try:
                fn()
            except (ApiError, PlanError, SystemsError) as e:
                self.say(f"\nERROR: {e}")
            except updater.UpdateError as e:
                self.say(f"\nUPDATE ERROR: {e}")
            except Exception as e:  # noqa: BLE001 - never let the window die silently
                self.say(f"\nUNEXPECTED ERROR: {type(e).__name__}: {e}")
            finally:
                self.busy -= 1
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
    def in_systems_tab(self) -> bool:
        return self.nb.index(self.nb.select()) == 0

    def selected_systems(self) -> List[System]:
        """Selected systems - only while the 'KeePass systems' tab is the active one."""
        if not self.in_systems_tab():
            return []
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
        self.sys_label.configure(text=f"{len(systems)} systems imported from {Path(path).name}")
        self.nb.select(0)
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
            self.say("Select systems on the \"KeePass systems\" tab, or fill in the PBX address, Client ID and Client Secret on the \"Single PBX\" tab."); return
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
        multi = bool(self.systems) and self.in_systems_tab() and (bool(sel) or has_system_col(headers))
        if not multi and not self.need_client():
            return
        self.ops, self.plans = [], []
        self.btn_apply.configure(state="disabled")
        self._clear_preview("Working out the changes …")

        def job_multi() -> None:
            pool, gone = resolve_pool(self.systems, headers, rows, sel or None)
            h2, by_sys, unknown = split_rows(headers, rows, pool)
            self.say(f"\n=== PREVIEW ({Path(path).name}) - nothing has been changed yet ===")
            self.say(f"Entity: {entity}; planning {len(pool)} system(s) ...")
            if gone:
                self.say(f"WARNING: the CSV mentions systems that are not available/selected: {', '.join(gone)}")
            plans = plan_all(pool, h2, by_sys, entity, self.v["key"].get().strip() or None, self.factory(),
                             self.allow_create.get(), self.allow_delete.get())
            self._show_plans(plans)
            t = totals(plans)
            self.q.put(lambda: self.summary.set(
                f"{t['systems']} systems  ·  {t['update']} updates  ·  {t['create']} creates  ·  {t['delete']} deletes  ·  "
                f"{t['error'] + t['systems_failed_to_plan']} errors"))
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
            s = summarize(ops)
            self._show_ops(ops)
            self.q.put(lambda: self.summary.set(
                f"{s['update']} updates  ·  {s['create']} creates  ·  {s['delete']} deletes  ·  {s['nochange']} unchanged  ·  {s['error']} errors"))
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

    # ------------------------------------------------------------ updates
    def check_updates(self, silent: bool = False) -> None:
        """Header button. Checks GitHub for a newer version; on a yes, downloads, verifies and installs it."""
        if self.busy:
            if not silent:
                self.say("Please wait until the current operation has finished before updating.")
            return

        def job() -> None:
            rel = self.pending_release
            if rel is None:
                if not silent:
                    self.say("Checking for updates ...")
                try:
                    rel = updater.check()
                except updater.UpdateError as e:
                    if not silent:
                        self.say(f"Update check failed: {e}")
                    return
            if rel is None:
                if not silent:
                    self.say(f"You are up to date (version {__version__}).")
                return
            self.pending_release = rel
            self.q.put(lambda: self.btn_update.configure(text=f"Update available: v{rel.version}", style="Accent.TButton"))
            if silent:
                return
            self.say(f"New version available: {rel.version}  ({rel.page})")
            notes = (rel.notes[:700] + " …") if len(rel.notes) > 700 else rel.notes
            if not self.ask(f"Version {rel.version} is available (you have {__version__}).\n\n{notes}\n\n"
                            "Download and install it now? The program will restart, so anything loaded "
                            "(systems, preview) will have to be loaded again."):
                self.say("Update postponed."); return
            self._install_update(rel)
        self.work(job)

    def _install_update(self, rel: "updater.Release") -> None:
        if not updater.is_frozen():
            self.say("This copy runs from source, so it cannot replace itself. "
                     f"Get the packaged program from {rel.page or 'the GitHub Releases page'} "
                     "(or update the source with git pull).")
            return
        def prog(got: int, total: int) -> None:
            if total:
                self.q.put(lambda: self.progress.configure(maximum=total, value=got))
        self.say(f"Downloading {rel.version} ...")
        f = updater.download(rel, Path.home() / ".tcx-bulk" / "update", kind=updater.current_kind(), progress=prog)
        self.say("Downloaded and verified (SHA-256). Installing ...")
        exe = updater.install(f)
        self.say(f"Updated to {rel.version}. Restarting ...")
        self.q.put(lambda: self.progress.configure(value=0))
        self.q.put(lambda: self.restart(str(exe)))

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
    if updater.is_frozen():
        updater.cleanup_old()  # remove the previous version left behind by an update
    try:  # sharp text on high-DPI Windows screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - not Windows
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
