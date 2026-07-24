"""SuG-Adapter (GraphQL): Paginierung, Feld-Mapping, consumption, Sync-Integration.

Ohne echtes Netz (in dieser Umgebung geblockt): ein Fake-GraphQL-Client liefert
Antworten in der echten Struktur (data.cars.docs/totalPages/totalDocs).
"""
from __future__ import annotations

import json

from sqlalchemy import select

import sug_adapter as sug
from aggregator.adapters.registry import get_adapters
from aggregator.adapters.sug import SugAdapter
from aggregator.models import Vehicle
from aggregator import sync as sync_module
from aggregator.sync import SyncEngine


def make_sug_doc(_id: str, *, price=25000, brand="BMW", name="320d") -> dict:
    return {
        "_id": _id,
        "uid": f"uid-{_id}",
        "link": f"https://www.sug.de/fahrzeug/{_id}",
        "brand": brand,
        "name": name,
        "model": "3er",
        "price": price,
        "priceWithoutTax": price - 4000,
        "mileage": 55000,
        "power": 140,
        "firstRegistration": "05/2021",
        "firstRegistrationDate": "2021-05-01",
        "location": {
            "name": "SuG Stuttgart",
            "street": "Teststr. 1",
            "zipCode": "70173",
            "city": "Stuttgart",
            "phone": "0711 12345",
            "__typename": "Location",
        },
        "emission": "120 g/km",
        "fuelConsumption": "5,1 l/100km",
        "combinedPowerConsumption": None,
        "images": [
            {"imagepath": f"https://img.sug.de/{_id}/1.jpg", "__typename": "Image"},
            {"imagepath": f"https://img.sug.de/{_id}/2.jpg", "__typename": "Image"},
        ],
        "categories": ["Limousine", "Business"],
        "color": "Blau",
        "engine": {"fuel": "Diesel", "gearbox": "Automatik", "__typename": "Engine"},
        "financing": {"rate": 249.0, "regulatory": "…", "__typename": "Financing"},
        "energyEfficiencyClass": "A",
        "wltp": {
            "combined": "5,1",
            "co": {"emission": "119", "klasse": "B", "__typename": "Co"},
            "stromverbrauch": {"kombiniert": {"elektro": None, "hybrid": None}},
            "kraftstoffverbrauch": {"kombiniert": {}},
            "__typename": "Wltp",
        },
        "vendor": "SuG",
        "__typename": "Car",
    }


class _FakeResp:
    def __init__(self, cars: dict):
        self._cars = cars

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": {"cars": self._cars}}


class FakeGraphQL:
    """httpx.Client-Ersatz, der die Cars-GraphQL-API seitenweise nachbildet."""

    def __init__(self, pages: list[list[dict]], total_docs: int):
        self.pages = pages
        self.total = total_docs
        self.calls: list[dict] = []

    def post(self, url, headers=None, content=None, timeout=None):
        body = json.loads(content)
        self.calls.append({"headers": headers, "body": body})
        page = body["variables"]["pagination"]["page"]
        limit = body["variables"]["pagination"]["limit"]
        total_pages = len(self.pages)
        if limit == 1:  # total_docs()-Sonderabfrage
            return _FakeResp({"docs": [], "totalDocs": self.total, "totalPages": total_pages})
        docs = self.pages[page - 1] if 1 <= page <= total_pages else []
        return _FakeResp({"docs": docs, "totalDocs": self.total, "totalPages": total_pages})


def test_pagination_uses_total_pages():
    pages = [
        [make_sug_doc("a"), make_sug_doc("b")],
        [make_sug_doc("c")],
    ]
    client = FakeGraphQL(pages, total_docs=3)
    raw = sug.fetch_all_raw(client, limit=2)
    assert [d["_id"] for d in raw] == ["a", "b", "c"]
    # Zwei Seiten abgefragt, operationName korrekt.
    requested_pages = [c["body"]["variables"]["pagination"]["page"] for c in client.calls]
    assert requested_pages == [1, 2]
    assert client.calls[0]["body"]["operationName"] == "Cars"
    assert client.calls[0]["headers"]["Origin"] == "https://www.sug.de"
    assert client.calls[0]["headers"]["Referer"] == "https://www.sug.de/"


def test_total_docs():
    client = FakeGraphQL([[make_sug_doc("a")]], total_docs=137)
    assert sug.total_docs(client) == 137


def test_normalize_mapping():
    v = sug.normalize(make_sug_doc("x1", price=31900, brand="Audi", name="A3 Sportback"))
    assert v["source"] == "sug"
    assert v["source_id"] == "x1"
    assert v["make"] == "Audi"
    assert v["model"] == "A3 Sportback"
    assert v["price"] == 31900
    assert v["mileage_km"] == 55000
    assert v["power_kw"] == 140
    assert v["first_registration"] == "05/2021"
    assert v["fuel"] == "Diesel"
    assert v["gearbox"] == "Automatik"
    assert v["color"] == "Blau"
    assert v["leasing_rate"] == 249.0 and v["financing_rate"] == 249.0
    assert v["url"] == "https://www.sug.de/fahrzeug/x1"
    assert v["images"] == [
        "https://img.sug.de/x1/1.jpg",
        "https://img.sug.de/x1/2.jpg",
    ]
    assert v["location"] == "SuG Stuttgart"
    assert v["category"] == "Limousine"
    # Rohdaten (Adresse/Telefon) bleiben erhalten.
    assert v["raw"]["location"]["phone"] == "0711 12345"


def test_consumption_summary():
    c = sug.normalize(make_sug_doc("x1"))["consumption"]
    assert "Verbrauch: 5,1 l/100km" in c
    assert "Emission: 120 g/km" in c
    assert "Effizienzklasse: A" in c
    assert "WLTP kombiniert: 5,1" in c
    assert "CO2 kombiniert: 119" in c


def test_registry_includes_sug_and_filter():
    assert "sug" in [a.name for a in get_adapters()]
    assert [a.name for a in get_adapters(["sug"])] == ["sug"]


class FakeSug(SugAdapter):
    def __init__(self, records):
        super().__init__()
        self._records = records

    def fetch(self):
        return list(self._records)


def test_sync_sug_inserts_with_source(db_session, monkeypatch):
    records = [make_sug_doc("s1"), make_sug_doc("s2", price=42000)]
    monkeypatch.setattr(sync_module, "get_adapters", lambda names=None: [FakeSug(records)])

    report = SyncEngine().run(sources=["sug"])
    assert report.results[0].inserted == 2

    rows = db_session.execute(select(Vehicle).where(Vehicle.source == "sug")).scalars().all()
    assert {r.source_id for r in rows} == {"s1", "s2"}
    assert all(r.consumption and "Verbrauch" in r.consumption for r in rows)
