"""Einstellungen aus der Weboberfläche.

Die Werte liegen in der Datenbank. Fehlt ein Wert dort, gilt die entsprechende
Umgebungsvariable bzw. die per EB_PRIVATE_KEY_PATH eingebundene Schlüsseldatei.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import jwt

from .client import EnableBankingClient
from .config import Config, ConfigError
from .storage import Storage

# Felder, die in der Weboberfläche bearbeitet werden können.
FIELDS = ("application_id", "private_key", "redirect_url", "aspsp_country")


@dataclass(frozen=True)
class Settings:
    application_id: str | None
    private_key: str | None
    private_key_source: str | None
    redirect_url: str | None
    aspsp_country: str

    @property
    def complete(self) -> bool:
        return bool(self.application_id and self.private_key)


def load(cfg: Config, storage: Storage) -> Settings:
    db = storage.settings()
    key, source = db.get("private_key"), "Datenbank"
    if not key:
        try:
            key, source = cfg.private_key_path.read_text(), str(cfg.private_key_path)
        except OSError:
            key = source = None
    return Settings(
        application_id=db.get("application_id") or cfg.application_id,
        private_key=key,
        private_key_source=source,
        redirect_url=db.get("redirect_url") or cfg.redirect_url,
        aspsp_country=db.get("aspsp_country") or cfg.aspsp_country,
    )


def make_client(cfg: Config, settings: Settings) -> EnableBankingClient:
    if not settings.complete:
        raise ConfigError(
            "Application ID und privater Schlüssel fehlen. "
            "Bitte in der Weboberfläche unter 'Einstellungen' eintragen."
        )
    return EnableBankingClient(settings.application_id, settings.private_key, cfg.api_base)


def check_private_key(pem: str) -> str | None:
    """Gibt eine Fehlermeldung zurück, wenn sich mit dem Schlüssel kein Token signieren lässt."""
    try:
        jwt.encode({"check": True}, pem, algorithm="RS256")
    except Exception as exc:  # PyJWT/cryptography werfen je nach Fehler verschiedene Typen
        return f"Der private Schlüssel ist ungültig: {exc}"
    return None


def parse_redirect(value: str) -> tuple[str, str | None]:
    """Liefert (code, state) aus einer Redirect-URL oder einem reinen Code.

    Wirft ValueError, wenn die Bank einen Fehler meldet oder kein Code enthalten ist.
    """
    value = value.strip()
    if "://" not in value and "?" not in value:
        if not value:
            raise ValueError("Es wurde weder eine URL noch ein Code angegeben.")
        return value, None
    query = parse_qs(urlparse(value).query)
    if "error" in query:
        desc = query.get("error_description", [""])[0]
        raise ValueError(f"Autorisierung fehlgeschlagen: {query['error'][0]} {desc}".strip())
    if "code" not in query:
        raise ValueError("In der URL wurde kein Parameter 'code' gefunden.")
    return query["code"][0], query.get("state", [None])[0]
