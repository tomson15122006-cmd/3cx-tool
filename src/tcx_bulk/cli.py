"""Command line interface: check | entities | export | apply | rollback."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

from .bulk import (
    PlanError, build_plan, execute, export_records, format_op, guess_key,
    read_csv, rollback, summarize, write_csv,
)
from . import __version__, updater
from .client import ApiError, XapiClient
from .multi import (check_all, describe, has_system_col, plan_all, resolve_pool, rollback_multi,
                    run_multi, split_rows, totals, write_plan_csv, safe_name)
from .systems import System, SystemsError, load_systems, select_systems


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (does not override variables already set in the environment)."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def make_client(args) -> XapiClient:
    fqdn = args.fqdn or os.environ.get("TCX_FQDN")
    cid = os.environ.get("TCX_CLIENT_ID")
    secret = os.environ.get("TCX_CLIENT_SECRET")
    missing = [n for n, v in (("TCX_FQDN (or --fqdn)", fqdn), ("TCX_CLIENT_ID", cid), ("TCX_CLIENT_SECRET", secret)) if not v]
    if missing:
        sys.exit("Missing settings: " + ", ".join(missing) + "\nSet them in a .env file (template: .env.example).")
    return XapiClient(fqdn, cid, secret, verify=get_verify(args))


def get_verify(args):
    verify = os.environ.get("TCX_CA_BUNDLE") or True
    if args.insecure or os.environ.get("TCX_VERIFY_TLS", "1") == "0":
        verify = False
        requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
        print("WARNING: TLS certificate verification is DISABLED.", file=sys.stderr)
    return verify


def load_all_systems(args):
    """Systems from --systems FILE (or TCX_SYSTEMS), or None when running against a single PBX."""
    path = args.systems or os.environ.get("TCX_SYSTEMS")
    if not path:
        return None
    try:
        systems, warnings = load_systems(path)
    except (SystemsError, OSError, UnicodeDecodeError) as e:
        sys.exit(f"SYSTEMS FILE ERROR: {e}")
    for w in warnings:
        print(f"note: {w}", file=sys.stderr)
    return systems


def chosen_systems(args, systems, default_all=False):
    flags = args.all or args.select or args.group
    if not flags:
        return list(systems) if default_all else None
    try:
        return select_systems(systems, args.select, args.group, args.all)
    except SystemsError as e:
        sys.exit(f"ERROR: {e}")


def factory_for(args):
    verify = get_verify(args)
    return lambda s: XapiClient(s.fqdn, s.client_id, s.secret, verify=verify)


def new_run_dir(base: str) -> Path:
    d = Path(base) / time.strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------ commands
def cmd_update(args) -> int:
    print(f"tcx-bulk {__version__}")
    try:
        rel = updater.check()
    except updater.UpdateError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if rel is None:
        print("You are up to date.")
        return 0
    print(f"New version available: {rel.version}  ({rel.page})")
    if rel.notes:
        print("\n" + rel.notes[:1500] + "\n")
    if args.check_only:
        return 0
    if not updater.is_frozen():
        print("This copy runs from source, so it cannot replace itself. Update it with git pull / pip install -U.")
        return 1
    if not args.yes and input("Install it now? Type YES to confirm: ").strip().upper() != "YES":
        print("Cancelled.")
        return 1
    try:
        tmp = Path(os.environ.get("TEMP") or "/tmp") / "tcx-bulk-update"
        f = updater.download(rel, tmp, kind="cli", progress=None)
        updater.install(f)
    except updater.UpdateError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"Updated to {rel.version}. Run the program again to use the new version.")
    return 0


def cmd_systems(args) -> int:
    try:
        systems, warnings = load_systems(args.file)
    except (SystemsError, OSError, UnicodeDecodeError) as e:
        sys.exit(f"SYSTEMS FILE ERROR: {e}")
    print(f"{'NAME':<30} {'ADDRESS':<36} {'CLIENT ID':<20} GROUP")
    for s in systems:
        print(f"{s.name:<30} {s.fqdn:<36} {s.client_id:<20} {s.group}")
    for w in warnings:
        print(f"note: {w}")
    print(f"\n{len(systems)} systems loaded ({len(warnings)} entries skipped). Passwords are not shown.")
    return 0


def cmd_check(args) -> int:
    systems = load_all_systems(args)
    if systems is not None:
        sel = chosen_systems(args, systems, default_all=True)
        bad = 0
        for s, ok, detail in check_all(sel, factory_for(args), args.workers):
            print(f"{'OK  ' if ok else 'FAIL'} {s.name:<30} {s.fqdn:<34} {'' if ok else detail}")
            bad += not ok
        print(f"\n{len(sel) - bad}/{len(sel)} systems reachable and logged in.")
        return 1 if bad else 0
    c = make_client(args)
    recs = c.list_all(args.entity, page=5)
    print(f"OK: connected to {c.base}, logged in, entity {args.entity} returned {len(recs)}+ records.")
    return 0


def cmd_entities(args) -> int:
    for name in make_client(args).entity_sets():
        print(name)
    return 0


def cmd_export(args) -> int:
    systems = load_all_systems(args)
    if systems is not None:
        sel = chosen_systems(args, systems)
        if not sel:
            sys.exit("Select systems to export: --all, --select or --group. -o must be a folder.")
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        fac = factory_for(args)
        for s in sel:
            try:
                recs = fac(s).list_all(args.entity)
                fields = args.fields.split(",") if args.fields else None
                headers, rows = export_records(recs, fields)
                write_csv(out_dir / f"{safe_name(s.name)}.csv", headers, rows)
                print(f"OK   {s.name}: {len(recs)} records")
            except ApiError as e:
                print(f"FAIL {s.name}: {e}")
        print(f"Saved to {out_dir}. WARNING: the files contain personal data (GDPR).")
        return 0
    c = make_client(args)
    records = c.list_all(args.entity)
    out = Path(args.output)
    if args.json:
        out.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        fields = args.fields.split(",") if args.fields else None
        headers, rows = export_records(records, fields)
        write_csv(out, headers, rows)
    print(f"Saved {len(records)} records ({args.entity}) to {out}")
    print("WARNING: the file contains personal data (GDPR) - do not email it or commit it to git.")
    return 0


def cmd_apply(args) -> int:
    headers, rows = read_csv(Path(args.file))
    if not rows:
        sys.exit("The CSV file has no data rows.")
    systems = load_all_systems(args)
    if systems is not None:
        return cmd_apply_multi(args, systems, headers, rows)
    if has_system_col(headers):
        sys.exit("The CSV has a 'system' column, so --systems FILE (KeePass export) is required.")
    c = make_client(args)
    print(f"Fetching the current state of {args.entity} from the PBX...")
    records = c.list_all(args.entity)
    try:
        key = args.key or guess_key(records)
        print(f"Key column: {key}")
        ops = build_plan(rows, headers, records, key, args.allow_create, args.allow_delete)
    except PlanError as e:
        sys.exit(f"FILE ERROR: {e}")

    run_dir = new_run_dir(args.out_dir)
    for o in ops:
        if o.action != "nochange" or args.verbose:
            print(format_op(o))
    s = summarize(ops)
    print(
        f"\nSUMMARY: updates={s['update']}, creates={s['create']}, deletes={s['delete']}, "
        f"no changes={s['nochange']}, errors={s['error']}"
    )
    write_csv(
        run_dir / "plan.csv",
        ["line", "action", "key", "message"],
        [{"line": o.line, "action": o.action, "key": o.key, "message": o.message} for o in ops],
    )

    todo = [o for o in ops if o.action in ("update", "create", "delete")]
    if args.limit:
        for o in todo[args.limit:]:
            o.action, o.message = "error", "beyond --limit"
        todo = todo[: args.limit]
        print(f"(--limit {args.limit}: only the first {len(todo)} changes will be applied)")

    if not args.execute:
        print("\nThis was a DRY RUN - nothing was changed in the PBX. Add --execute to apply.")
        return 1 if s["error"] else 0
    if not todo:
        print("Nothing to do.")
        return 1 if s["error"] else 0
    if s["error"] and not args.skip_errors:
        sys.exit(f"The plan has {s['error']} error(s). Fix the file, or use --skip-errors to skip the bad rows.")

    if not args.yes:
        ans = input(f"\nApply {len(todo)} changes on {c.base}? Type YES to confirm: ")
        if ans.strip().upper() != "YES":
            print("Cancelled.")
            return 1

    stats = execute(ops, c, args.entity, run_dir, stop_on_error=args.stop_on_error, delay=args.delay)
    print(f"\nDONE: ok={stats['ok']}, failed={stats['failed']}, skipped={stats['skipped']}")
    print(f"Backup: {run_dir / 'backup.json'}\nResults: {run_dir / 'result.csv'}")
    print(f"To undo: tcx-bulk rollback {run_dir / 'backup.json'} --execute")
    return 1 if stats["failed"] else 0


def cmd_apply_multi(args, systems, headers, rows) -> int:
    chosen = chosen_systems(args, systems)
    try:
        pool, gone = resolve_pool(systems, headers, rows, chosen)
    except SystemsError as e:
        sys.exit(f"ERROR: {e}")
    if not pool:
        sys.exit("No systems to work on.")
    h2, by_sys, unknown = split_rows(headers, rows, pool)
    if gone:
        print(f"WARNING: the CSV mentions systems that are not available/selected: {', '.join(gone)}")
    print(f"Planning {len(pool)} system(s) with {args.workers} parallel connections...")
    plans = plan_all(pool, h2, by_sys, args.entity, args.key, factory_for(args),
                     args.allow_create, args.allow_delete, workers=args.workers)
    run_dir = new_run_dir(args.out_dir)
    for line in describe(plans, args.verbose):
        print(line)
    write_plan_csv(plans, run_dir / "plan_changes.csv")
    t = totals(plans)
    print(f"\nSUMMARY: {t['systems']} systems ({t['systems_with_changes']} with changes, "
          f"{t['systems_failed_to_plan']} could not be planned); updates={t['update']}, creates={t['create']}, "
          f"deletes={t['delete']}, row errors={t['error']}, CSV rows for unavailable systems={len(unknown)}")
    print(f"Full plan: {run_dir / 'plan_changes.csv'}")
    if args.limit:
        keep = [p for p in plans if p.todo][: args.limit]
        for p in plans:
            if p.todo and p not in keep:
                p.ops = []
        print(f"(--limit {args.limit}: only the first {len(keep)} system(s) with changes will be touched)")
    bad = t["systems_failed_to_plan"] + t["error"] + len(unknown) + len(gone)
    if not args.execute:
        print("\nThis was a DRY RUN - nothing was changed on any PBX. Add --execute to apply.")
        return 1 if bad else 0
    todo = [p for p in plans if not p.error and p.todo]
    if not todo:
        print("Nothing to do.")
        return 1 if bad else 0
    if bad and not args.skip_errors:
        sys.exit("The plan has errors. Fix them, or use --skip-errors to apply to the systems/rows that are fine.")
    n = sum(p.todo for p in todo)
    if not args.yes:
        if input(f"\nApply {n} changes on {len(todo)} systems? Type YES to confirm: ").strip().upper() != "YES":
            print("Cancelled.")
            return 1
    ask = (lambda m: True) if args.yes else (lambda m: input(f"\n{m} Type YES to continue: ").strip().upper() == "YES")
    res = run_multi(plans, args.entity, run_dir, pilot=not args.no_pilot, stop_on_error=args.stop_on_error,
                    delay=args.delay, confirm=ask)
    failed = sum(int(r["failed"]) for r in res)
    print(f"\nDONE: {sum(int(r['ok']) for r in res)} ok, {failed} failed, "
          f"{sum(1 for r in res if r['status'] == 'not run')} system(s) not run.")
    print(f"Summary: {run_dir / 'summary.csv'}\nBackups: {run_dir}/<system>/backup.json")
    print(f"To undo: tcx-bulk rollback {run_dir} --systems <keepass.csv> --execute")
    return 1 if failed or any(r["status"] == "not run" for r in res) else 0


def cmd_rollback(args) -> int:
    if Path(args.backup).is_dir():
        systems = load_all_systems(args)
        if systems is None:
            sys.exit("Rolling back a whole run folder needs --systems FILE (the KeePass export).")
        st = rollback_multi(Path(args.backup), systems, factory_for(args), args.execute)
        print(f"\n{'DONE' if args.execute else 'DRY RUN'}: systems={st['systems']}, restored={st['ok']}, "
              f"failed={st['failed']}, not restorable={st['not_restorable']}")
        if not args.execute:
            print("Add --execute to really undo the changes.")
        return 1 if st["failed"] else 0
    c = make_client(args)
    stats = rollback(Path(args.backup), c, args.execute)
    if not args.execute:
        print("\nDRY RUN. Add --execute to really undo the changes.")
    else:
        print(f"\nDONE: restored={stats['ok']}, failed={stats['failed']}, not restorable={stats['not_restorable']}")
    return 1 if stats["failed"] else 0


# ------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--fqdn", help="PBX address, e.g. pbx.company.ie (or TCX_FQDN in .env)")
    common.add_argument("--systems", help="KeePass CSV export with your PBX systems (or TCX_SYSTEMS in .env)")
    common.add_argument("--select", help="system names, comma separated, wildcards allowed (e.g. 'acme*,beta')")
    common.add_argument("--group", help="select all systems in this KeePass group")
    common.add_argument("--all", action="store_true", help="select ALL systems from the systems file")
    common.add_argument("--workers", type=int, default=4, help="parallel connections when planning/checking (default 4)")
    common.add_argument("--insecure", action="store_true", help="disable TLS verification (not recommended)")
    ent = argparse.ArgumentParser(add_help=False)
    ent.add_argument("--entity", default="Users", help="XAPI entity (default: Users = extensions)")

    p = argparse.ArgumentParser(prog="tcx-bulk", description="Bulk changes in 3CX through the API (from CSV).")
    p.add_argument("--version", action="version", version=f"tcx-bulk {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("update", help="check for a new version and install it (packaged program only)")
    sp.add_argument("--check-only", action="store_true", help="only report, do not install")
    sp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sp.set_defaults(fn=cmd_update)

    sp = sub.add_parser("systems", help="show the systems found in a KeePass CSV export (no passwords)")
    sp.add_argument("file")
    sp.set_defaults(fn=cmd_systems)

    sp = sub.add_parser("check", parents=[common, ent], help="check connection and login")
    sp.set_defaults(fn=cmd_check)

    sp = sub.add_parser("entities", parents=[common], help="list all entities available on this PBX")
    sp.set_defaults(fn=cmd_entities)

    sp = sub.add_parser("export", parents=[common, ent], help="export the current state to CSV/JSON (backup + template)")
    sp.add_argument("-o", "--output", required=True)
    sp.add_argument("--fields", help="comma-separated list of fields (default: all simple fields)")
    sp.add_argument("--json", action="store_true", help="full JSON export (including nested data)")
    sp.set_defaults(fn=cmd_export)

    sp = sub.add_parser("apply", parents=[common, ent], help="apply changes from a CSV (DRY RUN by default)")
    sp.add_argument("file", help="CSV file")
    sp.add_argument("--key", help="column that identifies the record (default: Number, then Name, then Id)")
    sp.add_argument("--execute", action="store_true", help="REALLY apply the changes (without it: preview only)")
    sp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sp.add_argument("--allow-create", action="store_true", help="allow action=create")
    sp.add_argument("--allow-delete", action="store_true", help="allow action=delete")
    sp.add_argument("--limit", type=int, help="apply only the first N changes (multi-system: only the first N systems with changes)")
    sp.add_argument("--skip-errors", action="store_true", help="apply the valid rows even if some rows have errors")
    sp.add_argument("--stop-on-error", action="store_true", help="stop after the first API error")
    sp.add_argument("--no-pilot", action="store_true", help="multi-system: skip the pilot step (first system, then ask)")
    sp.add_argument("--delay", type=float, default=0.0, help="pause (seconds) between changes")
    sp.add_argument("--out-dir", default="tcx-runs", help="where to save the backup and logs")
    sp.add_argument("-v", "--verbose", action="store_true", help="also show rows with no changes")
    sp.set_defaults(fn=cmd_apply)

    sp = sub.add_parser("rollback", parents=[common], help="undo changes using a backup.json")
    sp.add_argument("backup")
    sp.add_argument("--execute", action="store_true")
    sp.set_defaults(fn=cmd_rollback)
    return p


def main(argv=None) -> int:
    load_dotenv()
    if updater.is_frozen():
        updater.cleanup_old()
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except ApiError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
