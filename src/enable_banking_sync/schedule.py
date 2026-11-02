"""Abrufzeiten je Wochentag.

Die Zeiten gelten in der Zeitzone des Prozesses (im Container: Variable TZ).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta

from .storage import Storage

log = logging.getLogger(__name__)

KEY = "fetch_schedule"
DAYS = (("mon", "Montag"), ("tue", "Dienstag"), ("wed", "Mittwoch"), ("thu", "Donnerstag"),
        ("fri", "Freitag"), ("sat", "Samstag"), ("sun", "Sonntag"))
DAY_KEYS = tuple(key for key, _ in DAYS)
DEFAULT_TIME = "12:00"
# Banken erlauben ohne erneute Anmeldung (PSD2) häufig nur so viele Abrufe pro Tag und Konto.
BANK_DAILY_LIMIT = 4


def default() -> dict[str, list[str]]:
    return {key: [DEFAULT_TIME] for key in DAY_KEYS}


def parse_times(text: str) -> list[str]:
    """Liest Uhrzeiten wie „8:00, 12:30 18“ und gibt sie sortiert als HH:MM zurück."""
    times = set()
    for item in re.split(r"[,;\s]+", text.strip()):
        if not item:
            continue
        match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", item)
        hour, minute = (int(match[1]), int(match[2] or 0)) if match else (24, 0)
        if hour > 23 or minute > 59:
            raise ValueError(f"„{item}“ ist keine gültige Uhrzeit, bitte z. B. 08:00 angeben.")
        times.add(f"{hour:02d}:{minute:02d}")
    return sorted(times)


def load(storage: Storage) -> dict[str, list[str]]:
    """Gespeicherter Zeitplan; ohne Konfiguration täglich um 12:00 Uhr."""
    raw = storage.settings().get(KEY)
    if not raw:
        return default()
    try:
        data = json.loads(raw)
        return {key: parse_times(" ".join(data.get(key, []))) for key in DAY_KEYS}
    except (ValueError, TypeError, AttributeError) as exc:
        log.error("Der gespeicherte Zeitplan ist ungültig (%s), es gilt der Standard.", exc)
        return default()


def save(storage: Storage, plan: dict[str, list[str]]) -> None:
    storage.save_settings({KEY: json.dumps(plan)})


def next_run(plan: dict[str, list[str]], now: datetime) -> datetime | None:
    """Erster geplanter Zeitpunkt nach now; None, wenn an keinem Tag eine Uhrzeit eingetragen ist."""
    today = now.astimezone().date()
    for offset in range(8):  # der achte Tag ist derselbe Wochentag eine Woche später
        day: date = today + timedelta(days=offset)
        for value in plan[DAY_KEYS[day.weekday()]]:
            candidate = datetime.combine(day, time.fromisoformat(value)).astimezone()
            if candidate > now:
                return candidate
    return None


def describe(plan: dict[str, list[str]]) -> str:
    """Kurzfassung für die Anzeige, z. B. „Mo–Fr 08:00, 12:00; Sa 10:00“."""
    if all(plan[key] == plan[DAY_KEYS[0]] for key in DAY_KEYS) and plan[DAY_KEYS[0]]:
        return "täglich " + ", ".join(plan[DAY_KEYS[0]])
    groups: list[tuple[int, int]] = []
    for index, key in enumerate(DAY_KEYS):
        if groups and plan[DAY_KEYS[groups[-1][0]]] == plan[key]:
            groups[-1] = (groups[-1][0], index)
        else:
            groups.append((index, index))
    parts = []
    for first, last in groups:
        times = plan[DAY_KEYS[first]]
        if times:
            names = DAYS[first][1][:2] + (f"–{DAYS[last][1][:2]}" if last > first else "")
            parts.append(f"{names} {', '.join(times)}")
    return "; ".join(parts)


def over_limit(plan: dict[str, list[str]]) -> bool:
    return any(len(times) > BANK_DAILY_LIMIT for times in plan.values())
