import base64
import http.client
import json
import threading
from pathlib import Path
from urllib.parse import urlencode

import pytest

from enable_banking_sync import settings
from enable_banking_sync.client import ApiError
from enable_banking_sync.config import Config
from enable_banking_sync.storage import Storage
from enable_banking_sync.web import make_server

TOKEN = "t0ken"


def _cfg(tmp_path: Path, token=None) -> Config:
    return Config(
        application_id=None, private_key_path=tmp_path / "missing.pem", api_base="http://x",
        redirect_url=None, aspsp_name=None, aspsp_country="DE", psu_type="personal",
        access_days=180, data_dir=tmp_path, fetch_interval=3600, initial_days=90,
        overlap_days=10, web_host="127.0.0.1", web_port=0, web_token=token,
    )


def _tx(ref, day):
    return {
        "transaction_amount": {"amount": "1.00", "currency": "EUR"},
        "credit_debit_indicator": "DBIT", "status": "BOOK", "booking_date": day,
        "creditor": {"name": "Laden"}, "entry_reference": ref,
    }


class FakeClient:
    def __init__(self):
        self.auth_calls = []

    def list_aspsps(self, country, psu_type):
        return [{"name": "Testbank", "country": country, "maximum_consent_validity": 90 * 86400}]

    def start_authorization(self, name, country, redirect_url, psu_type, days, state):
        self.auth_calls.append({"name": name, "redirect_url": redirect_url, "days": days, "state": state})
        return {"url": f"https://bank.example/auth?state={state}"}

    def create_session(self, code):
        if code != "good":
            raise ApiError(400, "code invalid")
        return {
            "session_id": "s2", "aspsp": {"name": "Testbank", "country": "DE"},
            "access": {"valid_until": "2099-01-01T00:00:00+00:00"},
            "accounts": [{"uid": "u9", "account_id": {"iban": "DE99"}, "name": "Neu"}],
        }

    def get_application(self):
        return {"name": "Meine App", "redirect_urls": ["https://elsewhere.example/callback"]}


class FakeSync:
    busy = False
    last_finished = last_error = next_run = None

    def __init__(self):
        self.triggered = 0

    def trigger(self):
        self.triggered += 1


class Site:
    def __init__(self, cfg, server, fake, sync):
        self.cfg, self.fake, self.sync = cfg, fake, sync
        self.port = server.server_address[1]

    def request(self, method, path, form=None, auth=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Authorization": auth} if auth else {}
        body = None
        if form is not None:
            body = urlencode(form)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), data

    def get(self, path, **kwargs):
        status, headers, data = self.request("GET", path, **kwargs)
        return status, headers, data.decode("utf-8-sig", "replace")

    def post(self, path, form, **kwargs):
        status, headers, data = self.request("POST", path, form=form, **kwargs)
        return status, headers, data.decode()


@pytest.fixture
def serve(tmp_path, monkeypatch):
    servers = []
    fake = FakeClient()
    monkeypatch.setattr(settings, "make_client", lambda cfg, st: fake)

    def start(token=None, configured=True):
        cfg = _cfg(tmp_path, token)
        with Storage(cfg.db_path) as s:
            if configured:
                s.save_settings({"application_id": "app", "private_key": "KEY"})
            s.save_session({"session_id": "s1", "accounts": [
                {"uid": "u1", "account_id": {"iban": "DE11"}, "name": "Giro"}]})
            s.store_transactions("u1", [_tx("a", "2026-09-01"), _tx("b", "2026-10-01")])
        sync = FakeSync()
        server = make_server(cfg, sync)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return Site(cfg, server, fake, sync)

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _basic(credentials):
    return "Basic " + base64.b64encode(credentials.encode()).decode()


# --- Übersicht & Export ------------------------------------------------------


def test_index_shows_accounts_and_recent_transactions(serve):
    status, _, body = serve().get("/")
    assert status == 200
    assert "DE11" in body and "Giro" in body
    assert "-1,00 EUR" in body and "Laden" in body
    assert 'action="/fetch"' in body


