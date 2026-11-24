"""Self-update from GitHub Releases.

How it works
  * CI publishes a Release for every tag v*.*.* with one file per platform
        tcx-bulk-gui-windows.exe  tcx-bulk-gui-linux  tcx-bulk-windows.exe  tcx-bulk-linux
    and a SHA256SUMS.txt.
  * check()    asks GitHub for the latest release and compares versions.
  * download() fetches the right file for this program/platform and verifies its SHA-256.
  * install()  swaps the running executable for the new one:
        Windows: a running .exe cannot be overwritten but CAN be renamed, so  exe -> exe.old,  new -> exe
        Linux/macOS: atomic os.replace().
  * restart()  starts the new version. The leftover .old file is removed on the next start.

Only works for the packaged program (PyInstaller). Running from source: use `git pull` / pip.
Files are accepted only from github.com hosts and only after the SHA-256 matches.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional
from urllib.parse import urlparse

import requests

from . import __version__

REPO = os.environ.get("TCX_UPDATE_REPO", "tomson15122006-cmd/3cx-tool")
# Tests / special setups can point to another API base (e.g. a local server). Host checks are then relaxed.
API_BASE = os.environ.get("TCX_UPDATE_API", "https://api.github.com")
MAX_BYTES = 300 * 1024 * 1024
TRUSTED_SUFFIXES = ("github.com", "githubusercontent.com")


class UpdateError(Exception):
    pass


@dataclass
class Release:
    tag: str
    version: str
    notes: str = ""
    page: str = ""
    assets: Dict[str, str] = field(default_factory=dict)  # file name -> download URL


def parse_version(v: str):
    nums = re.findall(r"\d+", v.split("-")[0])
    if not nums:
        raise UpdateError(f"Cannot read the version number {v!r}")
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - len(nums[:3]))


def is_newer(latest: str, current: str = __version__) -> bool:
    return parse_version(latest) > parse_version(current)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def platform_tag(platform: Optional[str] = None) -> str:
    p = platform or sys.platform
    return "windows" if p.startswith("win") else ("macos" if p == "darwin" else "linux")


def current_kind(exe: Optional[str] = None) -> str:
    """'gui' or 'cli', judged by the executable's name."""
    name = Path(exe or sys.executable).name.lower()
    return "gui" if "gui" in name else "cli"


def asset_name(kind: str, platform: Optional[str] = None) -> str:
    plat = platform_tag(platform)
    base = "tcx-bulk-gui" if kind == "gui" else "tcx-bulk"
    return f"{base}-{plat}" + (".exe" if plat == "windows" else "")


def _trusted(url: str) -> bool:
    if API_BASE != "https://api.github.com":  # overridden for tests
        return True
    u = urlparse(url)
    return u.scheme == "https" and any(u.hostname == s or (u.hostname or "").endswith("." + s) for s in TRUSTED_SUFFIXES)


def check(session: Optional[requests.Session] = None, timeout: float = 15) -> Optional[Release]:
    """Latest release if it is newer than this program, otherwise None."""
    s = session or requests.Session()
    url = f"{API_BASE}/repos/{REPO}/releases/latest"
    try:
        r = s.get(url, headers={"Accept": "application/vnd.github+json"}, timeout=timeout)
    except requests.RequestException as e:
        raise UpdateError(f"Cannot reach the update server: {e}") from e
    if r.status_code == 404:
        raise UpdateError("No release found. The repository may be private or there is no release yet.")
    if r.status_code == 403 and "rate limit" in r.text.lower():
        raise UpdateError("GitHub rate limit reached - try again in a while.")
    if r.status_code != 200:
        raise UpdateError(f"Update server answered HTTP {r.status_code}.")
    d = r.json()
    rel = Release(
        tag=d.get("tag_name", ""), version=d.get("tag_name", "").lstrip("vV"), notes=(d.get("body") or "").strip(),
        page=d.get("html_url", ""),
        assets={a["name"]: a["browser_download_url"] for a in d.get("assets", []) if "browser_download_url" in a},
    )
    return rel if is_newer(rel.version) else None


