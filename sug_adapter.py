"""
Regresi Fahrzeug-Aggregator — Adapter: SuG (sug.de)
====================================================
Quelle: GraphQL-API. ZWEISTUFIGER Abruf, weil die Listenabfrage "Cars" nur
unvollständige Daten liefert (kryptischer Titel, keine Bilder/Ausstattung):

  Stufe 1  "Cars"  -> paginiert alle _id einsammeln (Ende an totalPages).
  Stufe 2  "Car"   -> pro _id die vollständigen Detaildaten holen.

Rate-Limiting: begrenzte Parallelität + kleine Pause je Detailabfrage, damit
SuG bei ~2000 Detailabfragen pro Lauf nicht blockt. Ein Fehler eines einzelnen
Fahrzeugs überspringt nur diesen Datensatz, nicht den ganzen Lauf.

HINWEIS: Die "Car"-Detail-Query (Root-Feldname, Variablentyp für _id, exakte
Feldnamen) ist gegen das Live-Schema zu bestätigen; hier ist der Endpoint per
Egress-Policy geblockt. Uneindeutige Stellen sind als Konstanten markiert und
leicht anpassbar.

Setup:
    pip install httpx
Start:
    python sug_adapter.py          # schreibt vehicles.json
"""

from __future__ import annotations
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

ENDPOINT = "https://api.sug-gebrauchtwagen.de/"
DOMAIN = "https://www.sug.de"
SOURCE = "sug"

# Stufe 1: Seitengröße der Listenabfrage. Über page paginiert.
DEFAULT_LIMIT = 50

# Stufe 2: Rate-Limiting der Detailabfragen.
DETAIL_CONCURRENCY = 5       # max. gleichzeitige Detailabfragen
DETAIL_DELAY = 0.15          # kleine Pause (s) je Detailabfrage

# GraphQL-Typ der _id-Variable in der "Car"-Abfrage. Das Live-Schema nutzt
# ID! (die alphanumerischen _id-Werte wie "8523-00259" werden als String-Wert
# übergeben, der GraphQL-Typ ist aber ID!).
CAR_ID_GQL_TYPE = "ID!"

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

# --- Stufe 1: Liste, nur _id + Paginierungs-Metadaten -----------------------
CARS_LIST_QUERY = """query Cars($pagination: PaginationInput, $filter: CarFilterInput!) {
  cars(pagination: $pagination, filter: $filter) {
    docs {
      _id
      __typename
    }
    totalDocs
    totalPages
    __typename
  }
}"""

# --- Stufe 2: Detail pro Fahrzeug ------------------------------------------
# Vollständige Felder inkl. equipmentTranslations und imagebigthumbpath.
CAR_QUERY = """query Car($_id: %s) {
  car(_id: $_id) {
    _id
    uid
    link
    brand
    model
    name
    price
    priceWithoutTax
    mileage
    power
    firstRegistration
    firstRegistrationDate
    color
    categories
    equipmentTranslations
    location {
      name
      street
      zipCode
      city
      phone
      __typename
    }
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
    images {
      imagepath
      imagebigthumbpath
      __typename
    }
    emission
    fuelConsumption
    combinedPowerConsumption
    energyEfficiencyClass
    wltp {
      combined
      co {
        emission
        emission_gewichtet_kombiniert
        klasse
        klasse_gewichtet_kombiniert
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
}""" % CAR_ID_GQL_TYPE


def _post(client: httpx.Client, body: dict) -> dict:
    r = client.post(ENDPOINT, headers=HEADERS, content=json.dumps(body), timeout=30)
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL-Fehler: {payload['errors']}")
    return payload["data"]


def _query_cars(client: httpx.Client, page: int = 1, limit: int = DEFAULT_LIMIT) -> dict:
    """Eine Listen-Seite (Stufe 1). Gibt das `cars`-Objekt zurück."""
    body = {
        "operationName": "Cars",
        "variables": {
            "filter": {"utilityVehicle": False, "vehicleInventory": {}},
            "pagination": {"page": page, "limit": limit, "sort": "price"},
        },
        "query": CARS_LIST_QUERY,
    }
    return _post(client, body)["cars"]


