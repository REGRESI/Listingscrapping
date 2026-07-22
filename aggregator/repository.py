"""Datenzugriff: Upsert und Bestands-Deaktivierung.

Kernidee für den idempotenten Abgleich: jeder Lauf hat einen einzigen
Zeitstempel ``run_ts``. Jedes in diesem Lauf gesehene Fahrzeug bekommt
``last_seen_at = run_ts``. Anschließend gelten genau die Fahrzeuge einer
Quelle als "verschwunden", die aktiv sind, aber in diesem Lauf NICHT gesehen
wurden (``last_seen_at < run_ts``) — diese werden auf ``active=false`` +
``sold_at`` gesetzt. Kein riesiger ``NOT IN``-Filter nötig.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from .models import Vehicle
from .schema import NormalizedVehicle


def count_active(session: Session, source: str) -> int:
    return session.execute(
        select(func.count()).select_from(Vehicle).where(
            Vehicle.source == source, Vehicle.active.is_(True)
        )
    ).scalar_one()


def upsert_vehicle(session: Session, vehicle: NormalizedVehicle, run_ts: datetime) -> str:
    """Fügt ein Fahrzeug ein oder aktualisiert es (Upsert über source+source_id).

    Rückgabe: 'inserted' oder 'updated'. Ein zurückgekehrtes (zuvor als
    verkauft markiertes) Fahrzeug wird reaktiviert (active=true, sold_at=NULL).
    """
    values = vehicle.upsert_values()
    values.update(
        source=vehicle.source,
        source_id=vehicle.source_id,
        active=True,
        sold_at=None,
        last_seen_at=run_ts,
        first_seen_at=run_ts,   # nur bei INSERT wirksam (server_default sonst)
    )

    stmt = pg_insert(Vehicle).values(**values)

    # Beim Konflikt: alle inhaltlichen Felder aktualisieren, reaktivieren,
    # last_seen_at/updated_at setzen. first_seen_at/created_at bleiben erhalten.
    update_cols = {c: getattr(stmt.excluded, c) for c in vehicle.upsert_values().keys()}
    update_cols.update(
        active=True,
        sold_at=None,
        last_seen_at=run_ts,
        updated_at=run_ts,
    )
    # Postgres-Trick: bei INSERT ist die System-Spalte xmax = 0, bei einem
    # ON-CONFLICT-UPDATE ungleich 0 -> unterscheidet inserted vs. updated.
    stmt = stmt.on_conflict_do_update(
        constraint="uq_vehicles_source_source_id",
        set_=update_cols,
    ).returning(literal_column("(xmax = 0)").label("inserted"))

    inserted = session.execute(stmt).scalar_one()
    return "inserted" if inserted else "updated"


def deactivate_missing(session: Session, source: str, run_ts: datetime) -> int:
    """Aktive Fahrzeuge der Quelle, die in diesem Lauf nicht gesehen wurden,
    auf inaktiv setzen (active=false, sold_at=run_ts). Idempotent: bereits
    inaktive Fahrzeuge werden nicht erneut angefasst.
    """
    result = session.execute(
        update(Vehicle)
        .where(
            Vehicle.source == source,
            Vehicle.active.is_(True),
            (Vehicle.last_seen_at.is_(None)) | (Vehicle.last_seen_at < run_ts),
        )
        .values(active=False, sold_at=run_ts, updated_at=run_ts)
    )
    return result.rowcount or 0
