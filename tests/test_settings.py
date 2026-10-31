import pytest

from enable_banking_sync import settings
from enable_banking_sync.config import Config, ConfigError
from enable_banking_sync.storage import Storage
from enable_banking_sync.sync import NO_SESSION_RETRY, SyncService


def _cfg(tmp_path, **overrides):
    values = dict(
        application_id="env-app", private_key_path=tmp_path / "key.pem", api_base="http://x",
        redirect_url="https://env.example/callback", aspsp_name=None, aspsp_country="DE",
        psu_type="personal", access_days=180, data_dir=tmp_path, fetch_interval=3600,
        initial_days=90, overlap_days=10,
    )
    return Config(**(values | overrides))


def test_database_values_override_environment(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "key.pem").write_text("FILE-KEY")
    with Storage(cfg.db_path) as s:
        st = settings.load(cfg, s)
        assert (st.application_id, st.private_key, st.private_key_source) == (
            "env-app", "FILE-KEY", str(tmp_path / "key.pem"))

        s.save_settings({"application_id": "db-app", "private_key": "DB-KEY", "aspsp_country": "AT"})
        st = settings.load(cfg, s)
        assert (st.application_id, st.private_key, st.private_key_source) == ("db-app", "DB-KEY", "Datenbank")
        assert st.aspsp_country == "AT"
        assert st.redirect_url == "https://env.example/callback"

        s.save_settings({"application_id": ""})  # leer = zurück auf die Umgebungsvariable
        assert settings.load(cfg, s).application_id == "env-app"


def test_incomplete_settings_raise_config_error(tmp_path):
    cfg = _cfg(tmp_path, application_id=None)
    with Storage(cfg.db_path) as s:
        st = settings.load(cfg, s)
    assert not st.complete
    with pytest.raises(ConfigError):
        settings.make_client(cfg, st)


def test_parse_redirect():
    assert settings.parse_redirect("https://x/cb?state=s&code=abc") == ("abc", "s")
    assert settings.parse_redirect("  abc ") == ("abc", None)
    with pytest.raises(ValueError, match="access_denied"):
        settings.parse_redirect("https://x/cb?error=access_denied")
    with pytest.raises(ValueError):
        settings.parse_redirect("https://x/cb?state=s")


def test_sync_service_waits_briefly_without_setup(tmp_path):
    service = SyncService(_cfg(tmp_path, application_id=None))
    service.trigger()
    assert service.busy
    assert service.run_once() == NO_SESSION_RETRY
    assert not service.busy
    assert "Application ID" in service.last_error
