"""Interfejs wiersza polecen: check | export | apply | rollback."""
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
    """Minimalny loader .env (nie nadpisuje zmiennych juz ustawionych w srodowisku)."""
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
    missing = [n for n, v in (("TCX_FQDN (lub --fqdn)", fqdn), ("TCX_CLIENT_ID", cid), ("TCX_CLIENT_SECRET", secret)) if not v]
    if missing:
        sys.exit("Brakuje ustawien: " + ", ".join(missing) + "\nUstaw je w pliku .env (wzor: .env.example).")
    verify = os.environ.get("TCX_CA_BUNDLE") or True
    if args.insecure or os.environ.get("TCX_VERIFY_TLS", "1") == "0":
        verify = False
        requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
        print("UWAGA: weryfikacja certyfikatu TLS WYLACZONA.", file=sys.stderr)
    return XapiClient(fqdn, cid, secret, verify=verify)


def new_run_dir(base: str) -> Path:
    d = Path(base) / time.strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------ komendy
def cmd_check(args) -> int:
    c = make_client(args)
    recs = c.list_all(args.entity, page=5)
    print(f"OK: polaczono z {c.base}, zalogowano, encja {args.entity} zwrocila {len(recs)}+ rekordow.")
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
    print(f"Zapisano {len(records)} rekordow ({args.entity}) do {out}")
    print("UWAGA: plik zawiera dane osobowe (RODO/GDPR) - nie wysylaj go mailem i nie wrzucaj do gita.")
    return 0


def cmd_apply(args) -> int:
    headers, rows = read_csv(Path(args.file))
    if not rows:
        sys.exit("Plik CSV nie ma zadnych wierszy z danymi.")
    c = make_client(args)
    print(f"Pobieram aktualny stan {args.entity} z PBX...")
    records = c.list_all(args.entity)
    try:
        key = args.key or guess_key(records)
        print(f"Kolumna klucza: {key}")
        ops = build_plan(rows, headers, records, key, args.allow_create, args.allow_delete)
    except PlanError as e:
        sys.exit(f"BLAD PLIKU: {e}")

    run_dir = new_run_dir(args.out_dir)
    for o in ops:
        if o.action != "nochange" or args.verbose:
            print(format_op(o))
    s = summarize(ops)
    print(
        f"\nPODSUMOWANIE: zmian={s['update']}, nowych={s['create']}, usuniec={s['delete']}, "
        f"bez zmian={s['nochange']}, bledow={s['error']}"
    )
    write_csv(
        run_dir / "plan.csv",
        ["line", "action", "key", "message"],
        [{"line": o.line, "action": o.action, "key": o.key, "message": o.message} for o in ops],
    )

    todo = [o for o in ops if o.action in ("update", "create", "delete")]
    if args.limit:
        for o in todo[args.limit:]:
            o.action, o.message = "error", "poza limitem --limit"
        todo = todo[: args.limit]
        print(f"(--limit {args.limit}: wykonam tylko pierwsze {len(todo)} zmian)")

    if not args.execute:
        print("\nTo byl DRY-RUN - nic nie zostalo zmienione w PBX. Dodaj --execute zeby wykonac.")
        return 1 if s["error"] else 0
    if not todo:
        print("Nic do zrobienia.")
        return 1 if s["error"] else 0
    if s["error"] and not args.skip_errors:
        sys.exit(f"Plan ma {s['error']} bledow. Popraw plik albo uzyj --skip-errors, zeby pominac bledne wiersze.")

    if not args.yes:
        ans = input(f"\nWykonac {len(todo)} zmian na {c.base}? Wpisz TAK zeby potwierdzic: ")
        if ans.strip().upper() not in ("TAK", "YES"):
            print("Anulowano.")
            return 1

    stats = execute(ops, c, args.entity, run_dir, stop_on_error=args.stop_on_error, delay=args.delay)
    print(f"\nGOTOWE: ok={stats['ok']}, bledy={stats['failed']}, pominiete={stats['skipped']}")
    print(f"Backup: {run_dir / 'backup.json'}\nWyniki: {run_dir / 'result.csv'}")
    print(f"Cofniecie zmian: tcx-bulk rollback {run_dir / 'backup.json'} --execute")
    return 1 if stats["failed"] else 0


def cmd_rollback(args) -> int:
    c = make_client(args)
    stats = rollback(Path(args.backup), c, args.execute)
    if not args.execute:
        print("\nDRY-RUN. Dodaj --execute zeby naprawde cofnac zmiany.")
    else:
        print(f"\nGOTOWE: przywrocono={stats['ok']}, bledy={stats['failed']}, nieprzywracalne={stats['not_restorable']}")
    return 1 if stats["failed"] else 0


# ------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--fqdn", help="adres PBX, np. pbx.firma.ie (albo TCX_FQDN w .env)")
    common.add_argument("--insecure", action="store_true", help="wylacz weryfikacje TLS (niezalecane)")
    ent = argparse.ArgumentParser(add_help=False)
    ent.add_argument("--entity", default="Users", help="encja XAPI (domyslnie Users = rozszerzenia)")

    p = argparse.ArgumentParser(prog="tcx-bulk", description="Masowe zmiany w 3CX przez API (z CSV).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("check", parents=[common, ent], help="sprawdz polaczenie i logowanie")
    sp.set_defaults(fn=cmd_check)

    sp = sub.add_parser("entities", parents=[common], help="pokaz wszystkie encje dostepne w tej centrali")
    sp.set_defaults(fn=cmd_entities)

    sp = sub.add_parser("export", parents=[common, ent], help="wyeksportuj aktualny stan do CSV/JSON (backup + wzor)")
    sp.add_argument("-o", "--output", required=True)
    sp.add_argument("--fields", help="lista pol po przecinku (domyslnie wszystkie proste pola)")
    sp.add_argument("--json", action="store_true", help="pelny eksport JSON (z zagniezdzonymi danymi)")
    sp.set_defaults(fn=cmd_export)

    sp = sub.add_parser("apply", parents=[common, ent], help="zastosuj zmiany z CSV (domyslnie DRY-RUN)")
    sp.add_argument("file", help="plik CSV")
    sp.add_argument("--key", help="kolumna identyfikujaca rekord (domyslnie: Number, potem Name, potem Id)")
    sp.add_argument("--execute", action="store_true", help="NAPRAWDE wykonaj zmiany (bez tego tylko podglad)")
    sp.add_argument("--yes", action="store_true", help="nie pytaj o potwierdzenie")
    sp.add_argument("--allow-create", action="store_true", help="zezwol na action=create")
    sp.add_argument("--allow-delete", action="store_true", help="zezwol na action=delete")
    sp.add_argument("--limit", type=int, help="wykonaj tylko pierwsze N zmian (test na malej probce)")
    sp.add_argument("--skip-errors", action="store_true", help="wykonaj poprawne wiersze mimo blednych")
    sp.add_argument("--stop-on-error", action="store_true", help="przerwij po pierwszym bledzie API")
    sp.add_argument("--delay", type=float, default=0.0, help="pauza (s) miedzy zmianami")
    sp.add_argument("--out-dir", default="tcx-runs", help="gdzie zapisac backup i logi")
    sp.add_argument("-v", "--verbose", action="store_true", help="pokaz tez wiersze bez zmian")
    sp.set_defaults(fn=cmd_apply)

    sp = sub.add_parser("rollback", parents=[common], help="cofnij zmiany z backup.json")
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
        print(f"BLAD: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nPrzerwano.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