def test_index_asks_for_setup_when_not_configured(serve):
    _, _, body = serve(configured=False).get("/")
    assert 'href="/settings">Zu den Einstellungen' in body


def test_export_formats_and_filters(serve):
    site = serve()
    status, headers, data = site.request("GET", "/export?format=csv&account=u1")
    assert status == 200
    assert headers["Content-Type"].startswith("text/csv")
    assert "attachment" in headers["Content-Disposition"]
    assert data.startswith(b"\xef\xbb\xbf")  # BOM für Excel
    assert len(data.decode("utf-8-sig").splitlines()) == 3

    _, _, body = site.get("/export?format=json&date_from=2026-09-15")
    assert [t["entry_reference"] for t in json.loads(body)] == ["b"]

    _, headers, body = site.get("/export?format=xml&date_to=2026-09-15")
    assert headers["Content-Type"].startswith("application/xml")
    assert body.count("<transaction>") == 1


@pytest.mark.parametrize("query", ["format=pdf", "date_from=gestern"])
def test_export_rejects_invalid_parameters(serve, query):
    assert serve().get("/export?" + query)[0] == 400


def test_fetch_triggers_sync(serve):
    site = serve()
    status, headers, _ = site.post("/fetch", {})
    assert status == 303 and headers["Location"] == "/?msg=fetch"
    assert site.sync.triggered == 1


# --- Zugriffsschutz ------------------------------------------------------------


def test_without_token_access_is_open(serve):
    site = serve()
    assert site.get("/")[0] == 200
    assert site.get("/export")[0] == 200


@pytest.mark.parametrize("auth", [None, "Bearer falsch", "Bearer ", _basic("x:falsch"), "Token t0ken"])
def test_rejects_missing_or_wrong_token(serve, auth):
    status, headers, _ = serve(token=TOKEN).get("/export", auth=auth)
    assert status == 401
    assert headers["WWW-Authenticate"].startswith("Basic ")


@pytest.mark.parametrize("auth", [f"Bearer {TOKEN}", f"bearer {TOKEN}", _basic(f"egal:{TOKEN}")])
def test_accepts_token_as_bearer_or_basic_password(serve, auth):
    assert serve(token=TOKEN).get("/export", auth=auth)[0] == 200


def test_legal_pages_are_public_even_with_token(serve):
    site = serve(token=TOKEN)
    assert site.get("/privacy")[0] == 200
    assert site.get("/terms.html")[0] == 200


# --- Einstellungen -----------------------------------------------------------


def _pem():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def _form(**overrides):
    form = {"application_id": "app-1", "private_key": "", "redirect_url": "https://bank.example.org/callback",
            "aspsp_country": "de"}
    return form | overrides


def test_save_settings_stores_values_and_keeps_existing_key(serve):
    site = serve(configured=False)
    pem = _pem()
    status, headers, _ = site.post("/settings", _form(private_key=pem))
    assert status == 303 and headers["Location"] == "/settings?msg=saved"
    with Storage(site.cfg.db_path) as s:
        stored = s.settings()
    assert stored["application_id"] == "app-1"
    assert stored["aspsp_country"] == "DE"
    assert stored["private_key"].startswith("-----BEGIN PRIVATE KEY-----")

    site.post("/settings", _form(application_id="app-2"))  # Schlüsselfeld leer
    with Storage(site.cfg.db_path) as s:
        assert s.settings()["private_key"] == stored["private_key"]
    _, _, body = site.get("/settings?msg=saved")
    assert "Einstellungen gespeichert" in body and "Hinterlegt (Datenbank)" in body


