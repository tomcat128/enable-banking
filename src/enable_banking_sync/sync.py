"""Abruflogik: Umsätze und Salden aller Konten der aktiven Session synchronisieren."""

from __future__ import annotations

import logging
import signal
import threading
from datetime import date, datetime, timedelta, timezone

from .client import ApiError, EnableBankingClient
from .config import Config
from .storage import Storage, now_iso

log = logging.getLogger(__name__)

# Wartezeit im run-Modus, solange noch keine (gültige) Session existiert.
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
            "Keine aktive Session vorhanden. Bitte zuerst 'enable-banking authorize' ausführen."
        )
    valid_until = _parse_ts(session["valid_until"])
    if valid_until:
        remaining = valid_until - datetime.now(timezone.utc)
        if remaining.total_seconds() <= 0:
            raise NoSessionError(
                f"Die Zustimmung ist am {valid_until:%d.%m.%Y} abgelaufen. "
                "Bitte 'enable-banking authorize' erneut ausführen."
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
                    "Ggf. 'enable-banking authorize' erneut ausführen."
                )
        except Exception:
            ok = False
            log.exception("Unerwarteter Fehler beim Abruf für Konto %s", acc["uid"])
    return ok


def run_forever(client: EnableBankingClient, storage: Storage, cfg: Config) -> None:
    """Ruft in festem Intervall ab, bis SIGTERM/SIGINT empfangen wird."""
    stop = threading.Event()

    def _handle(signum, _frame):
        log.info("Signal %s empfangen, beende ...", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    log.info("Starte periodischen Abruf alle %d Sekunden", cfg.fetch_interval)
    waiting_for_session = False
    while not stop.is_set():
        try:
            sync_all(client, storage, cfg)
            waiting_for_session = False
        except NoSessionError as exc:
            # Ohne Session wird die API nicht angefragt; daher kurz warten, damit der
            # Abruf direkt nach "authorize" im laufenden Container startet.
            if not waiting_for_session:
                log.error("%s", exc)
                log.info("Prüfe alle %d Sekunden, ob eine Session vorhanden ist", NO_SESSION_RETRY)
            waiting_for_session = True
            stop.wait(NO_SESSION_RETRY)
            continue
        except Exception:
            log.exception("Abruf fehlgeschlagen")
        next_run = datetime.now() + timedelta(seconds=cfg.fetch_interval)
        log.info("Nächster Abruf: %s", next_run.strftime("%Y-%m-%d %H:%M:%S"))
        stop.wait(cfg.fetch_interval)
