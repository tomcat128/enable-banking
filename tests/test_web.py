import base64
import http.client
import json
import threading
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest

from enable_banking_sync import settings
from enable_banking_sync.client import ApiError
from enable_banking_sync.config import Config
from enable_banking_sync.storage import Storage
from enable_banking_sync.web import make_server

LOGIN = {"web_user": "tomek", "web_password": "geheim"}
EXPORT_AUTH = {"export_user": "skript", "export_password": "s3cret"}


def _cfg(tmp_path: Path, **overrides) -> Config:
    values = dict(
        application_id=None, private_key_path=tmp_path / "missing.pem", api_base="http://x",
        redirect_url=None, aspsp_name=None, aspsp_country="DE", psu_type="personal",
        access_days=180, data_dir=tmp_path, initial_days=90,
        overlap_days=10, web_host="127.0.0.1", web_port=0,
    )
    return Config(**(values | overrides))


def _tx(ref, day, creditor="Laden", amount="1.00", indicator="DBIT"):
    return {
        "transaction_amount": {"amount": amount, "currency": "EUR"},
        "credit_debit_indicator": indicator, "status": "BOOK", "booking_date": day,
        "creditor": {"name": creditor}, "entry_reference": ref,
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
    last_error = next_run = None

    def __init__(self):
        self.triggered = 0
        self.rescheduled = 0

    def trigger(self):
        self.triggered += 1

    def reschedule(self):
        self.rescheduled += 1


class Site:
    def __init__(self, cfg, server, fake, sync):
        self.cfg, self.fake, self.sync = cfg, fake, sync
        self.port = server.server_address[1]

    def request(self, method, path, form=None, auth=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = dict(headers or {})
        if auth:
            headers["Authorization"] = auth
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

    def start(configured=True, **overrides):
        cfg = _cfg(tmp_path, **overrides)
        with Storage(cfg.db_path) as s:
            if configured:
                s.save_settings({"application_id": "app", "private_key": "KEY"})
            s.save_session({"session_id": "s1", "accounts": [
                {"uid": "u1", "account_id": {"iban": "DE11"}, "name": "Giro"}]})
            s.store_transactions("u1", [_tx("a", "2026-09-01"),
                                        _tx("b", "2026-10-01", "Bäckerei Müller", "2.50")])
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


def test_index_shows_last_fetch_from_database(serve):
    site = serve()
    assert "Letzter Abruf:" not in site.get("/")[2]
    with Storage(site.cfg.db_path) as s:
        s.log_run("u1", "2026-10-30T08:15:00+00:00", "2026-10-01", None, 2, 1)
    assert "Letzter Abruf: 30.10.2026" in site.get("/")[2]


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


# --- Umsätze ----------------------------------------------------------------


def test_navigation_links_transactions_and_export_pages(serve):
    body = serve().get("/")[2]
    assert 'href="/transactions">Umsätze' in body and 'href="/export/links">Export' in body


def test_transactions_page_lists_and_filters(serve):
    site = serve()
    status, _, body = site.get("/transactions")
    assert status == 200
    assert "2 Umsätze" in body and "Bäckerei Müller" in body and "Laden" in body
    assert "Summe -3,50 EUR" in body
    assert 'href="/download?format=csv"' in body

    _, _, body = site.get("/transactions?q=b%C3%A4ckerei")
    assert "1 Umsatz<" in body

    _, _, body = site.get("/transactions?q=M%C3%BCller")
    assert "1 Umsatz<" in body and "Bäckerei Müller" in body and ">Laden<" not in body
    assert 'href="/download?q=M%C3%BCller&amp;format=csv"' in body

    _, _, body = site.get("/transactions?date_to=2026-09-15")
    assert "1 Umsatz<" in body and ">Laden<" in body


def test_transactions_page_reports_invalid_date(serve):
    status, _, body = serve().get("/transactions?date_from=gestern")
    assert status == 200 and "Ungültiges Datum" in body


def test_download_uses_same_filters(serve):
    _, _, body = serve().get("/download?format=json&q=M%C3%BCller")
    assert [t["entry_reference"] for t in json.loads(body)] == ["b"]


def test_search_treats_wildcards_literally(serve):
    assert "0 Umsätze" in serve().get("/transactions?q=%25")[2]


def _add_recent(site):
    today = date.today()
    with Storage(site.cfg.db_path) as s:
        s.store_transactions("u1", [_tx("heute", today.isoformat(), "Kiosk"),
                                    _tx("vor4", (today - timedelta(days=4)).isoformat(), "Tankstelle"),
                                    _tx("vor5", (today - timedelta(days=5)).isoformat(), "Apotheke")])


def test_days_limits_to_last_n_days(serve):
    site = serve()
    _add_recent(site)
    _, _, body = site.get("/export?format=json&days=5")
    assert sorted(t["entry_reference"] for t in json.loads(body)) == ["heute", "vor4"]
    _, _, body = site.get("/export?format=json&days=1")
    assert [t["entry_reference"] for t in json.loads(body)] == ["heute"]
    _, _, body = site.get("/transactions?days=5")
    assert "2 Umsätze" in body and "Kiosk" in body and "Apotheke" not in body
    assert 'href="/download?days=5&amp;format=csv"' in body


@pytest.mark.parametrize("query, message", [
    ("days=0", "days muss eine positive ganze Zahl"),
    ("days=fuenf", "days muss eine positive ganze Zahl"),
    ("days=5&date_from=2026-01-01", "nicht kombinieren"),
])
def test_days_rejects_invalid_values(serve, query, message):
    site = serve()
    status, _, body = site.get("/export?" + query)
    assert status == 400 and message in body
    assert message in site.get("/transactions?" + query)[2]


@pytest.mark.parametrize("account", ["DE11", "de 11", "u1"])
def test_account_filter_accepts_iban_or_uid(serve, account):
    _, _, body = serve().get("/export?format=json&" + urlencode({"account": account}))
    assert len(json.loads(body)) == 2


# --- Export-Seite --------------------------------------------------------------


def test_export_page_shows_links_for_all_formats(serve):
    site = serve()
    status, _, body = site.get("/export/links?days=5&account=DE11")
    base = f"http://127.0.0.1:{site.port}/export"
    assert status == 200
    for fmt in ("csv", "json", "xml"):
        assert f'value="{base}?account=DE11&amp;days=5&amp;format={fmt}"' in body
    assert "0 Umsätze, ab " in body
    assert 'name="days" min="1" value="5"' in body and '<option value="DE11" selected>' in body
    assert "Ohne Anmeldung abrufbar" in body


def test_export_page_shows_examples(serve):
    site = serve()
    _, _, body = site.get("/export/links")
    base = f"http://127.0.0.1:{site.port}/export"
    assert f"{base}?days=5&amp;format=csv" in body
    assert f"{base}?account=DE11&amp;days=5&amp;format=csv" in body
    assert "Vormonat" in body and "date_from=" in body and "format=xml" in body
    assert f"curl -o umsaetze.csv &#x27;{base}?days=5&amp;format=csv&#x27;" in body
    assert "<code>days</code>" in body  # Parametertabelle


def test_export_page_requires_login(serve):
    assert serve(**LOGIN).get("/export/links")[0] == 303


# --- Login -------------------------------------------------------------------


def _login(site, user="tomek", password="geheim", next_="/"):
    return site.post("/login", {"user": user, "password": password, "next": next_})


def _cookie(headers):
    return {"Cookie": headers["Set-Cookie"].split(";")[0]}


def test_without_login_configured_everything_is_open(serve):
    site = serve()
    assert site.get("/")[0] == 200
    assert site.get("/transactions")[0] == 200
    assert site.get("/login")[1]["Location"] == "/"


@pytest.mark.parametrize("overrides", [{"web_user": "tomek"}, {"web_password": "geheim"}])
def test_login_needs_user_and_password(serve, overrides):
    assert serve(**overrides).get("/")[0] == 200


def test_login_redirects_to_login_page(serve):
    site = serve(**LOGIN)
    status, headers, _ = site.get("/transactions?q=x")
    assert status == 303 and headers["Location"] == "/login?next=%2Ftransactions%3Fq%3Dx"
    status, headers, _ = site.post("/fetch", {})
    assert status == 303 and headers["Location"] == "/login?next=%2F"
    assert site.sync.triggered == 0
    _, _, body = site.get("/login?next=%2Ftransactions%3Fq%3Dx")
    assert 'name="next" value="/transactions?q=x"' in body and 'href="/settings"' not in body


def test_legal_pages_stay_public_with_login(serve):
    site = serve(**LOGIN)
    assert site.get("/privacy")[0] == 200
    assert site.get("/terms.html")[0] == 200


@pytest.mark.parametrize("user, password", [("tomek", "falsch"), ("falsch", "geheim"), ("", "")])
def test_login_rejects_wrong_credentials(serve, user, password):
    status, headers, body = _login(serve(**LOGIN), user, password)
    assert status == 200 and "Benutzer oder Passwort ist falsch" in body
    assert "Set-Cookie" not in headers


def test_login_sets_cookie_and_returns_to_target(serve):
    site = serve(**LOGIN)
    status, headers, _ = _login(site, next_="/transactions?q=x")
    assert status == 303 and headers["Location"] == "/transactions?q=x"
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Lax" in headers["Set-Cookie"]
    cookie = _cookie(headers)
    status, _, body = site.get("/", headers=cookie)
    assert status == 200 and 'action="/logout"' in body

    status, headers, _ = site.post("/logout", {})
    assert status == 303 and headers["Location"] == "/login?msg=logout"
    assert "Max-Age=0" in headers["Set-Cookie"]


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/", "/\\evil.example", "/x\r\nX: y"])
def test_login_ignores_foreign_targets(serve, target):
    status, headers, _ = _login(serve(**LOGIN), next_=target)
    assert status == 303 and headers["Location"] == "/"


def test_login_rejects_tampered_cookie(serve):
    site = serve(**LOGIN)
    _, headers, _ = _login(site)
    cookie = _cookie(headers)["Cookie"]
    expires, _, signature = cookie.partition("=")[2].partition(".")
    changed = signature[:-1] + ("1" if signature.endswith("0") else "0")
    for forged in (f"{int(expires) + 999}.{signature}", f"{expires}.{changed}", "kaputt", "1.abc"):
        assert site.get("/", headers={"Cookie": f"eb_session={forged}"})[0] == 303


def test_password_change_invalidates_sessions(serve):
    site = serve(**LOGIN)
    cookie = _cookie(_login(site)[1])
    assert site.get("/", headers=cookie)[0] == 200
    other = serve(web_user="tomek", web_password="neu")  # gleiche Datenbank, neues Passwort
    assert other.get("/", headers=cookie)[0] == 303


# --- Export-Pfad -------------------------------------------------------------


def test_export_is_free_without_export_credentials_even_with_login(serve):
    site = serve(**LOGIN)
    assert site.get("/export")[0] == 200
    assert site.get("/download")[0] == 303  # Download der Oberfläche braucht den Login


@pytest.mark.parametrize("overrides", [{"export_user": "skript"}, {"export_password": "s3cret"}])
def test_export_needs_user_and_password_for_protection(serve, overrides):
    assert serve(**overrides).get("/export")[0] == 200


@pytest.mark.parametrize("auth", [None, _basic("skript:falsch"), _basic("falsch:s3cret"), "Bearer s3cret",
                                  "Basic ###"])
def test_export_rejects_missing_or_wrong_credentials(serve, auth):
    status, headers, _ = serve(**EXPORT_AUTH).get("/export", auth=auth)
    assert status == 401
    assert headers["WWW-Authenticate"].startswith("Basic ")


def test_export_accepts_basic_auth(serve):
    site = serve(**EXPORT_AUTH)
    assert site.get("/export?format=json", auth=_basic("skript:s3cret"))[0] == 200
    assert site.get("/download")[0] == 200  # ohne Login frei, unabhängig vom Export-Schutz


def test_export_can_be_disabled(serve):
    site = serve(export_enabled=False)
    assert site.get("/export")[0] == 404
    assert site.get("/download")[0] == 200
    body = site.get("/export/links")[2]
    assert "abgeschaltet" in body
    assert f'value="http://127.0.0.1:{site.port}/download?format=csv"' in body


def test_export_page_describes_basic_auth(serve):
    body = serve(**EXPORT_AUTH).get("/export/links")[2]
    assert "Geschützt per Basic Auth" in body
    assert "curl -u BENUTZER:PASSWORT" in body and "s3cret" not in body


# --- Abrufsperre & Mail ------------------------------------------------------


def _set_block(site):
    from enable_banking_sync.sync import set_block
    with Storage(site.cfg.db_path) as s:
        set_block(s, "Die Bank verlangt eine erneute Anmeldung (EXPIRED_SESSION).")


def test_index_shows_block_and_resume_clears_it(serve):
    site = serve()
    _set_block(site)
    _, _, body = site.get("/")
    assert "Abruf gestoppt" in body and "EXPIRED_SESSION" in body and 'action="/fetch/resume"' in body
    assert 'action="/fetch"' not in body

    status, headers, _ = site.post("/fetch/resume", {})
    assert status == 303 and headers["Location"] == "/?msg=resumed"
    assert site.sync.triggered == 1
    assert "Abruf gestoppt" not in site.get("/")[2]


def test_new_connection_clears_block(serve):
    site = serve()
    _set_block(site)
    site.post("/callback", {"url": "https://x/callback?code=good"})
    assert "Abruf gestoppt" not in site.get("/")[2]


def test_mail_card_without_configuration(serve):
    _, _, body = serve().get("/settings")
    assert "Mailversand ist nicht eingerichtet" in body and 'action="/settings/mail"' not in body


MAIL = dict(smtp_host="smtp.example.org", smtp_user="bot@example.org", smtp_password="pw",
            alert_email=("notfall@example.org",))


def test_send_test_mail(serve, monkeypatch):
    from enable_banking_sync import mail
    sent = []
    monkeypatch.setattr(mail, "send", lambda cfg, subject, body: sent.append(subject) or "notfall@example.org")
    site = serve(**MAIL)
    _, _, body = site.get("/settings")
    assert "notfall@example.org" in body and "smtp.example.org:587 (starttls)" in body and "pw<" not in body
    status, _, body = site.post("/settings/mail", {})
    assert status == 200 and "Test-Mail an notfall@example.org gesendet" in body
    assert sent == ["Enable Banking: Test-Nachricht"]


def test_test_mail_shows_error(serve, monkeypatch):
    from enable_banking_sync import mail

    def fail(*args):
        raise OSError("Connection refused")

    monkeypatch.setattr(mail, "send", fail)
    _, _, body = serve(**MAIL).post("/settings/mail", {})
    assert "Die Test-Mail konnte nicht gesendet werden: Connection refused" in body


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


# --- Abrufzeiten und Erinnerungen ------------------------------------------------


def _times(**days):
    return {f"times_{key}": value for key, value in days.items()}


def test_settings_show_default_schedule(serve):
    site = serve()
    _, _, body = site.get("/settings")
    assert 'action="/settings/schedule"' in body
    for name in ("Montag", "Samstag", "Sonntag"):
        assert name in body
    assert 'name="times_sun" placeholder="kein Abruf" value="12:00"' in body
    assert "Automatisch: täglich 12:00." in site.get("/")[2]


def test_save_schedule_with_free_weekend(serve):
    site = serve()
    workday = "8:00, 12:30"
    status, headers, _ = site.post("/settings/schedule", _times(
        mon=workday, tue=workday, wed=workday, thu=workday, fri=workday, sat="", sun=""))
    assert status == 303 and headers["Location"] == "/settings?msg=schedule"
    assert site.sync.rescheduled == 1

    _, _, body = site.get("/settings?msg=schedule")
    assert "Abrufzeiten gespeichert." in body
    assert 'name="times_mon" placeholder="kein Abruf" value="08:00, 12:30"' in body
    assert 'name="times_sat" placeholder="kein Abruf" value=""' in body
    assert "Automatisch: Mo–Fr 08:00, 12:30." in site.get("/")[2]


def test_save_empty_schedule_turns_automatic_fetch_off(serve):
    site = serve()
    site.post("/settings/schedule", {})
    assert "Es ist kein automatischer Abruf eingeplant." in site.get("/")[2]
    assert 'name="times_mon" placeholder="kein Abruf" value=""' in site.get("/settings")[2]


def test_invalid_schedule_is_rejected_and_input_kept(serve):
    site = serve()
    _, _, body = site.post("/settings/schedule", _times(mon="08:00", tue="25:00", wed="<b>"))
    assert "Dienstag:" in body and "25:00" in body and "keine gültige Uhrzeit" in body
    assert 'name="times_mon" placeholder="kein Abruf" value="08:00"' in body
    assert "<b>" not in body.split("<form", 1)[1]
    assert site.sync.rescheduled == 0
    assert "Automatisch: täglich 12:00." in site.get("/")[2]  # nichts gespeichert


def test_schedule_warns_above_bank_limit(serve):
    site = serve()
    assert "mehr als 4 Abrufe" not in site.get("/settings")[2]
    site.post("/settings/schedule", _times(mon="8, 10, 12, 14, 16"))
    assert "mehr als 4 Abrufe" in site.get("/settings")[2]


def test_schedule_requires_login(serve):
    site = serve(**LOGIN)
    status, headers, _ = site.post("/settings/schedule", _times(mon="03:00"))
    assert status == 303 and headers["Location"].startswith("/login")
    with Storage(site.cfg.db_path) as s:
        assert "fetch_schedule" not in s.settings()


MAIL_SETUP = dict(smtp_host="smtp.example.org", smtp_user="bot@example.org", alert_email=("a@example.org",))


def test_block_card_announces_next_reminder(serve):
    from enable_banking_sync.sync import BLOCK_KEY, get_block
    site = serve(**MAIL_SETUP)
    _set_block(site)
    with Storage(site.cfg.db_path) as s:
        block = get_block(s) | {"mail": "Benachrichtigung an a@example.org gesendet.", "reminders": 1}
        s.save_settings({BLOCK_KEY: json.dumps(block)})
    assert "Nächste Erinnerung:" in site.get("/")[2]
    with Storage(site.cfg.db_path) as s:
        s.save_settings({BLOCK_KEY: json.dumps(block | {"reminders": 3})})
    assert "Es folgen keine weiteren Erinnerungen." in site.get("/")[2]


def test_mail_card_explains_reminders(serve):
    assert "Erinnerungen nach 7 und 14 Tagen" in serve(**MAIL_SETUP).get("/settings")[2]
