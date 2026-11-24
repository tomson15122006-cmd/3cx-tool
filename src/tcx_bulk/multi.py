"""Run one change file against many PBX systems: plan all, pilot first, isolate failures."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .bulk import (Op, PlanError, build_plan, execute, format_op, guess_key, rollback, summarize,
                   write_csv)
from .client import ApiError, XapiClient
from .systems import System

SYSTEM_COL = "system"
ClientFactory = Callable[[System], XapiClient]


@dataclass
class SystemPlan:
    system: System
    ops: List[Op] = field(default_factory=list)
    error: str = ""
    client: Optional[XapiClient] = None
    key: str = ""

    @property
    def todo(self) -> int:
        return sum(1 for o in self.ops if o.action in ("update", "create", "delete"))


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "system"


def split_rows(headers: List[str], rows: List[Dict[str, str]], selected: List[System]):
    """Returns (headers_without_system_col, {system name: rows}, [rows for unknown systems])."""
    if SYSTEM_COL not in [h.lower() for h in headers]:
        return headers, {s.name: rows for s in selected}, []
    sc = next(h for h in headers if h.lower() == SYSTEM_COL)
    by_name = {s.name.lower(): s.name for s in selected}
    out: Dict[str, List[Dict[str, str]]] = {}
    unknown = []
    for r in rows:
        nm = by_name.get(r.get(sc, "").strip().lower())
        (out.setdefault(nm, []) if nm else unknown).append(r) if nm else unknown.append(r)
    return [h for h in headers if h != sc], out, unknown


def plan_all(selected: List[System], headers: List[str], rows_by_system: Dict[str, List[Dict[str, str]]],
             entity: str, key: Optional[str], factory: ClientFactory, allow_create=False,
             allow_delete=False, workers: int = 4) -> List[SystemPlan]:
    def one(s: System) -> SystemPlan:
        p = SystemPlan(s)
        try:
            p.client = factory(s)
            records = p.client.list_all(entity)
            p.key = key or guess_key(records)
            p.ops = build_plan(rows_by_system.get(s.name, []), headers, records, p.key, allow_create, allow_delete)
        except (ApiError, PlanError) as e:
            p.error = str(e)
        return p

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return list(ex.map(one, selected))  # keeps the input order


def describe(plans: List[SystemPlan], verbose: bool = False) -> List[str]:
    lines = []
    for p in plans:
        s = summarize(p.ops)
        head = f"=== {p.system.name} ({p.system.fqdn}) ==="
        if p.error:
            lines += [head, f"  CANNOT PLAN: {p.error}"]
            continue
        lines.append(f"{head}  updates={s['update']} creates={s['create']} deletes={s['delete']} "
                     f"no changes={s['nochange']} errors={s['error']}")
        for o in p.ops:
            if o.action not in ("nochange",) or verbose:
                lines.append("  " + format_op(o).replace("\n", "\n  "))
    return lines


def totals(plans: List[SystemPlan]) -> Dict[str, int]:
    t = {"systems": len(plans), "systems_with_changes": 0, "systems_failed_to_plan": 0,
         "update": 0, "create": 0, "delete": 0, "error": 0}
    for p in plans:
        if p.error:
            t["systems_failed_to_plan"] += 1
            continue
        s = summarize(p.ops)
        t["systems_with_changes"] += 1 if p.todo else 0
        for k in ("update", "create", "delete", "error"):
            t[k] += s[k]
    return t


def write_plan_csv(plans: List[SystemPlan], path: Path) -> None:
    rows = []
    for p in plans:
        if p.error:
            rows.append({"system": p.system.name, "action": "system-error", "message": p.error})
        for o in p.ops:
            base = {"system": p.system.name, "line": o.line, "action": o.action, "key": o.key, "message": o.message}
            if o.action in ("update", "create") and o.changes:
                for f, (old, new) in o.changes.items():
                    rows.append({**base, "field": f, "old": old, "new": new})
            else:
                rows.append(base)
    write_csv(path, ["system", "line", "action", "key", "field", "old", "new", "message"], rows)


def run_multi(plans: List[SystemPlan], entity: str, run_dir: Path, pilot: bool = True,
              stop_on_error: bool = False, delay: float = 0.0,
              confirm: Callable[[str], bool] = lambda msg: True,
              progress: Optional[Callable[[str], None]] = print) -> List[Dict[str, object]]:
    """Pilot system first (if enabled), then the rest. A failing system never blocks the others
    unless stop_on_error is set. Backups go to run_dir/<system>/backup.json."""
    say = progress or (lambda _m: None)
    summary: Dict[str, Dict[str, object]] = {}
    for p in plans:
        s = summarize(p.ops) if not p.error else {}
        summary[p.system.name] = {
            "system": p.system.name, "status": "plan error" if p.error else ("no changes" if not p.todo else "pending"),
            "planned": p.todo, "ok": 0, "failed": 0, "detail": p.error}

    def do(p: SystemPlan) -> int:
        d = run_dir / safe_name(p.system.name)
        d.mkdir(parents=True, exist_ok=True)
        say(f"--- {p.system.name}: applying {p.todo} change(s)")
        st = execute(p.ops, p.client, entity, d, stop_on_error=stop_on_error, delay=delay,
                     progress=lambda l: say(f"[{p.system.name}]{l}"), system_name=p.system.name)
        r = summary[p.system.name]
        r.update(status="done" if not st["failed"] else "done with errors", ok=st["ok"], failed=st["failed"])
        return st["failed"]

    todo = [p for p in plans if not p.error and p.todo]
    order = todo
    halted = ""
    if pilot and len(todo) > 1:
        failed = do(todo[0])
        if failed:
            halted = f"pilot system {todo[0].system.name} had errors"
        elif not confirm(f"Pilot system '{todo[0].system.name}' finished without errors.\n"
                         f"Continue with the remaining {len(todo) - 1} systems?"):
            halted = "cancelled after pilot"
        order = todo[1:] if not halted else []
    for i, p in enumerate(order):
        if halted:
            break
        if do(p) and stop_on_error:
            halted = f"stopped after errors on {p.system.name}"
            order = order[i + 1:]
            break
    else:
        order = []
    if halted:
        say(f"STOPPED: {halted}. Remaining systems were not touched.")
        for r in summary.values():
            if r["status"] == "pending":
                r["status"], r["detail"] = "not run", halted
    write_csv(run_dir / "summary.csv", ["system", "status", "planned", "ok", "failed", "detail"], list(summary.values()))
    return list(summary.values())


def check_all(selected: List[System], factory: ClientFactory, workers: int = 8):
    def one(s: System):
        try:
            c = factory(s)
            c.list_all("Users", page=1)
            return s, True, "OK"
        except ApiError as e:
            return s, False, str(e)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return list(ex.map(one, selected))


def rollback_multi(run_dir: Path, systems: List[System], factory: ClientFactory, execute_it: bool,
                   progress: Callable[[str], None] = print) -> Dict[str, int]:
    import json
    by_name = {s.name: s for s in systems}
    tot = {"ok": 0, "failed": 0, "not_restorable": 0, "systems": 0}
    for b in sorted(Path(run_dir).glob("*/backup.json")):
        name = json.loads(b.read_text(encoding="utf-8")).get("system", "")
        if name not in by_name:
            progress(f"skipping {b.parent.name}: system {name!r} is not in the systems file")
            continue
        progress(f"--- {name}")
        st = rollback(b, factory(by_name[name]), execute_it, progress=progress)
        tot["systems"] += 1
        for k in ("ok", "failed", "not_restorable"):
            tot[k] += st[k]
    return tot
