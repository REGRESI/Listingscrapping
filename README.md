# Regresi Fahrzeug-Aggregator

Produktionsreifes Python-System, das Fahrzeugbestände von Partner-Autohäusern
einliest, in Postgres speichert und über einen **idempotenten, stündlichen
Sync** laufend aktuell hält. Ausgabe für die Webseite über eine schlanke
FastAPI (JSON + CSV).

Erste Datenquelle: **bhg / Alphartis** (Algolia-Suchindex). Weitere Plattformen
kommen als zusätzliche Adapter dazu, ohne den Kern anzufassen (Adapter-Muster).

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
