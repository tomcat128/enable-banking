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
    web_token: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "banking.sqlite3"

    @classmethod
    def from_env(cls) -> "Config":
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
            web_token=_env("EB_WEB_TOKEN"),
        )
