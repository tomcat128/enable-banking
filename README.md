# enable-banking-sync

Ruft Kontoumsätze und Salden automatisiert über die [Enable Banking API](https://enablebanking.com/docs/api/reference/)
ab und speichert sie lokal in einer SQLite-Datenbank. Alles läuft in einem einzigen Docker-Container mit
Weboberfläche: Einrichtung, Bankverbindung, Abruf und Export. Der Container ruft außerdem zu festen
Uhrzeiten (Standard: täglich um 12:00 Uhr) neue Umsätze ab.

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

### 3. Übersicht und Umsätze

Die **Übersicht** zeigt Verbindung, Konten mit Salden, den Stand des Abrufs und die letzten Umsätze.
**Jetzt abrufen** startet einen Abruf sofort.

Unter **Umsätze** lassen sich alle Umsätze nach Konto, Zeitraum (oder „letzte N Tage“) und Suchbegriff
(Gegenpartei, Verwendungszweck) filtern, mit Summe der Einnahmen und Ausgaben. Die Auswahl lässt sich als
CSV, JSON oder XML herunterladen.

Unter **Export** stehen für dieselbe Auswahl fertige Links für CSV, JSON und XML zum Öffnen und Kopieren
bereit, dazu Beispiele (letzte 5 Tage, Vormonat, ein Konto, Suche, curl) und eine Übersicht der Parameter.

Die Formate:

- **CSV:** Semikolon als Trennzeichen, Textfelder in Anführungszeichen, UTF-8 mit BOM (für Excel)
- **JSON:** die Original-Antwort der Bank je Umsatz, ergänzt um `account_uid`
- **XML:** ein `<transaction>`-Element je Umsatz mit denselben Feldern wie die CSV; leere Felder entfallen

## Automatischer Abruf

Der Abruf läuft im selben Container, ein Cron-Job ist nicht nötig. Die Uhrzeiten stehen unter
**Einstellungen → Abrufzeiten** und lassen sich je Wochentag festlegen, mehrere Uhrzeiten durch Komma
getrennt. Ein leeres Feld bedeutet: an diesem Tag kein Abruf, so lassen sich Nächte und Wochenenden
auslassen. Ohne eigene Einstellung wird täglich um 12:00 Uhr abgerufen. **Jetzt abrufen** in der Übersicht
startet einen Abruf sofort. Mit mehr als 4 Uhrzeiten pro Tag warnt die Seite, weil Banken ohne erneute
Anmeldung (PSD2) häufig nur 4 Abrufe pro Tag und Konto erlauben.

Die Uhrzeiten gelten in der Zeitzone aus `TZ`, im Container standardmäßig `Europe/Berlin`. Sie lässt sich in
der `.env` ändern.

Ein Neustart oder das Neuerstellen von Container und Image (z. B. bei einem Update) löst keinen zusätzlichen
Abruf aus; es gilt die nächste eingetragene Uhrzeit. Nur wenn noch nie abgerufen wurde, startet der Abruf
sofort, sobald eine Bank verbunden ist. Eine verpasste Uhrzeit, etwa weil der Server aus war, wird nicht
nachgeholt. Der nächste Abruf holt die fehlenden Umsätze mit ab (`EB_OVERLAP_DAYS`).

Verlangt die Bank eine erneute Anmeldung (Freigabe abgelaufen oder widerrufen, API-Fehler wie
`EXPIRED_SESSION`, `REVOKED_SESSION` oder `ASPSP_PSU_ACTION_REQUIRED`), stoppt der automatische Abruf und
unternimmt keine weiteren Versuche, auch nicht nach einem Neustart. Die Übersicht zeigt das an. Er läuft
wieder, sobald die Bank neu verbunden oder er dort mit **Abruf trotzdem wieder freigeben** freigegeben wird.

Ist der Mailversand eingerichtet, erhält der Notfallkontakt in diesem Fall insgesamt höchstens 3 E-Mails
mit Link zum Neuverbinden: sofort sowie 7 und 14 Tage nach der ersten Anfrage der Bank. Sobald die Bank neu
verbunden oder der Abruf freigegeben ist, entfallen die noch offenen Erinnerungen. Schlägt der Versand fehl,
versucht die Anwendung ihn stündlich erneut. Unter **Einstellungen** lässt sich eine Test-Mail senden.

```bash
EB_SMTP_HOST=smtp.example.org
EB_SMTP_USER=banking@example.org     # zugleich Absender, falls EB_SMTP_FROM fehlt
EB_SMTP_PASSWORD=...
EB_ALERT_EMAIL=ich@example.org       # Standard für den Notfallkontakt, mehrere durch Komma getrennt
```

Standard ist STARTTLS auf Port 587; mit `EB_SMTP_PORT=465` wird SSL/TLS verwendet.

`EB_ALERT_EMAIL` ist der Standard für den Notfallkontakt. Unter **Einstellungen → Benachrichtigung per E-Mail**
lassen sich stattdessen eine oder mehrere andere Adressen eintragen; sie gelten ab der nächsten Nachricht. Ein
leeres Feld stellt den Standard wieder her.

Abrufzeiten und eigene Adressen liegen wie die übrigen Einstellungen in der Datenbank im Datenverzeichnis. Sie
bleiben deshalb bei einem Neustart und beim Neuerstellen von Container und Image erhalten, solange das
Datenverzeichnis wie in der `docker-compose.yml` eingebunden ist. Einträge aus der Weboberfläche haben Vorrang
vor der `.env`.

## Export für Skripte

Unter `/export` liefert die Anwendung dieselben Daten ohne Login der Oberfläche. Fertige Links und
Beispiele zeigt die Seite **Export**.

| Parameter | Bedeutung |
|---|---|
| `format` | `csv` (Standard), `json` oder `xml` |
| `days` | nur die letzten N Tage; `days=5` = heute und die 4 Tage davor |
| `date_from` | ab diesem Datum (`JJJJ-MM-TT`); nicht zusammen mit `days` |
| `date_to` | bis zu diesem Datum, einschließlich |
| `account` | nur dieses Konto: IBAN (bleibt beim Neuverbinden gleich) oder Konto-ID |
| `q` | Suchbegriff in Gegenpartei oder Verwendungszweck |

```bash
# Letzte 5 Tage als CSV
curl -o umsaetze.csv 'https://bank.example.org/export?days=5&format=csv'
# Oktober 2026 eines Kontos als JSON
curl -o oktober.json 'https://bank.example.org/export?account=DE89370400440532013000&date_from=2026-10-01&date_to=2026-10-31&format=json'
```

Sind `EB_EXPORT_USER` und `EB_EXPORT_PASSWORD` gesetzt, verlangt der Pfad Basic Auth
(`curl -u benutzer:passwort …`); ist nur eines von beiden gesetzt, bleibt er frei abrufbar. Mit
`EB_EXPORT_ENABLED=false` ist er abgeschaltet; der Download auf der Seite **Umsätze** funktioniert weiter.

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

## Login

Ohne weitere Angaben ist die Oberfläche **ohne Zugriffsschutz** erreichbar. Sind `EB_WEB_USER` und
`EB_WEB_PASSWORD` gesetzt, verlangt sie eine Anmeldung; die Anmeldung gilt 30 Tage und endet, sobald sich
Benutzer oder Passwort ändern. Ist nur eines von beiden gesetzt, bleibt die Oberfläche offen (mit Warnung im
Log). Ohne Login erreichbar bleiben Datenschutz und Nutzungsbedingungen (Enable Banking ruft sie ab) sowie
`/export` mit seinem eigenen Schutz.

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
| `TZ` | `Europe/Berlin` | Zeitzone der Abrufzeiten und der Anzeige |
| `EB_INITIAL_DAYS` | `90` | Zeitraum des ersten Abrufs |
| `EB_OVERLAP_DAYS` | `10` | Überlappung bei Folgeabrufen |
| `EB_WEB_USER`, `EB_WEB_PASSWORD` | – | Login der Oberfläche (nur aktiv, wenn beide gesetzt) |
| `EB_EXPORT_ENABLED` | `true` | Export-Pfad `/export` an- oder abschalten |
| `EB_EXPORT_USER`, `EB_EXPORT_PASSWORD` | – | Basic Auth für `/export` (nur aktiv, wenn beide gesetzt) |
| `EB_SMTP_HOST` | – | SMTP-Server für Benachrichtigungen |
| `EB_SMTP_PORT` | `587` | SMTP-Port (`465` bei `ssl`, `25` bei `none`) |
| `EB_SMTP_SECURITY` | `starttls` | `starttls`, `ssl` oder `none`; bei Port 465 automatisch `ssl` |
| `EB_SMTP_USER`, `EB_SMTP_PASSWORD` | – | SMTP-Anmeldung |
| `EB_SMTP_FROM` | `EB_SMTP_USER` | Absenderadresse |
| `EB_ALERT_EMAIL` | – | Standard für den Notfallkontakt, mehrere durch Komma getrennt; in der Oberfläche überschreibbar |
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
und `fetch_runs`. In `settings` liegen auch der private Schlüssel und eine gegebenenfalls aktive Abrufsperre;
das Datenverzeichnis daher vertraulich behandeln. Die Original-JSON-Antwort jedes Umsatzes bleibt in der Spalte `raw` erhalten.

## Entwicklung

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