def total_docs(client: httpx.Client) -> int:
    """Gesamtzahl der Fahrzeuge (kleine Abfrage)."""
    return _query_cars(client, page=1, limit=1).get("totalDocs", 0)


def fetch_all_ids(client: httpx.Client, limit: int = DEFAULT_LIMIT) -> list[str]:
    """Stufe 1: alle _id über page/limit bis totalPages einsammeln."""
    ids: list[str] = []
    page = 1
    while True:
        cars = _query_cars(client, page=page, limit=limit)
        batch = cars.get("docs") or []
        for d in batch:
            _id = d.get("_id")
            if _id:
                ids.append(str(_id))
        total_pages = cars.get("totalPages") or 0
        if not batch or page >= total_pages:
            break
        page += 1
        time.sleep(0.2)   # höflich bleiben
    return ids


def fetch_car(client: httpx.Client, _id: str) -> dict | None:
    """Stufe 2: Detaildaten eines Fahrzeugs holen."""
    body = {
        "operationName": "Car",
        "variables": {"_id": _id},
        "query": CAR_QUERY,
    }
    return _post(client, body).get("car")


def fetch_all_raw(
    client: httpx.Client,
    limit: int = DEFAULT_LIMIT,
    concurrency: int = DETAIL_CONCURRENCY,
    delay: float = DETAIL_DELAY,
) -> list[dict]:
    """Zweistufiger Abruf: alle _id sammeln, dann Details parallel (begrenzt)
    holen. Fehler einzelner Fahrzeuge werden übersprungen, nicht der Lauf."""
    ids = fetch_all_ids(client, limit)

    def _one(_id: str) -> dict | None:
        time.sleep(delay)   # kleine Pause je Detailabfrage (Rate-Limiting)
        try:
            car = fetch_car(client, _id)
            if car:
                return car
            return None
        except Exception as exc:   # einzelnes Fahrzeug überspringen
            print(f"[sug] Detailabruf übersprungen (_id={_id}): {exc!r}")
            return None

    docs: list[dict] = []
    workers = max(1, concurrency)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, _id) for _id in ids]
        for fut in as_completed(futures):
            car = fut.result()
            if car:
                docs.append(car)
    return docs


# ---------------------------------------------------------------------------
# Kraftstoff: engine.fuel kommt als Zahlencode (z.B. 3, 10). Unser Schema
# erwartet einen lesbaren String. VORLÄUFIGE Zuordnung — gegen echte SuG-Daten
# bestätigen; unbekannte Codes gehen als String durch (kein Datenverlust).
FUEL_CODES: dict[int, str] = {
    1: "Benzin",
    2: "Diesel",
    3: "Autogas (LPG)",
    4: "Erdgas (CNG)",
    5: "Elektro",
    6: "Hybrid (Benzin/Elektro)",
    7: "Hybrid (Diesel/Elektro)",
    8: "Wasserstoff",
    9: "Ethanol",
    10: "Plug-in-Hybrid",
}


def _to_str(value) -> str | None:
    """Beliebigen Skalar robust in einen String wandeln (Zahlen -> String),
    damit Zahlwerte aus der API nicht an String-Feldern des Schemas scheitern."""
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    return str(value)


_clean = _to_str   # Alias


