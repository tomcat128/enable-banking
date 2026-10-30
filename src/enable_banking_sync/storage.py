"""Lokale Ablage der Daten in SQLite."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    aspsp_name   TEXT,
    aspsp_country TEXT,
    valid_until  TEXT,
    created_at   TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    raw          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    uid          TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    iban         TEXT,
    name         TEXT,
    currency     TEXT,
    raw          TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    account_uid      TEXT NOT NULL,
    tx_key           TEXT NOT NULL,
    status           TEXT,
    booking_date     TEXT,
    value_date       TEXT,
    transaction_date TEXT,
    amount           REAL,
    currency         TEXT,
    credit_debit     TEXT,
    counterparty     TEXT,
    counterparty_iban TEXT,
    remittance       TEXT,
    entry_reference  TEXT,
    transaction_id   TEXT,
    raw              TEXT NOT NULL,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    PRIMARY KEY (account_uid, tx_key)
);
CREATE INDEX IF NOT EXISTS idx_tx_booking ON transactions (account_uid, booking_date);

CREATE TABLE IF NOT EXISTS balances (
    account_uid    TEXT NOT NULL,
    fetched_at     TEXT NOT NULL,
    balance_type   TEXT,
    amount         REAL,
    currency       TEXT,
    reference_date TEXT,
    raw            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_uid  TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    date_from    TEXT,
    date_to      TEXT,
    received     INTEGER,
    inserted     INTEGER,
    error        TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def transaction_key(tx: dict) -> str:
    """Stabiler Schlüssel zur Deduplizierung.

    Gebuchte Umsätze werden bevorzugt über die bankseitige ``entry_reference`` bzw.
    ``transaction_id`` identifiziert. Liefert die Bank keine ID, wird ein Hash über die
    fachlich relevanten Felder gebildet.
    """
    for field in ("entry_reference", "transaction_id"):
        if tx.get(field):
            return f"{field[0]}:{tx[field]}"
    relevant = {
        k: tx.get(k)
        for k in (
            "booking_date",
            "value_date",
            "transaction_date",
            "transaction_amount",
            "credit_debit_indicator",
            "creditor",
            "creditor_account",
            "debtor",
            "debtor_account",
            "remittance_information",
            "status",
        )
    }
    return "h:" + hashlib.sha256(_canonical(relevant).encode()).hexdigest()[:32]


def _party(tx: dict) -> tuple[str | None, str | None]:
    """Gegenpartei aus Sicht des Kontoinhabers ermitteln."""
    side = "creditor" if tx.get("credit_debit_indicator") == "DBIT" else "debtor"
    name = (tx.get(side) or {}).get("name")
    iban = (tx.get(f"{side}_account") or {}).get("iban")
    return name, iban


def _amount(tx: dict) -> tuple[float | None, str | None]:
    amt = tx.get("transaction_amount") or {}
    try:
        value = float(amt.get("amount"))
    except (TypeError, ValueError):
        return None, amt.get("currency")
    if tx.get("credit_debit_indicator") == "DBIT":
        value = -abs(value)
    return value, amt.get("currency")


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- Sessions & Konten -------------------------------------------------

    def save_session(self, session: dict) -> None:
        aspsp = session.get("aspsp") or {}
        access = session.get("access") or {}
        with self.conn:
            # Es gibt immer genau eine aktive Session; ältere werden deaktiviert.
            self.conn.execute("UPDATE sessions SET active = 0")
            self.conn.execute(
                """INSERT OR REPLACE INTO sessions
                   (session_id, aspsp_name, aspsp_country, valid_until, created_at, active, raw)
                   VALUES (?, ?, ?, ?, ?, 1, ?)""",
                (
                    session["session_id"],
                    aspsp.get("name"),
                    aspsp.get("country"),
                    access.get("valid_until"),
                    now_iso(),
                    _canonical(session),
                ),
            )
            for acc in session.get("accounts") or []:
                self.save_account(session["session_id"], acc)

    def save_account(self, session_id: str, acc: dict) -> None:
        if isinstance(acc, str):  # ältere API-Versionen liefern nur die UID
            acc = {"uid": acc}
        self.conn.execute(
            """INSERT INTO accounts (uid, session_id, iban, name, currency, raw, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(uid) DO UPDATE SET
                 session_id = excluded.session_id, iban = excluded.iban, name = excluded.name,
                 currency = excluded.currency, raw = excluded.raw, updated_at = excluded.updated_at""",
            (
                acc["uid"],
                session_id,
                (acc.get("account_id") or {}).get("iban"),
                acc.get("name"),
                acc.get("currency"),
                _canonical(acc),
                now_iso(),
            ),
        )

    def active_session(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE active = 1 ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

    def accounts(self, session_id: str | None = None) -> list[sqlite3.Row]:
        if session_id:
            return self.conn.execute(
                "SELECT * FROM accounts WHERE session_id = ? ORDER BY uid", (session_id,)
            ).fetchall()
        return self.conn.execute("SELECT * FROM accounts ORDER BY uid").fetchall()

    # --- Umsätze -----------------------------------------------------------

    def latest_booking_date(self, account_uid: str) -> str | None:
        row = self.conn.execute(
            "SELECT MAX(booking_date) FROM transactions WHERE account_uid = ? AND status = 'BOOK'",
            (account_uid,),
        ).fetchone()
        return row[0] if row else None

    def store_transactions(self, account_uid: str, transactions: Iterable[dict]) -> tuple[int, int]:
        """Speichert Umsätze idempotent. Gibt (empfangen, neu) zurück.

        Vorgemerkte Umsätze (PDNG) haben bei vielen Banken keine stabile ID und verschwinden,
        sobald sie gebucht werden. Daher werden sie bei jedem Abruf komplett ersetzt.
        """
        ts = now_iso()
        received = inserted = 0
        with self.conn:
            self.conn.execute(
                "DELETE FROM transactions WHERE account_uid = ? AND status = 'PDNG'", (account_uid,)
            )
            for tx in transactions:
                received += 1
                key = transaction_key(tx)
                amount, currency = _amount(tx)
                party, party_iban = _party(tx)
                remittance = tx.get("remittance_information")
                if isinstance(remittance, list):
                    remittance = " ".join(r for r in remittance if r)
                is_new = not self.conn.execute(
                    "SELECT 1 FROM transactions WHERE account_uid = ? AND tx_key = ?",
                    (account_uid, key),
                ).fetchone()
                self.conn.execute(
                    """INSERT INTO transactions (
                           account_uid, tx_key, status, booking_date, value_date, transaction_date,
                           amount, currency, credit_debit, counterparty, counterparty_iban,
                           remittance, entry_reference, transaction_id, raw,
                           first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(account_uid, tx_key) DO UPDATE SET
                         status = excluded.status, booking_date = excluded.booking_date,
                         value_date = excluded.value_date, raw = excluded.raw,
                         last_seen_at = excluded.last_seen_at""",
                    (
                        account_uid,
                        key,
                        tx.get("status"),
                        tx.get("booking_date"),
                        tx.get("value_date"),
                        tx.get("transaction_date"),
                        amount,
                        currency,
                        tx.get("credit_debit_indicator"),
                        party,
                        party_iban,
                        remittance,
                        tx.get("entry_reference"),
                        tx.get("transaction_id"),
                        _canonical(tx),
                        ts,
                        ts,
                    ),
                )
                if is_new and tx.get("status") != "PDNG":
                    inserted += 1
        return received, inserted

    def transactions(self, account_uid: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM transactions"
        params: tuple = ()
        if account_uid:
            sql += " WHERE account_uid = ?"
            params = (account_uid,)
        sql += " ORDER BY COALESCE(booking_date, transaction_date, value_date), tx_key"
        return self.conn.execute(sql, params).fetchall()

    def count_transactions(self, account_uid: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE account_uid = ?", (account_uid,)
        ).fetchone()[0]

    # --- Salden & Protokoll ------------------------------------------------

    def store_balances(self, account_uid: str, balances: Iterable[dict]) -> None:
        ts = now_iso()
        with self.conn:
            for b in balances:
                amt = b.get("balance_amount") or {}
                try:
                    value = float(amt.get("amount"))
                except (TypeError, ValueError):
                    value = None
                self.conn.execute(
                    """INSERT INTO balances
                       (account_uid, fetched_at, balance_type, amount, currency, reference_date, raw)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        account_uid,
                        ts,
                        b.get("balance_type"),
                        value,
                        amt.get("currency"),
                        b.get("reference_date"),
                        _canonical(b),
                    ),
                )

    def latest_balances(self, account_uid: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM balances WHERE account_uid = ? AND fetched_at =
                 (SELECT MAX(fetched_at) FROM balances WHERE account_uid = ?)""",
            (account_uid, account_uid),
        ).fetchall()

    def log_run(
        self,
        account_uid: str,
        started_at: str,
        date_from: str | None,
        date_to: str | None,
        received: int | None,
        inserted: int | None,
        error: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO fetch_runs
                   (account_uid, started_at, date_from, date_to, received, inserted, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (account_uid, started_at, date_from, date_to, received, inserted, error),
            )

    def last_run(self, account_uid: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM fetch_runs WHERE account_uid = ? ORDER BY id DESC LIMIT 1",
            (account_uid,),
        ).fetchone()
