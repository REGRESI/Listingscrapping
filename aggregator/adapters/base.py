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

    def slim_raw(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Kompakte Teilmenge des raw-Feldes für die Speicherung.

        Standard: nichts speichern ({}) — alle Anzeigefelder stehen in den
        Tabellenspalten, das volle raw ist der größte Speicherfresser. Adapter,
        die gezielt Zusatzdaten behalten wollen (z.B. SuG: Adresse/Telefon +
        deutscher Ausstattungsblock), überschreiben diese Methode.
        """
        return {}

    def normalize_validated(self, raw: dict[str, Any]) -> NormalizedVehicle:
        """normalize() + pydantic-Validierung. Stellt `source` sicher und
        speichert nur ein verschlanktes raw."""
        data = self.normalize(raw)
        data.setdefault("source", self.name)
        data["raw"] = self.slim_raw(data.get("raw") or {})
        return NormalizedVehicle.model_validate(data)
