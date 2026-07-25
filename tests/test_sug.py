"""SuG-Adapter (GraphQL, zweistufig): Stufe1 (_id sammeln) + Stufe2 (Details),
Feld-Mapping (Titel brand+model, Bilder, Ausstattung), fuel-Mapping, Sync.

Ohne echtes Netz (in dieser Umgebung geblockt): ein Fake-GraphQL-Client
beantwortet "Cars" (Liste der _id) und "Car" (Detail je _id).
"""
from __future__ import annotations

import json

from sqlalchemy import select

import sug_adapter as sug
from aggregator.adapters.registry import get_adapters
from aggregator.adapters.sug import SugAdapter
from aggregator.models import Vehicle
from aggregator.schema import NormalizedVehicle
from aggregator import sync as sync_module
from aggregator.sync import SyncEngine


def make_detail(_id: str, *, brand="BMW", model="320d Touring", name="AB-CD 123 Aktion",
                fuel="Diesel", price=25000) -> dict:
    """Vollständiger 'Car'-Detaildatensatz (Stufe 2)."""
    return {
        "_id": _id,
        "uid": f"uid-{_id}",
        "link": f"https://www.sug.de/fahrzeug/{_id}",
        "brand": brand,
        "model": model,
        "name": name,   # kryptische Händler-Überschrift -> NICHT als Titel
        "price": price,
        "mileage": 55000,
        "power": 140,
        "firstRegistration": "05/2021",
        "color": "Blau",
        "categories": ["Kombi"],
        "equipmentTranslations": ["Klimaautomatik", "Navigationssystem", "LED-Scheinwerfer"],
        "location": {"name": "SuG Stuttgart", "city": "Stuttgart", "phone": "0711 12345"},
        "engine": {"fuel": fuel, "gearbox": "Automatik"},
        "financing": {"rate": 249.0},
        "images": [
            {"imagepath": f"https://digiaccess.example/{_id}/1.jpg",
             "imagebigthumbpath": f"https://digiaccess.example/{_id}/1_big.jpg"},
            {"imagepath": f"https://digiaccess.example/{_id}/2.jpg",
             "imagebigthumbpath": f"https://digiaccess.example/{_id}/2_big.jpg"},
        ],
        "emission": "120 g/km",
        "fuelConsumption": "5,1 l/100km",
        "energyEfficiencyClass": "A",
        "wltp": {"combined": "5,1", "co": {"emission": "119"}},
        "vendor": "SuG",
    }


class _Resp:
    def __init__(self, data: dict):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": self._data}


class FakeSugAPI:
    """Beantwortet 'Cars' (Liste der _id, paginiert) und 'Car' (Detail)."""

    def __init__(self, page_ids: list[list[str]], details: dict[str, dict],
                 broken_ids: set[str] | None = None):
        self.page_ids = page_ids
        self.details = details
        self.broken_ids = broken_ids or set()
        self.total = sum(len(p) for p in page_ids)
        self.calls: list[dict] = []

    def post(self, url, headers=None, content=None, timeout=None):
        body = json.loads(content)
        self.calls.append({"op": body["operationName"], "headers": headers, "vars": body["variables"]})
        if body["operationName"] == "Cars":
            page = body["variables"]["pagination"]["page"]
            limit = body["variables"]["pagination"]["limit"]
            total_pages = len(self.page_ids)
            if limit == 1:
                return _Resp({"cars": {"docs": [], "totalDocs": self.total, "totalPages": total_pages}})
            ids = self.page_ids[page - 1] if 1 <= page <= total_pages else []
            return _Resp({"cars": {
                "docs": [{"_id": i} for i in ids],
                "totalDocs": self.total,
                "totalPages": total_pages,
            }})
        # operationName == "Car"
        _id = body["variables"]["_id"]
        if _id in self.broken_ids:
            return _ErrResp()   # GraphQL-Fehler -> Detailabruf scheitert
        return _Resp({"car": self.details.get(_id)})


class _ErrResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"errors": [{"message": "boom"}]}


# --- Stufe 1 --------------------------------------------------------------
def test_stage1_collects_ids_across_pages():
    api = FakeSugAPI([["a", "b"], ["c"]], {})
    ids = sug.fetch_all_ids(api, limit=2)
    assert ids == ["a", "b", "c"]
    pages = [c["vars"]["pagination"]["page"] for c in api.calls if c["op"] == "Cars"]
    assert pages == [1, 2]
    assert api.calls[0]["op"] == "Cars"
    assert api.calls[0]["headers"]["Origin"] == "https://www.sug.de"


