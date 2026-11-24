import json

import pytest

from fakepbx import FakePBX, make_client
from tcx_bulk.bulk import read_csv
from tcx_bulk.multi import (check_all, plan_all, resolve_pool, rollback_multi, run_multi, split_rows,
                            totals, write_plan_csv)
from tcx_bulk.systems import SystemsError, host_from, load_systems, select_systems

KEEPASSXC = """Group,Title,Username,Password,URL,Notes,TOTP,Icon,Last Modified,Created
Root/Customers,acme,100,good,https://pbx.acme.ie:5001/#/login,"multi
line note",,0,2026-01-01,2026-01-01
Root/Customers,beta,101,good,pbx.beta.ie,,,0,,
Root/Internal,gamma,102,good,https://gamma.example.ie,,,0,,
Root/Personal,Netflix,me@x.com,secret,,,,0,,
Root/Personal,Wifi password,,hunter2,,,,0,,
"""
KEEPASS2 = '"Account","Login Name","Password","Web Site","Comments"\n"acme","100","good","https://pbx.acme.ie","x"\n'


@pytest.fixture
def kp(tmp_path):
    f = tmp_path / "kp.csv"
    f.write_text(KEEPASSXC, encoding="utf-8-sig")
    return f


def world(names=("acme", "beta", "gamma")):
    pbxs = {n: FakePBX() for n in names}
    factory = lambda s: make_client(pbxs[s.name])[0]
    return pbxs, factory


def csvfile(tmp_path, text):
    f = tmp_path / "chg.csv"
    f.write_text(text, encoding="utf-8")
    return read_csv(f)


# ---- systems file
def test_host_from():
    assert host_from("https://pbx.acme.ie:5001/#/login") == "pbx.acme.ie:5001"
    assert host_from("pbx.beta.ie") == "pbx.beta.ie"
    assert host_from("user@pbx.x.ie/path") == "pbx.x.ie"
    assert host_from("Netflix") is None and host_from("") is None


def test_load_keepassxc_skips_non_pbx_and_keeps_multiline_notes(kp):
    systems, warns = load_systems(kp)
    assert [s.name for s in systems] == ["acme", "beta", "gamma"]
    assert systems[0].fqdn == "pbx.acme.ie:5001" and systems[0].client_id == "100"
    assert systems[0].group == "Root/Customers"
    assert any("Wifi password" in w for w in warns)
    assert "good" not in repr(systems[0])  # secret never printed


def test_non_pbx_entries_with_a_url_cannot_be_told_apart(tmp_path):
    """A web login with a URL looks like a PBX. Select by group (or in the list) to avoid it."""
    f = tmp_path / "k.csv"
    f.write_text(KEEPASSXC.replace("Root/Personal,Netflix,me@x.com,secret,,", "Root/Personal,Netflix,me@x.com,secret,https://www.netflix.com,"), encoding="utf-8")
    systems, _ = load_systems(f)
    assert "Netflix" in [s.name for s in systems]
    assert "Netflix" not in [s.name for s in select_systems(systems, group="Customers")]


def test_load_keepass2_format(tmp_path):
    f = tmp_path / "k2.csv"; f.write_text(KEEPASS2, encoding="utf-8")
    systems, _ = load_systems(f)
    assert systems[0].fqdn == "pbx.acme.ie" and systems[0].secret == "good"


def test_load_rejects_non_keepass_and_duplicates(tmp_path):
    f = tmp_path / "x.csv"; f.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(SystemsError, match="KeePass"):
        load_systems(f)
    f.write_text("Title,Username,Password,URL\na,1,p,a.x.ie\nA,1,p,b.x.ie\n", encoding="utf-8")
    with pytest.raises(SystemsError, match="Duplicate"):
        load_systems(f)


def test_select(kp):
    systems, _ = load_systems(kp)
    assert [s.name for s in select_systems(systems, names="a*")] == ["acme"]
    assert [s.name for s in select_systems(systems, group="Customers")] == ["acme", "beta"]
    assert len(select_systems(systems, all_=True)) == 3
    with pytest.raises(SystemsError):
        select_systems(systems, names="nope")


# ---- planning
def test_pool_requires_explicit_selection_without_system_column(kp, tmp_path):
    systems, _ = load_systems(kp)
    h, rows = csvfile(tmp_path, "Number,FirstName\n100,X\n")
    with pytest.raises(SystemsError, match="Select the systems"):
        resolve_pool(systems, h, rows, None)
    assert [s.name for s in resolve_pool(systems, h, rows, systems[:2])[0]] == ["acme", "beta"]


