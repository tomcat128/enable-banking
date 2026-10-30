# enable-banking-sync

Ruft Kontoumsätze und Salden automatisiert über die [Enable Banking API](https://enablebanking.com/docs/api/reference/)
ab und speichert sie lokal in einer SQLite-Datenbank. Läuft als Docker-Container und ruft periodisch
(Standard: alle 6 Stunden) inkrementell neue Umsätze ab.

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

Die Authentifizierung gegenüber der API erfolgt per RS256-signiertem JWT mit dem privaten Schlüssel der
Enable-Banking-Anwendung.

## Einrichtung

```bash
cp .env.example .env            # Werte eintragen (Application ID, Redirect-URL, Bank)
mkdir -p secrets data
cp /pfad/zu/<application-id>.pem secrets/private.pem
chmod 600 secrets/private.pem
sudo chown 1000:1000 data       # der Container läuft als UID 1000
docker compose build
```

Ist die eigene UID nicht 1000 (`id -u`), kann der Container den Schlüssel trotz Mount nicht lesen
(`Permission denied: '/secrets/private.pem'`). Dann UID 1000 gezielt Leserecht geben:
`setfacl -m u:1000:r secrets/private.pem` (oder `sudo chown 1000 secrets/private.pem`).

Verbindung testen und Banknamen ermitteln:

```bash
docker compose run --rm enable-banking info
docker compose run --rm enable-banking banks --country DE --search sparkasse
```

Den exakten Banknamen in `.env` als `EB_ASPSP_NAME` eintragen.

## Konto autorisieren

```bash
docker compose run --rm enable-banking authorize
```

Die ausgegebene URL im Browser öffnen, bei der Bank anmelden und freigeben. Danach die komplette URL aus
der Adresszeile (die Seite der Redirect-URL muss dafür nicht erreichbar sein) in die Konsole einfügen.
Alternativ: `authorize --code "<redirect-url-oder-code>"`.

Die Zustimmung ist je nach Bank bis zu 90/180 Tage gültig (`EB_ACCESS_DAYS`). Sieben Tage vor Ablauf
erscheinen Warnungen im Log; danach `authorize` erneut ausführen.

## Betrieb

```bash
docker compose up -d                                   # periodischer Abruf
docker compose logs -f                                 # Logs ansehen
docker compose run --rm enable-banking fetch           # sofortiger Abruf
docker compose run --rm enable-banking fetch --date-from 2026-01-01
docker compose run --rm enable-banking status          # Session, Konten, Salden, letzter Abruf
docker compose run --rm enable-banking export --format csv -o /data/umsaetze.csv
```

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
