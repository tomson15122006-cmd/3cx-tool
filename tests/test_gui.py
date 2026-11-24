"""GUI test without a real window: needs tkinter and DISPLAY (xvfb-run)."""
import os

import pytest

tk = pytest.importorskip("tkinter")
if not os.environ.get("DISPLAY"):
    pytest.skip("no DISPLAY (run through xvfb-run)", allow_module_level=True)

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
    csv.write_text("Number,FirstName\n100,Johnny\n", encoding="utf-8")
    app._test_path = str(csv)
    app.load_csv()
    assert str(app.btn_apply["state"]) == "normal" and len(app.ops) == 1
    assert pbx.data["Users"][0]["FirstName"] == "Jan"  # the preview changed nothing

    app.apply()
    assert pbx.data["Users"][0]["FirstName"] == "Johnny"
    backup = next((tmp_path / "runs").glob("*/backup.json"))

    app._test_path = str(backup)
    app.rollback()
    assert pbx.data["Users"][0]["FirstName"] == "Jan"
    out = app.log.get("1.0", "end")
    assert "nothing has been changed yet" in out and "DONE" in out
    root.destroy()


def test_connect_with_bad_secret_shows_error(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "CONFIG", tmp_path / "profile.json")
    root = tk.Tk()
    app = gui.App(root, client_factory=lambda f, c, s, verify=True: make_client(secret="zle")[0], sync=True)
    for k, v in (("fqdn", "pbx.test"), ("cid", "cid"), ("secret", "x")):
        app.v[k].set(v)
    app.connect()
    assert app.client is None and "Login failed" in app.log.get("1.0", "end")
    root.destroy()


def test_multi_system_workflow(tmp_path, monkeypatch):
    """Import a KeePass CSV in the app, select systems, preview, apply with pilot, undo the run."""
    monkeypatch.setattr(gui, "CONFIG", tmp_path / "profile.json")
    monkeypatch.setattr(gui, "RUNS", tmp_path / "runs")
    pbxs = {n: FakePBX() for n in ("acme", "beta", "gamma")}
    by_host = {f"pbx.{n}.ie": pbxs[n] for n in pbxs}
    questions = []
    app_confirm = lambda t, m: questions.append(m) or True
    root = tk.Tk()
    app = gui.App(root, client_factory=lambda f, c, s, verify=True: make_client(by_host[f])[0], sync=True, confirm=app_confirm)

    kp = tmp_path / "kp.csv"
    kp.write_text("Group,Title,Username,Password,URL\nCustomers,acme,1,good,pbx.acme.ie\n"
                  "Customers,beta,1,good,pbx.beta.ie\nCustomers,gamma,1,good,pbx.gamma.ie\n", encoding="utf-8")
    app._test_systems_path = str(kp)
    app.import_systems()
    assert len(app.systems) == 3
    app.v["filter"].set("a")          # acme, beta, gamma all contain "a"
    app.v["filter"].set("")
    app.select_all_shown()
    assert len(app.selected_systems()) == 3
    app.connect()
    assert "3/3 systems OK" in app.status["text"]

    csv = tmp_path / "in.csv"
    csv.write_text("Number,FirstName\n100,Johnny\n", encoding="utf-8")
    app._test_path = str(csv)
    app.load_csv()
    assert str(app.btn_apply["state"]) == "normal" and len(app.plans) == 3
    assert all(p.data["Users"][0]["FirstName"] == "Jan" for p in pbxs.values())  # preview changed nothing

    app.apply()
    assert all(p.data["Users"][0]["FirstName"] == "Johnny" for p in pbxs.values())
    assert any("Pilot" in q or "pilot" in q.lower() for q in questions)
    run = next((tmp_path / "runs").iterdir())
    assert (run / "summary.csv").exists() and (run / "plan_changes.csv").exists()

    app._test_path = str(run)
    app.rollback_run()
    assert all(p.data["Users"][0]["FirstName"] == "Jan" for p in pbxs.values())
    root.destroy()


def test_import_rejects_non_keepass_file(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "CONFIG", tmp_path / "profile.json")
    root = tk.Tk()
    app = gui.App(root, sync=True)
    bad = tmp_path / "bad.csv"; bad.write_text("a,b\n1,2\n", encoding="utf-8")
    app._test_systems_path = str(bad)
    app.import_systems()
    assert app.systems == [] and "KeePass" in app.log.get("1.0", "end")
    root.destroy()
