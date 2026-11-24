"""Logika masowych zmian: wczytanie CSV, plan (dry-run), wykonanie, backup, rollback."""
from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .client import ApiError, XapiClient

CLEAR_TOKEN = "__EMPTY__"  # wpisz w komorke CSV, zeby ustawic pusta wartosc ""
ACTION_COL = "action"
ACTIONS = ("update", "create", "delete")
_TRUE = {"true", "1", "yes", "y", "tak", "t"}
_FALSE = {"false", "0", "no", "n", "nie", "f"}


class PlanError(Exception):
    """Blad calego pliku (np. literowka w nazwie kolumny) - przerywamy przed zmianami."""


# ---------------------------------------------------------------- CSV
def read_csv(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    # utf-8-sig zjada BOM, ktory dodaje Excel
    text = Path(path).read_text(encoding="utf-8-sig")
    first = text.splitlines()[0] if text.strip() else ""
    delim = max(",;\t", key=first.count)  # Excel w PL/EU czesto zapisuje ';'
    reader = csv.DictReader(text.splitlines(), delimiter=delim)
    headers = [h.strip() for h in (reader.fieldnames or [])]
    rows = []
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if any(row.values()):
            rows.append(row)
    return headers, rows


def write_csv(path: Path, headers: List[str], rows: List[Dict[str, Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- typy
class _Unset:
    pass


UNSET = _Unset()


def coerce(raw: str, current: Any) -> Any:
    """Zamienia tekst z CSV na wlasciwy typ, wzorujac sie na obecnej wartosci w PBX."""
    raw = raw.strip()
    if raw == "":
        return UNSET  # pusta komorka = nie zmieniaj
    if raw == CLEAR_TOKEN:
        return ""
    if isinstance(current, bool):
        low = raw.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ValueError(f"oczekiwano tak/nie (true/false), jest {raw!r}")
    if isinstance(current, int):
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"oczekiwano liczby calkowitej, jest {raw!r}") from None
    if isinstance(current, float):
        try:
            return float(raw.replace(",", "."))
        except ValueError:
            raise ValueError(f"oczekiwano liczby, jest {raw!r}") from None
    if isinstance(current, (dict, list)) or raw[:1] in "{[":
        try:
            return json.loads(raw)
        except ValueError as e:
            raise ValueError(f"niepoprawny JSON: {e}") from None
    if current is None and raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    return raw  # tekst (numery telefonow zostaja tekstem - waznie dla zer na poczatku)


# ---------------------------------------------------------------- plan
@dataclass
class Op:
    line: int
    action: str  # update | create | delete | nochange | error
    key: str
    rec_id: Any = None
    changes: Dict[str, Tuple[Any, Any]] = field(default_factory=dict)  # pole -> (stare, nowe)
    body: Dict[str, Any] = field(default_factory=dict)
    before: Dict[str, Any] = field(default_factory=dict)  # pelny stary rekord (dla delete)
    message: str = ""
    label: str = ""


def _scalar(v: Any) -> bool:
    return not isinstance(v, (dict, list))


def build_plan(
    rows: List[Dict[str, str]],
    headers: List[str],
    records: List[Dict[str, Any]],
    key_field: str,
    allow_create: bool = False,
    allow_delete: bool = False,
) -> List[Op]:
    if key_field not in headers:
        raise PlanError(f"Brak kolumny klucza {key_field!r} w pliku CSV (kolumny: {', '.join(headers)}).")

    known: Dict[str, Any] = {}  # pole -> przykladowa wartosc (do podpowiedzi typu)
    for rec in records:
        for k, v in rec.items():
            if known.get(k) is None:
                known[k] = v
    data_cols = [h for h in headers if h not in (key_field, ACTION_COL, "Id", "")]
    if records:
        unknown = [h for h in data_cols if h not in known]
        if unknown:
            raise PlanError(
                "Nieznane kolumny (literowka?): " + ", ".join(unknown)
                + "\nPoprawne pola tej encji: " + ", ".join(sorted(known))
            )

    index: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        index.setdefault(str(rec.get(key_field, "")), []).append(rec)

    ops: List[Op] = []
    seen: Dict[str, int] = {}
    for n, row in enumerate(rows, start=2):  # linia 1 = naglowek
        key = row.get(key_field, "")
        action = (row.get(ACTION_COL) or "update").lower()
        op = Op(line=n, action="error", key=key)
        ops.append(op)
        if not key:
            op.message = f"pusty klucz {key_field}"
            continue
        if action not in ACTIONS:
            op.message = f"nieznana akcja {action!r} (dozwolone: {', '.join(ACTIONS)})"
            continue
        if key in seen:
            op.message = f"duplikat klucza {key!r} (juz w linii {seen[key]})"
            continue
        seen[key] = n

        matches = index.get(key, [])
        if len(matches) > 1:
            op.message = f"klucz {key!r} nie jest unikalny w PBX ({len(matches)} rekordow)"
            continue
        rec = matches[0] if matches else None

        c = key_field
        try:
            if action == "create":
                if not allow_create:
                    op.message = "create wymaga flagi --allow-create"
                    continue
                if rec is not None:
                    op.message = "rekord juz istnieje w PBX (create pominiety)"
                    continue
                body: Dict[str, Any] = {}
                if key_field != "Id":  # Id nadaje sama centrala
                    body[key_field] = coerce(key, known.get(key_field))
                for c in data_cols:
                    v = coerce(row.get(c, ""), known.get(c))
                    if v is not UNSET:
                        body[c] = v
                op.action, op.body = "create", body
                op.changes = {c: (None, v) for c, v in body.items()}
                op.label = key
                continue

            if rec is None:
                op.message = f"nie znaleziono w PBX rekordu {key_field}={key!r}"
                continue
            op.rec_id = rec.get("Id")
            op.label = " ".join(
                str(rec.get(x)) for x in ("DisplayName", "Name") if rec.get(x)
            ) or key

            if action == "delete":
                if not allow_delete:
                    op.message = "delete wymaga flagi --allow-delete"
                    continue
                op.action, op.before = "delete", rec
                continue

            for c in data_cols:
                cur = rec.get(c)
                new = coerce(row.get(c, ""), cur if cur is not None else known.get(c))
                if new is UNSET or new == cur:
                    continue
                op.changes[c] = (cur, new)
            op.body = {c: new for c, (_, new) in op.changes.items()}
            op.action = "update" if op.changes else "nochange"
        except ValueError as e:
            op.action, op.message = "error", f"{c}: {e}"
    return ops


def guess_key(records: List[Dict[str, Any]]) -> str:
    """Wybiera kolumne identyfikujaca rekord: Number (rozszerzenia), potem Name, na koncu Id."""
    fields = {k for r in records for k in r}
    for cand in ("Number", "Name", "Id"):
        if cand in fields:
            return cand
    return "Id"


def summarize(ops: List[Op]) -> Dict[str, int]:
    s = {"update": 0, "create": 0, "delete": 0, "nochange": 0, "error": 0}
    for o in ops:
        s[o.action] += 1
    return s


def format_op(o: Op) -> str:
    tag = {"update": "~", "create": "+", "delete": "-", "nochange": "=", "error": "!"}[o.action]
    head = f"[{tag}] linia {o.line:>3}  {o.key} {o.label}".rstrip()
    if o.action == "error":
        return f"{head}  BŁĄD: {o.message}"
    if o.action == "nochange":
        return f"{head}  (bez zmian)"
    if o.action == "delete":
        return f"{head}  USUNIĘCIE rekordu Id={o.rec_id}"
    lines = [head]
    for c, (old, new) in o.changes.items():
        lines.append(f"        {c}: {old!r} -> {new!r}")
    return "\n".join(lines)


# ---------------------------------------------------------------- wykonanie
def execute(
    ops: List[Op],
    client: XapiClient,
    entity: str,
    run_dir: Path,
    stop_on_error: bool = False,
    delay: float = 0.0,
    progress: Optional[Callable[[str], None]] = print,
) -> Dict[str, int]:
    """Najpierw zapisuje backup, potem wykonuje zmiany jedna po drugiej."""
    todo = [o for o in ops if o.action in ("update", "create", "delete")]
    backup = {
        "entity": entity,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "entries": [
            {
                "action": o.action,
                "key": o.key,
                "id": o.rec_id,
                "before": {c: old for c, (old, _) in o.changes.items()} if o.action == "update" else {},
                "record": o.before if o.action == "delete" else None,
            }
            for o in todo
            if o.action in ("update", "delete")
        ],
    }
    (run_dir / "backup.json").write_text(json.dumps(backup, indent=2, ensure_ascii=False), encoding="utf-8")

    results: List[Dict[str, Any]] = []
    stats = {"ok": 0, "failed": 0, "skipped": 0}
    stop = False
    for i, o in enumerate(todo, start=1):
        if stop:
            stats["skipped"] += 1
            results.append({"line": o.line, "action": o.action, "key": o.key, "status": "skipped", "detail": "przerwano po bledzie"})
            continue
        try:
            if o.action == "update":
                client.patch(entity, o.rec_id, o.body)
            elif o.action == "create":
                client.create(entity, o.body)
            else:
                client.delete(entity, o.rec_id)
            stats["ok"] += 1
            status, detail = "ok", ""
        except ApiError as e:
            stats["failed"] += 1
            status, detail = "failed", str(e)
            stop = stop_on_error
        results.append({"line": o.line, "action": o.action, "key": o.key, "status": status, "detail": detail})
        if progress:
            progress(f"  [{i}/{len(todo)}] {o.action} {o.key}: {status} {detail}".rstrip())
        if delay:
            time.sleep(delay)
    write_csv(run_dir / "result.csv", ["line", "action", "key", "status", "detail"], results)
    return stats


def rollback(backup_path: Path, client: XapiClient, execute_it: bool, progress=print) -> Dict[str, int]:
    """Cofa zmiany typu 'update' z backup.json (przywraca stare wartosci pol)."""
    data = json.loads(Path(backup_path).read_text(encoding="utf-8"))
    entity = data["entity"]
    stats = {"ok": 0, "failed": 0, "not_restorable": 0}
    for e in data["entries"]:
        if e["action"] != "update":
            stats["not_restorable"] += 1
            progress(f"  pomijam {e['action']} {e['key']} (usuniete rekordy trzeba odtworzyc recznie, dane sa w backup.json)")
            continue
        progress(f"  {'PRZYWROCE' if execute_it else '[dry-run]'} {e['key']}: {e['before']}")
        if not execute_it:
            continue
        try:
            client.patch(entity, e["id"], e["before"])
            stats["ok"] += 1
        except ApiError as err:
            stats["failed"] += 1
            progress(f"    BLAD: {err}")
    return stats


def export_records(records: List[Dict[str, Any]], fields: Optional[List[str]] = None) -> Tuple[List[str], List[Dict[str, Any]]]:
    if not fields:
        seen: Dict[str, None] = {}
        for r in records:
            for k, v in r.items():
                if _scalar(v):
                    seen.setdefault(k)
        fields = list(seen)
    rows = [{f: ("" if r.get(f) is None else r.get(f)) for f in fields} for r in records]
    return fields, rows
