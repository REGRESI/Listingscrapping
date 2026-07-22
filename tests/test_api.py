"""Smoke-Tests der FastAPI gegen echtes Postgres."""
from __future__ import annotations

from fastapi.testclient import TestClient

from aggregator.api import app
from aggregator.adapters.bhg import BhgAdapter
from aggregator import sync as sync_module
from aggregator.sync import SyncEngine
from tests.test_sync import FakeBhg, make_raw


def _seed(monkeypatch):
    records = [
        make_raw("a1", price=15000, make="Volkswagen"),
        make_raw("a2", price=30000, make="Audi"),
        make_raw("a3", price=45000, make="Volkswagen"),
    ]
    monkeypatch.setattr(sync_module, "get_adapters", lambda names=None: [FakeBhg(records)])
    SyncEngine().run()


def test_list_and_filters(db_session, monkeypatch):
    _seed(monkeypatch)
    client = TestClient(app)

    # alle aktiven
    r = client.get("/vehicles")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3

    # Filter Marke
    r = client.get("/vehicles", params={"make": "Volkswagen"})
    assert r.json()["total"] == 2

    # Filter Preisbereich
    r = client.get("/vehicles", params={"price_min": 20000, "price_max": 40000})
    ids = [i["source_id"] for i in r.json()["items"]]
    assert ids == ["a2"]

    # Filter Kraftstoff + condition
    r = client.get("/vehicles", params={"fuel": "Benzin", "condition": "Gebraucht"})
    assert r.json()["total"] == 3


def test_makes_and_health(db_session, monkeypatch):
    _seed(monkeypatch)
    client = TestClient(app)
    assert client.get("/meta/makes").json() == ["Audi", "Volkswagen"]
    assert client.get("/healthz").json()["active_vehicles"] == 3


def test_csv_export(db_session, monkeypatch):
    _seed(monkeypatch)
    client = TestClient(app)
    r = client.get("/vehicles.csv", params={"make": "Volkswagen"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    assert lines[0].startswith("id,source,source_id")
    assert len(lines) == 1 + 2   # Header + 2 VW


def test_only_active_exposed(db_session, monkeypatch):
    # a2 verkauft -> darf nicht mehr auftauchen
    records = [make_raw("a1"), make_raw("a2")]
    monkeypatch.setattr(sync_module, "get_adapters", lambda names=None: [FakeBhg(records)])
    SyncEngine().run()
    monkeypatch.setattr(sync_module, "get_adapters", lambda names=None: [FakeBhg([make_raw("a1")])])
    SyncEngine().run()

    client = TestClient(app)
    body = client.get("/vehicles").json()
    assert body["total"] == 1
    assert body["items"][0]["source_id"] == "a1"
    # Einzelabruf des verkauften Fahrzeugs -> 404
    from aggregator.models import Vehicle
    from sqlalchemy import select
    sold = db_session.execute(select(Vehicle).where(Vehicle.source_id == "a2")).scalar_one()
    assert client.get(f"/vehicles/{sold.id}").status_code == 404
