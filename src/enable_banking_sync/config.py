"""Konfiguration über Umgebungsvariablen.

Zugangsdaten (Application ID, Schlüssel, Redirect-URL) werden normalerweise in der
Weboberfläche eingetragen; die Variablen dienen dann nur als Vorbelegung (siehe settings.py).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    pass


SMTP_PORTS = {"starttls": 587, "ssl": 465, "none": 25}


def _env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if value is not None:
        value = value.strip()
    if required and not value:
        raise ConfigError(f"Umgebungsvariable {name} ist nicht gesetzt")
    return value or None


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} muss eine ganze Zahl sein, ist aber {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    if raw.lower() in ("1", "true", "yes", "ja", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "nein", "off"):
        return False
    raise ConfigError(f"{name} muss true oder false sein, ist aber {raw!r}")


@dataclass(frozen=True)
class Config:
    application_id: str | None
    private_key_path: Path
    api_base: str
    redirect_url: str | None
    aspsp_name: str | None
    aspsp_country: str
    psu_type: str
    access_days: int
    data_dir: Path
    fetch_interval: int
    initial_days: int
    overlap_days: int
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    # Login für die Weboberfläche; nur aktiv, wenn Benutzer und Passwort gesetzt sind.
    web_user: str | None = None
    web_password: str | None = None
    # Export-Pfad /export für Skripte; Basic Auth nur, wenn Benutzer und Passwort gesetzt sind.
    export_enabled: bool = True
    export_user: str | None = None
    export_password: str | None = None
    # Benachrichtigung per E-Mail, wenn die Bank eine erneute Anmeldung verlangt.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_security: str = "starttls"
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    alert_email: tuple[str, ...] = ()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "banking.sqlite3"

    @property
    def login_required(self) -> bool:
        return bool(self.web_user and self.web_password)

    @property
    def export_protected(self) -> bool:
        return bool(self.export_user and self.export_password)

    @property
    def mail_configured(self) -> bool:
        return bool(self.smtp_host and self.alert_email and (self.smtp_from or self.smtp_user))

    @classmethod
    def from_env(cls) -> "Config":
        port = _env_int("EB_SMTP_PORT", 0)
        security = (_env("EB_SMTP_SECURITY") or ("ssl" if port == 465 else "starttls")).lower()
        if security not in SMTP_PORTS:
            raise ConfigError(f"EB_SMTP_SECURITY muss starttls, ssl oder none sein, ist aber {security!r}")
        return cls(
            application_id=_env("EB_APPLICATION_ID"),
            private_key_path=Path(_env("EB_PRIVATE_KEY_PATH", "/secrets/private.pem")),
            api_base=_env("EB_API_BASE", "https://api.enablebanking.com").rstrip("/"),
            redirect_url=_env("EB_REDIRECT_URL"),
            aspsp_name=_env("EB_ASPSP_NAME"),
            aspsp_country=_env("EB_ASPSP_COUNTRY", "DE").upper(),
            psu_type=_env("EB_PSU_TYPE", "personal"),
            access_days=_env_int("EB_ACCESS_DAYS", 180),
            data_dir=Path(_env("EB_DATA_DIR", "/data")),
            fetch_interval=_env_int("EB_FETCH_INTERVAL_SECONDS", 6 * 3600),
            initial_days=_env_int("EB_INITIAL_DAYS", 90),
            overlap_days=_env_int("EB_OVERLAP_DAYS", 10),
            web_host=_env("EB_WEB_HOST", "0.0.0.0"),
            web_port=_env_int("EB_WEB_PORT", 8080),
            web_user=_env("EB_WEB_USER"),
            web_password=_env("EB_WEB_PASSWORD"),
            export_enabled=_env_bool("EB_EXPORT_ENABLED", True),
            export_user=_env("EB_EXPORT_USER"),
            export_password=_env("EB_EXPORT_PASSWORD"),
            smtp_host=_env("EB_SMTP_HOST"),
            smtp_port=port or SMTP_PORTS[security],
            smtp_security=security,
            smtp_user=_env("EB_SMTP_USER"),
            smtp_password=_env("EB_SMTP_PASSWORD"),
            smtp_from=_env("EB_SMTP_FROM"),
            alert_email=tuple(a.strip() for a in (_env("EB_ALERT_EMAIL") or "").split(",") if a.strip()),
        )
