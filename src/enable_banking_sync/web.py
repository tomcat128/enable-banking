"""Weboberfläche: Einrichtung, Bankverbindung, Abruf, Export und Rechtstexte."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import io
import json
import logging
import secrets
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, quote, urlencode, urlparse

from . import export, mail, settings
from .client import ApiError
from .config import Config, ConfigError
from .storage import Storage
from .sync import SyncService, _parse_ts, clear_block, get_block

log = logging.getLogger(__name__)

MAX_BODY = 200_000
BANK_CACHE_SECONDS = 3600
PENDING_SECONDS = 3600
SESSION_COOKIE = "eb_session"
SESSION_SECONDS = 30 * 86400
DISPLAY_LIMIT = 500
FILTERS = ("account", "date_from", "date_to", "days", "q")
FORMATS = ("csv", "json", "xml")
MONTHS = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember")

MESSAGES = {
    "saved": "Einstellungen gespeichert.",
    "connected": "Bank verbunden. Der erste Abruf läuft.",
    "fetch": "Abruf gestartet.",
    "setup": "Bitte zuerst Application ID und privaten Schlüssel eintragen.",
    "logout": "Abgemeldet.",
    "resumed": "Automatischer Abruf freigegeben. Der Abruf läuft.",
}

NAV = (("/", "Übersicht"), ("/transactions", "Umsätze"), ("/export/links", "Export"),
       ("/connect", "Bank verbinden"), ("/settings", "Einstellungen"))

CSS = """
:root { --text: #1f2328; --muted: #59636e; --bg: #f6f8fa; --card: #ffffff; --line: #d1d9e0;
        --accent: #0969da; --accent-text: #ffffff; --ok: #1a7f37; --warn: #9a6700; --err: #cf222e;
        --notice-bg: #dafbe1; --error-bg: #ffebe9; }
@media (prefers-color-scheme: dark) {
  :root { --text: #e6edf3; --muted: #9198a1; --bg: #0d1117; --card: #151b23; --line: #3d444d;
          --accent: #4493f8; --accent-text: #0d1117; --ok: #3fb950; --warn: #d29922; --err: #f85149;
          --notice-bg: #12261e; --error-bg: #2d1214; }
}
* { box-sizing: border-box; }
body { font-family: system-ui, sans-serif; line-height: 1.5; color: var(--text); background: var(--bg);
       max-width: 64rem; margin: 0 auto; padding: 1rem; }
header { display: flex; flex-wrap: wrap; gap: .5rem 1.5rem; align-items: baseline; margin-bottom: 1rem; }
header nav { display: flex; flex-wrap: wrap; gap: 1rem; }
header form { margin-left: auto; }
header button { margin: 0; padding: .2rem .8rem; }
.button.secondary, button.secondary { background: transparent; color: var(--accent); border: 1px solid var(--line); }
.buttons { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; }
.buttons .button, .buttons button { margin-top: 0; }
input.url { font-family: ui-monospace, monospace; font-size: .85rem; min-width: 16rem; }
table.links th, table.links td { vertical-align: middle; }
td.actions { white-space: nowrap; width: 1%; }
td.actions .button, td.actions button { margin: 0 0 0 .25rem; padding: .3rem .8rem; }
a { color: var(--accent); }
h2 { font-size: 1.1rem; margin: 0 0 .75rem; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 1rem 1.25rem;
        margin-bottom: 1rem; }
.notice, .error { border-radius: 8px; padding: .75rem 1rem; margin-bottom: 1rem; }
.notice { background: var(--notice-bg); color: var(--ok); }
.error { background: var(--error-bg); color: var(--err); }
.muted, .hint { color: var(--muted); font-size: .9rem; }
.ok { color: var(--ok); } .warn { color: var(--warn); } .err { color: var(--err); }
label { display: block; font-weight: 600; margin-top: .75rem; }
input, select, textarea { font: inherit; width: 100%; padding: .4rem .5rem; border: 1px solid var(--line);
       border-radius: 6px; background: var(--card); color: var(--text); }
textarea { font-family: ui-monospace, monospace; font-size: .85rem; }
.inline { display: flex; flex-wrap: wrap; gap: .75rem; align-items: end; }
.inline > div { flex: 1 1 10rem; }
.radios label { display: inline; font-weight: normal; margin-right: 1rem; }
.radios input { width: auto; }
button, .button { font: inherit; display: inline-block; margin-top: 1rem; padding: .45rem 1.1rem;
       border: 0; border-radius: 6px; background: var(--accent); color: var(--accent-text);
       text-decoration: none; cursor: pointer; }
button:disabled { opacity: .5; cursor: not-allowed; }
.table { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
th, td { text-align: left; padding: .35rem .5rem; border-bottom: 1px solid var(--line); vertical-align: top; }
td.num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
td.neg { color: var(--err); }
code, pre { font-family: ui-monospace, monospace; font-size: .85rem; }
pre { overflow-x: auto; background: var(--bg); padding: .75rem; border-radius: 6px; }
footer { margin-top: 2rem; display: flex; gap: 1rem; }
"""


def esc(value: object) -> str:
    return "" if value is None else html.escape(str(value))


def card(title: str, body: str) -> str:
    return f'<section class="card"><h2>{esc(title)}</h2>{body}</section>'


def money(amount: float | None, currency: str | None) -> str:
    if amount is None:
        return ""
    text = f"{amount:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
    return f"{text} {currency or ''}".strip()


def day(value: str | None) -> str:
    try:
        return date.fromisoformat(value[:10]).strftime("%d.%m.%Y") if value else ""
    except ValueError:
        return value


def moment(value: str | datetime | None) -> str:
    dt = _parse_ts(value) if isinstance(value, str) else value
    return dt.astimezone().strftime("%d.%m.%Y %H:%M") if dt else ""


COPY_SCRIPT = """
<script>
  document.querySelectorAll('[data-copy]').forEach(function (button) {
    button.addEventListener('click', function () {
      var input = document.getElementById(button.dataset.copy);
      input.select();
      var done = function () { button.textContent = 'Kopiert'; };
      if (navigator.clipboard) navigator.clipboard.writeText(input.value).then(done, function () {
        document.execCommand('copy'); done(); });
      else { document.execCommand('copy'); done(); }
    });
  });
</script>"""


def _is_account(account, value: str) -> bool:
    return bool(value) and (value == account["uid"] or
                            value.replace(" ", "").upper() == (account["iban"] or "").replace(" ", "").upper())


def effective_from(filters: dict[str, str]) -> str | None:
    """Startdatum aus date_from oder days (days=1 ist nur heute)."""
    if filters.get("days"):
        return (date.today() - timedelta(days=int(filters["days"]) - 1)).isoformat()
    return filters.get("date_from") or None


def describe(filters: dict[str, str]) -> str:
    """Kurzbeschreibung des Zeitraums für die Anzeige."""
    start, end = effective_from(filters), filters.get("date_to")
    if start and end:
        return f", {day(start)} bis {day(end)}"
    if start:
        return f", ab {day(start)}"
    if end:
        return f", bis {day(end)}"
    return ""


def totals(rows) -> str:
    """Einnahmen, Ausgaben und Summe je Währung."""
    sums: dict[str, list[float]] = {}
    for r in rows:
        if r["amount"] is not None:
            pair = sums.setdefault(r["currency"] or "", [0.0, 0.0])
            pair[0 if r["amount"] >= 0 else 1] += r["amount"]
    return "".join(
        f' · Einnahmen <span class="ok">{money(income, cur)}</span>'
        f' · Ausgaben <span class="err">{money(spent, cur)}</span>'
        f" · Summe {money(income + spent, cur)}"
        for cur, (income, spent) in sorted(sums.items()))


def tx_table(rows, labels: dict[str, str] | None = None) -> str:
    """Umsatztabelle; mit labels zusätzlich eine Spalte für das Konto."""
    head = "<th>Datum</th>" + ("<th>Konto</th>" if labels else "") + (
        "<th>Betrag</th><th>Gegenpartei</th><th>Verwendungszweck</th><th></th>")
    body = "".join(
        f"<tr><td>{day(r['booking_date'] or r['transaction_date'] or r['value_date'])}</td>"
        + (f"<td>{esc(labels.get(r['account_uid'], r['account_uid']))}</td>" if labels else "")
        + f'<td class="num{" neg" if (r["amount"] or 0) < 0 else ""}">{money(r["amount"], r["currency"])}</td>'
        f"<td>{esc(r['counterparty'])}</td><td>{esc(r['remittance'])}</td>"
        f"<td>{'vorgemerkt' if r['status'] == 'PDNG' else ''}</td></tr>"
        for r in rows)
    return f'<div class="table"><table><tr>{head}</tr>{body}</table></div>'


def layout(title: str, content: str, *, notice: str = "", error: str = "", refresh: bool = False,
           nav: bool = True, logout: bool = False) -> str:
    links = "".join(f'<a href="{href}">{label}</a>' for href, label in NAV) if nav else ""
    logout_form = ('<form method="post" action="/logout"><button class="secondary">Abmelden</button></form>'
                   if logout else "")
    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
{'<meta http-equiv="refresh" content="3;url=/">' if refresh else ''}
<title>{esc(title)} – Enable Banking</title>
<style>{CSS}</style>
</head>
<body>
<header><strong>Enable Banking</strong><nav>{links}</nav>{logout_form}</header>
{f'<p class="notice">{esc(notice)}</p>' if notice else ''}
{f'<p class="error">{esc(error)}</p>' if error else ''}
{content}
<footer class="muted"><a href="/privacy">Datenschutz</a><a href="/terms">Nutzungsbedingungen</a></footer>
</body>
</html>
"""


def _same(given: str, expected: str | None) -> bool:
    return expected is not None and hmac.compare_digest(given.encode(), expected.encode())


def _safe_next(target: str) -> str:
    """Nur relative Ziele innerhalb der Anwendung zulassen (kein offener Redirect)."""
    if (target.startswith("/") and not target.startswith(("//", "/\\"))
            and not any(ord(c) < 32 for c in target)):
        return target
    return "/"


class App:
    """Gemeinsamer Zustand aller Anfragen."""

    def __init__(self, cfg: Config, sync: SyncService | None = None):
        self.cfg = cfg
        self.sync = sync
        self._lock = threading.Lock()
        self._pending: dict[str, float] = {}  # state -> Startzeit der Autorisierung
        self._banks: dict[tuple[str, str], tuple[float, list[dict]]] = {}
        self._session_key = self._make_session_key() if cfg.login_required else b""

    # --- Login -------------------------------------------------------------

    def _make_session_key(self) -> bytes:
        # Das Geheimnis liegt in der Datenbank, damit Anmeldungen einen Neustart überstehen.
        # Benutzer und Passwort fließen ein: Nach einer Änderung sind alte Anmeldungen ungültig.
        with Storage(self.cfg.db_path) as storage:
            secret = storage.settings().get("session_secret")
            if not secret:
                secret = secrets.token_hex(32)
                storage.save_settings({"session_secret": secret})
        material = f"{self.cfg.web_user}\0{self.cfg.web_password}".encode()
        return hmac.new(secret.encode(), material, hashlib.sha256).digest()

    def _sign(self, expires: int) -> bytes:
        return hmac.new(self._session_key, str(expires).encode(), hashlib.sha256).hexdigest().encode()

    def new_session(self) -> str:
        expires = int(time.time()) + SESSION_SECONDS
        return f"{expires}.{self._sign(expires).decode()}"

    def valid_session(self, token: str) -> bool:
        expires, _, signature = token.partition(".")
        if not expires.isdigit() or int(expires) < time.time():
            return False
        return hmac.compare_digest(signature.encode(), self._sign(int(expires)))

    def check_login(self, user: str, password: str) -> bool:
        return _same(user, self.cfg.web_user) & _same(password, self.cfg.web_password)

    def check_export_auth(self, user: str, password: str) -> bool:
        return _same(user, self.cfg.export_user) & _same(password, self.cfg.export_password)

    def remember_state(self, state: str) -> None:
        with self._lock:
            self._prune()
            self._pending[state] = time.time()

    def take_state(self, state: str) -> bool:
        with self._lock:
            self._prune()
            return self._pending.pop(state, None) is not None

    def _prune(self) -> None:
        limit = time.time() - PENDING_SECONDS
        for state in [s for s, started in self._pending.items() if started < limit]:
            del self._pending[state]

    def banks(self, client, country: str, psu_type: str) -> list[dict]:
        key = (country, psu_type)
        with self._lock:
            cached = self._banks.get(key)
        if cached and time.time() - cached[0] < BANK_CACHE_SECONDS:
            return cached[1]
        banks = sorted(client.list_aspsps(country, psu_type), key=lambda b: b["name"].lower())
        with self._lock:
            self._banks[key] = (time.time(), banks)
        return banks


class Handler(BaseHTTPRequestHandler):
    app: App  # wird in make_server gesetzt
    server_version = "enable-banking"
    sys_version = ""

    # Ohne Login erreichbar: Anmeldung, Rechtstexte (Enable Banking ruft sie ab) und der
    # Export-Pfad, der eine eigene Basic-Auth-Prüfung hat.
    PUBLIC = {"/login", "/logout", "/privacy", "/privacy.html", "/terms", "/terms.html", "/export"}

    ROUTES = {
        ("GET", "/"): "index",
        ("GET", "/login"): "login_page",
        ("POST", "/login"): "login",
        ("POST", "/logout"): "logout",
        ("GET", "/transactions"): "transactions_page",
        ("GET", "/download"): "download",
        ("GET", "/export/links"): "export_page",
        ("GET", "/settings"): "settings_page",
        ("POST", "/settings"): "save_settings",
        ("POST", "/settings/test"): "test_connection",
        ("GET", "/connect"): "connect_page",
        ("POST", "/connect"): "start_connect",
        ("GET", "/callback"): "callback",
        ("POST", "/callback"): "callback_manual",
        ("POST", "/fetch"): "fetch",
        ("POST", "/fetch/resume"): "resume_fetch",
        ("POST", "/settings/mail"): "test_mail",
        ("GET", "/export"): "export",
        ("GET", "/privacy"): "privacy",
        ("GET", "/privacy.html"): "privacy",
        ("GET", "/terms"): "terms",
        ("GET", "/terms.html"): "terms",
    }

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        self.params = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.form: dict[str, str] = {}
        if self.command == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._text(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Anfrage zu groß")
            body = self.rfile.read(length).decode("utf-8", "replace")
            self.form = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
        if path not in self.PUBLIC and not self._logged_in():
            target = self.path if self.command == "GET" else "/"
            return self._redirect("/login?next=" + quote(target, safe=""))
        name = self.ROUTES.get((self.command, path))
        if not name:
            return self._text(HTTPStatus.NOT_FOUND, "Nicht gefunden")
        try:
            getattr(self, name)()
        except Exception:
            log.exception("Fehler bei %s %s", self.command, path)
            self._page("Fehler", "", error="Interner Fehler, Details stehen im Log.",
                       status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _logged_in(self) -> bool:
        if not self.app.cfg.login_required:
            return True
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and self.app.valid_session(value):
                return True
        return False

    def _cookie(self, value: str, max_age: int) -> dict[str, str]:
        secure = "; Secure" if self._base_url().startswith("https://") else ""
        return {"Set-Cookie": f"{SESSION_COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; "
                              f"SameSite=Lax{secure}"}

    # --- Anmeldung ---------------------------------------------------------

    def login_page(self, *, error: str = "") -> None:
        if not self.app.cfg.login_required:
            return self._redirect("/")
        target = _safe_next(self.params.get("next") or self.form.get("next") or "/")
        form = f"""
<form method="post" action="/login">
  <input type="hidden" name="next" value="{esc(target)}">
  <label for="user">Benutzer</label>
  <input id="user" name="user" autocomplete="username" required autofocus>
  <label for="password">Passwort</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <button>Anmelden</button>
</form>"""
        self._page("Anmelden", card("Anmelden", form), error=error, nav=False)

    def login(self) -> None:
        if not self.app.cfg.login_required:
            return self._redirect("/")
        if not self.app.check_login(self.form.get("user", ""), self.form.get("password", "")):
            log.warning("Fehlgeschlagene Anmeldung von %s", self.address_string())
            time.sleep(1)  # bremst das Durchprobieren von Passwörtern
            return self.login_page(error="Benutzer oder Passwort ist falsch.")
        self._redirect(_safe_next(self.form.get("next") or "/"),
                       self._cookie(self.app.new_session(), SESSION_SECONDS))

    def logout(self) -> None:
        target = "/login?msg=logout" if self.app.cfg.login_required else "/"
        self._redirect(target, self._cookie("", 0))

    # --- Hilfen ------------------------------------------------------------

    def _storage(self) -> Storage:
        return Storage(self.app.cfg.db_path)

    def _settings(self) -> settings.Settings:
        with self._storage() as storage:
            return settings.load(self.app.cfg, storage)

    def _base_url(self) -> str:
        # Hinter einem Reverse Proxy (z. B. Caddy) die öffentliche Adresse verwenden.
        proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
        host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost")
        return f"{proto}://{host.split(',')[0].strip()}"

    def _redirect_url(self, st: settings.Settings) -> str:
        return st.redirect_url or f"{self._base_url()}/callback"

    # --- Übersicht ---------------------------------------------------------

    def index(self) -> None:
        with self._storage() as s:
            st = settings.load(self.app.cfg, s)
            session = s.active_session()
            accounts = [
                (a, s.count_transactions(a["uid"]), s.latest_booking_date(a["uid"]),
                 s.last_run(a["uid"]), s.latest_balances(a["uid"]))
                for a in (s.accounts(session["session_id"]) if session else [])
            ]
            total = s.count_transactions()
            recent = s.recent_transactions(10)
            block = get_block(s)
            last_fetch = s.last_fetch()

        parts = []
        if not st.complete:
            parts.append(card("Einrichtung", (
                "<p>Die Anwendung ist noch nicht eingerichtet. Trage zuerst die Application ID und den "
                "privaten Schlüssel deiner Enable-Banking-Anwendung ein.</p>"
                '<a class="button" href="/settings">Zu den Einstellungen</a>')))
        parts.append(self._session_card(session, st))
        parts.append(self._block_card(block) if block else self._sync_card(session, last_fetch))
        if accounts:
            parts.append(self._accounts_card(accounts))
        parts.append(self._recent_card(recent, total))
        busy = bool(self.app.sync and self.app.sync.busy)
        self._page("Übersicht", "".join(parts), refresh=busy)

    def _session_card(self, session, st: settings.Settings) -> str:
        if not session:
            button = ('<a class="button" href="/connect">Bank verbinden</a>' if st.complete
                      else '<p class="muted">Nach der Einrichtung kannst du hier deine Bank verbinden.</p>')
            return card("Bankverbindung", f"<p>Noch keine Bank verbunden.</p>{button}")
        valid = _parse_ts(session["valid_until"])
        state = ""
        if valid:
            days = (valid - datetime.now(timezone.utc)).days
            css = "err" if days < 0 else "warn" if days < 7 else "ok"
            text = "abgelaufen" if days < 0 else f"noch {days} Tage"
            state = f' Zugriff gültig bis {moment(valid)} (<span class="{css}">{text}</span>).'
        return card("Bankverbindung", (
            f"<p>Verbunden mit <strong>{esc(session['aspsp_name'])}</strong>"
            f" ({esc(session['aspsp_country'])}).{state}</p>"
            '<a class="button" href="/connect">Neu verbinden</a>'))

    def _block_card(self, block: dict) -> str:
        since = f" seit {moment(block['since'])}" if block.get("since") else ""
        return card("Abruf gestoppt", (
            f'<p class="err"><strong>Die Bank verlangt eine erneute Anmeldung.</strong> Der automatische Abruf '
            f"ist{since} gestoppt; es werden keine weiteren Versuche unternommen.</p>"
            f"<p>{esc(block.get('reason'))}</p>"
            + (f'<p class="muted">{esc(block["mail"])}</p>' if block.get("mail") else "")
            + '<div class="buttons" style="margin-top:1rem"><a class="button" href="/connect">Bank neu verbinden</a>'
            '<form method="post" action="/fetch/resume"><button class="secondary">Abruf trotzdem wieder '
            "freigeben</button></form></div>"
            '<p class="hint">Nach dem Neuverbinden läuft der Abruf automatisch wieder.</p>'))

    def _sync_card(self, session, last_fetch: str | None) -> str:
        sync = self.app.sync
        if sync is None:
            return card("Abruf", "<p class=\"muted\">Der automatische Abruf läuft in diesem Prozess nicht.</p>")
        lines = []
        if sync.busy:
            lines.append('<p><strong>Abruf läuft …</strong> Die Seite aktualisiert sich automatisch.</p>')
        elif last_fetch:
            lines.append(f"<p>Letzter Abruf: {moment(last_fetch)}</p>")
        if sync.last_error and not sync.busy:
            lines.append(f'<p class="err">{esc(sync.last_error)}</p>')
        if sync.next_run and not sync.busy:
            lines.append(f'<p class="muted">Nächster automatischer Abruf: {moment(sync.next_run)}</p>')
        disabled = " disabled" if sync.busy or not session else ""
        hours = self.app.cfg.fetch_interval / 3600
        lines.append(
            f'<form method="post" action="/fetch"><button{disabled}>Jetzt abrufen</button></form>'
            f'<p class="hint">Automatisch alle {hours:g} Stunden. Banken erlauben ohne erneute Anmeldung '
            "oft nur 4 Abrufe pro Tag und Konto.</p>")
        return card("Abruf", "".join(lines))

    def _accounts_card(self, accounts) -> str:
        rows = []
        for a, count, latest, run, balances in accounts:
            if run is None:
                result = '<span class="muted">noch nicht</span>'
            elif run["error"]:
                result = f'{moment(run["started_at"])}<br><span class="err">{esc(run["error"])}</span>'
            else:
                result = f'{moment(run["started_at"])}<br>{run["received"]} empfangen, {run["inserted"]} neu'
            saldo = "<br>".join(
                f'{money(b["amount"], b["currency"])} <span class="muted">{esc(b["balance_type"])}</span>'
                for b in balances)
            rows.append(
                f"<tr><td>{esc(a['iban'] or a['uid'])}<br><span class=\"muted\">{esc(a['name'])}</span></td>"
                f'<td class="num">{saldo}</td><td class="num">{count}</td><td>{day(latest)}</td>'
                f"<td>{result}</td></tr>")
        return card("Konten", (
            '<div class="table"><table><tr><th>Konto</th><th>Saldo</th><th>Umsätze</th>'
            f"<th>Letzte Buchung</th><th>Letzter Abruf</th></tr>{''.join(rows)}</table></div>"))

    def _recent_card(self, recent, total: int) -> str:
        if not recent:
            return card("Letzte Umsätze", '<p class="muted">Noch keine Umsätze gespeichert.</p>')
        return card("Letzte Umsätze", tx_table(recent) + (
            f'<a class="button" href="/transactions">Alle {total} Umsätze anzeigen und exportieren</a>'))

    # --- Einstellungen -----------------------------------------------------

    def settings_page(self, *, error: str = "", values: dict | None = None, result: str = "") -> None:
        st = self._settings()
        v = values or {
            "application_id": st.application_id, "redirect_url": st.redirect_url,
            "aspsp_country": st.aspsp_country,
        }
        base = self._base_url()
        redirect_url = v.get("redirect_url") or f"{base}/callback"
        key_hint = (f"Hinterlegt ({esc(st.private_key_source)}). Leer lassen, um ihn zu behalten."
                    if st.private_key else "Noch kein Schlüssel hinterlegt.")
        form = f"""
<form method="post" action="/settings">
  <label for="application_id">Application ID</label>
  <input id="application_id" name="application_id" value="{esc(v.get('application_id'))}"
         placeholder="00000000-0000-0000-0000-000000000000">
  <label for="private_key">Privater Schlüssel (PEM)</label>
  <textarea id="private_key" name="private_key" rows="6"
            placeholder="-----BEGIN PRIVATE KEY-----&#10;...&#10;-----END PRIVATE KEY-----"></textarea>
  <input type="file" id="key_file" accept=".pem,.key,.txt" style="margin-top:.4rem">
  <p class="hint">{key_hint} Die Datei <code>&lt;application-id&gt;.pem</code> aus dem Control Panel auswählen
    oder ihren Inhalt einfügen.</p>
  <label for="redirect_url">Redirect-URL</label>
  <input id="redirect_url" name="redirect_url" value="{esc(redirect_url)}">
  <p class="hint">Die öffentliche Adresse von <code>/callback</code> dieser Anwendung. Genau so im Control Panel
    eintragen.</p>
  <label for="aspsp_country">Land der Bank</label>
  <input id="aspsp_country" name="aspsp_country" value="{esc(v.get('aspsp_country'))}" maxlength="2">
  <button>Speichern</button>
</form>
<script>
  document.getElementById('key_file').addEventListener('change', function (event) {{
    var file = event.target.files[0];
    if (file) file.text().then(function (text) {{ document.getElementById('private_key').value = text; }});
  }});
</script>"""
        urls = f"""
<p>Diese Adressen bei der Anwendung im
  <a href="https://enablebanking.com/cp/applications" rel="noopener noreferrer">Control Panel</a> eintragen:</p>
<div class="table"><table>
  <tr><th>Redirect URL</th><td><code>{esc(redirect_url)}</code></td></tr>
  <tr><th>Privacy URL</th><td><code>{esc(base)}/privacy</code></td></tr>
  <tr><th>Terms URL</th><td><code>{esc(base)}/terms</code></td></tr>
</table></div>
<form method="post" action="/settings/test"><button>Verbindung testen</button></form>"""
        self._page("Einstellungen",
                   card("Zugangsdaten", form) + result + card("Control Panel", urls)
                   + self._mail_card(), error=error)

    def _mail_card(self) -> str:
        cfg = self.app.cfg
        intro = ("<p>Verlangt die Bank eine erneute Anmeldung, stoppt der automatische Abruf und der "
                 "Notfallkontakt erhält eine E-Mail.</p>")
        if not cfg.mail_configured:
            return card("Benachrichtigung per E-Mail", intro + (
                '<p class="warn">Der Mailversand ist nicht eingerichtet.</p><p class="hint">In der <code>.env</code> '
                "mindestens <code>EB_SMTP_HOST</code>, <code>EB_SMTP_USER</code> und <code>EB_SMTP_PASSWORD</code> "
                "(oder <code>EB_SMTP_FROM</code>) sowie <code>EB_ALERT_EMAIL</code> setzen und den Container neu "
                "starten.</p>"))
        rows = (("SMTP-Server", f"{cfg.smtp_host}:{cfg.smtp_port} ({cfg.smtp_security})"),
                ("Benutzer", cfg.smtp_user or "– (ohne Anmeldung)"),
                ("Absender", cfg.smtp_from or cfg.smtp_user),
                ("Notfallkontakt", ", ".join(cfg.alert_email)))
        table = "".join(f"<tr><th>{label}</th><td>{esc(value)}</td></tr>" for label, value in rows)
        return card("Benachrichtigung per E-Mail", intro + (
            f'<div class="table"><table>{table}</table></div>'
            '<form method="post" action="/settings/mail"><button>Test-Mail senden</button></form>'))

    def test_mail(self) -> None:
        subject, body = mail.test_message(mail.public_url(self._redirect_url(self._settings())))
        try:
            recipients = mail.send(self.app.cfg, subject, body)
        except mail.MAIL_ERRORS as exc:
            log.error("Test-Mail fehlgeschlagen: %s", exc)
            return self.settings_page(error=f"Die Test-Mail konnte nicht gesendet werden: {exc}")
        log.info("Test-Mail an %s gesendet", recipients)
        self.settings_page(result=card("Test-Mail", f'<p class="ok">Test-Mail an {esc(recipients)} gesendet.</p>'))

    def save_settings(self) -> None:
        values = {k: self.form.get(k, "").strip() for k in settings.FIELDS if k != "private_key"}
        values["aspsp_country"] = values["aspsp_country"].upper()
        if values["aspsp_country"] and not (len(values["aspsp_country"]) == 2
                                            and values["aspsp_country"].isalpha()):
            return self.settings_page(error="Das Land muss ein zweistelliger Code sein, z. B. DE.",
                                      values=values)
        if values["redirect_url"] and not values["redirect_url"].startswith(("http://", "https://")):
            return self.settings_page(error="Die Redirect-URL muss mit http:// oder https:// beginnen.",
                                      values=values)
        key = self.form.get("private_key", "").strip()
        if key:
            error = settings.check_private_key(key)
            if error:
                return self.settings_page(error=error, values=values)
            values["private_key"] = key + "\n"
        with self._storage() as storage:
            storage.save_settings(values)
        self._redirect("/settings?msg=saved")

    def test_connection(self) -> None:
        st = self._settings()
        try:
            info = settings.make_client(self.app.cfg, st).get_application()
        except (ConfigError, ApiError, OSError) as exc:
            return self.settings_page(error=f"Verbindungstest fehlgeschlagen: {exc}")
        check = ""
        registered = info.get("redirect_urls") if isinstance(info, dict) else None
        if isinstance(registered, list):
            url = self._redirect_url(st)
            check = (f'<p class="ok">Die Redirect-URL <code>{esc(url)}</code> ist registriert.</p>'
                     if url in registered else
                     f'<p class="err">Die Redirect-URL <code>{esc(url)}</code> ist im Control Panel nicht '
                     "eingetragen. Die Bank kann dann nicht zurückleiten.</p>")
        body = (f'<p class="ok">Verbindung zu Enable Banking erfolgreich.</p>{check}'
                f"<pre>{esc(json.dumps(info, indent=2, ensure_ascii=False))}</pre>")
        self.settings_page(result=card("Verbindungstest", body))

    # --- Bank verbinden ----------------------------------------------------

    def connect_page(self, *, error: str = "") -> None:
        cfg = self.app.cfg
        st = self._settings()
        if not st.complete:
            return self._redirect("/settings?msg=setup")
        country = (self.params.get("country") or st.aspsp_country).upper()
        psu_type = self.params.get("psu_type") or cfg.psu_type
        banks: list[dict] = []
        try:
            banks = self.app.banks(settings.make_client(cfg, st), country, psu_type)
        except (ApiError, OSError) as exc:
            error = error or f"Die Bankliste konnte nicht geladen werden: {exc}"
        options = "".join(f'<option value="{esc(b["name"])}">' for b in banks)
        selected = {psu_type: " selected"}
        choose = f"""
<form method="get" action="/connect" class="inline">
  <div><label for="country">Land</label><input id="country" name="country" value="{esc(country)}" maxlength="2"></div>
  <div><label for="psu_type">Kontoart</label>
    <select id="psu_type" name="psu_type">
      <option value="personal"{selected.get('personal', '')}>Privat</option>
      <option value="business"{selected.get('business', '')}>Geschäftlich</option>
    </select></div>
  <div><button>Banken laden</button></div>
</form>"""
        start = f"""
<form method="post" action="/connect">
  <input type="hidden" name="country" value="{esc(country)}">
  <input type="hidden" name="psu_type" value="{esc(psu_type)}">
  <label for="bank">Bank ({len(banks)} verfügbar)</label>
  <input id="bank" name="bank" list="banks" required autocomplete="off" value="{esc(cfg.aspsp_name)}"
         placeholder="Namen eintippen und aus der Liste wählen">
  <datalist id="banks">{options}</datalist>
  <label for="access_days">Gültigkeit der Freigabe (Tage)</label>
  <input id="access_days" name="access_days" type="number" min="1" max="730" value="{cfg.access_days}">
  <p class="hint">Wird automatisch auf das Maximum der Bank begrenzt.</p>
  <button>Weiter zur Bank</button>
</form>
<p class="hint">Nach der Freigabe leitet die Bank zurück zu <code>{esc(self._redirect_url(st))}</code>.</p>"""
        manual = """
<p>Falls die Bank auf eine andere Adresse zurückgeleitet hat: die komplette URL aus der Adresszeile
  (enthält <code>?code=…</code>) hier einfügen.</p>
<form method="post" action="/callback">
  <textarea name="url" rows="3" required></textarea>
  <button>Verbindung herstellen</button>
</form>"""
        self._page("Bank verbinden",
                   card("Land und Kontoart", choose) + card("Bank auswählen", start)
                   + card("Rückleitung hat nicht geklappt?", manual), error=error)

    def start_connect(self) -> None:
        cfg = self.app.cfg
        country = self.form.get("country", "").upper()
        psu_type = self.form.get("psu_type") or cfg.psu_type
        name = self.form.get("bank", "").strip()
        self.params = {"country": country, "psu_type": psu_type}  # für connect_page bei Fehlern
        st = self._settings()
        try:
            client = settings.make_client(cfg, st)
        except ConfigError:
            return self._redirect("/settings?msg=setup")
        try:
            banks = self.app.banks(client, country, psu_type)
        except (ApiError, OSError) as exc:
            return self.connect_page(error=f"Die Bankliste konnte nicht geladen werden: {exc}")
        bank = next((b for b in banks if b["name"] == name), None) or next(
            (b for b in banks if b["name"].lower() == name.lower()), None)
        if not bank:
            return self.connect_page(error=f"Die Bank „{name}“ wurde nicht gefunden. Bitte aus der Liste wählen.")
        try:
            days = max(1, int(self.form.get("access_days") or cfg.access_days))
        except ValueError:
            days = cfg.access_days
        maximum = bank.get("maximum_consent_validity")  # Sekunden
        if isinstance(maximum, int) and maximum >= 86400:
            days = min(days, maximum // 86400)
        state = str(uuid.uuid4())
        self.app.remember_state(state)
        try:
            auth = client.start_authorization(
                bank["name"], country, self._redirect_url(st), psu_type, days, state)
        except (ApiError, OSError) as exc:
            return self.connect_page(error=f"Enable Banking hat die Anfrage abgelehnt: {exc}")
        self._redirect(auth["url"])

    def callback(self) -> None:
        p = self.params
        if "error" in p:
            return self._page("Bankfreigabe", card("Bankfreigabe", '<a class="button" href="/connect">Erneut versuchen</a>'),
                              error=f"Die Bank hat die Freigabe nicht erteilt: {p['error']} {p.get('error_description', '')}")
        code = p.get("code")
        if not code:
            return self._page("Bankfreigabe", card("Bankfreigabe", '<a class="button" href="/connect">Bank verbinden</a>'),
                              error="Die Adresse enthält keinen Parameter 'code'.")
        state = p.get("state")
        if not state or not self.app.take_state(state):
            # Z. B. nach einem Neustart des Containers zwischen Start und Rückleitung.
            confirm = f"""
<p>Diese Freigabe wurde nicht von dieser Sitzung gestartet, etwa weil die Anwendung zwischendurch neu
  gestartet wurde. Wenn du sie gerade selbst bei deiner Bank erteilt hast, bestätige sie hier.</p>
<form method="post" action="/callback">
  <input type="hidden" name="url" value="{esc(self._base_url() + self.path)}">
  <button>Verbindung herstellen</button>
</form>"""
            return self._page("Bankfreigabe", card("Bitte bestätigen", confirm))
        self._finish_connect(code)

    def callback_manual(self) -> None:
        try:
            code, _ = settings.parse_redirect(self.form.get("url", ""))
        except ValueError as exc:
            return self.connect_page(error=str(exc))
        self._finish_connect(code)

    def _finish_connect(self, code: str) -> None:
        try:
            session = settings.make_client(self.app.cfg, self._settings()).create_session(code)
        except ConfigError:
            return self._redirect("/settings?msg=setup")
        except (ApiError, OSError) as exc:
            return self._page("Bankfreigabe", card("Bankfreigabe", (
                "<p>Der Code aus der Rückleitung ist nur kurz gültig und kann nur einmal verwendet werden.</p>"
                '<a class="button" href="/connect">Erneut verbinden</a>')),
                error=f"Die Verbindung konnte nicht hergestellt werden: {exc}")
        with self._storage() as storage:
            storage.save_session(session)
            clear_block(storage)  # neue Freigabe der Bank: automatischer Abruf läuft wieder
        log.info("Bank verbunden: %s", (session.get("aspsp") or {}).get("name"))
        if self.app.sync:
            self.app.sync.trigger()
        self._redirect("/?msg=connected")

    # --- Abruf & Export ----------------------------------------------------

    def fetch(self) -> None:
        if self.app.sync:
            self.app.sync.trigger()
        self._redirect("/?msg=fetch")

    def resume_fetch(self) -> None:
        with self._storage() as storage:
            clear_block(storage)
        if self.app.sync:
            self.app.sync.trigger()
        self._redirect("/?msg=resumed")

    def _filters(self) -> dict[str, str]:
        """Filter aus der Adresse; wirft ValueError bei ungültigen Angaben."""
        filters = {name: (self.params.get(name) or "").strip() for name in FILTERS}
        for name in ("date_from", "date_to"):
            if filters[name]:
                try:
                    filters[name] = date.fromisoformat(filters[name]).isoformat()
                except ValueError:
                    raise ValueError(f"Ungültiges Datum für {name}: {filters[name]}") from None
        if filters["days"]:
            if not filters["days"].isdigit() or not 1 <= int(filters["days"]) <= 36500:
                raise ValueError(f"days muss eine positive ganze Zahl sein, nicht {filters['days']!r}.")
            if filters["date_from"]:
                raise ValueError("days und date_from lassen sich nicht kombinieren; bitte nur eines angeben.")
        return filters

    @staticmethod
    def _rows(storage: Storage, filters: dict[str, str], *, newest_first: bool = False):
        return storage.transactions(filters["account"] or None, effective_from(filters),
                                    filters["date_to"] or None, search=filters["q"] or None,
                                    newest_first=newest_first)

    def _filter_form(self, action: str, filters: dict[str, str], accounts, button: str) -> str:
        options = "".join(
            f'<option value="{esc(a["iban"] or a["uid"])}"{" selected" if _is_account(a, filters["account"]) else ""}>'
            f'{esc(a["iban"] or a["uid"])} {esc(a["name"])} ({n})</option>'
            for a, n in accounts)
        return f"""
<form method="get" action="{action}">
  <div class="inline">
    <div><label for="account">Konto</label>
      <select id="account" name="account"><option value="">Alle Konten</option>{options}</select></div>
    <div><label for="days">Letzte Tage</label>
      <input type="number" id="days" name="days" min="1" value="{esc(filters['days'])}" placeholder="z. B. 5"></div>
    <div><label for="date_from">Von</label>
      <input type="date" id="date_from" name="date_from" value="{esc(filters['date_from'])}"></div>
    <div><label for="date_to">Bis</label>
      <input type="date" id="date_to" name="date_to" value="{esc(filters['date_to'])}"></div>
    <div><label for="q">Suche</label>
      <input id="q" name="q" value="{esc(filters['q'])}" placeholder="Gegenpartei, Verwendungszweck"></div>
  </div>
  <p class="hint">Entweder „Letzte Tage“ (5 = heute und die 4 Tage davor) oder „Von“ angeben. Alle Felder sind
    optional.</p>
  <div class="buttons"><button>{button}</button>
    <a class="button secondary" href="{action}">Zurücksetzen</a></div>
</form>"""

    def _filter_accounts(self, storage: Storage, selected: str):
        # Konten ohne Umsätze (z. B. aus abgelösten Verbindungen) nicht zur Auswahl anbieten.
        return [(a, n) for a in storage.accounts()
                if (n := storage.count_transactions(a["uid"])) or _is_account(a, selected)]

    def export(self) -> None:
        """Export-Pfad für Skripte: abschaltbar, optional mit eigener Basic Auth."""
        cfg = self.app.cfg
        if not cfg.export_enabled:
            return self._text(HTTPStatus.NOT_FOUND, "Nicht gefunden")
        if cfg.export_protected:
            scheme, _, credentials = self.headers.get("Authorization", "").partition(" ")
            user = password = ""
            if scheme.lower() == "basic":
                try:
                    user, _, password = base64.b64decode(credentials).decode().partition(":")
                except ValueError:
                    pass
            if not self.app.check_export_auth(user, password):
                return self._send(HTTPStatus.UNAUTHORIZED, "text/plain; charset=utf-8", b"Anmeldung erforderlich\n",
                                  {"WWW-Authenticate": 'Basic realm="export", charset="UTF-8"'})
        self._send_export()

    def download(self) -> None:
        """Download aus der Oberfläche; geschützt durch den Login der Oberfläche."""
        self._send_export()

    def _send_export(self) -> None:
        fmt = self.params.get("format") or "csv"
        if fmt not in export.FORMATS:
            return self._text(HTTPStatus.BAD_REQUEST, f"Unbekanntes Format: {fmt}")
        try:
            filters = self._filters()
        except ValueError as exc:
            return self._text(HTTPStatus.BAD_REQUEST, str(exc))
        with self._storage() as storage:
            rows = self._rows(storage, filters)
        content_type, encoding = export.FORMATS[fmt]
        out = io.StringIO(newline="")
        export.write(fmt, rows, out)
        filename = f"umsaetze-{date.today().isoformat()}.{fmt}"
        self._send(HTTPStatus.OK, f"{content_type}; charset=utf-8", out.getvalue().encode(encoding),
                   {"Content-Disposition": f'attachment; filename="{filename}"'})

    # --- Umsätze -----------------------------------------------------------

    def transactions_page(self) -> None:
        error = ""
        try:
            filters = self._filters()
        except ValueError as exc:
            filters, error = dict.fromkeys(FILTERS, ""), str(exc)
        with self._storage() as s:
            accounts = self._filter_accounts(s, filters["account"])
            rows = self._rows(s, filters, newest_first=True)
        labels = {a["uid"]: a["iban"] or a["name"] or a["uid"] for a, _ in accounts}
        form = self._filter_form("/transactions", filters, accounts, "Anzeigen")

        query = urlencode({k: v for k, v in filters.items() if v})
        sep = "&amp;" if query else ""
        downloads = "".join(
            f'<a class="button secondary" href="/download?{esc(query)}{sep}format={fmt}">{fmt.upper()}</a>'
            for fmt in FORMATS)
        count = "1 Umsatz" if len(rows) == 1 else f"{len(rows)} Umsätze"
        summary = f"<p><strong>{count}</strong>{totals(rows)}</p>"
        if len(rows) > DISPLAY_LIMIT:
            summary += (f'<p class="hint">Angezeigt werden die neuesten {DISPLAY_LIMIT}. '
                        "Der Export enthält alle Umsätze der Auswahl.</p>")
        listing = (tx_table(rows[:DISPLAY_LIMIT], labels if len(labels) > 1 else None) if rows
                   else '<p class="muted">Keine Umsätze für diese Auswahl.</p>')
        result = (summary + f'<div class="buttons" style="margin-bottom:1rem">'
                  f'<span>Auswahl herunterladen:</span>{downloads}</div>' + listing)
        self._page("Umsätze", card("Filter", form) + card("Umsätze", result), error=error)

    # --- Export-Seite ------------------------------------------------------

    def export_page(self) -> None:
        cfg = self.app.cfg
        error = ""
        try:
            filters = self._filters()
        except ValueError as exc:
            filters, error = dict.fromkeys(FILTERS, ""), str(exc)
        with self._storage() as s:
            accounts = self._filter_accounts(s, filters["account"])
            count = len(self._rows(s, filters))

        base = self._base_url()
        endpoint = "/export" if cfg.export_enabled else "/download"

        def url(params: dict[str, str]) -> str:
            return f"{base}{endpoint}?{urlencode({k: v for k, v in params.items() if v})}"

        selection = {k: v for k, v in filters.items() if v}
        rows = "".join(
            f'<tr><th>{fmt.upper()}</th><td><input class="url" id="link-{fmt}" readonly '
            f'value="{esc(url(selection | {"format": fmt}))}"></td><td class="actions">'
            f'<a class="button" href="{esc(url(selection | {"format": fmt}))}">Öffnen</a>'
            f'<button type="button" class="secondary" data-copy="link-{fmt}">Kopieren</button></td></tr>'
            for fmt in FORMATS)
        found = "1 Umsatz" if count == 1 else f"{count} Umsätze"
        links = (f"<p>Die Links enthalten die aktuelle Auswahl ({found}{describe(filters)}) und liefern "
                 f"bei jedem Aufruf den aktuellen Stand.</p>"
                 f'<div class="table"><table class="links">{rows}</table></div>{self._export_access()}')

        self._page("Export", card("Auswahl", self._filter_form("/export/links", filters, accounts,
                                                               "Links erzeugen"))
                   + card("Export-Links", links) + self._examples_card(url, accounts)
                   + self._parameters_card() + COPY_SCRIPT, error=error)

    def _export_access(self) -> str:
        cfg = self.app.cfg
        if not cfg.export_enabled:
            return ('<p class="warn">Der Export-Pfad <code>/export</code> ist abgeschaltet '
                    "(<code>EB_EXPORT_ENABLED=false</code>). Die Links zeigen deshalb auf <code>/download</code> "
                    "und funktionieren nur angemeldet in dieser Oberfläche.</p>")
        if cfg.export_protected:
            return ("<p class=\"hint\">Geschützt per Basic Auth: Beim Öffnen fragt der Browser nach Benutzer und "
                    "Passwort aus <code>EB_EXPORT_USER</code> / <code>EB_EXPORT_PASSWORD</code>.</p>")
        return ('<p class="hint"><span class="warn">Ohne Anmeldung abrufbar.</span> Mit <code>EB_EXPORT_USER</code> '
                "und <code>EB_EXPORT_PASSWORD</code> lässt sich der Pfad per Basic Auth schützen.</p>")

    def _examples_card(self, url, accounts) -> str:
        today = date.today()
        month_end = today.replace(day=1) - timedelta(days=1)
        month_start = month_end.replace(day=1)
        examples = [
            ("Alle Umsätze", {"format": "csv"}),
            (f"Letzte 5 Tage (ab {(today - timedelta(days=4)):%d.%m.%Y})", {"days": "5", "format": "csv"}),
            ("Letzte 30 Tage als JSON", {"days": "30", "format": "json"}),
            (f"Vormonat ({MONTHS[month_start.month - 1]} {month_start.year}) als XML",
             {"date_from": month_start.isoformat(), "date_to": month_end.isoformat(), "format": "xml"}),
            (f"Seit Jahresbeginn {today.year}", {"date_from": f"{today.year}-01-01", "format": "csv"}),
            ("Suche nach „Miete“ in den letzten 90 Tagen", {"days": "90", "q": "Miete", "format": "csv"}),
        ]
        if accounts:
            a = accounts[0][0]
            examples.insert(3, (f"Nur Konto {a['iban'] or a['name'] or a['uid']}, letzte 5 Tage",
                                {"account": a["iban"] or a["uid"], "days": "5", "format": "csv"}))
        rows = "".join(
            f'<tr><td>{esc(text)}</td><td><a href="{esc(url(params))}"><code>{esc(url(params))}</code></a></td></tr>'
            for text, params in examples)
        auth = "-u BENUTZER:PASSWORT " if self.app.cfg.export_protected else ""
        commands = "\n".join((
            "# Letzte 5 Tage als CSV speichern",
            f"curl {auth}-o umsaetze.csv '{url({'days': '5', 'format': 'csv'})}'",
            "",
            "# Vormonat als JSON",
            f"curl {auth}-o vormonat.json '{url({'date_from': month_start.isoformat(), 'date_to': month_end.isoformat(), 'format': 'json'})}'",
        ))
        return card("Beispiele", (
            f'<div class="table"><table><tr><th>Beschreibung</th><th>Link</th></tr>{rows}</table></div>'
            f"<p>Für Skripte, z. B. als täglicher Cron-Job:</p><pre>{esc(commands)}</pre>"))

    @staticmethod
    def _parameters_card() -> str:
        params = (
            ("format", "<code>csv</code> (Standard), <code>json</code> oder <code>xml</code>"),
            ("days", "Nur die letzten N Tage, z. B. <code>days=5</code> = heute und die 4 Tage davor"),
            ("date_from", "Ab diesem Datum (<code>JJJJ-MM-TT</code>); nicht zusammen mit <code>days</code>"),
            ("date_to", "Bis zu diesem Datum (<code>JJJJ-MM-TT</code>), einschließlich"),
            ("account", "Nur dieses Konto: IBAN (bleibt auch nach dem Neuverbinden der Bank gleich) "
                        "oder Konto-ID"),
            ("q", "Suchbegriff in Gegenpartei oder Verwendungszweck"),
        )
        rows = "".join(f"<tr><th><code>{name}</code></th><td>{text}</td></tr>" for name, text in params)
        return card("Parameter", (
            f'<div class="table"><table>{rows}</table></div>'
            '<p class="hint">Alle Parameter sind optional und lassen sich kombinieren. Maßgeblich ist das '
            "Buchungsdatum, ersatzweise Transaktions- oder Valutadatum.</p>"))

    # --- Rechtstexte -------------------------------------------------------

    def privacy(self) -> None:
        self._legal("privacy")

    def terms(self) -> None:
        self._legal("terms")

    def _legal(self, name: str) -> None:
        text = (resources.files(__package__) / "templates" / f"{name}.html").read_bytes()
        self._send(HTTPStatus.OK, "text/html; charset=utf-8", text)

    # --- Antworten ---------------------------------------------------------

    def _page(self, title: str, content: str, *, error: str = "", refresh: bool = False,
              status: HTTPStatus = HTTPStatus.OK, nav: bool = True) -> None:
        notice = MESSAGES.get(self.params.get("msg", ""), "")
        body = layout(title, content, notice=notice, error=error, refresh=refresh, nav=nav,
                      logout=nav and self.app.cfg.login_required)
        self._send(status, "text/html; charset=utf-8", body.encode())

    def _redirect(self, location: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _text(self, status: HTTPStatus, message: str) -> None:
        self._send(status, "text/plain; charset=utf-8", f"{message}\n".encode())

    def _send(self, status: HTTPStatus, content_type: str, body: bytes,
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # Ohne Query-String protokollieren, damit der Autorisierungscode nicht im Log landet.
        log.info("%s %s %s %s", self.address_string(), self.command, urlparse(self.path).path,
                 getattr(code, "value", code))

    def log_message(self, format: str, *args: object) -> None:
        log.info("%s %s", self.address_string(), format % args)


def make_server(cfg: Config, sync: SyncService | None = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": App(cfg, sync)})
    return ThreadingHTTPServer((cfg.web_host, cfg.web_port), handler)


def start_in_background(cfg: Config, sync: SyncService | None = None) -> ThreadingHTTPServer:
    server = make_server(cfg, sync)
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    host, port = server.server_address[:2]
    log.info("Weboberfläche erreichbar unter http://%s:%d/", host, port)
    if bool(cfg.web_user) != bool(cfg.web_password):
        log.warning("Für den Login müssen EB_WEB_USER und EB_WEB_PASSWORD beide gesetzt sein.")
    if not cfg.login_required:
        log.warning("Kein Login konfiguriert; die Weboberfläche ist ohne Zugriffsschutz erreichbar.")
    if cfg.export_enabled:
        if bool(cfg.export_user) != bool(cfg.export_password):
            log.warning("Für den Schutz des Exports müssen EB_EXPORT_USER und EB_EXPORT_PASSWORD "
                        "beide gesetzt sein.")
        if not cfg.export_protected:
            log.warning("Der Export unter /export ist ohne Anmeldung abrufbar.")
    return server
