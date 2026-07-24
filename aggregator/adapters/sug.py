"""Adapter für SuG (sug.de) — GraphQL-Quelle.

Dünner Wrapper um das gelieferte ``sug_adapter.py`` im Projekt-Wurzel-
verzeichnis. Importiert dessen GraphQL-Fetch + normalize und ergänzt nur das
SourceAdapter-Interface sowie Netzwerk-Retries mit exponentiellem Backoff.
Die Normalisierungslogik bleibt in ``sug_adapter.py``.
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

_ROOT = Path(__file__).resolve().parents[2]
_SUG_PATH = _ROOT / "sug_adapter.py"


def _load_original():
    spec = importlib.util.spec_from_file_location("sug_adapter_original", _SUG_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"sug_adapter.py nicht ladbar unter {_SUG_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_sug = _load_original()

_RETRYABLE = (
    httpx.TransportError,
    httpx.RemoteProtocolError,
)


class SugAdapter(SourceAdapter):
    name = "sug"

    def __init__(self) -> None:
        self._settings = get_settings()

    # -- discover -------------------------------------------------------
    def discover(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "endpoint": _sug.ENDPOINT,
            "operation": "Cars",
            "domain": _sug.DOMAIN,
        }
        try:
            with httpx.Client(timeout=30) as client:
                info["total_reported"] = _sug.total_docs(client)
        except Exception as exc:  # pragma: no cover - nur Debug
            info["discover_error"] = repr(exc)
        return info

    # -- fetch ----------------------------------------------------------
    def fetch(self) -> list[dict[str, Any]]:
        attempts = max(1, self._settings.fetch_max_retries)
        backoff = self._settings.fetch_retry_backoff
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                transport = httpx.HTTPTransport(retries=2)
                with httpx.Client(transport=transport, timeout=30) as client:
                    raw = _sug.fetch_all_raw(client)
                log.info("sug fetch: %d Rohdatensätze geholt", len(raw))
                return raw
            except _RETRYABLE as exc:
                last_exc = exc
                if attempt < attempts:
                    wait = backoff ** attempt
                    log.warning(
                        "sug fetch Netzwerkfehler (Versuch %d/%d): %s — retry in %.0fs",
                        attempt, attempts, exc, wait,
                    )
                    time.sleep(wait)
                else:
                    log.error("sug fetch endgültig fehlgeschlagen: %s", exc)
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                status = exc.response.status_code if exc.response else 0
                if 500 <= status < 600 and attempt < attempts:
                    wait = backoff ** attempt
                    log.warning("sug fetch HTTP %s — retry in %.0fs", status, wait)
                    time.sleep(wait)
                else:
                    log.error("sug fetch HTTP-Fehler %s — Abbruch", status)
                    raise
        assert last_exc is not None
        raise last_exc

    # -- normalize ------------------------------------------------------
    def normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        return _sug.normalize(raw)
