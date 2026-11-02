"""Abrufzeiten je Wochentag und Planung des nächsten Abrufs."""

from datetime import datetime

import pytest

from enable_banking_sync import schedule
from enable_banking_sync.storage import Storage



def _at(day: int, hour: int, minute: int = 0) -> datetime:
    """Lokaler Zeitpunkt im November 2026; der 2. ist ein Montag."""
    return datetime(2026, 11, day, hour, minute).astimezone()


def _plan(**days):
    return {key: days.get(key, []) for key in schedule.DAY_KEYS}


def test_default_is_daily_at_noon(tmp_path):
    with Storage(tmp_path / "db.sqlite3") as s:
        assert schedule.load(s) == {key: ["12:00"] for key in schedule.DAY_KEYS}


def test_saved_plan_survives_reload(tmp_path):
    plan = _plan(mon=["08:00", "17:30"], fri=["12:00"])
    with Storage(tmp_path / "db.sqlite3") as s:
        schedule.save(s, plan)
    with Storage(tmp_path / "db.sqlite3") as s:
        assert schedule.load(s) == plan


def test_empty_plan_stays_empty_and_is_not_replaced_by_default(tmp_path):
    with Storage(tmp_path / "db.sqlite3") as s:
        schedule.save(s, _plan())
        assert schedule.load(s) == _plan()


@pytest.mark.parametrize("raw", ["kaputt", "[1, 2]", '{"mon": ["25:00"]}', '{"mon": 5}'])
def test_broken_plan_falls_back_to_default(tmp_path, raw):
    with Storage(tmp_path / "db.sqlite3") as s:
        s.save_settings({schedule.KEY: raw})
        assert schedule.load(s) == schedule.default()


@pytest.mark.parametrize("text, expected", [
    ("", []),
    ("   ", []),
    ("12:00", ["12:00"]),
    ("8:00, 12:30 18", ["08:00", "12:30", "18:00"]),
    ("18:00;08:00,18:00", ["08:00", "18:00"]),
    ("0:00, 23:59", ["00:00", "23:59"]),
])
def test_parse_times(text, expected):
    assert schedule.parse_times(text) == expected


@pytest.mark.parametrize("text", ["24:00", "12:60", "mittags", "12:5", "-1", "12:00:00"])
def test_parse_times_rejects_invalid_values(text):
    with pytest.raises(ValueError, match="keine gültige Uhrzeit"):
        schedule.parse_times(text)


def test_next_run_on_same_day_then_next_slot():
    plan = _plan(mon=["08:00", "12:00"], tue=["09:00"])
    assert schedule.next_run(plan, _at(2, 7)) == _at(2, 8)
    assert schedule.next_run(plan, _at(2, 8)) == _at(2, 12)  # genau zur Uhrzeit: die nächste
    assert schedule.next_run(plan, _at(2, 13)) == _at(3, 9)


def test_next_run_skips_days_without_times():
    workdays = _plan(mon=["12:00"], tue=["12:00"], wed=["12:00"], thu=["12:00"], fri=["12:00"])
    assert schedule.next_run(workdays, _at(6, 13)) == _at(9, 12)  # Freitagmittag -> Montag


def test_next_run_with_single_weekday_wraps_to_next_week():
    assert schedule.next_run(_plan(mon=["12:00"]), _at(2, 13)) == _at(9, 12)


def test_next_run_without_any_time():
    assert schedule.next_run(_plan(), _at(2, 12)) is None


def test_describe():
    assert schedule.describe(schedule.default()) == "täglich 12:00"
    workdays = _plan(mon=["08:00", "12:00"], tue=["08:00", "12:00"], wed=["08:00", "12:00"],
                     thu=["08:00", "12:00"], fri=["08:00", "12:00"])
    assert schedule.describe(workdays) == "Mo–Fr 08:00, 12:00"
    assert schedule.describe(workdays | {"wed": [], "sat": ["10:00"]}) == (
        "Mo–Di 08:00, 12:00; Do–Fr 08:00, 12:00; Sa 10:00")
    assert schedule.describe(_plan()) == ""


def test_over_limit():
    assert not schedule.over_limit(_plan(mon=["08:00", "10:00", "12:00", "14:00"]))
    assert schedule.over_limit(_plan(mon=["08:00", "10:00", "12:00", "14:00", "16:00"]))
