"""Cienki klient HTTP do 3CX Configuration API (XAPI, V20).

Co robi:
  * pobiera token OAuth2 (POST /connect/token, grant_type=client_credentials),
  * odswieza token gdy wygasnie (HTTP 401),
  * ponawia zapytania przy chwilowych bledach (429/502/503/504, zerwane polaczenie),
  * pobiera cale listy stronami ($top/$skip),
  * PATCH / POST / DELETE na encjach, np. /xapi/v1/Users(123).
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional

import requests

ENTITY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
RETRY_STATUS = {429, 502, 503, 504}
IDEMPOTENT = {"GET", "PATCH", "PUT", "DELETE"}


class ApiError(Exception):
    """Blad komunikacji z PBX (z czytelnym opisem po polsku)."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _hint(status: int) -> str:
    if status == 401:
        return " (zle client_id/client_secret albo token wygasl)"
    if status == 403:
        return (
            " (brak uprawnien: sprawdz role klienta API w 3CX - potrzebna rola np. "
            "System Owner/Admin. Znany problem: token potrafi dostac role 'user' "
            "zamiast admina)"
        )
    if status == 404:
        return " (nie ma takiej encji/rekordu - sprawdz nazwe --entity i Id)"
    return ""


def _error_text(resp: requests.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)
        if err:
            return f"{err}: {data.get('error_description', '')}".strip(": ")
    except ValueError:
        pass
    return (resp.text or "").strip()[:300]


class XapiClient:
    def __init__(
        self,
        fqdn: str,
        client_id: str,
        client_secret: str,
        verify: Any = True,
        timeout: float = 30,
        max_retries: int = 4,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        fqdn = fqdn.strip().rstrip("/")
        if not fqdn.startswith(("http://", "https://")):
            fqdn = "https://" + fqdn
        self.base = fqdn
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.verify = verify
        self._sleep = sleep
        self._token: Optional[str] = None
        self._expires = 0.0

    # ---------- autoryzacja ----------
    def _ensure_token(self, force: bool = False) -> None:
        if not force and self._token and time.time() < self._expires:
            return
        try:
            r = self.session.post(
                f"{self.base}/connect/token",
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise ApiError(f"Nie moge polaczyc sie z {self.base}: {e}") from e
        if r.status_code != 200:
            raise ApiError(
                f"Logowanie nieudane (HTTP {r.status_code}): {_error_text(r)}"
                f"{_hint(r.status_code)}",
                r.status_code,
            )
        body = r.json()
        self._token = body["access_token"]
        self._expires = time.time() + max(int(body.get("expires_in", 3600)) - 60, 30)

    # ---------- zapytania ----------
    def request(self, method: str, path: str, params=None, json=None, accept="application/json") -> requests.Response:
        url = f"{self.base}/xapi/v1/{path.lstrip('/')}"
        refreshed = False
        attempt = 0
        while True:
            self._ensure_token()
            try:
                r = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "OData-Version": "4.0",
                        "Accept": accept,
                    },
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                if method in IDEMPOTENT and attempt < self.max_retries:
                    attempt += 1
                    self._sleep(min(2 ** attempt, 30))
                    continue
                raise ApiError(f"Blad polaczenia ({method} {path}): {e}") from e

            if r.status_code == 401 and not refreshed:
                refreshed = True
                self._ensure_token(force=True)
                continue
            if (
                r.status_code in RETRY_STATUS
                and attempt < self.max_retries
                and (method in IDEMPOTENT or r.status_code == 429)
            ):
                attempt += 1
                wait = r.headers.get("Retry-After")
                self._sleep(float(wait) if wait and wait.isdigit() else min(2 ** attempt, 30))
                continue
            if r.status_code >= 400:
                raise ApiError(
                    f"HTTP {r.status_code} przy {method} {path}: {_error_text(r)}"
                    f"{_hint(r.status_code)}",
                    r.status_code,
                )
            return r

    # ---------- operacje wysokiego poziomu ----------
    @staticmethod
    def _check_entity(entity: str) -> None:
        if not ENTITY_RE.match(entity):
            raise ApiError(f"Niepoprawna nazwa encji: {entity!r}")

    @staticmethod
    def _ref(entity: str, rec_id: Any) -> str:
        if isinstance(rec_id, int):
            return f"{entity}({rec_id})"
        if re.fullmatch(r"[A-Za-z0-9_\-]+", str(rec_id)):
            return f"{entity}('{rec_id}')"
        raise ApiError(f"Niepoprawne Id: {rec_id!r}")

    def entity_sets(self) -> List[str]:
        """Lista wszystkich encji (EntitySet) udostepnianych przez ta centrale - z $metadata."""
        r = self.request("GET", "$metadata", accept="application/xml")
        root = ET.fromstring(r.content)
        names = {el.get("Name") for el in root.iter() if el.tag.endswith("}EntitySet") or el.tag == "EntitySet"}
        return sorted(n for n in names if n)

    def list_all(self, entity: str, page: int = 100) -> List[Dict[str, Any]]:
        self._check_entity(entity)
        out: List[Dict[str, Any]] = []
        skip = 0
        prev = None
        while True:
            r = self.request("GET", entity, params={"$top": page, "$skip": skip})
            chunk = r.json().get("value", [])
            if skip and chunk and chunk == prev:
                raise ApiError("Serwer ignoruje $skip (strony sie powtarzaja) - przerywam.")
            out.extend(chunk)
            if len(chunk) < page:
                return out
            prev, skip = chunk, skip + page

    def patch(self, entity: str, rec_id: Any, body: Dict[str, Any]) -> None:
        self._check_entity(entity)
        self.request("PATCH", self._ref(entity, rec_id), json=body)

    def create(self, entity: str, body: Dict[str, Any]) -> Any:
        self._check_entity(entity)
        r = self.request("POST", entity, json=body)
        try:
            return r.json()
        except ValueError:
            return None

    def delete(self, entity: str, rec_id: Any) -> None:
        self._check_entity(entity)
        self.request("DELETE", self._ref(entity, rec_id))