def _get(s: requests.Session, url: str, timeout: float, stream=False) -> requests.Response:
    if not _trusted(url):
        raise UpdateError(f"Refusing to download from an untrusted address: {url}")
    r = s.get(url, timeout=timeout, stream=stream, allow_redirects=True)
    if r.status_code != 200:
        raise UpdateError(f"Download failed (HTTP {r.status_code}).")
    if not _trusted(r.url):
        raise UpdateError("The download was redirected to an untrusted address.")
    return r


def download(rel: Release, dest_dir: Path, kind: Optional[str] = None, platform: Optional[str] = None,
             progress: Optional[Callable[[int, int], None]] = None, session: Optional[requests.Session] = None,
             timeout: float = 60) -> Path:
    """Downloads the file for this platform into dest_dir and verifies its SHA-256."""
    s = session or requests.Session()
    name = asset_name(kind or current_kind(), platform)
    if name not in rel.assets:
        raise UpdateError(f"Release {rel.tag} has no file {name!r}.")
    if "SHA256SUMS.txt" not in rel.assets:
        raise UpdateError(f"Release {rel.tag} has no SHA256SUMS.txt - refusing to install an unverified file.")
    sums = _get(s, rel.assets["SHA256SUMS.txt"], timeout).text
    m = re.search(rf"^([0-9a-fA-F]{{64}})\s+\*?{re.escape(name)}\s*$", sums, re.M)
    if not m:
        raise UpdateError(f"{name} is not listed in SHA256SUMS.txt.")
    expected = m.group(1).lower()

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (name + ".download")
    r = _get(s, rel.assets[name], timeout, stream=True)
    total = int(r.headers.get("Content-Length") or 0)
    if total > MAX_BYTES:
        raise UpdateError("The file is unexpectedly large - refusing to download.")
    h, got = hashlib.sha256(), 0
    with open(dest, "wb") as f:
        for chunk in r.iter_content(1024 * 256):
            got += len(chunk)
            if got > MAX_BYTES:
                dest.unlink(missing_ok=True)
                raise UpdateError("The file is unexpectedly large - aborting.")
            h.update(chunk); f.write(chunk)
            if progress:
                progress(got, total)
    if h.hexdigest() != expected:
        dest.unlink(missing_ok=True)
        raise UpdateError("Checksum mismatch - the downloaded file is damaged or has been tampered with. Nothing was installed.")
    return dest


def install(new_file: Path, current_exe: Optional[str] = None, platform: Optional[str] = None) -> Path:
    """Replace the running executable with new_file. Returns the path of the (new) executable."""
    cur = Path(current_exe or sys.executable)
    if not cur.exists():
        raise UpdateError(f"Cannot find the current program at {cur}.")
    if not os.access(cur.parent, os.W_OK):
        raise UpdateError(f"No permission to write to {cur.parent}. Move the program to a folder you can write to "
                          "(e.g. your Documents folder) or run it as administrator once.")
    if platform_tag(platform) == "windows":
        old = cur.with_name(cur.name + ".old")
        try:
            old.unlink(missing_ok=True)
            cur.rename(old)           # allowed even while running
            try:
                os.replace(new_file, cur)
            except OSError:
                old.rename(cur)       # put the old one back
                raise
        except OSError as e:
            raise UpdateError(f"Could not replace the program file: {e}") from e
    else:
        try:
            os.chmod(new_file, 0o755)
            os.replace(new_file, cur)
        except OSError as e:
            raise UpdateError(f"Could not replace the program file: {e}") from e
    return cur


def cleanup_old(current_exe: Optional[str] = None) -> None:
    """Remove the .old file left by a previous update (harmless if it is missing or locked)."""
    cur = Path(current_exe or sys.executable)
    try:
        cur.with_name(cur.name + ".old").unlink(missing_ok=True)
    except OSError:
        pass


def restart(exe: Optional[str] = None, args=()) -> None:
    exe = exe or sys.executable
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")  # let the new onefile start its own unpacking
    subprocess.Popen([exe, *args], env=env, close_fds=True)
    os._exit(0)
