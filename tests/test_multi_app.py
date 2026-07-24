"""Mehrere Anwendungen (bhg, ahg) über denselben Algolia-Index.

Verifiziert die ahg-Anbindung analog zu bhg — ohne echtes Netz (in dieser
Umgebung ohnehin geblockt): ein Fake-Algolia-Client liefert Antworten in der
echten Struktur, sodass der reale Pfad fetch_all_raw -> normalize geprüft wird.
Zusätzlich Sync-Integration über beide Quellen und der SYNC_SOURCES-Filter.
"""
from __future__ import annotations

import json

from sqlalchemy import select

import bhg_adapter as bhg
from aggregator.adapters.alphartis import AhgAdapter
from aggregator.adapters.registry import get_adapters
from aggregator.models import Vehicle
from aggregator import sync as sync_module
from aggregator.sync import SyncEngine
from tests.test_sync import FakeBhg, make_raw as make_raw_bhg


def make_raw_app(object_id: str, app: str, *, location="Berlin", price=25000) -> dict:
    """Algolia-Hit mit Daten unter applicationData.<app> (Standort/cardData)."""
    return {
        "objectID": object_id,
        "make": "Audi",
        "model": "A4",
        "modelDescription": " 40 TDI ",
        "condition": "Gebraucht",
        "vehicleClass": "PKW",
        "category": "Kombi",
        "price": price,
        "originalPrice": price + 2000,
        "leasingPrice": 299.0,
        "financingPrice": 349.0,
        "financingDownPayment": 5000.0,
        "firstRegistration": "2022",
        "mileage": 30000,
        "power": 150,
        "fuel": "Diesel",
        "gearbox": "Automatik",
        "color": "Schwarz",
        "features": ["LED", "ACC"],
        "isReserved": False,
        "applicationData": {
            app: {
                "location": location,
                "cardData": {
                    "firstRegistration": "03/2022",
                    "url": f"https://www.{app}-mobile.de/fahrzeug/{object_id}",
                    "images": [f"https://images.ahg-mobile.de/{object_id}/1.jpg"],
                    "consumptionDescription": "4,8 l/100km",
                },
            }
        },
    }


class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"results": [self._payload]}


class FakeAlgolia:
    """Minimaler httpx.Client-Ersatz, der Algolia-Antworten nachbildet."""

    def __init__(self, hits_by_loc: dict[str, list[dict]]):
        self.hits_by_loc = hits_by_loc
        self.calls: list[dict] = []

    def post(self, url, headers=None, content=None, timeout=None):
        body = json.loads(content)
        req = body["requests"][0]
        self.calls.append({"headers": headers, "req": req})
        facet = req["facets"][0]
        if req["hitsPerPage"] == 0:
            counts = {loc: len(h) for loc, h in self.hits_by_loc.items()}
            return _FakeResp({"hits": [], "nbPages": 0, "facets": {facet: counts}})
        loc = next((l for l in self.hits_by_loc if f'{facet}:"{l}"' in req["filters"]), None)
        return _FakeResp({"hits": self.hits_by_loc.get(loc, []), "nbPages": 1, "facets": {}})


# --- Adapter-Modul (bhg_adapter.py): app-Parametrisierung -----------------
def test_bhg_backward_compatible():
    # Bestehende bhg-Konstanten unverändert.
    assert bhg.BASE_FILTER == 'language:"de" AND (applications:"bhg")'
    assert bhg.LOCATION_FACET == "applicationData.bhg.location"
    assert bhg.HEADERS["Origin"] == "https://www.bhg-mobile.de"
    assert bhg.HEADERS["Referer"] == "https://www.bhg-mobile.de/"
    # Default-Aufrufe verhalten sich wie bhg.
    v = bhg.normalize(make_raw_app("1", "bhg", location="Köln"))
    assert v["source"] == "bhg"
    assert v["location"] == "Köln"


def test_ahg_helpers():
    assert bhg.base_filter("ahg") == 'language:"de" AND (applications:"ahg")'
    assert bhg.location_facet("ahg") == "applicationData.ahg.location"
    h = bhg.build_headers("ahg")
    assert h["Origin"] == "https://www.ahg-mobile.de"
    assert h["Referer"] == "https://www.ahg-mobile.de/"
    # Application-Id/Key bleiben identisch zu bhg (kein neuer Key nötig).
    assert h["X-Algolia-Application-Id"] == bhg.APP_ID
    assert h["X-Algolia-API-Key"] == bhg.SEARCH_KEY


