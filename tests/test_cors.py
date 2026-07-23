"""CORS-Verhalten: nur konfigurierte Ursprünge dürfen die API im Browser rufen."""
from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

ALLOWED = "https://regresi.lovable.app"


def _reload_api_with_origins(monkeypatch, origins: str):
    monkeypatch.setenv("CORS_ORIGINS", origins)
    import aggregator.config as config
    config.get_settings.cache_clear()
    import aggregator.api as api
    importlib.reload(api)
    return api


def test_allowed_origin_gets_cors_header(db_session, monkeypatch):
    try:
        api = _reload_api_with_origins(monkeypatch, f"{ALLOWED}, https://zweite.example.com")
        client = TestClient(api.app)
        r = client.get("/healthz", headers={"Origin": ALLOWED})
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == ALLOWED
    finally:
        monkeypatch.setenv("CORS_ORIGINS", "")
        import aggregator.config as config
        config.get_settings.cache_clear()
        import aggregator.api as api
        importlib.reload(api)


def test_disallowed_origin_gets_no_cors_header(db_session, monkeypatch):
    try:
        api = _reload_api_with_origins(monkeypatch, ALLOWED)
        client = TestClient(api.app)
        r = client.get("/healthz", headers={"Origin": "https://boese.example.com"})
        assert r.status_code == 200
        # Kein Allow-Origin für einen nicht erlaubten Ursprung.
        assert r.headers.get("access-control-allow-origin") != "https://boese.example.com"
    finally:
        monkeypatch.setenv("CORS_ORIGINS", "")
        import aggregator.config as config
        config.get_settings.cache_clear()
        import aggregator.api as api
        importlib.reload(api)


def test_get_preflight_allowed(db_session, monkeypatch):
    """Preflight (OPTIONS) für eine GET-Anfrage vom erlaubten Ursprung geht durch."""
    try:
        api = _reload_api_with_origins(monkeypatch, ALLOWED)
        client = TestClient(api.app)
        r = client.options(
            "/vehicles",
            headers={
                "Origin": ALLOWED,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.status_code in (200, 204)
        assert r.headers.get("access-control-allow-origin") == ALLOWED
        assert "GET" in r.headers.get("access-control-allow-methods", "")
    finally:
        monkeypatch.setenv("CORS_ORIGINS", "")
        import aggregator.config as config
        config.get_settings.cache_clear()
        import aggregator.api as api
        importlib.reload(api)
