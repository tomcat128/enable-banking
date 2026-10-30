import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from enable_banking_sync.cli import _extract_code
from enable_banking_sync.client import EnableBankingClient


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


def test_jwt_matches_enable_banking_spec():
    pem, public_key = _keypair()
    token = EnableBankingClient("app-id", pem)._jwt()
    header = jwt.get_unverified_header(token)
    assert header == {"alg": "RS256", "typ": "JWT", "kid": "app-id"}
    claims = jwt.decode(token, public_key, algorithms=["RS256"], audience="api.enablebanking.com")
    assert claims["iss"] == "enablebanking.com"
    assert 0 < claims["exp"] - claims["iat"] <= 86400


def test_paginates_with_continuation_key(monkeypatch):
    pem, _ = _keypair()
    client = EnableBankingClient("app-id", pem)
    pages = [
        {"transactions": [{"n": 1}, {"n": 2}], "continuation_key": "k1"},
        {"transactions": [{"n": 3}], "continuation_key": None},
    ]
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append(dict(kwargs["params"]))
        return pages[len(calls) - 1]

    monkeypatch.setattr(client, "_request", fake_request)
    assert [t["n"] for t in client.iter_transactions("acc")] == [1, 2, 3]
    assert "continuation_key" not in calls[0]
    assert calls[1]["continuation_key"] == "k1"


def test_extract_code_from_redirect_url():
    assert _extract_code("https://localhost/cb?state=s&code=abc", "s") == "abc"
    assert _extract_code("  abc  ", None) == "abc"
