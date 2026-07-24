"""Adapter-Registry. Neue Plattformen/Gruppen hier eintragen — sonst nichts am Kern."""
from __future__ import annotations

from .alphartis import AhgAdapter, BhgAdapter
from .base import SourceAdapter
from .sug import SugAdapter

# Alle verfügbaren Adapter (Klassen).
ADAPTER_CLASSES: tuple[type[SourceAdapter], ...] = (
    BhgAdapter,
    AhgAdapter,
    SugAdapter,
    # Weitere Autohausgruppen/Plattformen hier ergänzen.
)


def all_adapters() -> list[SourceAdapter]:
    return [cls() for cls in ADAPTER_CLASSES]


def get_adapters(names: list[str] | None = None) -> list[SourceAdapter]:
    """Adapter-Instanzen; optional gefiltert nach Namen (== SYNC_SOURCES)."""
    adapters = all_adapters()
    if not names:
        return adapters
    wanted = {n.strip().lower() for n in names if n.strip()}
    return [a for a in adapters if a.name.lower() in wanted]
