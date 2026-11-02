import pytest

from enable_banking_sync import settings
from enable_banking_sync.config import Config, ConfigError
from enable_banking_sync.storage import Storage
from enable_banking_sync.sync import SyncService


def _cfg(tmp_path, **overrides):
    values = dict(
        application_id="env-app", private_key_path=tmp_path / "key.pem", api_base="http://x",
        redirect_url="https://env.example/callback", aspsp_name=None, aspsp_country="DE",
        psu_type="personal", access_days=180, data_dir=tmp_path,
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


def _log_run(cfg, hours_ago):
    from datetime import datetime, timedelta, timezone
    started = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    with Storage(cfg.db_path) as s:
        s.log_run("u1", started.isoformat(timespec="seconds"), None, None, 0, 0)


def test_first_fetch_after_start_follows_schedule(tmp_path):
    from datetime import datetime, timedelta, timezone
    cfg = _cfg(tmp_path)
    service = SyncService(cfg)
    assert service.first_run() <= datetime.now(timezone.utc)  # noch nie abgerufen: sofort

    # Auch nach langer Pause (z. B. Server war aus) kein Nachholen außerhalb des Zeitplans.
    _log_run(cfg, hours_ago=24 * 30)
    planned = SyncService(cfg).first_run()
    assert planned > datetime.now(timezone.utc)
    assert planned - datetime.now(timezone.utc) <= timedelta(days=1)
    assert planned.astimezone().strftime("%H:%M") == "12:00"


def test_sync_service_waits_briefly_without_setup(tmp_path):
    service = SyncService(_cfg(tmp_path, application_id=None))
    service.trigger()
    assert service.busy
    from datetime import datetime, timedelta, timezone
    assert service.run_once() - datetime.now(timezone.utc) <= timedelta(minutes=1)
    assert not service.busy
    assert "Application ID" in service.last_error
