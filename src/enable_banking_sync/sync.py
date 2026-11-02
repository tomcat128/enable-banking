"""Abruflogik: Umsätze und Salden aller Konten der aktiven Session synchronisieren."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
from datetime import date, datetime, timedelta, timezone

from . import mail, schedule, settings
from .client import ApiError, EnableBankingClient
from .config import Config, ConfigError
from .storage import Storage, now_iso

log = logging.getLogger(__name__)

# Wartezeit im run-Modus, solange Einrichtung oder (gültige) Session fehlen.
NO_SESSION_RETRY = 60
# So oft prüft der Hintergrund-Thread Abrufzeit und Erinnerungen. Der Vergleich mit der Uhrzeit statt
# einer langen Wartezeit übersteht auch Zeitumstellung und Ruhezustand des Servers.
TICK_SECONDS = 30
# Abstand der Versuche, wenn eine fällige Benachrichtigung nicht versendet werden konnte.
MAIL_RETRY = timedelta(hours=1)


# Fehlercodes der API (Feld "error"), nach denen die Bank eine erneute Anmeldung verlangt.
REAUTH_ERRORS = {
    "EXPIRED_SESSION", "REVOKED_SESSION", "CLOSED_SESSION", "SESSION_DOES_NOT_EXIST",
    "WRONG_SESSION_STATUS", "ASPSP_PSU_ACTION_REQUIRED",
}

# Schlüssel in der Tabelle settings, solange der automatische Abruf gesperrt ist.
BLOCK_KEY = "fetch_blocked"


class NoSessionError(RuntimeError):
    pass


class ReauthRequired(NoSessionError):
    """Die Bank verlangt eine erneute Anmeldung; weitere Abrufe sind zwecklos."""


def needs_reauth(exc: ApiError) -> bool:
    return isinstance(exc.payload, dict) and exc.payload.get("error") in REAUTH_ERRORS


def get_block(storage: Storage) -> dict | None:
    raw = storage.settings().get(BLOCK_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {"reason": raw}


def set_block(storage: Storage, reason: str) -> dict:
    block = {"reason": reason, "since": now_iso(), "mail": "", "reminders": 0}
    storage.save_settings({BLOCK_KEY: json.dumps(block, ensure_ascii=False)})
    return block


def reminders_sent(block: dict) -> int:
    """Anzahl der bereits verschickten Benachrichtigungen dieser Sperre."""
    if "reminders" in block:
        return block["reminders"]
    # Sperre aus einer älteren Version, die nur die erste Nachricht kannte.
    return 1 if str(block.get("mail", "")).startswith("Benachrichtigung an") else 0


def due_reminder(block: dict, now: datetime) -> int | None:
    """Position in REMINDER_DAYS der jetzt fälligen Nachricht, sonst None.

    War die Anwendung länger aus, ist nur die jüngste fällige Nachricht zu versenden, nicht alle.
    """
    since = _parse_ts(block.get("since"))
    if since is None:
        return None
    due = [index for index, days in enumerate(mail.REMINDER_DAYS)
           if index >= reminders_sent(block) and now >= since + timedelta(days=days)]
    return due[-1] if due else None


def next_reminder(block: dict) -> datetime | None:
    """Zeitpunkt der nächsten Nachricht; None, wenn keine mehr vorgesehen ist."""
    since, sent = _parse_ts(block.get("since")), reminders_sent(block)
    if since is None or sent >= len(mail.REMINDER_DAYS):
        return None
    return since + timedelta(days=mail.REMINDER_DAYS[sent])


def clear_block(storage: Storage) -> bool:
    """Gibt den automatischen Abruf wieder frei. True, wenn er gesperrt war."""
    if get_block(storage) is None:
        return False
    storage.save_settings({BLOCK_KEY: None})
    log.info("Automatischer Abruf wieder freigegeben.")
    return True


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
            raise ReauthRequired(
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
            if needs_reauth(exc):
                # Gilt für die ganze Session; die übrigen Konten nicht mehr anfragen.
                raise ReauthRequired(
                    f"Die Bank verlangt eine erneute Anmeldung ({exc.payload['error']}: {exc})."
                ) from exc
            ok = False
            log.error("Abruf für Konto %s (%s) fehlgeschlagen: %s", acc["uid"], acc["iban"], exc)
        except Exception:
            ok = False
            log.exception("Unerwarteter Fehler beim Abruf für Konto %s", acc["uid"])
    return ok


class SyncService:
    """Abruf nach Zeitplan im Hintergrund, der sich aus der Weboberfläche anstoßen lässt."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.running = False
        self.requested = False
        self.last_error: str | None = None
        self.next_run: datetime | None = None
        self._mail_retry: datetime | None = None
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

    def reschedule(self) -> None:
        """Plant den nächsten Abruf neu, nachdem der Zeitplan geändert wurde."""
        self.next_run = self._next_slot(datetime.now(timezone.utc))

    def retry_mail(self) -> None:
        """Eine fällige Benachrichtigung ohne Wartezeit erneut versuchen, z. B. an neue Adressen."""
        self._mail_retry = None

    def _next_slot(self, now: datetime) -> datetime | None:
        with Storage(self.cfg.db_path) as storage:
            slot = schedule.next_run(schedule.load(storage), now)
        if slot:
            log.info("Nächster Abruf: %s", slot.astimezone().strftime("%Y-%m-%d %H:%M:%S"))
        return slot

    def run_once(self) -> datetime | None:
        """Führt einen Abruf aus und gibt den Zeitpunkt des nächsten zurück.

        None bedeutet: Der Abruf ist gesperrt oder es ist keine Uhrzeit eingetragen.
        """
        with self._lock:
            self.running, self.requested = True, False
            try:
                with Storage(self.cfg.db_path) as storage:
                    if get_block(storage):
                        self.last_error = None
                        return None
                    client = settings.make_client(self.cfg, settings.load(self.cfg, storage))
                    ok = sync_all(client, storage, self.cfg)
                self.last_error = None if ok else "Abruf für mindestens ein Konto fehlgeschlagen (siehe Konten)."
                return self._next_slot(datetime.now(timezone.utc))
            except ReauthRequired as exc:
                self._block(str(exc))
                return None
            except (ConfigError, NoSessionError) as exc:
                # Ohne Einrichtung oder Session wird die API nicht angefragt; daher kurz warten.
                if str(exc) != self.last_error:
                    log.info("%s", exc)
                self.last_error = str(exc)
                return datetime.now(timezone.utc) + timedelta(seconds=NO_SESSION_RETRY)
            except Exception as exc:
                log.exception("Abruf fehlgeschlagen")
                self.last_error = str(exc)
                return self._next_slot(datetime.now(timezone.utc))
            finally:
                self.running = False

    def _block(self, reason: str) -> None:
        """Sperrt den automatischen Abruf und benachrichtigt den Notfallkontakt."""
        log.error("%s Der automatische Abruf ist gestoppt, bis die Bank neu verbunden oder der "
                  "Abruf in der Weboberfläche freigegeben wird.", reason)
        self.last_error = None
        self._mail_retry = None
        with Storage(self.cfg.db_path) as storage:
            set_block(storage, reason)
        self.send_due_reminder()

    def send_due_reminder(self, now: datetime | None = None) -> None:
        """Versendet die fällige Benachrichtigung, solange die Sperre besteht."""
        now = now or datetime.now(timezone.utc)
        if self._mail_retry and now < self._mail_retry:
            return
        with Storage(self.cfg.db_path) as storage:
            block = get_block(storage)
            index = due_reminder(block, now) if block else None
            if index is None:
                return
            session = storage.active_session()
            st = settings.load(self.cfg, storage)
        if self.cfg.smtp_configured and st.alert_email:
            subject, body = mail.reauth_message(
                session and session["aspsp_name"], block.get("reason", ""), mail.public_url(st.redirect_url),
                index, _parse_ts(block.get("since")))
            try:
                recipients = mail.send(self.cfg, st.alert_email, subject, body)
            except mail.MAIL_ERRORS as exc:
                block["mail"] = f"Benachrichtigung konnte nicht gesendet werden: {exc}"
                self._mail_retry = now + MAIL_RETRY
                log.error("%s", block["mail"])
            else:
                block["mail"] = (f"Benachrichtigung an {recipients} gesendet." if index == 0 else
                                 f"Erinnerung {index + 1} von {len(mail.REMINDER_DAYS)} an {recipients} gesendet.")
                block["reminders"] = index + 1
                self._mail_retry = None
                log.info("%s", block["mail"])
        else:
            block["mail"] = "Keine Benachrichtigung gesendet, der Mailversand ist nicht eingerichtet."
            self._mail_retry = now + MAIL_RETRY
        # Wurde die Bank während des Versands neu verbunden, bleibt die Sperre aufgehoben.
        with Storage(self.cfg.db_path) as storage:
            storage.update_setting(BLOCK_KEY, json.dumps(block, ensure_ascii=False))

    def first_run(self) -> datetime | None:
        """Erster Abruf nach dem Start.

        Wurde noch nie abgerufen, sofort. Sonst gilt der Zeitplan: Ein Neustart oder das Neuerstellen
        von Container und Image löst keinen zusätzlichen Abruf aus, und eine verpasste Uhrzeit wird
        nicht nachgeholt, damit nachts und am Wochenende nichts abgerufen wird.
        """
        now = datetime.now(timezone.utc)
        with Storage(self.cfg.db_path) as storage:
            last = storage.last_fetch()
        return self._next_slot(now) if last else now

    def tick(self, now: datetime | None = None) -> None:
        """Eine Runde des Hintergrund-Threads: abrufen oder erinnern, falls gerade fällig."""
        now = now or datetime.now(timezone.utc)
        if self.requested:
            self.next_run = self.run_once()
            return
        with Storage(self.cfg.db_path) as storage:
            blocked = get_block(storage) is not None
        if blocked:
            self.next_run = None
            self.send_due_reminder(now)
            return
        if self.next_run is None:
            self.next_run = self._next_slot(now)
        if self.next_run and now >= self.next_run:
            self.next_run = self.run_once()

    def run_forever(self) -> None:
        """Ruft nach Zeitplan ab, bis SIGTERM/SIGINT empfangen wird."""

        def _handle(signum, _frame):
            log.info("Signal %s empfangen, beende ...", signal.Signals(signum).name)
            self.stop()

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)

        if os.environ.get("EB_FETCH_INTERVAL_SECONDS"):
            log.warning("EB_FETCH_INTERVAL_SECONDS wird nicht mehr verwendet. Die Abrufzeiten stehen "
                        "in der Weboberfläche unter 'Einstellungen'.")
        self.next_run = self.first_run()
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Fehler im Hintergrund-Thread")
            self._wake.wait(TICK_SECONDS)
            # Ein Anstoß während des laufenden Abrufs bleibt gesetzt und führt sofort zum nächsten.
            self._wake.clear()
