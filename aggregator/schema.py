"""Einheitliches pydantic-Schema.

Eingang ist die Ausgabe von `normalize()` des jeweiligen Adapters. Die
Feldnamen entsprechen exakt dem bhg-Adapter; das Schema validiert/typisiert
sie und ist tolerant gegenüber locker getippten Rohwerten (Strings statt
Zahlen etc.), damit ein einzelner krummer Datensatz den Lauf nicht kippt.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Spalten, die beim Upsert aktualisiert werden dürfen (aus normalize()).
UPSERTABLE_FIELDS = (
    "make", "model", "variant", "condition", "vehicle_class", "category",
    "price", "original_price", "leasing_rate", "financing_rate",
    "financing_down_payment", "first_registration", "mileage_km", "power_kw",
    "fuel", "gearbox", "color", "consumption", "location", "reserved",
    "url", "features", "images", "raw", "scraped_at",
)

def _to_float(v: Any) -> float | None:
    """Robuste Zahl-Erkennung. Zahlen kommen i.d.R. schon numerisch aus dem
    Adapter; Strings werden tolerant geparst (auch dt. Format '24.500,50')."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    # nur Ziffern, Trenner und Vorzeichen behalten
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        # letztes Trennzeichen ist das Dezimaltrennzeichen
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")   # dt.: 24.500,50
        else:
            s = s.replace(",", "")                       # en.: 24,500.50
    elif "," in s:
        s = s.replace(",", ".")                          # 24500,50
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(v: Any) -> int | None:
    f = _to_float(v)
    return int(round(f)) if f is not None else None


class NormalizedVehicle(BaseModel):
    """Validierte Form eines normalisierten Fahrzeugs."""

    model_config = ConfigDict(extra="ignore")

    source: str
    source_id: str

    make: str | None = None
    model: str | None = None
    variant: str | None = None
    condition: str | None = None
    vehicle_class: str | None = None
    category: str | None = None

    price: float | None = None
    original_price: float | None = None
    leasing_rate: float | None = None
    financing_rate: float | None = None
    financing_down_payment: float | None = None

    first_registration: str | None = None
    mileage_km: int | None = None
    power_kw: int | None = None
    fuel: str | None = None
    gearbox: str | None = None
    color: str | None = None
    consumption: str | None = None
    location: str | None = None
    reserved: bool = False

    url: str | None = None
    features: list[str] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # --- Validatoren / Coercion ----------------------------------------
    @field_validator("source_id", mode="before")
    @classmethod
    def _sid_str(cls, v: Any) -> str:
        if v is None:
            raise ValueError("source_id fehlt")
        return str(v)

    @field_validator(
        "price", "original_price", "leasing_rate", "financing_rate",
        "financing_down_payment", mode="before",
    )
    @classmethod
    def _floats(cls, v: Any) -> float | None:
        return _to_float(v)

    @field_validator("mileage_km", "power_kw", mode="before")
    @classmethod
    def _ints(cls, v: Any) -> int | None:
        return _to_int(v)

    @field_validator("first_registration", mode="before")
    @classmethod
    def _reg_str(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    @field_validator("features", "images", mode="before")
    @classmethod
    def _list_or_empty(cls, v: Any) -> list:
        if v is None:
            return []
        if isinstance(v, list):
            return v
        return [v]

    def upsert_values(self) -> dict[str, Any]:
        """Nur die für den Upsert relevanten Spalten (ohne source/source_id)."""
        d = self.model_dump()
        return {k: d[k] for k in UPSERTABLE_FIELDS}
