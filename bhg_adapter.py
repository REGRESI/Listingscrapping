"""
Regresi Fahrzeug-Aggregator — Adapter: bhg / Alphartis
=======================================================
Quelle: Algolia-Suchindex 'production_stock_vehicles' (öffentlicher Such-Key,
nur lesend). Holt den KOMPLETTEN bhg-Bestand und übersetzt ihn in ein
einheitliches Schema.

Wichtig — Algolias Paginierungslimit:
Der Index meldet ~2700 Fahrzeuge, gibt über die normale Suche aber nur ~1000
zum Durchblättern frei. Deshalb holen wir den Bestand NICHT am Stück, sondern
in Scheiben pro Standort (max ~500 je Standort) und führen alles über die
objectID wieder zusammen. So bekommen wir jedes Auto, ohne ans Limit zu stoßen.

Setup:
    pip install httpx
Start:
    python bhg_adapter.py          # schreibt vehicles.json
"""

from __future__ import annotations
import json
import time
import httpx

APP_ID = "LR1T17NC7G"
SEARCH_KEY = "a497cebc6cfaf7121d3c5fa04f07604d"   # öffentlicher Such-Key (read-only)
INDEX = "production_stock_vehicles"
ENDPOINT = f"https://{APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries"
LOCATION_FACET = "applicationData.bhg.location"

HEADERS = {
    "Content-Type": "application/json",
    "X-Algolia-Application-Id": APP_ID,
    "X-Algolia-API-Key": SEARCH_KEY,
    "Origin": "https://www.bhg-mobile.de",
    "Referer": "https://www.bhg-mobile.de/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
}

BASE_FILTER = 'language:"de" AND (applications:"bhg")'


def _query(client: httpx.Client, filters: str, page: int = 0, hits_per_page: int = 1000) -> dict:
    body = {
        "requests": [{
            "indexName": INDEX,
            "filters": filters,
            "hitsPerPage": hits_per_page,
            "page": page,
            "query": "",
            "facets": [LOCATION_FACET],
            "maxValuesPerFacet": 1000,
        }]
    }
    r = client.post(ENDPOINT, headers=HEADERS, content=json.dumps(body), timeout=30)
    r.raise_for_status()
    return r.json()["results"][0]


def list_locations(client: httpx.Client) -> dict:
    """Eine Abfrage nur für die Standort-Liste inkl. Fahrzeugzahl -> {name: anzahl}."""
    res = _query(client, BASE_FILTER, hits_per_page=0)
    return res.get("facets", {}).get(LOCATION_FACET, {})


def fetch_all_raw(client: httpx.Client) -> list[dict]:
    """Holt alle bhg-Fahrzeuge, Scheibe pro Standort, dedupliziert über objectID."""
    locations = list_locations(client)
    seen: dict[str, dict] = {}
    for loc in locations:
        loc_filter = f'{BASE_FILTER} AND {LOCATION_FACET}:"{loc}"'
        page = 0
        while True:
            res = _query(client, loc_filter, page=page, hits_per_page=1000)
            for hit in res["hits"]:
                seen[hit["objectID"]] = hit
            if page + 1 >= res["nbPages"]:
                break
            page += 1
        time.sleep(0.2)   # höflich bleiben
    return list(seen.values())


def normalize(hit: dict) -> dict:
    """Rohdatensatz -> einheitliches Schema für Datenbank und Webseite."""
    bhg = hit.get("applicationData", {}).get("bhg", {})
    card = bhg.get("cardData", {})
    return {
        "source": "bhg",
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
        "location": bhg.get("location"),
        "reserved": hit.get("isReserved", False),
        "url": card.get("url"),
        "images": card.get("images") or [],
        "raw": hit,                                       # Original behalten, falls später Felder fehlen
    }


def main() -> None:
    with httpx.Client() as client:
        raw = fetch_all_raw(client)
        vehicles = [normalize(h) for h in raw]
    with open("vehicles.json", "w", encoding="utf-8") as f:
        json.dump(vehicles, f, ensure_ascii=False, indent=2)
    print(f"{len(vehicles)} Fahrzeuge geholt und normalisiert -> vehicles.json")


if __name__ == "__main__":
    main()
