"""FastAPI — schlanke Ausgabe des Bestands für die Webseite.

Endpunkte:
    GET /vehicles       gefilterte JSON-Liste (nur active=true)
    GET /vehicles.csv   dieselben Filter als CSV-Download
    GET /vehicles/{id}  Einzelfahrzeug
    GET /meta/makes     verfügbare Marken (für Filter-Dropdowns)
    GET /healthz        Health-Check

Diese API liest nur aus der DB — sie macht KEINE Netzabrufe und läuft daher
problemlos auch in abgeschotteten Umgebungen. Befüllt wird die DB vom
Sync-Job (aggregator.sync / aggregator.scheduler).
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import distinct, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import SessionLocal, init_db
from .models import Vehicle

settings = get_settings()
app = FastAPI(
    title="Regresi Fahrzeug-Aggregator API",
    version="1.0.0",
    description="Aggregierter Fahrzeugbestand der Partner-Autohäuser.",
)


@app.on_event("startup")
def _ensure_schema() -> None:
    """Tabellen anlegen, falls die DB frisch ist und der Web-Dienst vor dem
    ersten Sync startet. create_all ist idempotent — ändert nichts, wenn die
    Tabellen bereits existieren."""
    init_db()


def get_session() -> Session:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# --- Antwort-Schemas ---------------------------------------------------
class VehicleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    source_id: str
    make: str | None
    model: str | None
    variant: str | None
    condition: str | None
    vehicle_class: str | None
    category: str | None
    price: float | None
    original_price: float | None
    leasing_rate: float | None
    financing_rate: float | None
    financing_down_payment: float | None
    first_registration: str | None
    mileage_km: int | None
    power_kw: int | None
    fuel: str | None
    gearbox: str | None
    color: str | None
    consumption: str | None
    location: str | None
    reserved: bool
    url: str | None
    features: list[Any]
    images: list[Any]
    active: bool
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    scraped_at: datetime | None


class VehicleListOut(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[VehicleOut]


# --- Filter-Logik (geteilt von JSON und CSV) ---------------------------
def _apply_filters(
    stmt,
    make: list[str] | None,
    fuel: list[str] | None,
    condition: list[str] | None,
    price_min: float | None,
    price_max: float | None,
    location: str | None,
    source: str | None,
    q: str | None,
):
    stmt = stmt.where(Vehicle.active.is_(True))
    if make:
        stmt = stmt.where(Vehicle.make.in_(make))
    if fuel:
        stmt = stmt.where(Vehicle.fuel.in_(fuel))
    if condition:
        stmt = stmt.where(Vehicle.condition.in_(condition))
    if price_min is not None:
        stmt = stmt.where(Vehicle.price >= price_min)
    if price_max is not None:
        stmt = stmt.where(Vehicle.price <= price_max)
    if location:
        stmt = stmt.where(Vehicle.location == location)
    if source:
        stmt = stmt.where(Vehicle.source == source)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            Vehicle.make.ilike(like)
            | Vehicle.model.ilike(like)
            | Vehicle.variant.ilike(like)
        )
    return stmt


# Filter-Query-Parameter als wiederverwendbare Dependency.
def filter_params(
    make: list[str] | None = Query(default=None, description="Marke(n), z.B. Volkswagen"),
    fuel: list[str] | None = Query(default=None, description="Kraftstoff(e), z.B. Elektro"),
    condition: list[str] | None = Query(
        default=None, description="Zustand: Neu/Gebraucht/Jahreswagen/Vorführwagen"
    ),
    price_min: float | None = Query(default=None, ge=0, description="Mindestpreis"),
    price_max: float | None = Query(default=None, ge=0, description="Höchstpreis"),
    location: str | None = Query(default=None, description="Standort"),
    source: str | None = Query(default=None, description="Quelle, z.B. bhg"),
    q: str | None = Query(default=None, description="Freitext (Marke/Modell/Variante)"),
) -> dict[str, Any]:
    return dict(
        make=make, fuel=fuel, condition=condition, price_min=price_min,
        price_max=price_max, location=location, source=source, q=q,
    )


_SORT_COLUMNS = {
    "price": Vehicle.price,
    "mileage_km": Vehicle.mileage_km,
    "first_registration": Vehicle.first_registration,
    "first_seen_at": Vehicle.first_seen_at,
    "power_kw": Vehicle.power_kw,
}


@app.get("/vehicles", response_model=VehicleListOut)
def list_vehicles(
    filters: dict = Depends(filter_params),
    limit: int = Query(default=settings.api_default_limit, ge=1, le=settings.api_max_limit),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="first_seen_at", description=f"Sortierfeld: {list(_SORT_COLUMNS)}"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    session: Session = Depends(get_session),
) -> VehicleListOut:
    from sqlalchemy import func

    base = _apply_filters(select(Vehicle), **filters)

    total = session.execute(
        _apply_filters(select(func.count(Vehicle.id)), **filters)
    ).scalar_one()

    col = _SORT_COLUMNS.get(sort, Vehicle.first_seen_at)
    col = col.desc() if order == "desc" else col.asc()
    rows = session.execute(
        base.order_by(col, Vehicle.id.desc()).limit(limit).offset(offset)
    ).scalars().all()

    return VehicleListOut(
        total=total,
        limit=limit,
        offset=offset,
        items=[VehicleOut.model_validate(r) for r in rows],
    )


_CSV_FIELDS = [
    "id", "source", "source_id", "make", "model", "variant", "condition",
    "vehicle_class", "category", "price", "original_price", "leasing_rate",
    "financing_rate", "financing_down_payment", "first_registration",
    "mileage_km", "power_kw", "fuel", "gearbox", "color", "location",
    "reserved", "url", "first_seen_at", "last_seen_at", "scraped_at",
]


@app.get("/vehicles.csv")
def export_csv(
    filters: dict = Depends(filter_params),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    stmt = _apply_filters(select(Vehicle), **filters).order_by(Vehicle.id.asc())

    def rows():
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)
        # streamend, damit auch der volle Bestand ohne Speicherdruck geht
        for v in session.execute(stmt).scalars().yield_per(500):
            writer.writerow({f: getattr(v, f) for f in _CSV_FIELDS})
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)

    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="vehicles.csv"'},
    )


@app.get("/vehicles/{vehicle_id}", response_model=VehicleOut)
def get_vehicle(vehicle_id: int, session: Session = Depends(get_session)) -> VehicleOut:
    v = session.get(Vehicle, vehicle_id)
    if v is None or not v.active:
        raise HTTPException(status_code=404, detail="Fahrzeug nicht gefunden")
    return VehicleOut.model_validate(v)


@app.get("/meta/makes")
def list_makes(session: Session = Depends(get_session)) -> list[str]:
    rows = session.execute(
        select(distinct(Vehicle.make))
        .where(Vehicle.active.is_(True), Vehicle.make.is_not(None))
        .order_by(Vehicle.make.asc())
    ).scalars().all()
    return list(rows)


@app.get("/healthz")
def healthz(session: Session = Depends(get_session)) -> dict[str, Any]:
    from sqlalchemy import func

    total = session.execute(
        select(func.count(Vehicle.id)).where(Vehicle.active.is_(True))
    ).scalar_one()
    return {"status": "ok", "active_vehicles": total}
