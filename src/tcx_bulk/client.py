"""Thin HTTP client for the 3CX Configuration API (XAPI, V20).

What it does:
  * gets an OAuth2 token (POST /connect/token, grant_type=client_credentials),
  * refreshes the token when it expires (HTTP 401),
  * retries on temporary errors (429/502/503/504, dropped connection),
  * fetches whole lists page by page ($top/$skip),
  * PATCH / POST / DELETE on entities, e.g. /xapi/v1/Users(123).
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
    """Communication error with the PBX (with a readable description)."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _hint(status: int) -> str:
    if status == 401:
        return " (wrong client_id/client_secret, or the token has expired)"
    if status == 403:
        return (
            " (permission denied: check the role of the API client in 3CX - a role such as "
            "System Owner/Admin is needed. Known issue: the token can get the 'user' role "
            "instead of admin)"
        )
    if status == 404:
        return " (no such entity/record - check the --entity name and the Id)"
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
            raise ApiError(f"Cannot connect to {self.base}: {e}") from e
        if r.status_code != 200:
            text = _error_text(r)
            if "invalid_client" in text:
                hint = (
                    "\nThe PBX answered, so the address is right, but it REJECTED the Client ID / API key. Check:"
                    "\n  1. Client ID = the ID of the API client in 3CX (Admin > Integrations > API), not an extension "
                    "or admin login."
                    "\n  2. API key = the secret shown when the API client was created (it is shown only once - "
                    "if it is lost, generate a new one)."
                    "\n  3. The API client is enabled and has the Configuration API / an admin role."
                    "\n  4. No stray characters were copied with the key (the tool trims spaces at the ends)."
                )
            else:
                hint = _hint(r.status_code)
            raise ApiError(f"Login failed (HTTP {r.status_code}): {text}{hint}", r.status_code)
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
                raise ApiError(f"Connection error ({method} {path}): {e}") from e

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
                    f"HTTP {r.status_code} on {method} {path}: {_error_text(r)}"
                    f"{_hint(r.status_code)}",
                    r.status_code,
                )
            return r

    # ---------- operacje wysokiego poziomu ----------
    @staticmethod
    def _check_entity(entity: str) -> None:
        if not ENTITY_RE.match(entity):
            raise ApiError(f"Invalid entity name: {entity!r}")

    @staticmethod
    def _ref(entity: str, rec_id: Any) -> str:
        if isinstance(rec_id, int):
            return f"{entity}({rec_id})"
        if re.fullmatch(r"[A-Za-z0-9_\-]+", str(rec_id)):
            return f"{entity}('{rec_id}')"
        raise ApiError(f"Invalid Id: {rec_id!r}")

    def entity_sets(self) -> List[str]:
        """All entities (EntitySets) exposed by this PBX, read from $metadata."""
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
                raise ApiError("The server ignores $skip (pages repeat) - aborting.")
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
