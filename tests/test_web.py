import base64
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from enable_banking_sync.config import Config
from enable_banking_sync.storage import Storage
from enable_banking_sync.web import make_server


TOKEN = "t0ken"


def _cfg(tmp_path: Path, token=TOKEN) -> Config:
    return Config(
        application_id="app", private_key_path=tmp_path / "key.pem", api_base="http://x",
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


@pytest.fixture
def serve(tmp_path):
    servers = []

    def start(token=TOKEN):
        cfg = _cfg(tmp_path, token)
        with Storage(cfg.db_path) as s:
            s.save_session({"session_id": "s1", "accounts": [
                {"uid": "u1", "account_id": {"iban": "DE11"}, "name": "Giro"}]})
            s.store_transactions("u1", [_tx("a", "2026-09-01"), _tx("b", "2026-10-01")])
        server = make_server(cfg)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _get(url, auth=f"Bearer {TOKEN}"):
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", auth)
    with urllib.request.urlopen(req) as resp:
        return resp.status, resp.headers, resp.read()


def test_index_lists_accounts(serve):
    status, _, body = _get(serve() + "/")
    assert status == 200
    assert 'value="u1"' in body.decode()
    assert "Giro (2 Umsätze)" in body.decode()


def test_export_formats_and_filters(serve):
    base = serve()
    status, headers, body = _get(base + "/export?format=csv&account=u1")
    assert headers["Content-Type"].startswith("text/csv")
    assert "attachment" in headers["Content-Disposition"]
    assert body.startswith(b"\xef\xbb\xbf")  # BOM für Excel
    assert len(body.decode("utf-8-sig").splitlines()) == 3

    _, _, body = _get(base + "/export?format=json&date_from=2026-09-15")
    assert [t["entry_reference"] for t in json.loads(body)] == ["b"]

    _, headers, body = _get(base + "/export?format=xml&date_to=2026-09-15")
    assert headers["Content-Type"].startswith("application/xml")
    assert body.count(b"<transaction>") == 1


@pytest.mark.parametrize("query", ["format=pdf", "date_from=gestern"])
def test_export_rejects_invalid_parameters(serve, query):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(serve() + "/export?" + query)
    assert exc.value.code == 400


def _basic(credentials):
    return "Basic " + base64.b64encode(credentials.encode()).decode()


@pytest.mark.parametrize("auth", [None, "Bearer falsch", "Bearer ", _basic("x:falsch"), "Token t0ken"])
def test_rejects_missing_or_wrong_token(serve, auth):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(serve() + "/export", auth=auth)
    assert exc.value.code == 401
    assert exc.value.headers["WWW-Authenticate"].startswith("Basic ")


@pytest.mark.parametrize("auth", [f"Bearer {TOKEN}", f"bearer {TOKEN}", _basic(f"egal:{TOKEN}")])
def test_accepts_token_as_bearer_or_basic_password(serve, auth):
    assert _get(serve() + "/export", auth=auth)[0] == 200


def test_without_configured_token_everything_is_blocked(serve):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(serve(token=None) + "/", auth=None)
    assert exc.value.code == 503
