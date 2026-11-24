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
from .client import ApiError, XapiClient


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
    verify = os.environ.get("TCX_CA_BUNDLE") or True
    if args.insecure or os.environ.get("TCX_VERIFY_TLS", "1") == "0":
        verify = False
        requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
        print("WARNING: TLS certificate verification is DISABLED.", file=sys.stderr)
    return XapiClient(fqdn, cid, secret, verify=verify)


def new_run_dir(base: str) -> Path:
    d = Path(base) / time.strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------ commands
def cmd_check(args) -> int:
    c = make_client(args)
    recs = c.list_all(args.entity, page=5)
    print(f"OK: connected to {c.base}, logged in, entity {args.entity} returned {len(recs)}+ records.")
    return 0


def cmd_entities(args) -> int:
    for name in make_client(args).entity_sets():
        print(name)
    return 0


def cmd_export(args) -> int:
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


def cmd_rollback(args) -> int:
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
    common.add_argument("--insecure", action="store_true", help="disable TLS verification (not recommended)")
    ent = argparse.ArgumentParser(add_help=False)
    ent.add_argument("--entity", default="Users", help="XAPI entity (default: Users = extensions)")

    p = argparse.ArgumentParser(prog="tcx-bulk", description="Bulk changes in 3CX through the API (from CSV).")
    sub = p.add_subparsers(dest="cmd", required=True)

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
    sp.add_argument("--limit", type=int, help="apply only the first N changes (test on a small sample)")
    sp.add_argument("--skip-errors", action="store_true", help="apply the valid rows even if some rows have errors")
    sp.add_argument("--stop-on-error", action="store_true", help="stop after the first API error")
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