def test_total_docs():
    api = FakeSugAPI([["a"], ["b"], ["c"]], {})
    assert sug.total_docs(api) == 3


# --- Stufe 2 (zweistufiger Abruf) -----------------------------------------
def test_two_stage_fetch_returns_full_details():
    ids = ["a", "b", "c"]
    details = {i: make_detail(i) for i in ids}
    api = FakeSugAPI([ids], details)
    raw = sug.fetch_all_raw(api, delay=0)
    assert {d["_id"] for d in raw} == set(ids)
    # Detaildaten sind vollständig (equipmentTranslations vorhanden).
    assert all("equipmentTranslations" in d for d in raw)
    # Es gab je _id genau eine "Car"-Detailabfrage.
    car_calls = [c for c in api.calls if c["op"] == "Car"]
    assert {c["vars"]["_id"] for c in car_calls} == set(ids)


def test_detail_error_is_skipped_not_fatal():
    ids = ["a", "b", "c"]
    details = {i: make_detail(i) for i in ids}
    api = FakeSugAPI([ids], details, broken_ids={"b"})
    raw = sug.fetch_all_raw(api, delay=0)
    # b scheitert -> übersprungen, a und c kommen zurück.
    assert {d["_id"] for d in raw} == {"a", "c"}


# --- Feld-Mapping ---------------------------------------------------------
def test_normalize_title_images_features():
    v = sug.normalize(make_detail("x1", brand="Audi", model="A3 Sportback", name="ZZ-99 Angebot"))
    # Titel = brand + model, NICHT name.
    assert v["make"] == "Audi"
    assert v["model"] == "A3 Sportback"
    assert v["raw"]["name"] == "ZZ-99 Angebot"      # name nur in raw
    # Bilder: vollständige URLs aus imagepath, als Liste.
    assert v["images"] == [
        "https://digiaccess.example/x1/1.jpg",
        "https://digiaccess.example/x1/2.jpg",
    ]
    # Ausstattung aus equipmentTranslations.
    assert v["features"] == ["Klimaautomatik", "Navigationssystem", "LED-Scheinwerfer"]
    # weitere Felder
    assert v["source"] == "sug"
    assert v["fuel"] == "Diesel"
    assert v["gearbox"] == "Automatik"
    assert v["url"] == "https://www.sug.de/fahrzeug/x1"
    assert v["location"] == "SuG Stuttgart"
    assert v["raw"]["location"]["phone"] == "0711 12345"


def test_images_fallback_to_bigthumb_when_no_imagepath():
    d = make_detail("x2")
    d["images"] = [{"imagebigthumbpath": "https://digiaccess.example/x2/only_big.jpg"}]
    assert sug.normalize(d)["images"] == ["https://digiaccess.example/x2/only_big.jpg"]


def test_relative_imagepath_gets_base_url_single_slash():
    d = make_detail("x3")
    d["images"] = [
        {"imagepath": "2026/07/10/20/43/2/original/20432_1.jpg"},
        {"imagepath": "2026/07/10/20/43/2/original/20432_2.jpg"},
    ]
    urls = sug.normalize(d)["images"]
    assert urls == [
        "https://www.sug-verwaltung.de/public/images/2026/07/10/20/43/2/original/20432_1.jpg",
        "https://www.sug-verwaltung.de/public/images/2026/07/10/20/43/2/original/20432_2.jpg",
    ]
    # kein doppelter Schrägstrich zwischen Basis und Pfad
    assert "public/images//" not in urls[0]


def test_full_image_url_helper_no_double_slash():
    # Basis ohne, Pfad mit führendem Slash -> trotzdem genau ein Slash
    assert sug._full_image_url("/a/b.jpg") == "https://www.sug-verwaltung.de/public/images/a/b.jpg"
    # bereits absolute URL bleibt unverändert
    assert sug._full_image_url("https://x/y.jpg") == "https://x/y.jpg"
    assert sug._full_image_url(None) is None


