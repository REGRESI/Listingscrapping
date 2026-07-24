# Regresi Fahrzeug-Aggregator

Produktionsreifes Python-System, das Fahrzeugbestände von Partner-Autohäusern
einliest, in Postgres speichert und über einen **idempotenten, stündlichen
Sync** laufend aktuell hält. Ausgabe für die Webseite über eine schlanke
FastAPI (JSON + CSV).

Datenquellen: **bhg** und **ahg** (beide auf der Alphartis-Plattform, gleicher
Algolia-Index, Unterscheidung nur über das Anwendungskürzel). Jede Gruppe ist
ein eigener Adapter mit eigenem `source`-Wert; weitere Gruppen/Plattformen
kommen als zusätzliche Adapter dazu, ohne den Kern anzufassen (Adapter-Muster).
Steuerbar über `SYNC_SOURCES` (leer = alle, sonst z.B. `bhg,ahg`).

---

## Kernlogik: der laufende Abgleich

Jeder Sync-Lauf hält den Bestand pro Quelle synchron und ist **idempotent**
(zweimal laufen = gleiches Ergebnis):

- **neu** → einfügen
- **vorhanden, geändert** (v.a. Preis/Leasingrate) → aktualisieren (Upsert)
- **verschwunden** (in diesem Lauf nicht mehr geliefert) → `active=false` + `sold_at`
  setzen, **nicht löschen** → verkaufte Autos fallen automatisch von der Seite
- **zurückgekehrt** → reaktivieren (`active=true`, `sold_at=NULL`)

Eindeutiger Schlüssel: **`(source, source_id)`**.

Umgesetzt über einen einzigen Lauf-Zeitstempel `run_ts`: jedes gesehene
Fahrzeug bekommt `last_seen_at = run_ts`; anschließend gelten genau die aktiven
Fahrzeuge als verkauft, deren `last_seen_at < run_ts` ist. Kein Löschen, kein
riesiger `NOT IN`-Filter.

**Sicherheitsnetz gegen Fehl-Deaktivierung:** Liefert ein Lauf weniger als
`MIN_FETCH_RATIO` (Standard 50 %) des zuletzt aktiven Bestands – z.B. bei einem
Teilausfall der Quelle – wird die Deaktivierung **übersprungen**, damit nicht
versehentlich der halbe Bestand als „verkauft" markiert wird. Ein
fehlgeschlagener Fetch deaktiviert **nichts**.

Robustheit: pro Datensatz `try/except` (ein krummer Satz kippt nie den Lauf),
Netzwerk-Retries mit exponentiellem Backoff, sauberes Logging.

---

## Ordnerstruktur

```
.
├── bhg_adapter.py              # Gelieferter Quell-Adapter (UNVERÄNDERT), holt+normalisiert
├── aggregator/
│   ├── config.py               # Settings aus .env (pydantic-settings)
│   ├── logging_conf.py         # einheitliches Logging
│   ├── db.py                   # SQLAlchemy Engine/Session/Base
│   ├── models.py               # Tabelle `vehicles` (unique (source, source_id))
│   ├── schema.py               # pydantic NormalizedVehicle (Eingang = normalize()-Ausgabe)
│   ├── repository.py           # Upsert + Deaktivierung verschwundener Fahrzeuge
│   ├── sync.py                 # Sync-Engine + CLI  (python -m aggregator.sync)
│   ├── scheduler.py            # APScheduler, stündlich (python -m aggregator.scheduler)
│   ├── api.py                  # FastAPI: GET /vehicles, /vehicles.csv, ...
│   └── adapters/
│       ├── base.py             # SourceAdapter (discover/fetch/normalize)
│       ├── bhg.py              # Wrapper um bhg_adapter.py (+ Retries)
│       └── registry.py         # Liste der aktiven Adapter
├── tools/discover.py           # Schritt-1-Discovery (Playwright-Netzwerkmitschnitt)
├── tests/                      # Sync- + API-Tests gegen echtes Postgres
├── requirements.txt
├── docker-compose.yml          # Postgres für Dev
├── .env.example
├── DISCOVERY.md                # Runbook zur JSON-Schnittstellen-Analyse
└── README.md
```

Der gelieferte **`bhg_adapter.py` wird nicht verändert** – der Kern importiert
ihn und nutzt dessen `fetch_all_raw()`/`normalize()`.

---

## Setup

