"""Adapter für die Alphartis-Plattform (bhg, ahg, ...).

Dünner Wrapper um das gelieferte ``bhg_adapter.py`` im Projekt-Wurzel-
verzeichnis. Wir importieren dessen (app-parametrisierte) Funktionen und
ergänzen nur, was der Kern braucht: das SourceAdapter-Interface sowie
Netzwerk-Retries mit exponentiellem Backoff. Die Normalisierungslogik bleibt
komplett in ``bhg_adapter.py``.

Mehrere Autohausgruppen liegen im selben Algolia-Index und unterscheiden sich
nur durch das Anwendungskürzel (``app``). Jede Gruppe wird als eigener Adapter
mit eigenem ``name`` (== Feld ``source``) registriert.
"""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import get_settings
from ..logging_conf import get_logger
from .base import SourceAdapter

log = get_logger(__name__)

# --- das gelieferte Original-Modul aus dem Repo-Wurzelverzeichnis laden ---
_ROOT = Path(__file__).resolve().parents[2]
_BHG_PATH = _ROOT / "bhg_adapter.py"


def _load_original():
    spec = importlib.util.spec_from_file_location("bhg_adapter_original", _BHG_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"bhg_adapter.py nicht ladbar unter {_BHG_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_bhg = _load_original()

# Netzwerkfehler, bei denen ein Retry sinnvoll ist.
_RETRYABLE = (
    httpx.TransportError,   # Connect/Read/Write/Pool-Timeouts, Verbindungsabbrüche
    httpx.RemoteProtocolError,
)


class AlphartisAdapter(SourceAdapter):
    """Generischer Adapter für eine Alphartis-Anwendung.

    Unterklassen setzen ``name`` (== Kürzel == Feld ``source``). Der Kern
    braucht nichts weiter zu wissen.
    """

    #: Anwendungskürzel (bhg, ahg, ...). Standard = name.
    app: str = ""

    def __init__(self) -> None:
        self._settings = get_settings()
        if not self.app:
            self.app = self.name

    # -- discover -------------------------------------------------------
    def discover(self) -> dict[str, Any]:
        """Endpoint- und Bestands-Metadaten (Standort-Facette) ermitteln."""
        info: dict[str, Any] = {
            "app": self.app,
            "endpoint": _bhg.ENDPOINT,
            "index": _bhg.INDEX,
            "base_filter": _bhg.base_filter(self.app),
            "location_facet": _bhg.location_facet(self.app),
            "domain": _bhg.app_domain(self.app),
        }
        try:
            with httpx.Client(timeout=30) as client:
                locations = _bhg.list_locations(client, self.app)
            info["locations"] = locations
            info["total_reported"] = sum(locations.values()) if locations else 0
        except Exception as exc:  # pragma: no cover - nur Debug
            info["discover_error"] = repr(exc)
        return info

    # -- fetch ----------------------------------------------------------
    def fetch(self) -> list[dict[str, Any]]:
        """Alle Rohfahrzeuge der Anwendung holen, mit Retry/Backoff bei Netzfehlern."""
        attempts = max(1, self._settings.fetch_max_retries)
        backoff = self._settings.fetch_retry_backoff
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                transport = httpx.HTTPTransport(retries=2)
                with httpx.Client(transport=transport, timeout=30) as client:
                    raw = _bhg.fetch_all_raw(client, self.app)
                log.info("%s fetch: %d Rohdatensätze geholt", self.app, len(raw))
                return raw
            except _RETRYABLE as exc:
                last_exc = exc
                if attempt < attempts:
                    wait = backoff ** attempt
                    log.warning(
                        "%s fetch Netzwerkfehler (Versuch %d/%d): %s — retry in %.0fs",
                        self.app, attempt, attempts, exc, wait,
                    )
                    time.sleep(wait)
                else:
                    log.error("%s fetch endgültig fehlgeschlagen: %s", self.app, exc)
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                status = exc.response.status_code if exc.response else 0
                if 500 <= status < 600 and attempt < attempts:
                    wait = backoff ** attempt
                    log.warning("%s fetch HTTP %s — retry in %.0fs", self.app, status, wait)
                    time.sleep(wait)
                else:
                    log.error("%s fetch HTTP-Fehler %s — Abbruch", self.app, status)
                    raise
        assert last_exc is not None
        raise last_exc

    # -- normalize ------------------------------------------------------
    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        return _bhg.normalize(raw, self.app)


class BhgAdapter(AlphartisAdapter):
    name = "bhg"
    app = "bhg"


class AhgAdapter(AlphartisAdapter):
    name = "ahg"
    app = "ahg"
