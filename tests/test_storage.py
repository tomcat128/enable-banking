from enable_banking_sync.storage import Storage, transaction_key


def _tx(ref=None, amount="12.34", cdi="DBIT", status="BOOK", day="2026-10-01", **extra):
    tx = {
        "transaction_amount": {"amount": amount, "currency": "EUR"},
        "credit_debit_indicator": cdi,
        "status": status,
        "booking_date": day,
        "creditor": {"name": "Supermarkt"},
        "creditor_account": {"iban": "DE00123"},
        "debtor": {"name": "Ich"},
        "remittance_information": ["Einkauf", "Karte 1"],
    }
    if ref:
        tx["entry_reference"] = ref
    tx.update(extra)
    return tx


def test_transaction_key_prefers_bank_reference():
    assert transaction_key(_tx("abc")) == "e:abc"
    assert transaction_key(_tx(transaction_id="t1")) == "t:t1"
    assert transaction_key(_tx()) == transaction_key(_tx())
    assert transaction_key(_tx()) != transaction_key(_tx(amount="1.00"))


def test_store_is_idempotent(tmp_path):
    with Storage(tmp_path / "db.sqlite3") as s:
        assert s.store_transactions("acc", [_tx("a"), _tx("b")]) == (2, 2)
        assert s.store_transactions("acc", [_tx("a"), _tx("b"), _tx("c")]) == (3, 1)
        assert s.count_transactions("acc") == 3


def test_fields_are_mapped(tmp_path):
    with Storage(tmp_path / "db.sqlite3") as s:
        s.store_transactions("acc", [_tx("a"), _tx("b", cdi="CRDT", amount="100")])
        rows = {r["entry_reference"]: r for r in s.transactions("acc")}
        assert rows["a"]["amount"] == -12.34
        assert rows["a"]["counterparty"] == "Supermarkt"
        assert rows["a"]["remittance"] == "Einkauf Karte 1"
        assert rows["b"]["amount"] == 100.0
        assert rows["b"]["counterparty"] == "Ich"
        assert s.latest_booking_date("acc") == "2026-10-01"


def test_pending_transactions_are_replaced(tmp_path):
    with Storage(tmp_path / "db.sqlite3") as s:
        s.store_transactions("acc", [_tx(status="PDNG", booking_date=None)])
        assert s.count_transactions("acc") == 1
        # Vormerkung wurde gebucht und kommt jetzt mit anderer Gestalt zurück
        s.store_transactions("acc", [_tx("booked")])
        rows = s.transactions("acc")
        assert [r["status"] for r in rows] == ["BOOK"]


def test_session_and_accounts(tmp_path):
    session = {
        "session_id": "s1",
        "aspsp": {"name": "Testbank", "country": "DE"},
        "access": {"valid_until": "2027-04-01T00:00:00+00:00"},
        "accounts": [{"uid": "u1", "account_id": {"iban": "DE11"}, "currency": "EUR"}],
    }
    with Storage(tmp_path / "db.sqlite3") as s:
        s.save_session(session)
        s.save_session(session | {"session_id": "s2"})
        assert s.active_session()["session_id"] == "s2"
        accounts = s.accounts("s2")
        assert [a["iban"] for a in accounts] == ["DE11"]