Voraussetzungen: Python 3.11+, Postgres 14+ (oder Docker).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env anpassen – vor allem DATABASE_URL
```

Postgres per Docker (optional):

```bash
docker compose up -d db
```

`DATABASE_URL` (SQLAlchemy, Treiber psycopg v3):

```
postgresql+psycopg://aggregator:aggregator@localhost:5432/aggregator
```

Die Tabellen werden beim ersten Sync automatisch angelegt (`init_db()`).

---

## Betrieb

> **Wichtig – Netzwerk:** Der Sync-Job macht die Netzabrufe zur Quelle und
> gehört auf **euren Server mit offenem Internet**. Die **API** macht keine
> Netzabrufe (liest nur die DB) und kann getrennt/abgeschottet laufen.

### Einmal-Lauf

```bash
python -m aggregator.sync            # alle Adapter
python -m aggregator.sync --source bhg
```

Beispiel-Log eines erfolgreichen Laufs:

```
[bhg] fetched=2701 inserted=2701 updated=0 deactivated=0 errors=0
```

### Stündlich – Variante A: eingebauter Scheduler (APScheduler)

Ein langlaufender Prozess (z.B. als systemd-Service oder Container):

```bash
python -m aggregator.scheduler
```

Cron-Ausdruck und Initial-Lauf über `.env` (`SCHEDULE_CRON`, `RUN_ON_START`).

### Stündlich – Variante B: System-Cron

`crontab -e`:

```cron
# Fahrzeug-Sync jede volle Stunde
0 * * * * cd /opt/regresi/listingscrapping && /opt/regresi/listingscrapping/.venv/bin/python -m aggregator.sync >> /var/log/regresi-sync.log 2>&1
```

Hinweis: Der Sync schützt sich selbst vor Überlappung (Variante A via
`max_instances=1`). Für Cron den Intervall so wählen, dass ein Lauf sicher vor
dem nächsten fertig ist (ein Voll-Lauf über ~2700 Fahrzeuge dauert wenige
Minuten inkl. höflichem Delay).

### systemd (optional)

```ini
# /etc/systemd/system/regresi-sync.service
[Unit]
Description=Regresi Fahrzeug-Sync (stündlich)
After=network-online.target postgresql.service

[Service]
WorkingDirectory=/opt/regresi/listingscrapping
EnvironmentFile=/opt/regresi/listingscrapping/.env
ExecStart=/opt/regresi/listingscrapping/.venv/bin/python -m aggregator.scheduler
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

---

## Deployment auf Railway

Das Repo enthält `nixpacks.toml` (Build + Default-Startbefehl) und `.python-version`.
Auf Railway laufen **drei Bausteine im selben Projekt**, alle mit derselben
`DATABASE_URL`:

1. **PostgreSQL** – von Railway bereitgestellt.
2. **Web-Dienst (API)** – dauerhaft, Start `uvicorn aggregator.api:app --host 0.0.0.0 --port $PORT`.
3. **Cron-Dienst (Sync)** – stündlich, Befehl `python -m aggregator.sync`.

> **Wichtig:** Die API hört auf Railways `$PORT` (nicht fest 8000) – das steckt
> schon im Default-Startbefehl. `DATABASE_URL` wird aus den Umgebungsvariablen
> gelesen; die Env-Variable hat Vorrang vor dem lokalen Default. Railway liefert
> die URL als `postgresql://…` – der Code schreibt sie intern automatisch auf den
> `postgresql+psycopg://`-Treiber um. Der erste Sync-Lauf legt die Tabellen bei
> einer frischen DB automatisch an (`init_db()`); zusätzlich tut das auch der
> Web-Dienst beim Start.

### Schritt für Schritt (Railway-Oberfläche)

**A. Projekt anlegen & GitHub-Repo verbinden**
1. Auf **railway.app** einloggen → Button **„New Project"**.
2. **„Deploy from GitHub repo"** wählen. Beim ersten Mal **„Configure GitHub App"**
   klicken, Railway Zugriff auf das Repo geben, dann das Repo auswählen.
