"""Basisklasse für Quell-Adapter (Adapter-Muster).

Jeder Adapter kapselt genau eine Plattform. Der Kern (Sync, DB, API) kennt
nur dieses Interface — neue Plattformen kommen als neuer Adapter dazu, ohne
den Kern anzufassen.

Vertrag:
    name        eindeutiger Quell-Schlüssel (== Feld `source` der Datensätze)
    discover()  optionale Endpoint-Analyse; gibt Metadaten zurück (Debug)
    fetch()     holt ALLE Rohdatensätze der Quelle (Liste dicts)
    normalize() übersetzt EINEN Rohdatensatz in das einheitliche Schema
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..schema import NormalizedVehicle


class SourceAdapter(ABC):
    #: Eindeutiger Quell-Name; landet als `source` in der DB.
    name: str = ""

    @abstractmethod
    def discover(self) -> dict[str, Any]:
        """Endpoint/Struktur ermitteln (optional, v.a. für Debug/Doku)."""
        raise NotImplementedError

    @abstractmethod
    def fetch(self) -> list[dict[str, Any]]:
        """Alle Roh-Fahrzeuge der Quelle holen."""
        raise NotImplementedError

    @abstractmethod
    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Einen Rohdatensatz in das einheitliche (dict-)Schema übersetzen."""
        raise NotImplementedError

    def normalize_validated(self, raw: dict[str, Any]) -> NormalizedVehicle:
        """normalize() + pydantic-Validierung. Stellt `source` sicher."""
        data = self.normalize(raw)
        data.setdefault("source", self.name)
        return NormalizedVehicle.model_validate(data)
