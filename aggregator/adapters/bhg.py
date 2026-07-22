"""Adapter für bhg / Alphartis.

Dünner Wrapper um das gelieferte, unveränderte ``bhg_adapter.py`` im
Projekt-Wurzelverzeichnis. Wir importieren dessen Funktionen und ergänzen
nur das, was der Kern braucht: das SourceAdapter-Interface sowie
Netzwerk-Retries mit exponentiellem Backoff (der Original-Adapter bleibt
unangetastet).
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

# --- das unveränderte Original-Modul aus dem Repo-Wurzelverzeichnis laden ---
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


class BhgAdapter(SourceAdapter):
    name = "bhg"

    def __init__(self) -> None:
        self._settings = get_settings()

    # -- discover -------------------------------------------------------
    def discover(self) -> dict[str, Any]:
        """Endpoint- und Bestands-Metadaten (Standort-Facette) ermitteln."""
        info: dict[str, Any] = {
            "endpoint": _bhg.ENDPOINT,
            "index": _bhg.INDEX,
            "base_filter": _bhg.BASE_FILTER,
            "location_facet": _bhg.LOCATION_FACET,
        }
        try:
            with httpx.Client(timeout=30) as client:
                locations = _bhg.list_locations(client)
            info["locations"] = locations
            info["total_reported"] = sum(locations.values()) if locations else 0
        except Exception as exc:  # pragma: no cover - nur Debug
            info["discover_error"] = repr(exc)
        return info

    # -- fetch ----------------------------------------------------------
    def fetch(self) -> list[dict[str, Any]]:
        """Alle bhg-Rohfahrzeuge holen, mit Retry/Backoff bei Netzfehlern.

        ``fetch_all_raw`` akzeptiert einen httpx.Client — wir reichen einen
        Client mit transport-level Retries hinein, ohne den Adapter selbst
        zu ändern.
        """
        attempts = max(1, self._settings.fetch_max_retries)
        backoff = self._settings.fetch_retry_backoff
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                transport = httpx.HTTPTransport(retries=2)
                with httpx.Client(transport=transport, timeout=30) as client:
                    raw = _bhg.fetch_all_raw(client)
                log.info("bhg fetch: %d Rohdatensätze geholt", len(raw))
                return raw
            except _RETRYABLE as exc:
                last_exc = exc
                if attempt < attempts:
                    wait = backoff ** attempt
                    log.warning(
                        "bhg fetch Netzwerkfehler (Versuch %d/%d): %s — retry in %.0fs",
                        attempt, attempts, exc, wait,
                    )
                    time.sleep(wait)
                else:
                    log.error("bhg fetch endgültig fehlgeschlagen: %s", exc)
            except httpx.HTTPStatusError as exc:
                # 4xx/5xx: bei 5xx retryen, bei 4xx sofort abbrechen.
                last_exc = exc
                status = exc.response.status_code if exc.response else 0
                if 500 <= status < 600 and attempt < attempts:
                    wait = backoff ** attempt
                    log.warning("bhg fetch HTTP %s — retry in %.0fs", status, wait)
                    time.sleep(wait)
                else:
                    log.error("bhg fetch HTTP-Fehler %s — Abbruch", status)
                    raise
        assert last_exc is not None
        raise last_exc

    # -- normalize ------------------------------------------------------
    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        return _bhg.normalize(raw)