NESTED_EQUIPMENT = {
    "de": {
        "Komfort": ["Klimaanlage", "elektrische Fensterheber", "Sitzheizung"],
        "Sicherheit": ["ABS", "ESP", "ABS"],           # Duplikat innerhalb
        "Exterieur": ["LED-Scheinwerfer"],
        "Media": ["Navigationssystem", "Klimaanlage"],  # Duplikat kategorieübergreifend
        "Weitere Informationen": ["Klimaanlage", "irgendein Dubletten-Text"],  # ignoriert
    }
}


def test_nested_equipment_flattened_dedup_stable():
    d = make_detail("e1")
    d["equipmentTranslations"] = NESTED_EQUIPMENT
    feats = sug.normalize(d)["features"]
    assert feats == [
        "Klimaanlage",
        "elektrische Fensterheber",
        "Sitzheizung",
        "ABS",
        "ESP",
        "LED-Scheinwerfer",
        "Navigationssystem",
    ]
    # "Weitere Informationen" wurde ignoriert (kein zusätzlicher Dubletten-Text)
    assert "irgendein Dubletten-Text" not in feats


def test_equipment_language_fallback_when_no_de():
    d = make_detail("e2")
    d["equipmentTranslations"] = {"en": {"Comfort": ["Air conditioning", "Heated seats"]}}
    assert sug.normalize(d)["features"] == ["Air conditioning", "Heated seats"]


def test_consumption_summary():
    c = sug.normalize(make_detail("x1"))["consumption"]
    assert "Verbrauch: 5,1 l/100km" in c
    assert "Emission: 120 g/km" in c
    assert "CO2 kombiniert: 119" in c


# --- fuel-Codes (Regression) ---------------------------------------------
def test_fuel_numeric_code_is_mapped():
    d = make_detail("f1")
    d["engine"]["fuel"] = 2
    assert sug.normalize(d)["fuel"] == "Diesel"
    d["engine"]["fuel"] = "5"
    assert sug.normalize(d)["fuel"] == "Elektro"


def test_fuel_unknown_code_falls_back_to_string():
    d = make_detail("f2")
    d["engine"]["fuel"] = 9999
    v = sug.normalize(d)
    assert v["fuel"] == "9999"
    assert NormalizedVehicle.model_validate(v).fuel == "9999"


def test_int_fields_do_not_break_validation():
    d = make_detail("f3")
    d["engine"]["fuel"] = 3
    d["engine"]["gearbox"] = 10
    model = NormalizedVehicle.model_validate(sug.normalize(d))
    assert isinstance(model.fuel, str) and isinstance(model.gearbox, str)
    assert model.gearbox == "10"


# --- Registry / Sync ------------------------------------------------------
def test_registry_includes_sug_and_filter():
    assert "sug" in [a.name for a in get_adapters()]
    assert [a.name for a in get_adapters(["sug"])] == ["sug"]


class FakeSug(SugAdapter):
    """SuG-Adapter, aber fetch() liefert Detail-Fixtures; normalize() ist echt."""

    def __init__(self, records):
        super().__init__()
        self._records = records

    def fetch(self):
        return list(self._records)


def test_sync_sug_inserts_full_data(db_session, monkeypatch):
    records = [make_detail("s1"), make_detail("s2", price=42000)]
    monkeypatch.setattr(sync_module, "get_adapters", lambda names=None: [FakeSug(records)])

    report = SyncEngine().run(sources=["sug"])
    assert report.results[0].inserted == 2

    rows = db_session.execute(select(Vehicle).where(Vehicle.source == "sug")).scalars().all()
    assert {r.source_id for r in rows} == {"s1", "s2"}
    for r in rows:
        assert r.make == "BMW" and r.model == "320d Touring"
        assert len(r.images) == 2
        assert "Klimaautomatik" in r.features


def test_sync_sug_numeric_fuel_no_errors(db_session, monkeypatch):
    docs = []
    for i, code in enumerate([1, 2, 3, 5, 10, 9999]):
        d = make_detail(f"n{i}")
        d["engine"]["fuel"] = code
        docs.append(d)
    monkeypatch.setattr(sync_module, "get_adapters", lambda names=None: [FakeSug(docs)])

    report = SyncEngine().run(sources=["sug"])
    res = report.results[0]
    assert res.fetched == len(docs)
    assert res.errors == 0
    assert res.inserted == len(docs)
