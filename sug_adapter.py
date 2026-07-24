"""
Regresi Fahrzeug-Aggregator — Adapter: SuG (sug.de)
====================================================
Quelle: GraphQL-API (operationName "Cars"). Holt den kompletten SuG-Bestand
und übersetzt ihn in das einheitliche Schema (gleiches Format wie bhg/ahg).

Paginierung über page/limit; das Ende wird an ``totalPages`` aus der Antwort
erkannt. Query, Variablenstruktur und Feldliste entsprechen exakt dem
nachweislich funktionierenden Browser-Request.

Setup:
    pip install httpx
Start:
    python sug_adapter.py          # schreibt vehicles.json
"""

from __future__ import annotations
import json
import time
import httpx

ENDPOINT = "https://api.sug-gebrauchtwagen.de/"
DOMAIN = "https://www.sug.de"
SOURCE = "sug"

# Seitengröße wie im Beispiel-Request (bewährt). Über page paginiert.
DEFAULT_LIMIT = 20

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": DOMAIN,
    "Referer": f"{DOMAIN}/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.4 Safari/605.1.15"
    ),
}

# GraphQL-Query exakt aus dem funktionierenden Browser-Request.
QUERY = """query Cars($pagination: PaginationInput, $filter: CarFilterInput!) {
  cars(pagination: $pagination, filter: $filter) {
    docs {
      _id
      uid
      link
      brand
      name
      price
      priceWithoutTax
      mileage
      power
      firstRegistration
      firstRegistrationDate
      location {
        name
        street
        zipCode
        city
        phone
        clickAndMeetOrClickAndCollect
        clickAndMeetUrl
        __typename
      }
      emission
      fuelConsumption
      combinedPowerConsumption
      images {
        imagepath
        __typename
      }
      model
      categories
      color
      engine {
        fuel
        gearbox
        __typename
      }
      financing {
        rate
        regulatory
        __typename
      }
      energyEfficiencyClass
      vatReportable
      warranty
      qualitySeal {
        youngStar
        __typename
      }
      wltp {
        combined
        co {
          klasse
          klasse_gewichtet_kombiniert
          klasse_entladene_batterie
          emission
          emission_gewichtet_kombiniert
          __typename
        }
        stromverbrauch {
          kombiniert {
            elektro
            hybrid
            __typename
          }
          __typename
        }
        kraftstoffverbrauch {
          kombiniert {
            hybrid_entladene_batterie
            hybrid_geladene_batterie
            __typename
          }
          __typename
        }
        __typename
      }
      vendor
      __typename
    }
    totalDocs
    totalPages
    linkList {
      _id
      link
      __typename
    }
    __typename
  }
}"""


def _query(client: httpx.Client, page: int = 1, limit: int = DEFAULT_LIMIT) -> dict:
    """Eine GraphQL-Seite holen. Gibt das `cars`-Objekt (docs/totalDocs/…) zurück."""
    body = {
        "operationName": "Cars",
        "variables": {
            "filter": {"utilityVehicle": False, "vehicleInventory": {}},
            "pagination": {"page": page, "limit": limit, "sort": "price"},
        },
        "query": QUERY,
    }
    r = client.post(ENDPOINT, headers=HEADERS, content=json.dumps(body), timeout=30)
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL-Fehler: {payload['errors']}")
    return payload["data"]["cars"]


def total_docs(client: httpx.Client) -> int:
    """Gesamtzahl der Fahrzeuge (eine kleine Abfrage)."""
    cars = _query(client, page=1, limit=1)
    return cars.get("totalDocs", 0)


