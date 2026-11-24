import hashlib
import io
import json
import os

import pytest
import requests
from requests.adapters import BaseAdapter

from tcx_bulk import __version__, updater


class FakeGitHub(BaseAdapter):
    """Serves a fake 'latest release' and its files."""

    def __init__(self, tag="v9.0.0", files=None, status=200, sums_override=None, host="github.com", no_sums=False):
        super().__init__()
        self.files = files if files is not None else {"tcx-bulk-linux": b"NEW-CLI", "tcx-bulk-gui-linux": b"NEW-GUI",
                                                       "tcx-bulk-windows.exe": b"NEW-WIN", "tcx-bulk-gui-windows.exe": b"NEW-WINGUI"}
        self.tag, self.status, self.host = tag, status, host
        self.sums = None if no_sums else (sums_override if sums_override is not None else "".join(
            f"{hashlib.sha256(b).hexdigest()}  {n}\n" for n, b in self.files.items()))

    def close(self):
        pass

    def send(self, req, **kw):
        r = requests.Response(); r.url = req.url; r.request = req; r.status_code = 200
        if req.url.endswith("/releases/latest"):
            r.status_code = self.status
            assets = {n: f"https://{self.host}/dl/{n}" for n in self.files}
            if self.sums is not None:
                assets["SHA256SUMS.txt"] = f"https://{self.host}/dl/SHA256SUMS.txt"
            r._content = json.dumps({"tag_name": self.tag, "body": "- shiny things", "html_url": "https://github.com/x/y/releases/tag/" + self.tag,
                                     "assets": [{"name": n, "browser_download_url": u} for n, u in assets.items()]}).encode()
            return r
        name = req.url.rsplit("/", 1)[1]
        body = self.sums.encode() if name == "SHA256SUMS.txt" else self.files.get(name)
        if body is None:
            r.status_code = 404; r._content = b""
        else:
            r.raw = io.BytesIO(body); r.headers["Content-Length"] = str(len(body))
        return r


def session(adapter):
    s = requests.Session(); s.mount("https://", adapter); return s


def test_versions():
    assert updater.parse_version("v1.2.3") == (1, 2, 3) and updater.parse_version("0.10") == (0, 10, 0)
    assert updater.is_newer("0.10.0", "0.9.9") and not updater.is_newer("0.2.0", "0.2.0") and not updater.is_newer("0.1.9", "0.2.0")
    with pytest.raises(updater.UpdateError):
        updater.parse_version("latest")


def test_asset_names():
    assert updater.asset_name("gui", "win32") == "tcx-bulk-gui-windows.exe"
    assert updater.asset_name("cli", "win32") == "tcx-bulk-windows.exe"
    assert updater.asset_name("gui", "linux") == "tcx-bulk-gui-linux" and updater.asset_name("cli", "linux") == "tcx-bulk-linux"
    assert updater.current_kind("C:/x/tcx-bulk-gui.exe") == "gui" and updater.current_kind("/a/tcx-bulk") == "cli"


def test_check_newer_none_and_errors():
    rel = updater.check(session(FakeGitHub("v9.0.0")))
    assert rel.version == "9.0.0" and "shiny" in rel.notes and "SHA256SUMS.txt" in rel.assets
    assert updater.check(session(FakeGitHub("v" + __version__))) is None  # same version -> up to date
    with pytest.raises(updater.UpdateError, match="private"):
        updater.check(session(FakeGitHub(status=404)))
    with pytest.raises(updater.UpdateError, match="HTTP 500"):
        updater.check(session(FakeGitHub(status=500)))


def test_download_verifies_checksum(tmp_path):
    gh = FakeGitHub(); s = session(gh); rel = updater.check(s)
    seen = []
    f = updater.download(rel, tmp_path, kind="gui", platform="linux", session=s, progress=lambda g, t: seen.append((g, t)))
    assert f.read_bytes() == b"NEW-GUI" and seen and seen[-1][0] == 7


def test_download_rejects_tampered_file(tmp_path):
    gh = FakeGitHub(); s = session(gh); rel = updater.check(s)
    gh.files["tcx-bulk-gui-linux"] = b"EVIL"  # served file no longer matches the published checksum
    with pytest.raises(updater.UpdateError, match="Checksum mismatch"):
        updater.download(rel, tmp_path, kind="gui", platform="linux", session=s)
    assert not list(tmp_path.glob("*.download"))  # bad file removed


def test_download_refuses_without_checksums_or_listing(tmp_path):
    s = session(FakeGitHub(no_sums=True)); rel = updater.check(s)
    with pytest.raises(updater.UpdateError, match="unverified"):
        updater.download(rel, tmp_path, kind="gui", platform="linux", session=s)
    s = session(FakeGitHub(sums_override="deadbeef  other\n")); rel = updater.check(s)
    with pytest.raises(updater.UpdateError, match="not listed"):
        updater.download(rel, tmp_path, kind="gui", platform="linux", session=s)


def test_download_refuses_untrusted_host(tmp_path):
    s = session(FakeGitHub(host="evil.example.com")); rel = updater.check(s)
    with pytest.raises(updater.UpdateError, match="untrusted"):
        updater.download(rel, tmp_path, kind="gui", platform="linux", session=s)


def test_install_posix_replaces_in_place(tmp_path):
    exe = tmp_path / "tcx-bulk-gui"; exe.write_bytes(b"OLD"); new = tmp_path / "n.download"; new.write_bytes(b"NEW")
    assert updater.install(new, str(exe), "linux") == exe
    assert exe.read_bytes() == b"NEW" and os.access(exe, os.X_OK) and not new.exists()


def test_install_windows_renames_then_cleanup(tmp_path):
    exe = tmp_path / "tcx-bulk-gui.exe"; exe.write_bytes(b"OLD"); new = tmp_path / "n.download"; new.write_bytes(b"NEW")
    updater.install(new, str(exe), "win32")
    assert exe.read_bytes() == b"NEW" and (tmp_path / "tcx-bulk-gui.exe.old").read_bytes() == b"OLD"
    updater.cleanup_old(str(exe))
    assert not (tmp_path / "tcx-bulk-gui.exe.old").exists()


def test_install_windows_failure_restores_old(tmp_path, monkeypatch):
    exe = tmp_path / "tcx-bulk-gui.exe"; exe.write_bytes(b"OLD"); new = tmp_path / "n.download"; new.write_bytes(b"NEW")
    def boom(*a, **k):
        raise OSError("locked")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(updater.UpdateError):
        updater.install(new, str(exe), "win32")
    assert exe.read_bytes() == b"OLD"  # the working program is never lost