def _map_fuel(value) -> str | None:
    """Kraftstoff-Code -> Bezeichnung. Lesbare Strings bleiben; unbekannter
    Zahlencode wird als String durchgereicht (kein Datenverlust)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.isdigit():
            return FUEL_CODES.get(int(s), s)
        return s
    if isinstance(value, (int, float)):
        return FUEL_CODES.get(int(value), str(value))
    return str(value)


def _build_consumption(doc: dict) -> str | None:
    """emission, fuelConsumption und die wltp-Daten zu einem Anzeige-Text
    für Verbrauch/CO2 zusammenfassen (Pflichtangaben)."""
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
            geladen = _clean(kraft.get("hybrid_geladene_batterie"))
            entladen = _clean(kraft.get("hybrid_entladene_batterie"))
            if geladen:
                parts.append(f"Kraftstoff WLTP (geladen): {geladen}")
            if entladen:
                parts.append(f"Kraftstoff WLTP (entladen): {entladen}")

    return " · ".join(parts) if parts else None


def _extract_images(doc: dict) -> list[str]:
    """Bild-URLs aus images übernehmen (imagepath bzw. imagebigthumbpath).
    Es sind bereits vollständige digiaccess-URLs -> unverändert übernehmen."""
    out: list[str] = []
    for img in doc.get("images") or []:
        if isinstance(img, dict):
            url = img.get("imagepath") or img.get("imagebigthumbpath")
            if url:
                out.append(str(url))
        elif isinstance(img, str) and img.strip():
            out.append(img.strip())
    return out


def _extract_features(doc: dict) -> list[str]:
    """equipmentTranslations in unser features-Feld übernehmen."""
    equip = doc.get("equipmentTranslations")
    out: list[str] = []
    if isinstance(equip, list):
        for e in equip:
            if isinstance(e, str) and e.strip():
                out.append(e.strip())
            elif isinstance(e, dict):
                val = e.get("value") or e.get("name") or e.get("label") or e.get("translation")
                if val:
                    out.append(str(val).strip())
    elif isinstance(equip, str) and equip.strip():
        out.append(equip.strip())
    return out


def _build_title_model(doc: dict) -> str | None:
    """Titel-Basis = brand + model (NICHT das kryptische name-Feld). Wir liefern
    hier den model-Anteil; make (brand) steht separat im Schema, die Webseite
    setzt den Titel aus make + model zusammen."""
    return _to_str(doc.get("model"))


def normalize(doc: dict) -> dict:
    """SuG-Detaildatensatz (Stufe 2) -> einheitliches Schema."""
    engine = doc.get("engine") or {}
    financing = doc.get("financing") or {}
    location = doc.get("location") or {}
    categories = doc.get("categories") or []

    rate = financing.get("rate") if isinstance(financing, dict) else None
    category = None
    if isinstance(categories, list) and categories:
        category = _clean(categories[0])
    elif isinstance(categories, str):
        category = _clean(categories)

    return {
        "source": SOURCE,
        "source_id": str(doc.get("_id") or doc.get("uid")),
        # Titel: brand + model. name bleibt nur in raw (kryptische Händler-Überschrift).
        "make": _to_str(doc.get("brand")),
        "model": _build_title_model(doc),
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
        "first_registration": _to_str(doc.get("firstRegistration")),
        "mileage_km": doc.get("mileage"),
        "power_kw": doc.get("power"),
        # fuel: Zahlencode -> Bezeichnung; gearbox robust in String.
        "fuel": _map_fuel(engine.get("fuel")) if isinstance(engine, dict) else None,
        "gearbox": _to_str(engine.get("gearbox")) if isinstance(engine, dict) else None,
        "color": _to_str(doc.get("color")),
        # Ausstattung aus equipmentTranslations.
        "features": _extract_features(doc),
        "consumption": _build_consumption(doc),
        # Standort: name bzw. city; volle Adresse/Telefon bleiben in raw erhalten.
        "location": _to_str(location.get("name") or location.get("city")) if isinstance(location, dict) else None,
        "reserved": False,
        "url": _to_str(doc.get("link")),
        # Bilder aus dem Detail (imagepath/imagebigthumbpath), vollständige URLs.
        "images": _extract_images(doc),
        "raw": doc,   # vollständige Detaildaten (inkl. name, Adresse/Telefon)
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
