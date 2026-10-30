"""Kommandozeilenschnittstelle."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import uuid
from datetime import date
from urllib.parse import parse_qs, urlparse

from . import __version__
from .client import ApiError, EnableBankingClient
from .config import Config, ConfigError
from .storage import Storage
from .sync import NoSessionError, run_forever, sync_all

log = logging.getLogger("enable_banking_sync")


def _client(cfg: Config) -> EnableBankingClient:
    return EnableBankingClient(cfg.application_id, cfg.read_private_key(), cfg.api_base)


def _extract_code(value: str, expected_state: str | None) -> str:
    """Akzeptiert entweder den reinen Code oder die komplette Redirect-URL."""
    value = value.strip()
    if "://" not in value and "?" not in value:
        return value
    query = parse_qs(urlparse(value).query)
    if "error" in query:
        desc = query.get("error_description", [""])[0]
        raise SystemExit(f"Autorisierung fehlgeschlagen: {query['error'][0]} {desc}".strip())
    if "code" not in query:
        raise SystemExit("In der URL wurde kein Parameter 'code' gefunden.")
    state = query.get("state", [None])[0]
    if expected_state and state and state != expected_state:
        raise SystemExit("Der Parameter 'state' passt nicht zur gestarteten Autorisierung.")
    return query["code"][0]


# --- Befehle ---------------------------------------------------------------


def cmd_info(cfg: Config, args: argparse.Namespace) -> int:
    app = _client(cfg).get_application()
    print(json.dumps(app, indent=2, ensure_ascii=False))
    return 0


def cmd_banks(cfg: Config, args: argparse.Namespace) -> int:
    aspsps = _client(cfg).list_aspsps(args.country or cfg.aspsp_country, cfg.psu_type)
    for a in aspsps:
        if args.search and args.search.lower() not in a["name"].lower():
            continue
        print(f"{a['name']}\t{a['country']}")
    return 0


def cmd_authorize(cfg: Config, args: argparse.Namespace) -> int:
    client = _client(cfg)
    aspsp_name = args.bank or cfg.aspsp_name
    country = (args.country or cfg.aspsp_country).upper()
    redirect_url = args.redirect_url or cfg.redirect_url
    if not aspsp_name:
        raise ConfigError("Bank fehlt: EB_ASPSP_NAME setzen oder --bank angeben (siehe 'banks').")
    if not redirect_url:
        raise ConfigError("Redirect-URL fehlt: EB_REDIRECT_URL setzen oder --redirect-url angeben.")

    if args.code:
        code = _extract_code(args.code, None)
    else:
        state = str(uuid.uuid4())
        auth = client.start_authorization(
            aspsp_name, country, redirect_url, cfg.psu_type, cfg.access_days, state
        )
        print("\n1. Diese URL im Browser öffnen und den Zugriff bei der Bank freigeben:\n")
        print(f"   {auth['url']}\n")
        print("2. Anschließend wird auf die Redirect-URL weitergeleitet. Die komplette URL")
        print("   aus der Adresszeile (enthält ?code=...) hier einfügen.\n")
        try:
            code = _extract_code(input("Redirect-URL oder Code: "), state)
        except EOFError:
            print(
                "\nKeine Eingabe möglich. Container interaktiv starten (docker compose run --rm ...)"
                " oder den Code per --code übergeben.",
                file=sys.stderr,
            )
            return 2

    session = client.create_session(code)
    with Storage(cfg.db_path) as storage:
        storage.save_session(session)
        accounts = storage.accounts(session["session_id"])
    print(f"\nSession {session['session_id']} gespeichert, gültig bis "
          f"{(session.get('access') or {}).get('valid_until')}.")
    print(f"{len(accounts)} Konto/Konten freigegeben:")
    for acc in accounts:
        print(f"  - {acc['uid']}  {acc['iban'] or ''}  {acc['name'] or ''}")
    return 0


def cmd_fetch(cfg: Config, args: argparse.Namespace) -> int:
    date_from = date.fromisoformat(args.date_from) if args.date_from else None
    date_to = date.fromisoformat(args.date_to) if args.date_to else None
    with Storage(cfg.db_path) as storage:
        ok = sync_all(_client(cfg), storage, cfg, date_from, date_to)
    return 0 if ok else 1


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    with Storage(cfg.db_path) as storage:
        run_forever(_client(cfg), storage, cfg)
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    with Storage(cfg.db_path) as storage:
        session = storage.active_session()
        if not session:
            print("Keine aktive Session. Bitte 'authorize' ausführen.")
            return 1
        print(f"Session:     {session['session_id']}")
        print(f"Bank:        {session['aspsp_name']} ({session['aspsp_country']})")
        print(f"Gültig bis:  {session['valid_until']}")
        for acc in storage.accounts(session["session_id"]):
            print(f"\nKonto {acc['uid']}  {acc['iban'] or ''}  {acc['name'] or ''}")
            print(f"  Umsätze gespeichert: {storage.count_transactions(acc['uid'])}")
            print(f"  Letzte Buchung:      {storage.latest_booking_date(acc['uid']) or '-'}")
            run = storage.last_run(acc["uid"])
            if run:
                result = f"FEHLER: {run['error']}" if run["error"] else (
                    f"{run['received']} empfangen, {run['inserted']} neu")
                print(f"  Letzter Abruf:       {run['started_at']} – {result}")
            for b in storage.latest_balances(acc["uid"]):
                print(f"  Saldo {b['balance_type']}: {b['amount']:.2f} {b['currency']}"
                      f" ({b['reference_date'] or b['fetched_at']})")
    return 0


EXPORT_COLUMNS = [
    "account_uid", "status", "booking_date", "value_date", "amount", "currency",
    "credit_debit", "counterparty", "counterparty_iban", "remittance",
    "entry_reference", "transaction_id",
]


def write_csv(rows, out) -> None:
    # Alle Textfelder in Anführungszeichen, damit Kommas/Semikolons im Verwendungszweck
    # beim Import in Office nicht als Spaltentrenner gelten. Beträge bleiben Zahlen.
    writer = csv.writer(out, delimiter=";", quoting=csv.QUOTE_NONNUMERIC)
    writer.writerow(EXPORT_COLUMNS)
    for r in rows:
        writer.writerow([r[c] for c in EXPORT_COLUMNS])


def cmd_export(cfg: Config, args: argparse.Namespace) -> int:
    with Storage(cfg.db_path) as storage:
        rows = storage.transactions(args.account)
    # CSV-Dateien mit BOM, damit Excel UTF-8 (Umlaute) erkennt.
    encoding = "utf-8-sig" if args.format == "csv" else "utf-8"
    out = open(args.output, "w", newline="", encoding=encoding) if args.output else sys.stdout
    try:
        if args.format == "json":
            json.dump([json.loads(r["raw"]) | {"account_uid": r["account_uid"]} for r in rows],
                      out, indent=2, ensure_ascii=False)
            out.write("\n")
        else:
            write_csv(rows, out)
    finally:
        if out is not sys.stdout:
            out.close()
    return 0


# --- Einstiegspunkt --------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="enable-banking",
        description="Kontoumsätze über Enable Banking abrufen und lokal speichern.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug-Ausgaben aktivieren")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="Daten der Enable-Banking-Anwendung anzeigen (Verbindungstest)")

    b = sub.add_parser("banks", help="Verfügbare Banken (ASPSPs) auflisten")
    b.add_argument("--country", help="Ländercode, z. B. DE")
    b.add_argument("--search", help="Nach Namen filtern")

    a = sub.add_parser("authorize", help="Kontozugriff bei der Bank autorisieren (einmalig/bei Ablauf)")
    a.add_argument("--bank", help="Name der Bank laut 'banks' (Standard: EB_ASPSP_NAME)")
    a.add_argument("--country", help="Ländercode der Bank (Standard: EB_ASPSP_COUNTRY)")
    a.add_argument("--redirect-url", help="Registrierte Redirect-URL (Standard: EB_REDIRECT_URL)")
    a.add_argument("--code", help="Bereits erhaltenen Code bzw. Redirect-URL direkt übergeben")

    f = sub.add_parser("fetch", help="Einmaliger Abruf der Umsätze aller Konten")
    f.add_argument("--date-from", help="Startdatum YYYY-MM-DD (Standard: inkrementell)")
    f.add_argument("--date-to", help="Enddatum YYYY-MM-DD")

    sub.add_parser("run", help="Periodischer Abruf (Standard im Container)")
    sub.add_parser("status", help="Session, Konten und letzten Abruf anzeigen")

    e = sub.add_parser("export", help="Gespeicherte Umsätze exportieren")
    e.add_argument("--format", choices=["csv", "json"], default="csv")
    e.add_argument("--account", help="Nur dieses Konto (UID)")
    e.add_argument("-o", "--output", help="Zieldatei (Standard: stdout)")
    return p


COMMANDS = {
    "info": cmd_info,
    "banks": cmd_banks,
    "authorize": cmd_authorize,
    "fetch": cmd_fetch,
    "run": cmd_run,
    "status": cmd_status,
    "export": cmd_export,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.DEBUG if args.verbose else getattr(
        logging, os.environ.get("EB_LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        cfg = Config.from_env()
        return COMMANDS[args.command](cfg, args)
    except (ConfigError, NoSessionError) as exc:
        log.error("%s", exc)
        return 2
    except ApiError as exc:
        log.error("API-Fehler: %s", exc)
        if exc.payload:
            log.debug("Antwort: %s", exc.payload)
        return 1


if __name__ == "__main__":
    sys.exit(main())
