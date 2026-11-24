import json

import pytest

from fakepbx import FakePBX, make_client
from tcx_bulk.bulk import (CLEAR_TOKEN, PlanError, build_plan, coerce, execute, guess_key,
                           read_csv, rollback, summarize, UNSET)
from tcx_bulk.client import ApiError


def plan(tmp_path, csv_text, pbx=None, **kw):
    f = tmp_path / "in.csv"
    f.write_text(csv_text, encoding="utf-8-sig")
    client, pbx = make_client(pbx)
    headers, rows = read_csv(f)
    recs = client.list_all("Users")
    return client, pbx, build_plan(rows, headers, recs, kw.pop("key", "Number"), **kw)


# ---- klient
def test_bad_secret_gives_clear_error():
    client, _ = make_client(secret="zle")
    with pytest.raises(ApiError, match="Logowanie nieudane"):
        client.list_all("Users")


def test_pagination_collects_everything():
    client, _ = make_client()
    assert len(client.list_all("Users", page=2)) == 3


def test_token_refresh_on_401():
    client, pbx = make_client()
    client.list_all("Users")
    pbx.expire_token_once = True
    assert len(client.list_all("Users")) == 3
    assert pbx.token_calls == 2


def test_retry_on_503_for_get_but_success_after():
    client, pbx = make_client()
    pbx.fail_next = [503, 503]
    assert len(client.list_all("Users")) == 3


def test_post_not_retried_on_503():
    client, pbx = make_client()
    pbx.fail_next = [503]
    with pytest.raises(ApiError):
        client.create("Users", {"Number": "300"})


def test_entity_sets_from_metadata():
    client, _ = make_client()
    assert client.entity_sets() == ["Groups", "Users"]


def test_entity_name_injection_rejected():
    client, _ = make_client()
    with pytest.raises(ApiError):
        client.list_all("Users/../Secrets")


# ---- typy
def test_coerce():
    assert coerce("", "x") is UNSET
    assert coerce(CLEAR_TOKEN, "x") == ""
    assert coerce("tak", True) is True and coerce("0", True) is False
    assert coerce("12", 5) == 12
    assert coerce("0851234567", None) == "0851234567"  # zero na poczatku zostaje
    assert coerce('[{"a":1}]', []) == [{"a": 1}]
    with pytest.raises(ValueError):
        coerce("abc", 5)
    with pytest.raises(ValueError):
        coerce("moze", True)


# ---- plan
def test_plan_update_and_idempotence(tmp_path):
    _, _, ops = plan(tmp_path, "Number,EmailAddress,Enabled\n100,jan@new.ie,tak\n101,anna@old.ie,\n")
    assert [o.action for o in ops] == ["update", "nochange"]
    assert ops[0].body == {"EmailAddress": "jan@new.ie"}  # Enabled juz true -> pomijamy


def test_semicolon_delimiter_and_bom(tmp_path):
    _, _, ops = plan(tmp_path, "Number;FirstName\n100;Janusz\n")
    assert ops[0].action == "update" and ops[0].body == {"FirstName": "Janusz"}


def test_unknown_column_aborts_whole_file(tmp_path):
    with pytest.raises(PlanError, match="EmailAdres"):
        plan(tmp_path, "Number,EmailAdres\n100,x\n")


def test_row_errors_do_not_stop_other_rows(tmp_path):
    _, _, ops = plan(tmp_path, "Number,FirstName\n999,X\n100,Y\n100,Z\n,Q\n")
    assert [o.action for o in ops] == ["error", "update", "error", "error"]


def test_bad_bool_is_row_error(tmp_path):
    _, _, ops = plan(tmp_path, "Number,Enabled\n100,moze\n")
    assert ops[0].action == "error" and "Enabled" in ops[0].message


def test_create_delete_need_flags(tmp_path):
    csv = "action,Number,FirstName\ncreate,300,Nowy\ndelete,199,\n"
    _, _, ops = plan(tmp_path, csv)
    assert [o.action for o in ops] == ["error", "error"]
    _, _, ops = plan(tmp_path, csv, allow_create=True, allow_delete=True)
    assert [o.action for o in ops] == ["create", "delete"]


def test_clear_value(tmp_path):
    _, _, ops = plan(tmp_path, f"Number,EmailAddress\n100,{CLEAR_TOKEN}\n")
    assert ops[0].body == {"EmailAddress": ""}


def test_guess_key():
    assert guess_key([{"Id": 1, "Number": "1"}]) == "Number"
    assert guess_key([{"Id": 1, "Name": "x"}]) == "Name"
    assert guess_key([{"Id": 1}]) == "Id"


# ---- wykonanie, backup, rollback
def test_dry_run_has_no_side_effects(tmp_path):
    _, pbx, ops = plan(tmp_path, "Number,FirstName\n100,Zmiana\n")
    assert not [c for c in pbx.calls if c[0] != "GET"]


def test_execute_backup_then_rollback(tmp_path):
    client, pbx, ops = plan(tmp_path, "Number,FirstName,EmailAddress\n100,Janusz,j@new.ie\n101,Anna,reject@x.ie\n")
    run = tmp_path / "run"; run.mkdir()
    stats = execute(ops, client, "Users", run, progress=None)
    assert stats == {"ok": 1, "failed": 1, "skipped": 0}  # blad jednego wiersza nie zatrzymuje reszty
    assert pbx.data["Users"][0]["FirstName"] == "Janusz"
    backup = json.loads((run / "backup.json").read_text())
    assert backup["entries"][0]["before"] == {"FirstName": "Jan", "EmailAddress": "jan@old.ie"}
    assert "email odrzucony" in (run / "result.csv").read_text(encoding="utf-8-sig")

    rollback(run / "backup.json", client, execute_it=False, progress=lambda *_: None)
    assert pbx.data["Users"][0]["FirstName"] == "Janusz"  # dry-run nie cofa
    rollback(run / "backup.json", client, execute_it=True, progress=lambda *_: None)
    assert pbx.data["Users"][0]["FirstName"] == "Jan"
    assert pbx.data["Users"][0]["EmailAddress"] == "jan@old.ie"


def test_create_and_delete_execute(tmp_path):
    client, pbx, ops = plan(tmp_path, "action,Number,FirstName\ncreate,300,Nowy\ndelete,199,\n",
                            allow_create=True, allow_delete=True)
    run = tmp_path / "run"; run.mkdir()
    execute(ops, client, "Users", run, progress=None)
    nums = [u["Number"] for u in pbx.data["Users"]]
    assert "300" in nums and "199" not in nums
    deleted = json.loads((run / "backup.json").read_text())["entries"][0]
    assert deleted["action"] == "delete" and deleted["record"]["Number"] == "199"


def test_stop_on_error(tmp_path):
    client, pbx, ops = plan(tmp_path, "Number,EmailAddress\n100,reject@x.ie\n101,ok@x.ie\n")
    run = tmp_path / "run"; run.mkdir()
    stats = execute(ops, client, "Users", run, stop_on_error=True, progress=None)
    assert stats == {"ok": 0, "failed": 1, "skipped": 1}
    assert summarize(ops)["update"] == 2