3. Railway legt einen ersten Dienst an und startet einen Build. Das wird unser
   **Web-Dienst**. (Dank `nixpacks.toml` verschwindet die Meldung
   „No start command detected".)

**B. PostgreSQL hinzufügen**
1. In der Projekt-Ansicht (Canvas) oben rechts **„New"** → **„Database"** →
   **„Add PostgreSQL"**.
2. Es erscheint ein Dienst namens **Postgres**. Er stellt automatisch die
   Variable **`DATABASE_URL`** bereit (interner Host `…railway.internal`).

**C. `DATABASE_URL` an den Web-Dienst weiterreichen**
1. Den **Web-Dienst** anklicken → Tab **„Variables"**.
2. **„New Variable"** → als Namen **`DATABASE_URL`** eingeben, als Wert die
   **Referenz** `${{ Postgres.DATABASE_URL }}` (Railway bietet beim Tippen von
   `${{` die verbundenen Dienste zur Auswahl an; alternativ Button
   **„Add Reference"** → Dienst **Postgres** → Variable **DATABASE_URL**).
3. Speichern. Der Dienst deployt neu.

**D. Web-Dienst öffentlich erreichbar machen**
1. Web-Dienst → Tab **„Settings"** → Abschnitt **„Networking"** →
   **„Generate Domain"**. Railway vergibt eine öffentliche URL.
2. Optional: **„Settings"** → **„Deploy"** → **„Healthcheck Path"** = `/healthz`.
3. Der Startbefehl ist über `nixpacks.toml` bereits gesetzt; ein eigener Eintrag
   unter **„Custom Start Command"** ist für den Web-Dienst nicht nötig.
4. **CORS für die Webseite:** Web-Dienst → Tab **„Variables"** → **„New Variable"**
   → Name **`CORS_ORIGINS`**, Wert = die Domain(s) eurer Webseite, kommagetrennt,
   z.B. `https://deine-app.lovable.app,https://www.deine-domain.de`. Ohne diesen
   Eintrag blockiert der Browser die direkten API-Aufrufe (nur die Methode GET
   ist freigegeben).

**E. Zweiten Dienst für den stündlichen Sync anlegen**
1. Zurück auf den Projekt-Canvas → **„New"** → **„GitHub Repo"** → **dasselbe Repo**
   erneut auswählen. So entsteht ein **zweiter Dienst** aus demselben Code.
2. Diesen Dienst z.B. in **„Settings"** → **„Service Name"** in `sync` umbenennen.
3. Tab **„Variables"** → **`DATABASE_URL`** = `${{ Postgres.DATABASE_URL }}`
   (wie in Schritt C).
4. Tab **„Settings"** → **„Deploy"** → **„Custom Start Command"** =
   `python -m aggregator.sync`.
5. Im selben Bereich **„Cron Schedule"** = `0 * * * *` (stündlich zur vollen
   Stunde, UTC).
6. Der Sync-Dienst braucht **keine** öffentliche Domain (kein „Generate Domain").

**F. Fertig**
- Der Cron-Dienst läuft stündlich, holt den Bestand und aktualisiert die DB;
  er beendet sich nach jedem Lauf (Railway startet ihn zum Zeitplan neu).
- Der Web-Dienst liefert den Bestand unter der generierten Domain aus, z.B.
  `https://<deine-domain>/vehicles` und `…/healthz`.
- Ersten Sync sofort testen: den Sync-Dienst öffnen → Menü **„⋮"** /
  **„Deploy"**-Bereich → **„Restart"** (oder „Redeploy"), dann in den **Logs**
  die Zeile `[bhg] fetched=… inserted=…` prüfen.

### Hinweise
- Beide App-Dienste teilen sich die **eine** Postgres-Instanz über dieselbe
  `DATABASE_URL`-Referenz.
- Feld- und Button-Namen können sich bei Railway-Updates leicht ändern; die
  Reihenfolge (Repo → Postgres → Variablen → Start/Cron) bleibt gleich.

---

## API (für die Webseite)

```bash
uvicorn aggregator.api:app --host 0.0.0.0 --port 8000
# Doku: http://localhost:8000/docs
```

Nur `active=true` wird ausgegeben.

| Endpoint | Zweck |
| --- | --- |
| `GET /vehicles` | gefilterte, paginierte JSON-Liste |
| `GET /vehicles.csv` | dieselben Filter als CSV-Download (gestreamt) |
| `GET /vehicles/{id}` | Einzelfahrzeug |
| `GET /meta/makes` | verfügbare Marken (Filter-Dropdown) |
| `GET /healthz` | Health-Check + Anzahl aktiver Fahrzeuge |

Filter (kombinierbar, Mehrfachwerte möglich): `make`, `fuel`, `condition`,
`price_min`, `price_max`, `location`, `source`, `q` (Freitext).
Paginierung: `limit`, `offset`. Sortierung: `sort`, `order`.

Beispiele:

```bash
curl "http://localhost:8000/vehicles?make=Volkswagen&fuel=Elektro&condition=Gebraucht&price_max=40000&limit=20"
curl -L "http://localhost:8000/vehicles.csv?make=Audi" -o audi.csv
```

---

## Neuen Adapter hinzufügen

1. Neues Modul unter `aggregator/adapters/<plattform>.py`, Klasse erbt von
   `SourceAdapter`, setzt `name` und implementiert `discover()`, `fetch()`,
   `normalize()` (Rückgabe im Schema von `NormalizedVehicle`).
2. In `aggregator/adapters/registry.py` zur `ADAPTER_CLASSES` hinzufügen.

Fertig – Sync, DB, Deaktivierungslogik und API gelten automatisch. Der Kern
wird nicht angefasst.

---

## Tests

Die Tests laufen gegen ein **echtes Postgres** (Upsert/JSONB sind
Postgres-spezifisch) und brauchen **kein Netz** (Fixture-Adapter mit den echten
`normalize()`):

```bash
export TEST_DATABASE_URL=postgresql+psycopg://aggregator:aggregator@localhost:5432/aggregator
pytest -q
```

Abgedeckt: Insert, Idempotenz, Preis-Update + Normalisierung, verschwunden →
`sold`, zurückgekehrt → reaktiviert, Sicherheitsnetz gegen Massen-Deaktivierung,
Isolierung defekter Datensätze, API-Filter + CSV + „nur aktiv".

---

## Discovery (Schritt 1)

Analyse der JSON-Schnittstelle: siehe **`DISCOVERY.md`** und
`python -m tools.discover`. Der bhg-Adapter nutzt den öffentlichen Algolia-
Suchindex; die Discovery-Tools dienen der Dokumentation und künftigen Adaptern.
