"""Weboberfläche zum Export der gespeicherten Umsätze."""

from __future__ import annotations

import base64
import hmac
import html
import io
import logging
import threading
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import export
from .config import Config
from .storage import Storage

log = logging.getLogger(__name__)

PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Umsatzexport</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 40rem; margin: 2rem auto; padding: 0 1rem; }}
  label {{ display: block; margin-top: 1rem; font-weight: 600; }}
  select, input {{ font: inherit; padding: .3rem; width: 100%; box-sizing: border-box; }}
  fieldset {{ border: none; padding: 0; margin-top: 1rem; }}
  fieldset label {{ display: inline; font-weight: normal; margin-right: 1rem; }}
  fieldset input {{ width: auto; }}
  button {{ font: inherit; margin-top: 1.5rem; padding: .5rem 1.5rem; }}
  .hint {{ color: #666; font-size: .9rem; }}
</style>
</head>
<body>
<h1>Umsatzexport</h1>
<form action="export" method="get">
  <label for="account">Konto</label>
  <select id="account" name="account">
    <option value="">Alle Konten ({total} Umsätze)</option>
    {accounts}
  </select>
  <label for="date_from">Von</label>
  <input type="date" id="date_from" name="date_from">
  <label for="date_to">Bis</label>
  <input type="date" id="date_to" name="date_to">
  <fieldset>
    <legend><strong>Format</strong></legend>
    <label><input type="radio" name="format" value="csv" checked> CSV</label>
    <label><input type="radio" name="format" value="json"> JSON</label>
    <label><input type="radio" name="format" value="xml"> XML</label>
  </fieldset>
  <button type="submit">Herunterladen</button>
</form>
<p class="hint">Direkter Abruf: <code>/export?format=csv&amp;account=&lt;uid&gt;&amp;date_from=YYYY-MM-DD&amp;date_to=YYYY-MM-DD</code>
mit Header <code>Authorization: Bearer &lt;token&gt;</code></p>
</body>
</html>
"""


class ExportHandler(BaseHTTPRequestHandler):
    cfg: Config  # wird in make_server gesetzt

    def do_GET(self) -> None:
        if not self._authorized():
            return
        url = urlparse(self.path)
        if url.path == "/":
            self._index()
        elif url.path == "/export":
            self._export(parse_qs(url.query))
        else:
            self._error(HTTPStatus.NOT_FOUND, "Nicht gefunden")

    def _authorized(self) -> bool:
        """Prüft das Token aus EB_WEB_TOKEN.

        Skripte senden es als ``Authorization: Bearer <token>``. Browser können beim Aufruf
        einer Seite keine eigenen Header setzen; dort wird das Token als Passwort der
        Basic-Auth-Anmeldung akzeptiert (Benutzername beliebig).
        """
        if not self.cfg.web_token:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "EB_WEB_TOKEN ist nicht gesetzt.")
            return False
        scheme, _, credentials = self.headers.get("Authorization", "").partition(" ")
        token = ""
        if scheme.lower() == "bearer":
            token = credentials.strip()
        elif scheme.lower() == "basic":
            try:
                token = base64.b64decode(credentials).decode().partition(":")[2]
            except ValueError:
                pass
        if token and hmac.compare_digest(token.encode(), self.cfg.web_token.encode()):
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        # Löst im Browser den Anmeldedialog aus; Skripte nutzen stattdessen Bearer.
        self.send_header("WWW-Authenticate", 'Basic realm="enable-banking", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _index(self) -> None:
        with Storage(self.cfg.db_path) as storage:
            accounts = storage.accounts()
            counts = {a["uid"]: storage.count_transactions(a["uid"]) for a in accounts}
        options = "\n    ".join(
            f'<option value="{html.escape(a["uid"])}">'
            f'{html.escape(a["iban"] or a["uid"])} {html.escape(a["name"] or "")}'
            f' ({counts[a["uid"]]} Umsätze)</option>'
            for a in accounts
        )
        body = PAGE.format(accounts=options, total=sum(counts.values()))
        self._send(HTTPStatus.OK, "text/html; charset=utf-8", body.encode())

    def _export(self, query: dict[str, list[str]]) -> None:
        def param(name: str) -> str | None:
            return (query.get(name, [""])[0]).strip() or None

        fmt = param("format") or "csv"
        if fmt not in export.FORMATS:
            return self._error(HTTPStatus.BAD_REQUEST, f"Unbekanntes Format: {fmt}")
        dates = {}
        for name in ("date_from", "date_to"):
            value = param(name)
            try:
                dates[name] = value and date.fromisoformat(value).isoformat()
            except ValueError:
                return self._error(HTTPStatus.BAD_REQUEST, f"Ungültiges Datum für {name}: {value}")

        with Storage(self.cfg.db_path) as storage:
            rows = storage.transactions(param("account"), dates["date_from"], dates["date_to"])
        content_type, encoding = export.FORMATS[fmt]
        out = io.StringIO(newline="")
        export.write(fmt, rows, out)
        filename = f"umsaetze-{date.today().isoformat()}.{fmt}"
        self._send(
            HTTPStatus.OK,
            f"{content_type}; charset=utf-8",
            out.getvalue().encode(encoding),
            {"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send(status, "text/plain; charset=utf-8", f"{message}\n".encode())

    def _send(self, status: HTTPStatus, content_type: str, body: bytes,
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        log.info("%s %s", self.address_string(), format % args)


def make_server(cfg: Config) -> ThreadingHTTPServer:
    handler = type("Handler", (ExportHandler,), {"cfg": cfg})
    return ThreadingHTTPServer((cfg.web_host, cfg.web_port), handler)


def start_in_background(cfg: Config) -> ThreadingHTTPServer:
    server = make_server(cfg)
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    host, port = server.server_address[:2]
    log.info("Exportseite erreichbar unter http://%s:%d/", host, port)
    if not cfg.web_token:
        log.warning("EB_WEB_TOKEN ist nicht gesetzt; die Exportseite lehnt alle Zugriffe ab.")
    return server
