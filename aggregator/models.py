"""ORM-Modell für die Tabelle `vehicles`.

Die Spalten spiegeln 1:1 die Ausgabe von `normalize()` des Adapters wider,
ergänzt um Bestands-/Lebenszyklus-Felder (active, sold_at, Zeitstempel).
Eindeutiger Schlüssel: (source, source_id).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Vehicle(Base):
    __tablename__ = "vehicles"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_vehicles_source_source_id"),
        Index("ix_vehicles_active", "active"),
        Index("ix_vehicles_make", "make"),
        Index("ix_vehicles_fuel", "fuel"),
        Index("ix_vehicles_condition", "condition"),
        Index("ix_vehicles_price", "price"),
        Index("ix_vehicles_source_active", "source", "active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Identität ----------------------------------------------------------
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # Fahrzeugdaten (aus normalize()) -----------------------------------
    make: Mapped[str | None] = mapped_column(String(128))
    model: Mapped[str | None] = mapped_column(String(256))
    variant: Mapped[str | None] = mapped_column(String(512))
    condition: Mapped[str | None] = mapped_column(String(64))
    vehicle_class: Mapped[str | None] = mapped_column(String(64))
    category: Mapped[str | None] = mapped_column(String(128))

    price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    original_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    leasing_rate: Mapped[float | None] = mapped_column(Numeric(12, 2))
    financing_rate: Mapped[float | None] = mapped_column(Numeric(12, 2))
    financing_down_payment: Mapped[float | None] = mapped_column(Numeric(12, 2))

    first_registration: Mapped[str | None] = mapped_column(String(16))  # MM/YYYY oder YYYY
    mileage_km: Mapped[int | None] = mapped_column(Integer)
    power_kw: Mapped[int | None] = mapped_column(Integer)
    fuel: Mapped[str | None] = mapped_column(String(64))
    gearbox: Mapped[str | None] = mapped_column(String(64))
    color: Mapped[str | None] = mapped_column(String(128))
    consumption: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(String(256))
    reserved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    url: Mapped[str | None] = mapped_column(Text)
    features: Mapped[list] = mapped_column(JSONB, default=list)
    images: Mapped[list] = mapped_column(JSONB, default=list)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Lebenszyklus / Bestand --------------------------------------------
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Vehicle {self.source}:{self.source_id} "
            f"{self.make} {self.model} active={self.active}>"
        )
