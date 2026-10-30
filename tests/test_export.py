import csv
import xml.etree.ElementTree as ET
import io

from enable_banking_sync.export import EXPORT_COLUMNS, write_csv, write_xml
from enable_banking_sync.storage import Storage


def test_csv_quotes_text_with_commas(tmp_path):
    tx = {
        "transaction_amount": {"amount": "250.00", "currency": "EUR"},
        "credit_debit_indicator": "DBIT",
        "status": "BOOK",
        "booking_date": "2026-08-03",
        "creditor": {"name": "Müller, Hans"},
        "remittance_information": ['Rücklagen, Steuern; "NK"'],
        "entry_reference": "r1",
    }
    with Storage(tmp_path / "db.sqlite3") as s:
        s.store_transactions("acc", [tx])
        rows = s.transactions("acc")
    out = io.StringIO()
    write_csv(rows, out)

    line = out.getvalue().splitlines()[1]
    assert '"Müller, Hans"' in line
    assert ";-250.0;" in line

    header, row = csv.reader(io.StringIO(out.getvalue()), delimiter=";")
    assert len(row) == len(header) == len(EXPORT_COLUMNS)
    record = dict(zip(header, row))
    assert record["counterparty"] == "Müller, Hans"
    assert record["remittance"] == 'Rücklagen, Steuern; "NK"'


def test_xml_contains_columns_and_escapes(tmp_path):
    tx = {
        "transaction_amount": {"amount": "5.00", "currency": "EUR"},
        "credit_debit_indicator": "CRDT",
        "status": "BOOK",
        "booking_date": "2026-08-03",
        "debtor": {"name": "A & B <GmbH>"},
        "entry_reference": "r1",
    }
    with Storage(tmp_path / "db.sqlite3") as s:
        s.store_transactions("acc", [tx])
        rows = s.transactions("acc")
    out = io.StringIO()
    write_xml(rows, out)

    root = ET.fromstring(out.getvalue())
    [elem] = root.findall("transaction")
    assert elem.findtext("counterparty") == "A & B <GmbH>"
    assert elem.findtext("amount") == "5.0"
    assert elem.find("value_date") is None  # leere Felder werden weggelassen
