"""Sperre des automatischen Abrufs bei erneuter Bank-Anmeldung und E-Mail-Benachrichtigung."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from enable_banking_sync import mail, schedule, settings
from enable_banking_sync.client import ApiError
from enable_banking_sync.config import Config, ConfigError
from enable_banking_sync.storage import Storage
from enable_banking_sync.sync import SyncService, clear_block, get_block, next_reminder

MAIL = dict(smtp_host="smtp.example.org", smtp_user="bot@example.org", smtp_password="pw",
            alert_email=("notfall@example.org",))


def _cfg(tmp_path, **overrides):
    values = dict(
        application_id="app", private_key_path=tmp_path / "key.pem", api_base="http://x",
        redirect_url="https://bank.example.org/callback", aspsp_name=None, aspsp_country="DE",
        psu_type="personal", access_days=180, data_dir=tmp_path,
        initial_days=90, overlap_days=10,
    )
    return Config(**(values | overrides))


receivers = []  # Empfänger der versendeten Nachrichten


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def iter_transactions(self, uid, date_from, date_to):
        self.calls.append(uid)
        if self.error:
            raise self.error
        return iter([])

    def get_balances(self, uid):
        return []


@pytest.fixture
def setup(tmp_path, monkeypatch):
    sent = []
    receivers.clear()

    def start(error=None, valid_until="2099-01-01T00:00:00+00:00", mail_error=None, **overrides):
        cfg = _cfg(tmp_path, **overrides)
        client = FakeClient(error)
        monkeypatch.setattr(settings, "make_client", lambda c, st: client)

        def fake_send(c, recipients, subject, body):
            if mail_error:
                raise mail_error
            sent.append((subject, body))
            receivers.append(recipients)
            return ", ".join(recipients)

        monkeypatch.setattr(mail, "send", fake_send)
        with Storage(cfg.db_path) as s:
            s.save_settings({"application_id": "app", "private_key": "KEY"})
            s.save_session({
                "session_id": "s1", "aspsp": {"name": "Testbank", "country": "DE"},
                "access": {"valid_until": valid_until},
                "accounts": [{"uid": "u1", "account_id": {"iban": "DE11"}},
                             {"uid": "u2", "account_id": {"iban": "DE22"}}],
            })
        return cfg, client, sent

    return start


def _block(cfg):
    with Storage(cfg.db_path) as s:
        return get_block(s)


def test_reauth_error_blocks_fetch_and_notifies(setup):
    cfg, client, sent = setup(ApiError(401, "Session is expired", {"error": "EXPIRED_SESSION"}), **MAIL)
    service = SyncService(cfg)
    assert service.run_once() is None
    assert client.calls == ["u1"]  # die übrigen Konten werden nicht mehr angefragt
    block = _block(cfg)
    assert "EXPIRED_SESSION" in block["reason"]
    assert block["mail"] == "Benachrichtigung an notfall@example.org gesendet."
    subject, body = sent[0]
    assert "neu verbinden" in subject
    assert "Testbank" in body and "https://bank.example.org/connect" in body

    # Weitere Durchläufe fragen die API nicht an und benachrichtigen nicht erneut.
    assert service.run_once() is None
    assert client.calls == ["u1"] and len(sent) == 1

    with Storage(cfg.db_path) as s:
        assert clear_block(s)
        assert not clear_block(s)
    client.error = None
    assert service.run_once() is not None
    assert client.calls == ["u1", "u1", "u2"]


def test_expired_consent_blocks_without_api_call(setup):
    cfg, client, sent = setup(valid_until="2020-01-01T00:00:00+00:00", **MAIL)
    assert SyncService(cfg).run_once() is None
    assert client.calls == []
    assert "abgelaufen" in _block(cfg)["reason"] and len(sent) == 1


@pytest.mark.parametrize("code", ["ASPSP_RATE_LIMIT_EXCEEDED", "ASPSP_ERROR", None])
def test_other_errors_do_not_block(setup, code):
    cfg, client, sent = setup(ApiError(429, "nope", {"error": code} if code else None), **MAIL)
    service = SyncService(cfg)
    assert service.run_once() is not None
    assert _block(cfg) is None and sent == []
    assert client.calls == ["u1", "u2"]
    assert "mindestens ein Konto" in service.last_error


def test_block_without_mail_configuration(setup):
    cfg, _, sent = setup(ApiError(403, "revoked", {"error": "REVOKED_SESSION"}))
    assert SyncService(cfg).run_once() is None
    assert "nicht eingerichtet" in _block(cfg)["mail"] and sent == []


def test_block_survives_failed_mail(setup):
    cfg, _, _ = setup(ApiError(403, "revoked", {"error": "REVOKED_SESSION"}),
                      mail_error=OSError("Connection refused"), **MAIL)
    assert SyncService(cfg).run_once() is None
    assert "konnte nicht gesendet werden: Connection refused" in _block(cfg)["mail"]


# --- Versand -------------------------------------------------------------------


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.actions = host, port, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.actions.append("starttls")

    def login(self, user, password):
        self.actions.append(("login", user, password))

    def send_message(self, msg):
        self.actions.append(("send", msg["From"], msg["To"], msg["Subject"]))


@pytest.mark.parametrize("security, cls, expect_tls", [("starttls", "SMTP", True), ("ssl", "SMTP_SSL", False),
                                                       ("none", "SMTP", False)])
def test_send_uses_configured_security(tmp_path, monkeypatch, security, cls, expect_tls):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(mail.smtplib, cls, FakeSMTP)
    cfg = _cfg(tmp_path, **(MAIL | {"smtp_security": security, "smtp_port": 2525}))
    assert mail.send(cfg, ("a@example.org", "b@example.org"), "Betreff", "Text") == "a@example.org, b@example.org"
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.example.org", 2525)
    assert ("starttls" in smtp.actions) == expect_tls
    assert ("login", "bot@example.org", "pw") in smtp.actions
    assert ("send", "bot@example.org", "a@example.org, b@example.org", "Betreff") in smtp.actions


def test_send_requires_configuration(tmp_path):
    with pytest.raises(ConfigError, match="nicht eingerichtet"):
        mail.send(_cfg(tmp_path), ("a@example.org",), "x", "y")
    with pytest.raises(ConfigError, match="Notfallkontakt"):
        mail.send(_cfg(tmp_path, **MAIL), (), "x", "y")


def test_smtp_settings_from_env(monkeypatch):
    for key, value in {"EB_SMTP_HOST": "smtp.example.org", "EB_SMTP_PORT": "465",
                       "EB_ALERT_EMAIL": "a@example.org, b@example.org", "EB_SMTP_USER": "bot"}.items():
        monkeypatch.setenv(key, value)
    cfg = Config.from_env()
    assert (cfg.smtp_port, cfg.smtp_security) == (465, "ssl")
    assert cfg.alert_email == ("a@example.org", "b@example.org") and cfg.smtp_configured

    monkeypatch.delenv("EB_SMTP_PORT")
    monkeypatch.setenv("EB_SMTP_SECURITY", "none")
    assert Config.from_env().smtp_port == 25
    monkeypatch.setenv("EB_SMTP_SECURITY", "tls")
    with pytest.raises(ConfigError, match="EB_SMTP_SECURITY"):
        Config.from_env()


def test_public_url():
    assert mail.public_url("https://bank.example.org/callback") == "https://bank.example.org"
    assert mail.public_url(None) is None and mail.public_url("kaputt") is None


# --- Erinnerungen --------------------------------------------------------------

REVOKED = ApiError(403, "revoked", {"error": "REVOKED_SESSION"})


def _since(cfg):
    return datetime.fromisoformat(_block(cfg)["since"])


def test_reminders_after_7_and_14_days_then_none(setup):
    cfg, _, sent = setup(REVOKED, **MAIL)
    service = SyncService(cfg)
    service.run_once()
    start = _since(cfg)
    assert len(sent) == 1 and "Erinnerung" not in sent[0][0]
    assert "nach 7 und 14 Tagen" in sent[0][1]

    service.send_due_reminder(start + timedelta(days=6, hours=23))
    assert len(sent) == 1

    service.send_due_reminder(start + timedelta(days=7))
    service.send_due_reminder(start + timedelta(days=7, hours=1))  # nicht doppelt
    assert len(sent) == 2
    assert "Erinnerung 2 von 3" in sent[1][0] and "weiterhin gestoppt" in sent[1][1]
    assert "Testbank" in sent[1][1] and "https://bank.example.org/connect" in sent[1][1]
    assert f"{(start + timedelta(days=14)).astimezone():%d.%m.%Y}" in sent[1][1]
    assert _block(cfg)["mail"] == "Erinnerung 2 von 3 an notfall@example.org gesendet."

    service.send_due_reminder(start + timedelta(days=13, hours=23))
    assert len(sent) == 2
    service.send_due_reminder(start + timedelta(days=14))
    assert len(sent) == 3
    assert "Erinnerung 3 von 3" in sent[2][0] and "letzte Erinnerung" in sent[2][1]
    assert next_reminder(_block(cfg)) is None

    service.send_due_reminder(start + timedelta(days=60))
    assert len(sent) == 3


def test_no_reminder_after_authorization(setup):
    cfg, _, sent = setup(REVOKED, **MAIL)
    service = SyncService(cfg)
    service.run_once()
    start = _since(cfg)
    with Storage(cfg.db_path) as s:
        assert clear_block(s)
    service.send_due_reminder(start + timedelta(days=7))
    service.tick(start + timedelta(days=14))
    assert len(sent) == 1


def test_reconnect_while_sending_does_not_restore_block(setup, monkeypatch):
    cfg, _, sent = setup(REVOKED, **MAIL)
    service = SyncService(cfg)
    service.run_once()
    start = _since(cfg)

    def send_and_reconnect(c, recipients, subject, body):
        with Storage(cfg.db_path) as s:
            clear_block(s)
        return "notfall@example.org"

    monkeypatch.setattr(mail, "send", send_and_reconnect)
    service.send_due_reminder(start + timedelta(days=7))
    assert _block(cfg) is None


def test_only_latest_reminder_after_downtime(setup):
    cfg, _, sent = setup(REVOKED, **MAIL)
    service = SyncService(cfg)
    service.run_once()
    service = SyncService(cfg)  # Neustart: der Stand kommt aus der Datenbank
    service.send_due_reminder(_since(cfg) + timedelta(days=20))
    service.send_due_reminder(_since(cfg) + timedelta(days=21))
    assert len(sent) == 2 and "Erinnerung 3 von 3" in sent[1][0]


def test_reminder_count_survives_restart(setup):
    cfg, _, sent = setup(REVOKED, **MAIL)
    SyncService(cfg).run_once()
    SyncService(cfg).send_due_reminder(_since(cfg) + timedelta(days=3))
    assert len(sent) == 1


def test_failed_mail_is_retried_hourly_and_not_counted(setup, monkeypatch):
    cfg, _, sent = setup(REVOKED, mail_error=OSError("Connection refused"), **MAIL)
    service = SyncService(cfg)
    service.run_once()
    start = _since(cfg)
    assert _block(cfg)["reminders"] == 0
    assert next_reminder(_block(cfg)) == start

    attempts = []

    def send_after_failure(c, recipients, subject, body):
        attempts.append(subject)
        return "notfall@example.org"

    monkeypatch.setattr(mail, "send", send_after_failure)
    service.send_due_reminder(start + timedelta(minutes=30))
    assert attempts == []  # noch innerhalb der Wartezeit
    service.send_due_reminder(start + timedelta(hours=2))
    assert len(attempts) == 1 and _block(cfg)["reminders"] == 1
    assert next_reminder(_block(cfg)) == start + timedelta(days=7)


def test_block_from_older_version_continues_with_reminders(setup):
    cfg, _, sent = setup(**MAIL)
    since = datetime.now(timezone.utc) - timedelta(days=8)
    legacy = {"reason": "abgelaufen", "since": since.isoformat(timespec="seconds"),
              "mail": "Benachrichtigung an notfall@example.org gesendet."}
    with Storage(cfg.db_path) as s:
        s.save_settings({"fetch_blocked": json.dumps(legacy)})
    SyncService(cfg).tick()
    assert len(sent) == 1 and "Erinnerung 2 von 3" in sent[0][0]


def test_block_without_timestamp_sends_nothing(setup):
    cfg, _, sent = setup(**MAIL)
    with Storage(cfg.db_path) as s:
        s.save_settings({"fetch_blocked": "alter Eintrag"})
    SyncService(cfg).tick()
    assert sent == []


# --- Hintergrund-Thread --------------------------------------------------------


def _local(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute).astimezone()


def test_tick_fetches_only_when_slot_is_reached(setup):
    cfg, client, _ = setup()
    service = SyncService(cfg)
    service.next_run = _local(2, 12)
    service.tick(_local(2, 11, 59))
    assert client.calls == []
    service.tick(_local(2, 12))
    assert client.calls == ["u1", "u2"]
    assert service.next_run > datetime.now(timezone.utc)


def test_tick_sends_reminders_while_blocked_without_calling_bank(setup):
    cfg, client, sent = setup(REVOKED, **MAIL)
    service = SyncService(cfg)
    service.run_once()
    calls = list(client.calls)
    start = _since(cfg)
    service.next_run = start  # ein alter Zeitpunkt darf die Sperre nicht umgehen
    service.tick(start + timedelta(days=7))
    assert client.calls == calls and len(sent) == 2 and service.next_run is None


def test_tick_after_reconnect_runs_triggered_fetch_and_plans_again(setup):
    cfg, client, sent = setup(REVOKED, **MAIL)
    service = SyncService(cfg)
    service.run_once()
    client.calls.clear()
    client.error = None
    with Storage(cfg.db_path) as s:
        clear_block(s)
    service.trigger()
    service.tick()
    assert client.calls == ["u1", "u2"] and not service.busy
    assert service.next_run is not None and len(sent) == 1


def test_trigger_while_blocked_does_not_stay_busy(setup):
    cfg, client, _ = setup(REVOKED)
    service = SyncService(cfg)
    service.run_once()
    calls = list(client.calls)
    service.trigger()
    service.tick()
    assert not service.busy and client.calls == calls


def test_tick_plans_again_when_block_is_cleared_elsewhere(setup):
    cfg, _, _ = setup(REVOKED)
    service = SyncService(cfg)
    service.run_once()
    assert service.next_run is None
    with Storage(cfg.db_path) as s:
        clear_block(s)
    service.tick()
    assert service.next_run is not None


def test_empty_schedule_means_no_automatic_fetch(setup):
    cfg, client, _ = setup()
    with Storage(cfg.db_path) as s:
        schedule.save(s, {key: [] for key in schedule.DAY_KEYS})
    service = SyncService(cfg)
    service.tick()
    assert service.next_run is None and client.calls == []
    service.trigger()  # „Jetzt abrufen“ geht trotzdem
    service.tick()
    assert client.calls == ["u1", "u2"] and service.next_run is None


def test_reschedule_uses_new_plan(setup):
    cfg, _, _ = setup()
    service = SyncService(cfg)
    with Storage(cfg.db_path) as s:
        schedule.save(s, {key: ["03:15"] for key in schedule.DAY_KEYS})
    service.reschedule()
    assert service.next_run.astimezone().strftime("%H:%M") == "03:15"


# --- Notfallkontakt ------------------------------------------------------------


def _set_contact(cfg, value):
    with Storage(cfg.db_path) as s:
        s.save_settings({settings.ALERT_KEY: value})


def test_default_recipient_comes_from_environment(setup):
    cfg, _, _ = setup(REVOKED, **MAIL)
    SyncService(cfg).run_once()
    assert receivers == [("notfall@example.org",)]


def test_custom_recipients_override_default(setup):
    cfg, _, _ = setup(REVOKED, **MAIL)
    _set_contact(cfg, "a@example.org, b@example.org")
    SyncService(cfg).run_once()
    assert receivers == [("a@example.org", "b@example.org")]
    assert _block(cfg)["mail"] == "Benachrichtigung an a@example.org, b@example.org gesendet."


def test_changed_recipients_apply_to_next_reminder(setup):
    cfg, _, _ = setup(REVOKED, **MAIL)
    service = SyncService(cfg)
    service.run_once()
    _set_contact(cfg, "neu@example.org")
    service.send_due_reminder(_since(cfg) + timedelta(days=7))
    assert receivers == [("notfall@example.org",), ("neu@example.org",)]


def test_custom_recipients_work_without_environment_default(setup):
    cfg, _, sent = setup(REVOKED, **(MAIL | {"alert_email": ()}))
    _set_contact(cfg, "a@example.org")
    SyncService(cfg).run_once()
    assert receivers == [("a@example.org",)] and len(sent) == 1


def test_contact_added_later_gets_due_message_without_waiting(setup):
    cfg, _, sent = setup(REVOKED, **(MAIL | {"alert_email": ()}))
    service = SyncService(cfg)
    service.run_once()
    assert sent == [] and "nicht eingerichtet" in _block(cfg)["mail"]
    assert _block(cfg)["reminders"] == 0

    _set_contact(cfg, "a@example.org")
    service.send_due_reminder()
    assert sent == []  # die Wartezeit läuft noch
    service.retry_mail()
    service.send_due_reminder()
    assert receivers == [("a@example.org",)] and _block(cfg)["reminders"] == 1


@pytest.mark.parametrize("text, expected", [
    ("", ()),
    ("  ", ()),
    ("a@example.org", ("a@example.org",)),
    ("a@example.org, b@example.org;c@example.org d@example.org",
     ("a@example.org", "b@example.org", "c@example.org", "d@example.org")),
    ("a@example.org\nb@example.org", ("a@example.org", "b@example.org")),
    ("a@example.org, A@EXAMPLE.org", ("a@example.org",)),
])
def test_parse_emails(text, expected):
    assert settings.parse_emails(text) == expected


@pytest.mark.parametrize("text", ["kein-adresse", "a@b", "@example.org", "a@", "a@@example.org",
                                  "Name <a@example.org>", "a@example.org, falsch", "a@exam\x00ple.org"])
def test_parse_emails_rejects_invalid(text):
    with pytest.raises(ValueError, match="keine gültige E-Mail-Adresse"):
        settings.parse_emails(text)
