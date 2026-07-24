"""
Regresi Fahrzeug-Aggregator — Adapter: Alphartis (bhg, ahg, ...)
================================================================
Quelle: Algolia-Suchindex 'production_stock_vehicles' (öffentlicher Such-Key,
nur lesend). Holt den KOMPLETTEN Bestand einer Anwendung (Autohausgruppe) und
übersetzt ihn in ein einheitliches Schema.

Mehrere Anwendungen (bhg, ahg, perspektivisch weitere) liegen im SELBEN Index
und teilen sich Application-Id und Such-Key. Sie unterscheiden sich nur durch:
  * den Filter        applications:"<app>"
  * das Location-Facet applicationData.<app>.location
  * die Daten unter    applicationData.<app> (Standort, cardData: url/images/...)
  * die Header Origin/Referer (Domain der jeweiligen Gruppe)
Der Such-Key muss also NICHT pro Gruppe neu geholt werden.

Wichtig — Algolias Paginierungslimit:
Der Index meldet ~2700 Fahrzeuge, gibt über die normale Suche aber nur ~1000
zum Durchblättern frei. Deshalb holen wir den Bestand NICHT am Stück, sondern
in Scheiben pro Standort (max ~500 je Standort) und führen alles über die
objectID wieder zusammen. So bekommen wir jedes Auto, ohne ans Limit zu stoßen.

Setup:
    pip install httpx
Start:
    python bhg_adapter.py              # bhg -> vehicles.json
    python bhg_adapter.py ahg          # ahg -> vehicles.json
"""

from __future__ import annotations
import json
import sys
import time
import httpx

APP_ID = "LR1T17NC7G"
SEARCH_KEY = "a497cebc6cfaf7121d3c5fa04f07604d"   # öffentlicher Such-Key (read-only)
INDEX = "production_stock_vehicles"
ENDPOINT = f"https://{APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries"

# Standard-Anwendung (Rückwärtskompatibilität: alle Funktionen defaulten auf bhg).
DEFAULT_APP = "bhg"

# Domain je Anwendung für Origin/Referer. Neue Gruppen hier ergänzen; ist eine
# Gruppe nicht eingetragen, wird "www.<app>-mobile.de" angenommen.
APP_DOMAINS = {
    "bhg": "www.bhg-mobile.de",
    "ahg": "www.ahg-mobile.de",
}


def app_domain(app: str = DEFAULT_APP) -> str:
    return APP_DOMAINS.get(app, f"www.{app}-mobile.de")


def build_headers(app: str = DEFAULT_APP) -> dict:
    """Algolia-Header inkl. gruppenspezifischem Origin/Referer."""
    domain = app_domain(app)
    return {
        "Content-Type": "application/json",
        "X-Algolia-Application-Id": APP_ID,
        "X-Algolia-API-Key": SEARCH_KEY,
        "Origin": f"https://{domain}",
        "Referer": f"https://{domain}/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        ),
    }


def base_filter(app: str = DEFAULT_APP) -> str:
    return f'language:"de" AND (applications:"{app}")'


def location_facet(app: str = DEFAULT_APP) -> str:
    return f"applicationData.{app}.location"


# Rückwärtskompatible Modul-Konstanten (entsprechen der bhg-Anwendung wie bisher).
HEADERS = build_headers(DEFAULT_APP)
BASE_FILTER = base_filter(DEFAULT_APP)
LOCATION_FACET = location_facet(DEFAULT_APP)


def _query(
    client: httpx.Client,
    filters: str,
    page: int = 0,
    hits_per_page: int = 1000,
    app: str = DEFAULT_APP,
) -> dict:
    body = {
        "requests": [{
            "indexName": INDEX,
            "filters": filters,
            "hitsPerPage": hits_per_page,
            "page": page,
            "query": "",
            "facets": [location_facet(app)],
            "maxValuesPerFacet": 1000,
        }]
    }
    r = client.post(ENDPOINT, headers=build_headers(app), content=json.dumps(body), timeout=30)
    r.raise_for_status()
    return r.json()["results"][0]


def list_locations(client: httpx.Client, app: str = DEFAULT_APP) -> dict:
    """Eine Abfrage nur für die Standort-Liste inkl. Fahrzeugzahl -> {name: anzahl}."""
    res = _query(client, base_filter(app), hits_per_page=0, app=app)
    return res.get("facets", {}).get(location_facet(app), {})


def fetch_all_raw(client: httpx.Client, app: str = DEFAULT_APP) -> list[dict]:
    """Holt alle Fahrzeuge einer Anwendung, Scheibe pro Standort, dedupliziert über objectID."""
    locations = list_locations(client, app)
    seen: dict[str, dict] = {}
    for loc in locations:
        loc_filter = f'{base_filter(app)} AND {location_facet(app)}:"{loc}"'
        page = 0
        while True:
            res = _query(client, loc_filter, page=page, hits_per_page=1000, app=app)
            for hit in res["hits"]:
                seen[hit["objectID"]] = hit
            if page + 1 >= res["nbPages"]:
                break
            page += 1
        time.sleep(0.2)   # höflich bleiben
    return list(seen.values())


def normalize(hit: dict, app: str = DEFAULT_APP) -> dict:
    """Rohdatensatz -> einheitliches Schema für Datenbank und Webseite."""
    appdata = hit.get("applicationData", {}).get(app, {})
    card = appdata.get("cardData", {})
    return {
        "source": app,
        "source_id": hit["objectID"],
        "make": hit.get("make"),
        "model": hit.get("model"),
        "variant": (hit.get("modelDescription") or "").strip(),
        "condition": hit.get("condition"),               # Neu / Gebraucht / Vorführfahrzeug
        "vehicle_class": hit.get("vehicleClass"),         # PKW / Nutzfahrzeug
        "category": hit.get("category"),                  # SUV / Kombi / Limousine / ...
        "price": hit.get("price"),
        "original_price": hit.get("originalPrice"),
        "leasing_rate": hit.get("leasingPrice"),
        "financing_rate": hit.get("financingPrice"),
        "financing_down_payment": hit.get("financingDownPayment"),
        # cardData hat MM/YYYY (genauer), Top-Level nur das Jahr — wir nehmen das genauere.
        "first_registration": card.get("firstRegistration") or hit.get("firstRegistration"),
        "mileage_km": hit.get("mileage"),
        "power_kw": hit.get("power"),
        "fuel": hit.get("fuel"),
        "gearbox": hit.get("gearbox"),
        "color": hit.get("color"),
        "features": hit.get("features") or [],
        "consumption": card.get("consumptionDescription"),
        "location": appdata.get("location"),
        "reserved": hit.get("isReserved", False),
        "url": card.get("url"),
        "images": card.get("images") or [],
        "raw": hit,                                       # Original behalten, falls später Felder fehlen
    }


def main() -> None:
    app = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_APP
    with httpx.Client() as client:
        raw = fetch_all_raw(client, app)
        vehicles = [normalize(h, app) for h in raw]
    with open("vehicles.json", "w", encoding="utf-8") as f:
        json.dump(vehicles, f, ensure_ascii=False, indent=2)
    print(f"{len(vehicles)} Fahrzeuge ({app}) geholt und normalisiert -> vehicles.json")


if __name__ == "__main__":
    main()
