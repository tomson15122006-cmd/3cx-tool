"""Test GUI bez prawdziwego okna: wymaga tkinter i DISPLAY (xvfb-run)."""
import os

import pytest

tk = pytest.importorskip("tkinter")
if not os.environ.get("DISPLAY"):
    pytest.skip("brak DISPLAY (uruchom przez xvfb-run)", allow_module_level=True)

from fakepbx import FakePBX, make_client  # noqa: E402
from tcx_bulk import gui  # noqa: E402


def test_full_workflow(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "CONFIG", tmp_path / "profile.json")
    monkeypatch.setattr(gui, "RUNS", tmp_path / "runs")
    pbx = FakePBX()
    root = tk.Tk()
    app = gui.App(root, client_factory=lambda f, c, s, verify=True: make_client(pbx)[0], sync=True, confirm=lambda t, m: True)
    for k, v in (("fqdn", "pbx.test"), ("cid", "cid"), ("secret", "good")):
        app.v[k].set(v)
    app.connect()
    assert app.client is not None

    csv = tmp_path / "in.csv"
    csv.write_text("Number,FirstName\n100,Janusz\n", encoding="utf-8")
    app._test_path = str(csv)
    app.load_csv()
    assert str(app.btn_apply["state"]) == "normal" and len(app.ops) == 1
    assert pbx.data["Users"][0]["FirstName"] == "Jan"  # podglad nic nie zmienil

    app.apply()
    assert pbx.data["Users"][0]["FirstName"] == "Janusz"
    backup = next((tmp_path / "runs").glob("*/backup.json"))

    app._test_path = str(backup)
    app.rollback()
    assert pbx.data["Users"][0]["FirstName"] == "Jan"
    out = app.log.get("1.0", "end")
    assert "nic jeszcze nie zmieniono" in out and "GOTOWE" in out
    root.destroy()


def test_connect_with_bad_secret_shows_error(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "CONFIG", tmp_path / "profile.json")
    root = tk.Tk()
    app = gui.App(root, client_factory=lambda f, c, s, verify=True: make_client(secret="zle")[0], sync=True)
    for k, v in (("fqdn", "pbx.test"), ("cid", "cid"), ("secret", "x")):
        app.v[k].set(v)
    app.connect()
    assert app.client is None and "Logowanie nieudane" in app.log.get("1.0", "end")
    root.destroy()