def fetch_all_raw(client: httpx.Client, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Alle SuG-Fahrzeuge holen, über page/limit bis totalPages paginiert."""
    docs: list[dict] = []
    page = 1
    while True:
        cars = _query(client, page=page, limit=limit)
        batch = cars.get("docs") or []
        docs.extend(batch)
        total_pages = cars.get("totalPages") or 0
        if not batch or page >= total_pages:
            break
        page += 1
        time.sleep(0.2)   # höflich bleiben
    return docs


def _clean(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _build_consumption(doc: dict) -> str | None:
    """emission, fuelConsumption und die wltp-Daten sinnvoll zu einem
    Anzeige-Text für Verbrauch/CO2 zusammenfassen (Pflichtangaben)."""
    parts: list[str] = []

    fc = _clean(doc.get("fuelConsumption"))
    if fc:
        parts.append(f"Verbrauch: {fc}")

    cpc = _clean(doc.get("combinedPowerConsumption"))
    if cpc:
        parts.append(f"Stromverbrauch: {cpc}")

    em = _clean(doc.get("emission"))
    if em:
        parts.append(f"Emission: {em}")

    eec = _clean(doc.get("energyEfficiencyClass"))
    if eec:
        parts.append(f"Effizienzklasse: {eec}")

    wltp = doc.get("wltp") or {}
    if isinstance(wltp, dict):
        combined = _clean(wltp.get("combined"))
        if combined:
            parts.append(f"WLTP kombiniert: {combined}")
        co = wltp.get("co") or {}
        if isinstance(co, dict):
            co_em = _clean(co.get("emission")) or _clean(co.get("emission_gewichtet_kombiniert"))
            if co_em:
                parts.append(f"CO2 kombiniert: {co_em}")
            co_kl = _clean(co.get("klasse")) or _clean(co.get("klasse_gewichtet_kombiniert"))
            if co_kl:
                parts.append(f"CO2-Klasse: {co_kl}")
        strom = (((wltp.get("stromverbrauch") or {}).get("kombiniert")) or {})
        if isinstance(strom, dict):
            elektro = _clean(strom.get("elektro"))
            hybrid = _clean(strom.get("hybrid"))
            if elektro:
                parts.append(f"Stromverbrauch WLTP: {elektro}")
            if hybrid:
                parts.append(f"Stromverbrauch WLTP (Hybrid): {hybrid}")
        kraft = (((wltp.get("kraftstoffverbrauch") or {}).get("kombiniert")) or {})
        if isinstance(kraft, dict):
            entladen = _clean(kraft.get("hybrid_entladene_batterie"))
            geladen = _clean(kraft.get("hybrid_geladene_batterie"))
            if geladen:
                parts.append(f"Kraftstoff WLTP (geladen): {geladen}")
            if entladen:
                parts.append(f"Kraftstoff WLTP (entladen): {entladen}")

    return " · ".join(parts) if parts else None


def normalize(doc: dict) -> dict:
    """SuG-Rohdatensatz -> einheitliches Schema."""
    engine = doc.get("engine") or {}
    financing = doc.get("financing") or {}
    location = doc.get("location") or {}
    images = doc.get("images") or []
    categories = doc.get("categories") or []

    image_urls = [img.get("imagepath") for img in images if isinstance(img, dict) and img.get("imagepath")]
    rate = financing.get("rate") if isinstance(financing, dict) else None
    category = None
    if isinstance(categories, list) and categories:
        category = _clean(categories[0])
    elif isinstance(categories, str):
        category = _clean(categories)

    return {
        "source": SOURCE,
        "source_id": str(doc.get("_id") or doc.get("uid")),
        "make": doc.get("brand"),
        "model": doc.get("name") or doc.get("model"),
        "variant": None,
        "condition": None,
        "vehicle_class": None,
        "category": category,
        "price": doc.get("price"),
        "original_price": None,
        # SuG liefert eine Finanzierungsrate; wir befüllen financing_rate und
        # spiegeln sie in leasing_rate, damit die Webseite die Monatsrate
        # unabhängig vom gelesenen Feld anzeigen kann.
        "leasing_rate": rate,
        "financing_rate": rate,
        "financing_down_payment": None,
        "first_registration": doc.get("firstRegistration"),
        "mileage_km": doc.get("mileage"),
        "power_kw": doc.get("power"),
        "fuel": engine.get("fuel") if isinstance(engine, dict) else None,
        "gearbox": engine.get("gearbox") if isinstance(engine, dict) else None,
        "color": doc.get("color"),
        "features": [],
        # emission + fuelConsumption + wltp zu einem Anzeige-Text zusammengefasst.
        "consumption": _build_consumption(doc),
        # Standort: name bzw. city; volle Adresse/Telefon bleiben in raw erhalten.
        "location": (location.get("name") or location.get("city")) if isinstance(location, dict) else None,
        "reserved": False,
        "url": doc.get("link"),
        "images": image_urls,
        "raw": doc,   # enthält u.a. location.street/zipCode/city/phone für spätere Anzeige
    }


def main() -> None:
    with httpx.Client() as client:
        total = total_docs(client)
        raw = fetch_all_raw(client)
        vehicles = [normalize(d) for d in raw]
    with open("vehicles.json", "w", encoding="utf-8") as f:
        json.dump(vehicles, f, ensure_ascii=False, indent=2)
    print(f"{len(vehicles)} von {total} Fahrzeugen (sug) geholt und normalisiert -> vehicles.json")


if __name__ == "__main__":
    main()
