"""Speicher-Optimierung: raw-Slimming pro Quelle + Maintenance-Aufräumen."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

import sug_adapter as sug
from aggregator import sync as sync_module
from aggregator.adapters.alphartis import BhgAdapter
from aggregator.adapters.sug import SugAdapter
from aggregator.maintenance import delete_stale_inactive, reslim_existing_raw
from aggregator.models import Vehicle
from aggregator.sync import SyncEngine
from tests.test_sug import FakeSug, make_detail
from tests.test_sync import FakeBhg, make_raw as make_raw_bhg


NESTED_EQUIP = {
    "de": {"Komfort": ["Klimaanlage", "Sitzheizung"], "Sicherheit": ["ABS"]},
    "en": {"Comfort": ["Air conditioning"]},
    "fr": {"Confort": ["Climatisation"]},
}


# --- raw-Slimming pro Adapter --------------------------------------------
def test_sug_slim_raw_keeps_only_german_and_location():
    doc = make_detail("s1")
    doc["equipmentTranslations"] = NESTED_EQUIP
    slim = sug.slim_raw(doc)
    # nur der deutsche Block, keine weiteren Sprachen
    assert set(slim["equipmentTranslations"].keys()) == {"de"}
    assert slim["equipmentTranslations"]["de"] == NESTED_EQUIP["de"]
    # Standort (Adresse/Telefon) bleibt erhalten
    assert slim["location"]["phone"] == "0711 12345"
    # große/überflüssige Felder sind weg
    assert "wltp" not in slim and "name" not in slim and "images" not in slim


def test_alphartis_slim_raw_is_empty():
    assert BhgAdapter().slim_raw({"make": "VW", "big": "x" * 1000}) == {}


def test_sync_stores_slim_raw(db_session, monkeypatch):
    bhg_records = [make_raw_bhg("b1")]
    ahg = None
    sug_doc = make_detail("s1")
    sug_doc["equipmentTranslations"] = NESTED_EQUIP
    monkeypatch.setattr(
        sync_module, "get_adapters",
        lambda names=None: [FakeBhg(bhg_records), FakeSug([sug_doc])],
    )
    SyncEngine().run()

    rows = {(v.source, v.source_id): v for v in db_session.execute(select(Vehicle)).scalars()}
    # bhg: kein raw gespeichert
    assert rows[("bhg", "b1")].raw == {}
    # sug: nur deutscher Ausstattungsblock + Standort
    sug_raw = rows[("sug", "s1")].raw
    assert set(sug_raw["equipmentTranslations"].keys()) == {"de"}
    assert sug_raw["location"]["phone"] == "0711 12345"
    # Anzeigefelder trotzdem vollständig (aus Spalten):
    assert rows[("sug", "s1")].features and rows[("sug", "s1")].images


# --- Maintenance: Löschen alter Inaktiver --------------------------------
def _add(session, source, sid, *, active, last_seen):
    session.add(Vehicle(source=source, source_id=sid, make="VW", model="Golf",
                        active=active, last_seen_at=last_seen, first_seen_at=last_seen,
                        sold_at=(None if active else last_seen), raw={}))


def test_delete_stale_inactive(db_session):
    now = datetime.now(timezone.utc)
    _add(db_session, "sug", "active_recent", active=True, last_seen=now)
    _add(db_session, "sug", "inactive_recent", active=False, last_seen=now - timedelta(days=5))
    _add(db_session, "sug", "inactive_old", active=False, last_seen=now - timedelta(days=45))
    _add(db_session, "sug", "inactive_edge", active=False, last_seen=now - timedelta(days=31))
    db_session.commit()

    deleted = delete_stale_inactive(db_session, older_than_days=30)
    assert deleted == 2   # old + edge

    remaining = {v.source_id for v in db_session.execute(select(Vehicle)).scalars()}
    assert remaining == {"active_recent", "inactive_recent"}


def test_reslim_existing_raw_shrinks_stored_raw(db_session):
    now = datetime.now(timezone.utc)
    fat = make_detail("s1")
    fat["equipmentTranslations"] = NESTED_EQUIP
    db_session.add(Vehicle(source="sug", source_id="s1", make="VW", model="Golf",
                           active=True, last_seen_at=now, first_seen_at=now, raw=fat))
    db_session.add(Vehicle(source="bhg", source_id="b1", make="VW", model="Golf",
                           active=True, last_seen_at=now, first_seen_at=now,
                           raw={"objectID": "b1", "big": "x" * 500}))
    db_session.commit()

    updated = reslim_existing_raw(db_session)
    assert updated == 2

    rows = {(v.source, v.source_id): v for v in db_session.execute(select(Vehicle)).scalars()}
    assert rows[("bhg", "b1")].raw == {}
    assert set(rows[("sug", "s1")].raw["equipmentTranslations"].keys()) == {"de"}
