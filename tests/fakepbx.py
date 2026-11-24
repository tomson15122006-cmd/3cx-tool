"""Udawana centrala 3CX (adapter dla requests) - do testow bez prawdziwego PBX."""
import json
import re
from urllib.parse import parse_qs, urlparse

import requests
from requests.adapters import BaseAdapter

METADATA = """<?xml version="1.0"?>
<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" Version="4.0"><edmx:DataServices>
<Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="Pbx">
<EntityContainer Name="C"><EntitySet Name="Users" EntityType="Pbx.User"/><EntitySet Name="Groups" EntityType="Pbx.G"/></EntityContainer>
</Schema></edmx:DataServices></edmx:Edmx>"""


class FakePBX(BaseAdapter):
    def __init__(self):
        super().__init__()
        self.data = {
            "Users": [
                {"Id": 1, "Number": "100", "FirstName": "Jan", "LastName": "Kowalski", "DisplayName": "Jan Kowalski",
                 "EmailAddress": "jan@old.ie", "MobileNumber": "0851111111", "Enabled": True, "Require2FA": False,
                 "Groups": [{"GroupId": 1}]},
                {"Id": 2, "Number": "101", "FirstName": "Anna", "LastName": "Nowak", "DisplayName": "Anna Nowak",
                 "EmailAddress": "anna@old.ie", "MobileNumber": None, "Enabled": True, "Require2FA": False,
                 "Groups": []},
                {"Id": 3, "Number": "199", "FirstName": "Del", "LastName": "Me", "DisplayName": "Del Me",
                 "EmailAddress": None, "MobileNumber": None, "Enabled": False, "Require2FA": False, "Groups": []},
            ],
            "Groups": [{"Id": 10, "Name": "Support", "Number": None}],
        }
        self.calls = []  # (method, path, body)
        self.token_calls = 0
        self.fail_next = []  # np. [503, 503] - kolejne odpowiedzi bledu
        self.expire_token_once = False
        self.valid_token = "tok1"
        self.next_id = 100

    def close(self):
        pass

    def _resp(self, req, status, body=None, raw=None):
        r = requests.Response()
        r.status_code = status
        r._content = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
        r.headers["Content-Type"] = "application/json"
        r.url = req.url
        r.request = req
        return r

    def send(self, req, **kw):
        u = urlparse(req.url)
        path, q = u.path, parse_qs(u.query)
        if path == "/connect/token":
            self.token_calls += 1
            form = parse_qs(req.body if isinstance(req.body, str) else req.body.decode())
            if form.get("client_secret") != ["good"]:
                return self._resp(req, 400, {"error": "invalid_client"})
            self.valid_token = f"tok{self.token_calls}"
            return self._resp(req, 200, {"access_token": self.valid_token, "expires_in": 3600})
        if req.headers.get("Authorization") != f"Bearer {self.valid_token}" or self.expire_token_once:
            self.expire_token_once = False
            return self._resp(req, 401, {})
        if self.fail_next:
            return self._resp(req, self.fail_next.pop(0), {"error": {"message": "chwilowy blad"}})
        body = json.loads(req.body) if req.body else None
        rel = path.replace("/xapi/v1/", "", 1)
        self.calls.append((req.method, rel, body))
        if rel == "$metadata":
            return self._resp(req, 200, raw=METADATA.encode())
        m = re.fullmatch(r"(\w+)(?:\((\d+)\))?", rel)
        if not m or m.group(1) not in self.data:
            return self._resp(req, 404, {"error": {"message": "not found"}})
        ent, rid = m.group(1), m.group(2)
        rows = self.data[ent]
        if req.method == "GET":
            top, skip = int(q.get("$top", [1000])[0]), int(q.get("$skip", [0])[0])
            return self._resp(req, 200, {"value": rows[skip: skip + top]})
        if req.method == "POST":
            body["Id"] = self.next_id = self.next_id + 1
            rows.append(body)
            return self._resp(req, 201, body)
        rec = next((r for r in rows if str(r["Id"]) == rid), None)
        if rec is None:
            return self._resp(req, 404, {"error": {"message": "no such record"}})
        if req.method == "PATCH":
            if body.get("EmailAddress") == "reject@x.ie":
                return self._resp(req, 400, {"error": {"message": "email odrzucony"}})
            rec.update(body)
            return self._resp(req, 204, raw=b"")
        if req.method == "DELETE":
            rows.remove(rec)
            return self._resp(req, 204, raw=b"")
        return self._resp(req, 405, {})


def make_client(pbx=None, secret="good", **kw):
    from tcx_bulk.client import XapiClient

    pbx = pbx or FakePBX()
    s = requests.Session()
    s.mount("https://", pbx)
    return XapiClient("pbx.test", "cid", secret, session=s, sleep=lambda _s: None, **kw), pbx
