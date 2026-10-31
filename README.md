# enable-banking-sync

Ruft Kontoumsätze und Salden automatisiert über die [Enable Banking API](https://enablebanking.com/docs/api/reference/)
ab und speichert sie lokal in einer SQLite-Datenbank. Alles läuft in einem einzigen Docker-Container mit
Weboberfläche: Einrichtung, Bankverbindung, Abruf und Export. Der Container ruft außerdem periodisch
(Standard: alle 6 Stunden) neue Umsätze ab.

## Schnellstart

```bash
mkdir -p data && sudo chown 1000:1000 data   # der Container läuft als UID 1000
docker compose up -d --build
```

Dann `http://<server>:8080/` im Browser öffnen und den drei Schritten folgen.

### 1. Einstellungen

Unter **Einstellungen** eintragen:

- **Application ID** und **privater Schlüssel** aus dem
  [Enable Banking Control Panel](https://enablebanking.com/cp/applications). Den Schlüssel als Datei
  `<application-id>.pem` auswählen oder seinen Inhalt einfügen.
- **Redirect-URL**: ist mit der Adresse vorbelegt, unter der die Seite gerade aufgerufen wird
  (`…/callback`). Hinter einem Reverse Proxy die öffentliche HTTPS-Adresse verwenden.

Die Seite zeigt außerdem die Adressen an, die bei der Anwendung im Control Panel einzutragen sind:
Redirect URL, Privacy URL (`/privacy`) und Terms URL (`/terms`). **Verbindung testen** prüft Application ID
und Schlüssel und meldet, ob die Redirect-URL im Control Panel registriert ist.

### 2. Bank verbinden

Unter **Bank verbinden** die Bank aus der Liste wählen und **Weiter zur Bank** klicken. Nach Anmeldung und
Freigabe leitet die Bank zurück; die Verbindung wird gespeichert und der erste Abruf startet sofort.

Hat die Bank auf eine andere Adresse zurückgeleitet, lässt sich die komplette URL aus der Adresszeile auf
derselben Seite unter **Rückleitung hat nicht geklappt?** einfügen.

Die Freigabe gilt je nach Bank bis zu 90 oder 180 Tage. Die Übersicht zeigt, wie lange noch; danach die Bank
einfach erneut verbinden. Bereits gespeicherte Umsätze werden dabei anhand des `identification_hash` dem
Konto der neuen Verbindung zugeordnet, es entstehen keine Dubletten.

### 3. Übersicht und Export

Die **Übersicht** zeigt Verbindung, Konten mit Salden, den Stand des Abrufs und die letzten Umsätze.
**Jetzt abrufen** startet einen Abruf sofort. Banken erlauben ohne erneute Anmeldung (PSD2) häufig nur
4 Abrufe pro Tag und Konto.

Der **Export** liefert die Umsätze nach Konto und Zeitraum als CSV, JSON oder XML:

- **CSV:** Semikolon als Trennzeichen, Textfelder in Anführungszeichen, UTF-8 mit BOM (für Excel)
- **JSON:** die Original-Antwort der Bank je Umsatz, ergänzt um `account_uid`
- **XML:** ein `<transaction>`-Element je Umsatz mit denselben Feldern wie die CSV; leere Felder entfallen

Für Skripte ist der Export direkt abrufbar (alle Parameter optional):

```bash
curl -o umsaetze.csv 'http://<server>:8080/export?format=csv&account=<uid>&date_from=2026-01-01&date_to=2026-06-30'
```

## Hinter Caddy

Enable Banking verlangt für die Redirect-URL in der Regel HTTPS. Läuft Caddy als Container, den
enable-banking-Container in dessen Docker-Netz hängen und weiterleiten:

```
bank.example.org {
    reverse_proxy enable-banking:8080
}
```

Caddy setzt `X-Forwarded-Proto` und `Host`, die Oberfläche schlägt deshalb automatisch
`https://bank.example.org/callback` als Redirect-URL vor.

## Absicherung

Ohne weitere Angaben ist die Oberfläche **ohne Zugriffsschutz** erreichbar. Mit `EB_WEB_TOKEN`
(z. B. `openssl rand -hex 32`) verlangt sie ein Token: Browser fragen es als Passwort ab (Benutzername
beliebig), Skripte senden `Authorization: Bearer <token>`. Datenschutz und Nutzungsbedingungen bleiben
öffentlich, weil Enable Banking sie abruft.

## Konfiguration

Alles Nötige wird in der Weboberfläche eingetragen und in der Datenbank gespeichert. Optional lassen sich
Werte über eine `.env` (Vorlage: `.env.example`) vorbelegen; Einträge aus der Weboberfläche haben Vorrang.

| Variable | Standard | Beschreibung |
|---|---|---|
| `EB_APPLICATION_ID` | – | Vorbelegung der Application ID |
| `EB_PRIVATE_KEY_PATH` | `/secrets/private.pem` | Schlüsseldatei, falls kein Schlüssel in der Oberfläche hinterlegt ist |
| `EB_REDIRECT_URL` | – | Vorbelegung der Redirect-URL |
| `EB_ASPSP_NAME` | – | Vorbelegung der Bank |
| `EB_ASPSP_COUNTRY` | `DE` | Vorbelegung des Landes |
| `EB_PSU_TYPE` | `personal` | Vorbelegung der Kontoart (`personal` oder `business`) |
| `EB_ACCESS_DAYS` | `180` | Gewünschte Gültigkeit der Freigabe (wird auf das Maximum der Bank begrenzt) |
| `EB_FETCH_INTERVAL_SECONDS` | `21600` | Abrufintervall |
| `EB_INITIAL_DAYS` | `90` | Zeitraum des ersten Abrufs |
| `EB_OVERLAP_DAYS` | `10` | Überlappung bei Folgeabrufen |
| `EB_WEB_TOKEN` | – | Optionales Zugriffstoken der Oberfläche |
| `EB_LOG_LEVEL` | `INFO` | Log-Level |

Nur für `docker compose` (Host-Seite): `EB_DATA_PATH` (Datenverzeichnis, Standard `./data`),
`EB_WEB_BIND` (Standard `0.0.0.0`) und `EB_WEB_HOST_PORT` (Standard `8080`).

## Kommandozeile (optional)

Alle Funktionen gibt es zusätzlich als Befehl im laufenden Container, z. B.:

```bash
docker compose exec enable-banking enable-banking status
docker compose exec enable-banking enable-banking fetch --date-from 2026-01-01
docker compose exec enable-banking enable-banking export --format xml -o /data/umsaetze.xml
```

## Datenbank

`data/banking.sqlite3` enthält die Tabellen `settings`, `sessions`, `accounts`, `transactions`, `balances`
und `fetch_runs`. Die Original-JSON-Antwort jedes Umsatzes bleibt in der Spalte `raw` erhalten.

## Entwicklung

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
