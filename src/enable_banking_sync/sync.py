"""Abruflogik: Umsätze und Salden aller Konten der aktiven Session synchronisieren."""

from __future__ import annotations

import logging
import signal
import threading
from datetime import date, datetime, timedelta, timezone

from . import settings
from .client import ApiError, EnableBankingClient
from .config import Config, ConfigError
from .storage import Storage, now_iso

log = logging.getLogger(__name__)

# Wartezeit im run-Modus, solange Einrichtung oder (gültige) Session fehlen.
NO_SESSION_RETRY = 60


class NoSessionError(RuntimeError):
    pass


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def check_session_validity(storage: Storage) -> str:
    """Prüft die aktive Session und gibt deren ID zurück."""
    session = storage.active_session()
    if not session:
        raise NoSessionError(
            "Keine aktive Session vorhanden. Bitte in der Weboberfläche die Bank verbinden."
        )
    valid_until = _parse_ts(session["valid_until"])
    if valid_until:
        remaining = valid_until - datetime.now(timezone.utc)
        if remaining.total_seconds() <= 0:
            raise NoSessionError(
                f"Die Zustimmung ist am {valid_until:%d.%m.%Y} abgelaufen. "
                "Bitte die Bank in der Weboberfläche neu verbinden."
            )
        if remaining < timedelta(days=7):
            log.warning(
                "Die Zustimmung läuft in %d Tag(en) ab (%s). Bitte rechtzeitig neu autorisieren.",
                remaining.days,
                valid_until.date(),
            )
    return session["session_id"]


def sync_account(
    client: EnableBankingClient,
    storage: Storage,
    cfg: Config,
    account_uid: str,
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[int, int]:
    started = now_iso()
    if date_from is None:
        latest = storage.latest_booking_date(account_uid)
        if latest:
            date_from = date.fromisoformat(latest[:10]) - timedelta(days=cfg.overlap_days)
        else:
            date_from = date.today() - timedelta(days=cfg.initial_days)
    try:
        transactions = list(client.iter_transactions(account_uid, date_from, date_to))
        received, inserted = storage.store_transactions(account_uid, transactions)
        try:
            storage.store_balances(account_uid, client.get_balances(account_uid))
        except ApiError as exc:
            log.warning("Salden für Konto %s konnten nicht abgerufen werden: %s", account_uid, exc)
    except Exception as exc:
        storage.log_run(
            account_uid, started, date_from.isoformat(), date_to and date_to.isoformat(),
            None, None, str(exc),
        )
        raise
    storage.log_run(
        account_uid, started, date_from.isoformat(), date_to and date_to.isoformat(),
        received, inserted,
    )
    log.info(
        "Konto %s: %d Umsätze ab %s empfangen, davon %d neu",
        account_uid, received, date_from, inserted,
    )
    return received, inserted


def sync_all(
    client: EnableBankingClient,
    storage: Storage,
    cfg: Config,
    date_from: date | None = None,
    date_to: date | None = None,
) -> bool:
    """Synchronisiert alle Konten. Gibt True zurück, wenn alle Konten erfolgreich waren."""
    session_id = check_session_validity(storage)
    accounts = storage.accounts(session_id)
    if not accounts:
        log.warning("Die aktive Session enthält keine Konten.")
        return False
    ok = True
    for acc in accounts:
        try:
            sync_account(client, storage, cfg, acc["uid"], date_from, date_to)
        except ApiError as exc:
            ok = False
            log.error("Abruf für Konto %s (%s) fehlgeschlagen: %s", acc["uid"], acc["iban"], exc)
            if exc.status in (401, 403):
                log.error(
                    "Die Session ist möglicherweise ungültig oder widerrufen. "
                    "Ggf. die Bank in der Weboberfläche neu verbinden."
                )
        except Exception:
            ok = False
            log.exception("Unerwarteter Fehler beim Abruf für Konto %s", acc["uid"])
    return ok


class SyncService:
    """Periodischer Abruf im Hintergrund, der sich aus der Weboberfläche anstoßen lässt."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.running = False
        self.requested = False
        self.last_finished: datetime | None = None
        self.last_error: str | None = None
        self.next_run: datetime | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self.running or self.requested

    def trigger(self) -> None:
        """Startet den nächsten Abruf sofort."""
        self.requested = True
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run_once(self) -> float:
        """Führt einen Abruf aus und gibt die Wartezeit bis zum nächsten zurück."""
        with self._lock:
            self.running, self.requested = True, False
            try:
                with Storage(self.cfg.db_path) as storage:
                    client = settings.make_client(self.cfg, settings.load(self.cfg, storage))
                    ok = sync_all(client, storage, self.cfg)
                self.last_error = None if ok else "Abruf für mindestens ein Konto fehlgeschlagen (siehe Konten)."
                return self.cfg.fetch_interval
            except (ConfigError, NoSessionError) as exc:
                # Ohne Einrichtung oder Session wird die API nicht angefragt; daher kurz warten.
                if str(exc) != self.last_error:
                    log.info("%s", exc)
                self.last_error = str(exc)
                return NO_SESSION_RETRY
            except Exception as exc:
                log.exception("Abruf fehlgeschlagen")
                self.last_error = str(exc)
                return self.cfg.fetch_interval
            finally:
                self.running = False
                self.last_finished = datetime.now(timezone.utc)

    def run_forever(self) -> None:
        """Ruft in festem Intervall ab, bis SIGTERM/SIGINT empfangen wird."""

        def _handle(signum, _frame):
            log.info("Signal %s empfangen, beende ...", signal.Signals(signum).name)
            self.stop()

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)

        log.info("Starte periodischen Abruf alle %d Sekunden", self.cfg.fetch_interval)
        while not self._stop.is_set():
            delay = self.run_once()
            self._wake.clear()
            if self._stop.is_set():
                break
            self.next_run = datetime.now(timezone.utc) + timedelta(seconds=delay)
            if delay == self.cfg.fetch_interval:
                log.info("Nächster Abruf: %s", self.next_run.astimezone().strftime("%Y-%m-%d %H:%M:%S"))
            self._wake.wait(delay)
