"""Load the list of PBX systems (name, address, Client ID, API key) from a KeePass CSV export.

Supported exports: KeePass 2.x / KeePass 1.x CSV and KeePassXC CSV.
Column mapping (header names are case-insensitive):
    name      <- Title | Account | Name          (used to refer to the system, must be unique)
    client_id <- Username | Login Name | User Name
    secret    <- Password                        (the 3CX API key)
    url       <- URL | Web Site                  (PBX address; falls back to the Title if it looks like a host name)
    group     <- Group                           (optional, lets you select systems by group)

Entries that are not PBXs (no host name, no user, no password) are skipped and reported.
The file is only read - it is never copied, and secrets are never printed.
"""
from __future__ import annotations

import csv
import fnmatch
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


class SystemsError(Exception):
    pass


@dataclass
class System:
    name: str
    fqdn: str
    client_id: str
    secret: str = field(repr=False)  # repr=False: never shows up in logs/tracebacks
    group: str = ""


ALIASES = {
    "name": ["title", "account", "name"],
    "client_id": ["username", "login name", "user name", "login", "user"],
    "secret": ["password"],
    "url": ["url", "web site", "website"],
    "group": ["group", "grouping"],
}
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-\.]*[A-Za-z0-9])?(:\d+)?$")


def host_from(value: str) -> Optional[str]:
    """'https://pbx.firma.ie:5001/#/login' -> 'pbx.firma.ie:5001'; None if it is not a host name."""
    v = re.sub(r"^[A-Za-z][A-Za-z0-9+.\-]*://", "", (value or "").strip())
    v = v.split("/")[0].split("?")[0].split("#")[0]
    if "@" in v:
        v = v.rsplit("@", 1)[1]
    return v if "." in v and _HOST.match(v) else None


def load_systems(path) -> Tuple[List[System], List[str]]:
    """Returns (systems, warnings). Warnings describe skipped rows (no secrets in them)."""
    text = Path(path).read_text(encoding="utf-8-sig")
    first = text.splitlines()[0] if text.strip() else ""
    delim = max(",;\t", key=first.count)
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delim)
    lowered = {(h or "").strip().lower(): h for h in (reader.fieldnames or [])}
    col = {}
    for canon, names in ALIASES.items():
        col[canon] = next((lowered[n] for n in names if n in lowered), None)
    missing = [c for c in ("name", "client_id", "secret") if not col[c]]
    if missing:
        raise SystemsError(
            f"This does not look like a KeePass CSV export: no column for {', '.join(missing)}. "
            f"Columns found: {', '.join(reader.fieldnames or [])}"
        )
    systems: List[System] = []
    warnings: List[str] = []
    seen = {}
    for n, row in enumerate(reader, start=2):
        get = lambda k: (row.get(col[k]) or "").strip() if col[k] else ""
        name, cid, secret = get("name"), get("client_id"), get("secret")
        fqdn = host_from(get("url")) or host_from(name)
        why = ("no title" if not name else "no user name" if not cid else
               "no password" if not secret else "no PBX address in URL/Title" if not fqdn else "")
        if why:
            if any((name, cid, secret, get("url"))):
                warnings.append(f"line {n}: skipped '{name or '?'}' ({why})")
            continue
        key = name.lower()
        if key in seen:
            raise SystemsError(f"Duplicate system name {name!r} (lines {seen[key]} and {n}). Names must be unique.")
        seen[key] = n
        systems.append(System(name, fqdn, cid, secret, get("group")))
    if not systems:
        raise SystemsError("No usable PBX entries found in the file.")
    return systems, warnings


def select_systems(systems: List[System], names: Optional[str] = None, group: Optional[str] = None,
                   all_: bool = False) -> List[System]:
    """--all | --select 'a,b,client_*' | --group 'Customers'. Wildcards (*, ?) are allowed."""
    if all_:
        chosen = list(systems)
    else:
        chosen = []
        if names:
            for pat in (p.strip() for p in names.split(",") if p.strip()):
                hit = [s for s in systems if fnmatch.fnmatch(s.name.lower(), pat.lower())]
                if not hit:
                    raise SystemsError(f"No system matches {pat!r}.")
                chosen += [s for s in hit if s not in chosen]
        if group:
            hit = [s for s in systems if s.group.lower() == group.lower() or s.group.lower().endswith("/" + group.lower())]
            if not hit:
                raise SystemsError(f"No systems in group {group!r}.")
            chosen += [s for s in hit if s not in chosen]
    return chosen
