# enable-banking-sync

Ruft Kontoumsätze und Salden automatisiert über die [Enable Banking API](https://enablebanking.com/docs/api/reference/)
ab und speichert sie lokal in einer SQLite-Datenbank. Alles läuft in einem einzigen Docker-Container:
Er ruft periodisch (Standard: alle 6 Stunden) inkrementell neue Umsätze ab und stellt eine Webseite
bereit, über die sich die Umsätze als CSV, JSON oder XML exportieren lassen. Die Datenbank legt die
Anwendung beim ersten Start selbst im Datenverzeichnis an.

## Funktionsweise

1. **Autorisierung (einmalig, bzw. bei Ablauf der Zustimmung):** Über `POST /auth` wird ein Link zur Bank
   erzeugt. Nach der Freigabe leitet die Bank auf die hinterlegte Redirect-URL mit `?code=...` weiter.
   Mit diesem Code wird per `POST /sessions` eine Session angelegt; Session und freigegebene Konten werden
   in der Datenbank gespeichert.
2. **Abruf:** Für jedes Konto der aktiven Session werden über `GET /accounts/{uid}/transactions`
   (inkl. Paginierung per `continuation_key`) die Umsätze geholt und über `GET /accounts/{uid}/balances`
   die Salden. Der erste Abruf reicht `EB_INITIAL_DAYS` zurück, danach wird ab der letzten Buchung minus
   `EB_OVERLAP_DAYS` abgerufen.
3. **Speicherung:** Umsätze werden über `entry_reference` / `transaction_id` (Fallback: Hash der
   Umsatzdaten) dedupliziert. Vorgemerkte Umsätze (`PDNG`) werden bei jedem Abruf ersetzt. Die
   Original-JSON-Antwort jedes Umsatzes bleibt in der Spalte `raw` erhalten.
4. **Export:** Die Exportseite (Port 8080, mit Token geschützt) liest direkt aus der Datenbank.

Die Authentifizierung gegenüber der API erfolgt per RS256-signiertem JWT mit dem privaten Schlüssel der
Enable-Banking-Anwendung.

## Einrichtung

```bash
cp .env.example .env            # Werte eintragen (Application ID, Redirect-URL, Bank)
mkdir -p secrets data
cp /pfad/zu/<application-id>.pem secrets/private.pem
chmod 600 secrets/private.pem
sudo chown 1000:1000 data       # der Container läuft als UID 1000
docker compose up -d --build
```

Datenverzeichnis und Schlüssel werden von außen eingebunden. Ohne weitere Angaben sind das `./data` und
`./secrets/private.pem`; andere Host-Pfade lassen sich in `.env` über `EB_DATA_PATH` und `EB_KEY_PATH`
festlegen.

Ist die eigene UID nicht 1000 (`id -u`), kann der Container den Schlüssel trotz Mount nicht lesen
(`Permission denied: '/secrets/private.pem'`). Dann UID 1000 gezielt Leserecht geben:
`setfacl -m u:1000:r secrets/private.pem` (oder `sudo chown 1000 secrets/private.pem`).

Alle weiteren Befehle laufen per `docker compose exec` im bereits laufenden Container. Verbindung
testen und Banknamen ermitteln:

```bash
docker compose exec enable-banking enable-banking info
docker compose exec enable-banking enable-banking banks --country DE --search sparkasse
```

Den exakten Banknamen in `.env` als `EB_ASPSP_NAME` eintragen und den Container mit
`docker compose up -d` neu erstellen, damit er die Änderung übernimmt.

## Konto autorisieren

```bash
docker compose exec enable-banking enable-banking authorize
```

Die ausgegebene URL im Browser öffnen, bei der Bank anmelden und freigeben. Danach die komplette URL aus
der Adresszeile (die Seite der Redirect-URL muss dafür nicht erreichbar sein) in die Konsole einfügen.
Alternativ: `authorize --code "<redirect-url-oder-code>"`. Solange keine Session existiert, prüft der
Container jede Minute erneut und startet den ersten Abruf direkt nach der Autorisierung.

### Seite für die Redirect-URL (optional)

`redirect/` enthält eine kleine PHP-Seite, die als Ziel der Redirect-URL dient. Sie zeigt die
vollständige URL samt `?code=...` zum Kopieren an, dazu den fertigen `authorize --code`-Befehl. Sie
speichert nichts, und das Image protokolliert Zugriffe ohne Query-String, damit der Code nicht im Log
landet. Daneben liegen eine Datenschutzerklärung und Nutzungsbedingungen (jeweils Deutsch und Englisch),
die bei der Registrierung der Anwendung im Control Panel angegeben werden können.

| Datei | Adresse |
|---|---|
| `html/callback/callback.php` | `/callback/` bzw. `/callback/callback.php` |
| `html/callback/privacy.html` | `/callback/privacy.html` |
| `html/callback/terms.html` | `/callback/terms.html` |

In beiden HTML-Dateien vor dem Bauen die Platzhalter `{{NAME}}`, `{{ADRESSE}}` und `{{EMAIL}}` ersetzen.

```bash
docker build -t enable-banking-redirect redirect/
docker run -d --name enable-banking-redirect -p 8081:80 --restart unless-stopped enable-banking-redirect
```

Die öffentliche Adresse (z. B. `https://bank.example.org/callback/`) als Redirect-URL im Enable Banking
Control Panel und als `EB_REDIRECT_URL` eintragen. Läuft ein Reverse Proxy mit HTTPS davor, muss er
`X-Forwarded-Proto: https` setzen, damit die angezeigte URL mit `https://` beginnt. Alternativ lässt sich
der Inhalt von `redirect/html/` auf jeden vorhandenen PHP-Webserver kopieren.

Die Zustimmung ist je nach Bank bis zu 90/180 Tage gültig (`EB_ACCESS_DAYS`). Sieben Tage vor Ablauf
erscheinen Warnungen im Log; danach `authorize` erneut ausführen.

Bei jeder Autorisierung vergibt die API neue Konto-UIDs. Bereits gespeicherte Umsätze, Salden und
Abrufprotokolle werden anhand des `identification_hash` dem Konto der neuen Session zugeordnet, sodass
der Abruf inkrementell weiterläuft und keine Umsätze doppelt gespeichert werden.

## Betrieb

```bash
docker compose up -d                                               # Abruf + Exportseite
docker compose logs -f                                             # Logs ansehen
docker compose exec enable-banking enable-banking fetch            # sofortiger Abruf
docker compose exec enable-banking enable-banking fetch --date-from 2026-01-01
docker compose exec enable-banking enable-banking status           # Session, Konten, Salden, letzter Abruf
docker compose exec enable-banking enable-banking export --format xml -o /data/umsaetze.xml
```

## Exportseite

Unter `http://<server>:8080/` lassen sich Konto, Zeitraum und Format (CSV, JSON, XML) wählen. Der
Export ist auch direkt abrufbar, z. B. für Skripte:

```bash
curl -H "Authorization: Bearer $EB_WEB_TOKEN" -o umsaetze.csv \
  'http://localhost:8080/export?format=csv&account=<uid>&date_from=2026-01-01&date_to=2026-06-30'
```

Alle Parameter sind optional (Standard: CSV, alle Konten, gesamter Zeitraum).

- **CSV:** Semikolon als Trennzeichen, Textfelder in Anführungszeichen, UTF-8 mit BOM (für Excel)
- **JSON:** die Original-Antwort der Bank je Umsatz, ergänzt um `account_uid`
- **XML:** ein `<transaction>`-Element je Umsatz mit denselben Feldern wie die CSV; leere Felder entfallen

### Zugriffsschutz

Die Seite ist im Netzwerk erreichbar und wird durch das Token aus `EB_WEB_TOKEN` geschützt (erzeugen z. B.
mit `openssl rand -hex 32`). Ist kein Token gesetzt, lehnt die Seite jeden Zugriff ab.

- **Skripte und andere Anwendungen** senden den Header `Authorization: Bearer <token>`.
- **Browser** können beim Aufruf einer Seite keine eigenen Header mitsenden. Sie zeigen stattdessen
  einen Anmeldedialog: Benutzername beliebig, als Passwort das Token.

Die Verbindung ist unverschlüsseltes HTTP. Für Zugriffe über fremde Netze gehört ein Reverse Proxy mit
HTTPS davor. Soll die Seite nur vom Server selbst erreichbar sein, `EB_WEB_BIND=127.0.0.1` setzen.

## Konfiguration

| Variable | Standard | Beschreibung |
|---|---|---|
| `EB_APPLICATION_ID` | – (Pflicht) | Application ID aus dem Control Panel |
| `EB_PRIVATE_KEY_PATH` | `/secrets/private.pem` | Privater Schlüssel der Anwendung |
| `EB_REDIRECT_URL` | – | Bei der Anwendung registrierte Redirect-URL |
| `EB_ASPSP_NAME` | – | Name der Bank (siehe `banks`) |
| `EB_ASPSP_COUNTRY` | `DE` | Land der Bank |
| `EB_PSU_TYPE` | `personal` | `personal` oder `business` |
| `EB_ACCESS_DAYS` | `180` | Gewünschte Gültigkeit der Zustimmung |
| `EB_FETCH_INTERVAL_SECONDS` | `21600` | Abrufintervall im `run`-Modus |
| `EB_INITIAL_DAYS` | `90` | Zeitraum des ersten Abrufs |
| `EB_OVERLAP_DAYS` | `10` | Überlappung bei Folgeabrufen |
| `EB_DATA_DIR` | `/data` | Ablageort der Datenbank `banking.sqlite3` |
| `EB_API_BASE` | `https://api.enablebanking.com` | API-Basis-URL |
| `EB_LOG_LEVEL` | `INFO` | Log-Level |
| `EB_WEB_TOKEN` | – | Zugriffstoken der Exportseite; ohne Angabe ist die Seite gesperrt |
| `EB_WEB_PORT` | `8080` | Port der Exportseite im Container |

Nur für `docker compose` (Host-Seite):

| Variable | Standard | Beschreibung |
|---|---|---|
| `EB_DATA_PATH` | `./data` | Host-Verzeichnis für die Datenbank |
| `EB_KEY_PATH` | `./secrets/private.pem` | Host-Pfad des privaten Schlüssels |
| `EB_WEB_BIND` | `0.0.0.0` | Netzwerkadresse der Exportseite auf dem Host |
| `EB_WEB_HOST_PORT` | `8080` | Port der Exportseite auf dem Host |

Hinweis: Banken erlauben ohne aktive Nutzerinteraktion (PSD2) häufig nur 4 Abrufe pro Tag und Konto.

## Datenbank

`data/banking.sqlite3` enthält die Tabellen `sessions`, `accounts`, `transactions`, `balances` und
`fetch_runs`. Beispiel:

```bash
sqlite3 data/banking.sqlite3 \
  "SELECT booking_date, amount, currency, counterparty, remittance FROM transactions ORDER BY booking_date DESC LIMIT 20"
```

## Entwicklung

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