def test_system_column_routes_rows(kp, tmp_path):
    systems, _ = load_systems(kp)
    h, rows = csvfile(tmp_path, "system,Number,FirstName\nacme,100,A\nBETA,100,B\nghost,100,C\n")
    pool, gone = resolve_pool(systems, h, rows, None)
    assert [s.name for s in pool] == ["acme", "beta"] and gone == ["ghost"]
    h2, by, unknown = split_rows(h, rows, pool)
    assert "system" not in h2 and len(by["acme"]) == 1 and len(by["beta"]) == 1 and len(unknown) == 1


def test_plan_all_isolates_unreachable_system(kp, tmp_path):
    systems, _ = load_systems(kp)
    pbxs, factory = world()
    pbxs["beta"].fail_next = [400] * 10  # broken system
    h, rows = csvfile(tmp_path, "Number,FirstName\n100,Johnny\n")
    plans = plan_all(systems, h, {s.name: rows for s in systems}, "Users", None, factory, workers=3)
    assert [bool(p.error) for p in plans] == [False, True, False]
    t = totals(plans)
    assert t["update"] == 2 and t["systems_failed_to_plan"] == 1
    write_plan_csv(plans, tmp_path / "plan.csv")
    assert "Johnny" in (tmp_path / "plan.csv").read_text(encoding="utf-8-sig")
    assert all(f["FirstName"] == "Jan" for f in (x.data["Users"][0] for x in pbxs.values()))  # dry-run


def test_check_all(kp):
    systems, _ = load_systems(kp)
    pbxs, factory = world()
    res = check_all(systems, factory)
    assert [ok for _, ok, _ in res] == [True, True, True]


# ---- execution
def plans_for(systems, factory, tmp_path, text="Number,FirstName\n100,Johnny\n"):
    h, rows = csvfile(tmp_path, text)
    return plan_all(systems, h, {s.name: rows for s in systems}, "Users", None, factory)


def test_pilot_then_rest(kp, tmp_path):
    systems, _ = load_systems(kp)
    pbxs, factory = world()
    plans = plans_for(systems, factory, tmp_path)
    asked = []
    res = run_multi(plans, "Users", tmp_path / "run", confirm=lambda m: asked.append(m) or True, progress=None)
    assert len(asked) == 1 and "acme" in asked[0]
    assert all(p.data["Users"][0]["FirstName"] == "Johnny" for p in pbxs.values())
    assert (tmp_path / "run" / "acme" / "backup.json").exists() and (tmp_path / "run" / "summary.csv").exists()
    assert {r["status"] for r in res} == {"done"}


def test_pilot_declined_touches_only_pilot(kp, tmp_path):
    systems, _ = load_systems(kp)
    pbxs, factory = world()
    plans = plans_for(systems, factory, tmp_path)
    res = run_multi(plans, "Users", tmp_path / "run", confirm=lambda m: False, progress=None)
    assert pbxs["acme"].data["Users"][0]["FirstName"] == "Johnny"
    assert pbxs["beta"].data["Users"][0]["FirstName"] == "Jan" and pbxs["gamma"].data["Users"][0]["FirstName"] == "Jan"
    assert [r["status"] for r in res] == ["done", "not run", "not run"]


def test_pilot_failure_halts_everything(kp, tmp_path):
    systems, _ = load_systems(kp)
    pbxs, factory = world()
    plans = plans_for(systems, factory, tmp_path, "Number,EmailAddress\n100,reject@x.ie\n")
    res = run_multi(plans, "Users", tmp_path / "run", confirm=lambda m: True, progress=None)
    assert [r["status"] for r in res] == ["done with errors", "not run", "not run"]


def test_failure_on_one_system_does_not_block_others(kp, tmp_path):
    systems, _ = load_systems(kp)
    pbxs, factory = world()
    plans = plans_for(systems, factory, tmp_path)
    pbxs["beta"].data["Users"][0]["Id"] = 1  # keep ids; make PATCH fail instead:
    pbxs["beta"].fail_next = []
    plans[1].ops[0].rec_id = 9999  # record vanished between planning and applying -> 404
    res = run_multi(plans, "Users", tmp_path / "run", confirm=lambda m: True, progress=None)
    assert [r["status"] for r in res] == ["done", "done with errors", "done"]
    assert pbxs["gamma"].data["Users"][0]["FirstName"] == "Johnny"


def test_no_pilot_and_rollback_whole_run(kp, tmp_path):
    systems, _ = load_systems(kp)
    pbxs, factory = world()
    plans = plans_for(systems, factory, tmp_path)
    run_multi(plans, "Users", tmp_path / "run", pilot=False, confirm=lambda m: pytest.fail("no prompt"), progress=None)
    assert json.loads((tmp_path / "run" / "beta" / "backup.json").read_text())["system"] == "beta"
    st = rollback_multi(tmp_path / "run", systems, factory, True, progress=lambda *_: None)
    assert st["systems"] == 3 and st["ok"] == 3
    assert all(p.data["Users"][0]["FirstName"] == "Jan" for p in pbxs.values())
