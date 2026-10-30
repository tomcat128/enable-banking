"""Schlanker Client für die Enable Banking REST API."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator

import jwt
import requests

log = logging.getLogger(__name__)

# Die API akzeptiert Tokens mit maximal 24 h Laufzeit.
TOKEN_TTL_SECONDS = 3600


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str, payload: Any = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.payload = payload


class EnableBankingClient:
    def __init__(
        self,
        application_id: str,
        private_key: str,
        api_base: str = "https://api.enablebanking.com",
        timeout: float = 60.0,
    ):
        self.application_id = application_id
        self.private_key = private_key
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._token: str | None = None
        self._token_exp = 0

    # --- Authentifizierung -------------------------------------------------

    def _jwt(self) -> str:
        now = int(time.time())
        # Token wiederverwenden, solange er noch mindestens 60 s gültig ist.
        if self._token and self._token_exp - now > 60:
            return self._token
        exp = now + TOKEN_TTL_SECONDS
        self._token = jwt.encode(
            {
                "iss": "enablebanking.com",
                "aud": "api.enablebanking.com",
                "iat": now,
                "exp": exp,
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": self.application_id},
        )
        self._token_exp = exp
        return self._token

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.api_base}{path}"
        headers = {"Authorization": f"Bearer {self._jwt()}"}
        resp = self._session.request(method, url, headers=headers, timeout=self.timeout, **kwargs)
        if not resp.ok:
            try:
                payload = resp.json()
                message = payload.get("message") or payload.get("detail") or resp.text
            except ValueError:
                payload, message = None, resp.text
            raise ApiError(resp.status_code, str(message), payload)
        return resp.json() if resp.content else None

    # --- Endpunkte ---------------------------------------------------------

    def get_application(self) -> dict:
        return self._request("GET", "/application")

    def list_aspsps(self, country: str | None = None, psu_type: str | None = None) -> list[dict]:
        params = {k: v for k, v in {"country": country, "psu_type": psu_type}.items() if v}
        return self._request("GET", "/aspsps", params=params)["aspsps"]

    def start_authorization(
        self,
        aspsp_name: str,
        aspsp_country: str,
        redirect_url: str,
        psu_type: str = "personal",
        access_days: int = 180,
        state: str | None = None,
    ) -> dict:
        valid_until = datetime.now(timezone.utc) + timedelta(days=access_days)
        body = {
            "access": {"valid_until": valid_until.isoformat()},
            "aspsp": {"name": aspsp_name, "country": aspsp_country},
            "state": state or str(uuid.uuid4()),
            "redirect_url": redirect_url,
            "psu_type": psu_type,
        }
        return self._request("POST", "/auth", json=body)

    def create_session(self, code: str) -> dict:
        return self._request("POST", "/sessions", json={"code": code})

    def get_session(self, session_id: str) -> dict:
        return self._request("GET", f"/sessions/{session_id}")

    def delete_session(self, session_id: str) -> None:
        self._request("DELETE", f"/sessions/{session_id}")

    def get_account_details(self, account_uid: str) -> dict:
        return self._request("GET", f"/accounts/{account_uid}/details")

    def get_balances(self, account_uid: str) -> list[dict]:
        return self._request("GET", f"/accounts/{account_uid}/balances").get("balances", [])

    def iter_transactions(
        self,
        account_uid: str,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> Iterator[dict]:
        """Liefert alle Umsätze im Zeitraum; folgt dabei der Paginierung per continuation_key."""
        params: dict[str, str] = {}
        if date_from:
            params["date_from"] = date_from.isoformat()
        if date_to:
            params["date_to"] = date_to.isoformat()
        page = 0
        while True:
            page += 1
            data = self._request("GET", f"/accounts/{account_uid}/transactions", params=params)
            transactions = data.get("transactions") or []
            log.debug("Konto %s: Seite %d mit %d Umsätzen", account_uid, page, len(transactions))
            yield from transactions
            key = data.get("continuation_key")
            if not key:
                break
            params["continuation_key"] = key
