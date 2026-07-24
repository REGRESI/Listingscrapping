"""End-to-End-Tests der Sync-Logik gegen echtes Postgres, ohne Netz.

Ein FakeBhg-Adapter liefert Algolia-förmige Rohdaten, die vom ECHTEN
``bhg_adapter.normalize()`` verarbeitet werden — so wird der reale Pfad
(normalize -> Schema -> Upsert -> Deaktivierung) geprüft.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from aggregator.adapters.alphartis import BhgAdapter
from aggregator.models import Vehicle
from aggregator.repository import count_active
from aggregator import sync as sync_module
from aggregator.sync import SyncEngine


def make_raw(object_id: str, *, price=25000, make="Volkswagen", model="Golf",
             reserved=False, location="Musterstadt") -> dict:
    """Algolia-Hit in der Struktur, die bhg_adapter.normalize() erwartet."""
    return {
        "objectID": object_id,
        "make": make,
        "model": model,
        "modelDescription": "  1.5 TSI Life  ",
        "condition": "Gebraucht",
        "vehicleClass": "PKW",
        "category": "Limousine",
        "price": price,
        "originalPrice": price + 1500,
        "leasingPrice": 199.0,
        "financingPrice": 249.0,
        "financingDownPayment": 3000.0,
        "firstRegistration": "2021",
        "mileage": 42000,
        "power": 110,
        "fuel": "Benzin",
        "gearbox": "Automatik",
        "color": "Grau",
        "features": ["Klima", "Navi"],
        "isReserved": reserved,
        "applicationData": {
            "bhg": {
                "location": location,
                "cardData": {
                    "firstRegistration": "06/2021",
                    "url": f"https://www.bhg-mobile.de/fahrzeug/{object_id}",
                    "images": [f"https://images.ahg-mobile.de/{object_id}/1.jpg"],
                    "consumptionDescription": "5,2 l/100km",
                },
            }
        },
    }


class FakeBhg(BhgAdapter):
    """bhg-Adapter, aber fetch() liefert Fixtures statt Netzabruf.
    normalize() bleibt das echte aus bhg_adapter.py."""

    def __init__(self, records):
        super().__init__()
        self._records = records

    def fetch(self):
        return list(self._records)


def _run_with(monkeypatch, adapter) -> None:
    monkeypatch.setattr(sync_module, "get_adapters", lambda names=None: [adapter])
    return SyncEngine().run()


def _all(session):
    return session.execute(select(Vehicle)).scalars().all()


def test_insert_then_idempotent(db_session, monkeypatch):
    records = [make_raw(f"v{i}", price=20000 + i) for i in range(5)]

    r1 = _run_with(monkeypatch, FakeBhg(records))
    res1 = r1.results[0]
    assert res1.fetched == 5
    assert res1.inserted == 5
    assert res1.updated == 0
    assert res1.deactivated == 0
    assert count_active(db_session, "bhg") == 5

    # Zweiter Lauf, identische Daten -> gleicher Zustand (idempotent).
    r2 = _run_with(monkeypatch, FakeBhg(records))
    res2 = r2.results[0]
    assert res2.inserted == 0
    assert res2.updated == 5
    assert res2.deactivated == 0
    assert count_active(db_session, "bhg") == 5


def test_price_update_and_normalization(db_session, monkeypatch):
    _run_with(monkeypatch, FakeBhg([make_raw("v1", price=20000)]))
    _run_with(monkeypatch, FakeBhg([make_raw("v1", price=18500)]))

    v = db_session.execute(select(Vehicle).where(Vehicle.source_id == "v1")).scalar_one()
    assert float(v.price) == 18500.0
    # genauere firstRegistration aus cardData (MM/YYYY) gewinnt
    assert v.first_registration == "06/2021"
    assert v.variant == "1.5 TSI Life"       # getrimmt
    assert v.images == ["https://images.ahg-mobile.de/v1/1.jpg"]
    assert v.updated_at is not None


def test_disappeared_marked_sold(db_session, monkeypatch):
    records = [make_raw("v1"), make_raw("v2"), make_raw("v3")]
    _run_with(monkeypatch, FakeBhg(records))

    # v2 verschwindet aus dem Bestand.
    _run_with(monkeypatch, FakeBhg([make_raw("v1"), make_raw("v3")]))

    v2 = db_session.execute(select(Vehicle).where(Vehicle.source_id == "v2")).scalar_one()
    assert v2.active is False
    assert v2.sold_at is not None
    assert count_active(db_session, "bhg") == 2


def test_reappeared_reactivated(db_session, monkeypatch):
    _run_with(monkeypatch, FakeBhg([make_raw("v1"), make_raw("v2")]))
    _run_with(monkeypatch, FakeBhg([make_raw("v1")]))          # v2 weg
    _run_with(monkeypatch, FakeBhg([make_raw("v1"), make_raw("v2")]))  # v2 zurück

    v2 = db_session.execute(select(Vehicle).where(Vehicle.source_id == "v2")).scalar_one()
    assert v2.active is True
    assert v2.sold_at is None
    assert count_active(db_session, "bhg") == 2


def test_safety_net_skips_deactivation(db_session, monkeypatch):
    # 10 Fahrzeuge aufbauen.
    base = [make_raw(f"v{i}") for i in range(10)]
    _run_with(monkeypatch, FakeBhg(base))
    assert count_active(db_session, "bhg") == 10

    # Teilausfall: nur 2 von 10 kommen (< 50 %) -> Deaktivierung übersprungen.
    r = _run_with(monkeypatch, FakeBhg([make_raw("v0"), make_raw("v1")]))
    assert r.results[0].skipped_deactivation is True
    assert count_active(db_session, "bhg") == 10   # Bestand unverändert


def test_bad_record_does_not_abort(db_session, monkeypatch):
    good = make_raw("v1")
    bad = {"no_object_id": True}   # KeyError in normalize -> übersprungen
    r = _run_with(monkeypatch, FakeBhg([good, bad, make_raw("v2")]))
    res = r.results[0]
    assert res.errors == 1
    assert res.inserted == 2
    assert count_active(db_session, "bhg") == 2