def test_ahg_fetch_and_normalize_local():
    """ahg liefert Fahrzeuge zurück (lokal simuliert) — Struktur & Wiring korrekt."""
    hits = [make_raw_app("x1", "ahg"), make_raw_app("x2", "ahg", location="Berlin")]
    client = FakeAlgolia({"Berlin": hits})

    raw = bhg.fetch_all_raw(client, "ahg")
    assert len(raw) == 2

    vehicles = [bhg.normalize(h, "ahg") for h in raw]
    assert all(v["source"] == "ahg" for v in vehicles)
    assert vehicles[0]["location"] == "Berlin"
    assert vehicles[0]["variant"] == "40 TDI"                 # getrimmt, Logik unverändert
    assert vehicles[0]["url"].startswith("https://www.ahg-mobile.de/")

    # Requests nutzten den ahg-Filter, das ahg-Facet und ahg-Header.
    assert any('applications:"ahg"' in c["req"]["filters"] for c in client.calls)
    assert client.calls[0]["req"]["facets"] == ["applicationData.ahg.location"]
    assert client.calls[0]["headers"]["Origin"] == "https://www.ahg-mobile.de"


# --- Registry / SYNC_SOURCES ---------------------------------------------
def test_registry_and_sync_sources_filter():
    assert [a.name for a in get_adapters()][:2] == ["bhg", "ahg"]
    assert [a.name for a in get_adapters(["ahg"])] == ["ahg"]
    assert [a.name for a in get_adapters(["bhg"])] == ["bhg"]
    assert {a.name for a in get_adapters(["bhg", "ahg"])} == {"bhg", "ahg"}


# --- Sync-Integration über beide Quellen ----------------------------------
class FakeAhg(AhgAdapter):
    """ahg-Adapter, aber fetch() liefert Fixtures; normalize() ist das echte."""

    def __init__(self, records):
        super().__init__()
        self._records = records

    def fetch(self):
        return list(self._records)


def test_sync_both_sources_distinct(db_session, monkeypatch):
    bhg_records = [make_raw_bhg("b1"), make_raw_bhg("b2")]
    ahg_records = [make_raw_app("a1", "ahg"), make_raw_app("a2", "ahg"), make_raw_app("a3", "ahg")]
    monkeypatch.setattr(
        sync_module, "get_adapters",
        lambda names=None: [FakeBhg(bhg_records), FakeAhg(ahg_records)],
    )

    report = SyncEngine().run()
    by_source = {r.source: r for r in report.results}
    assert by_source["bhg"].inserted == 2
    assert by_source["ahg"].inserted == 3

    rows = db_session.execute(select(Vehicle)).scalars().all()
    assert {v.source for v in rows} == {"bhg", "ahg"}
    assert {v.source_id for v in rows if v.source == "ahg"} == {"a1", "a2", "a3"}


def test_sync_one_source_does_not_touch_other(db_session, monkeypatch):
    # Erst beide anlegen (ahg mit 2 Fahrzeugen, damit das Sicherheitsnetz beim
    # Wegfall EINES Fahrzeugs nicht anspringt: 1/2 = 50% >= min_fetch_ratio).
    monkeypatch.setattr(
        sync_module, "get_adapters",
        lambda names=None: [
            FakeBhg([make_raw_bhg("b1")]),
            FakeAhg([make_raw_app("a1", "ahg"), make_raw_app("a2", "ahg")]),
        ],
    )
    SyncEngine().run()

    # Nur ahg syncen (a2 verschwindet) -> bhg bleibt unangetastet, a2 wird sold.
    monkeypatch.setattr(
        sync_module, "get_adapters",
        lambda names=None: [FakeAhg([make_raw_app("a1", "ahg")])],
    )
    SyncEngine().run(sources=["ahg"])

    rows = {(v.source, v.source_id): v for v in db_session.execute(select(Vehicle)).scalars()}
    assert rows[("bhg", "b1")].active is True          # bhg unberührt
    assert rows[("ahg", "a1")].active is True          # weiterhin im Bestand
    assert rows[("ahg", "a2")].active is False         # ahg verschwunden -> inaktiv
    assert rows[("ahg", "a2")].sold_at is not None