@pytest.mark.parametrize("form, message", [
    (_form(private_key="kein schlüssel"), "Der private Schlüssel ist ungültig"),
    (_form(aspsp_country="Deutschland"), "zweistelliger Code"),
    (_form(redirect_url="bank.example.org"), "muss mit http:// oder https://"),
])
def test_save_settings_rejects_invalid_input(serve, form, message):
    site = serve(configured=False)
    status, _, body = site.post("/settings", form)
    assert status == 200 and message in body
    with Storage(site.cfg.db_path) as s:
        assert "application_id" not in s.settings()


def test_settings_page_shows_urls_for_control_panel(serve):
    site = serve()
    _, _, body = site.get("/settings")
    assert f"http://127.0.0.1:{site.port}/callback" in body
    assert f"http://127.0.0.1:{site.port}/privacy" in body


def test_settings_use_forwarded_host_behind_proxy(serve):
    site = serve()
    conn = http.client.HTTPConnection("127.0.0.1", site.port, timeout=5)
    conn.request("GET", "/settings", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "bank.example.org"})
    body = conn.getresponse().read().decode()
    assert "https://bank.example.org/callback" in body


def test_connection_test_warns_about_unregistered_redirect_url(serve):
    status, _, body = serve().post("/settings/test", {})
    assert status == 200
    assert "Verbindung zu Enable Banking erfolgreich" in body
    assert "nicht eingetragen" in body
    assert "Meine App" in body


def test_legal_pages_are_served(serve):
    site = serve()
    for path, title in (("/privacy", "Datenschutzerklärung"), ("/terms", "Nutzungsbedingungen")):
        status, headers, body = site.get(path)
        assert status == 200 and headers["Content-Type"].startswith("text/html")
        assert title in body


# --- Bank verbinden ----------------------------------------------------------


def test_connect_requires_setup(serve):
    status, headers, _ = serve(configured=False).get("/connect")
    assert status == 303 and headers["Location"] == "/settings?msg=setup"


def test_connect_and_callback_create_session(serve):
    site = serve()
    _, _, body = site.get("/connect")
    assert '<option value="Testbank">' in body

    status, headers, _ = site.post("/connect", {"bank": "testbank", "country": "DE",
                                                "psu_type": "personal", "access_days": "180"})
    assert status == 303 and headers["Location"].startswith("https://bank.example/auth")
    call = site.fake.auth_calls[0]
    assert call["name"] == "Testbank"
    assert call["days"] == 90  # auf das Maximum der Bank begrenzt
    assert call["redirect_url"] == f"http://127.0.0.1:{site.port}/callback"

    status, headers, _ = site.get(f"/callback/?code=good&state={call['state']}")
    assert status == 303 and headers["Location"] == "/?msg=connected"
    assert site.sync.triggered == 1
    with Storage(site.cfg.db_path) as s:
        assert s.active_session()["session_id"] == "s2"


def test_connect_rejects_unknown_bank(serve):
    status, _, body = serve().post("/connect", {"bank": "Gibtsnicht", "country": "DE", "psu_type": "personal"})
    assert status == 200 and "wurde nicht gefunden" in body


def test_callback_with_unknown_state_asks_for_confirmation(serve):
    site = serve()
    status, _, body = site.get("/callback?code=good&state=fremd")
    assert status == 200 and "Bitte bestätigen" in body
    with Storage(site.cfg.db_path) as s:
        assert s.active_session()["session_id"] == "s1"

    status, headers, _ = site.post("/callback", {"url": "https://x/callback?code=good&state=fremd"})
    assert status == 303 and headers["Location"] == "/?msg=connected"


def test_callback_shows_bank_error_escaped(serve):
    _, _, body = serve().get("/callback?error=access_denied&error_description=%3Cscript%3E")
    assert "access_denied" in body and "&lt;script&gt;" in body and "<script>" not in body


def test_callback_with_invalid_code_shows_error(serve):
    site = serve()
    status, _, body = site.post("/callback", {"url": "bad"})
    assert status == 200 and "Die Verbindung konnte nicht hergestellt werden" in body
    assert site.sync.triggered == 0
