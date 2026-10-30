"""Ausgabe gespeicherter Umsätze als CSV, JSON oder XML."""

from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from typing import IO, Iterable

EXPORT_COLUMNS = [
    "account_uid", "status", "booking_date", "value_date", "amount", "currency",
    "credit_debit", "counterparty", "counterparty_iban", "remittance",
    "entry_reference", "transaction_id",
]

FORMATS = {
    "csv": ("text/csv", "utf-8-sig"),  # mit BOM, damit Excel UTF-8 (Umlaute) erkennt
    "json": ("application/json", "utf-8"),
    "xml": ("application/xml", "utf-8"),
}


def write_csv(rows: Iterable, out: IO[str]) -> None:
    # Alle Textfelder in Anführungszeichen, damit Kommas/Semikolons im Verwendungszweck
    # beim Import in Office nicht als Spaltentrenner gelten. Beträge bleiben Zahlen.
    writer = csv.writer(out, delimiter=";", quoting=csv.QUOTE_NONNUMERIC)
    writer.writerow(EXPORT_COLUMNS)
    for r in rows:
        writer.writerow([r[c] for c in EXPORT_COLUMNS])


def write_json(rows: Iterable, out: IO[str]) -> None:
    # Original-Antwort der API je Umsatz, ergänzt um das Konto.
    json.dump([json.loads(r["raw"]) | {"account_uid": r["account_uid"]} for r in rows],
              out, indent=2, ensure_ascii=False)
    out.write("\n")


def write_xml(rows: Iterable, out: IO[str]) -> None:
    root = ET.Element("transactions")
    for r in rows:
        tx = ET.SubElement(root, "transaction")
        for c in EXPORT_COLUMNS:
            if r[c] is not None:
                ET.SubElement(tx, c).text = str(r[c])
    ET.indent(root)
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.write(ET.tostring(root, encoding="unicode"))
    out.write("\n")


WRITERS = {"csv": write_csv, "json": write_json, "xml": write_xml}


def write(fmt: str, rows: Iterable, out: IO[str]) -> None:
    WRITERS[fmt](rows, out)
