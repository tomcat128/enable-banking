"""Sperre des automatischen Abrufs bei erneuter Bank-Anmeldung und E-Mail-Benachrichtigung."""

import pytest

from enable_banking_sync import mail, settings
from enable_banking_sync.client import ApiError
from enable_banking_sync.config import Config, ConfigError
from enable_banking_sync.storage import Storage
from enable_banking_sync.sync import SyncService, clear_block, get_block

MAIL = dict(smtp_host="smtp.example.org", smtp_user="bot@example.org", smtp_password="pw",
            alert_email=("notfall@example.org",))


def _cfg(tmp_path, **overrides):
    values = dict(
        application_id="app", private_key_path=tmp_path / "key.pem", api_base="http://x",
        redirect_url="https://bank.example.org/callback", aspsp_name=None, aspsp_country="DE",
        psu_type="personal", access_days=180, data_dir=tmp_path, fetch_interval=3600,
        initial_days=90, overlap_days=10,
    )
    return Config(**(values | overrides))


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

    def start(error=None, valid_until="2099-01-01T00:00:00+00:00", mail_error=None, **overrides):
        cfg = _cfg(tmp_path, **overrides)
        client = FakeClient(error)
        monkeypatch.setattr(settings, "make_client", lambda c, st: client)

        def fake_send(c, subject, body):
            if mail_error:
                raise mail_error
            sent.append((subject, body))
            return ", ".join(c.alert_email)

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
    assert service.run_once() == cfg.fetch_interval
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
    assert service.run_once() == cfg.fetch_interval
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
    cfg = _cfg(tmp_path, **(MAIL | {"smtp_security": security, "smtp_port": 2525,
                                    "alert_email": ("a@example.org", "b@example.org")}))
    assert mail.send(cfg, "Betreff", "Text") == "a@example.org, b@example.org"
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.example.org", 2525)
    assert ("starttls" in smtp.actions) == expect_tls
    assert ("login", "bot@example.org", "pw") in smtp.actions
    assert ("send", "bot@example.org", "a@example.org, b@example.org", "Betreff") in smtp.actions


def test_send_requires_configuration(tmp_path):
    with pytest.raises(ConfigError, match="nicht eingerichtet"):
        mail.send(_cfg(tmp_path), "x", "y")


def test_smtp_settings_from_env(monkeypatch):
    for key, value in {"EB_SMTP_HOST": "smtp.example.org", "EB_SMTP_PORT": "465",
                       "EB_ALERT_EMAIL": "a@example.org, b@example.org", "EB_SMTP_USER": "bot"}.items():
        monkeypatch.setenv(key, value)
    cfg = Config.from_env()
    assert (cfg.smtp_port, cfg.smtp_security) == (465, "ssl")
    assert cfg.alert_email == ("a@example.org", "b@example.org") and cfg.mail_configured

    monkeypatch.delenv("EB_SMTP_PORT")
    monkeypatch.setenv("EB_SMTP_SECURITY", "none")
    assert Config.from_env().smtp_port == 25
    monkeypatch.setenv("EB_SMTP_SECURITY", "tls")
    with pytest.raises(ConfigError, match="EB_SMTP_SECURITY"):
        Config.from_env()


def test_public_url():
    assert mail.public_url("https://bank.example.org/callback") == "https://bank.example.org"
    assert mail.public_url(None) is None and mail.public_url("kaputt") is None
