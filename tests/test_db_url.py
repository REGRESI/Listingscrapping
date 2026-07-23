"""Regression: DB-URL wird zuverlässig auf den psycopg-(v3)-Treiber
normalisiert — auch für Railways treiberloses `postgresql://`-Format.
"""
from __future__ import annotations

import importlib

import pytest

from aggregator.config import normalize_database_url


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("postgres://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
        ("postgresql://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
        # Railway-typisch: interner Host
        ("postgresql://postgres:secret@monorail.proxy.rlwy.net:1234/railway",
         "postgresql+psycopg://postgres:secret@monorail.proxy.rlwy.net:1234/railway"),
        # bereits mit Treiber -> unangetastet
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        # Whitespace / Schema-Großschreibung
        ("  POSTGRES://u:p@h/db  ", "postgresql+psycopg://u:p@h/db"),
    ],
)
def test_normalize(raw, expected):
    assert normalize_database_url(raw) == expected


def test_db_engine_uses_psycopg_even_from_bare_postgresql(monkeypatch):
    """Zentrale Stelle (aggregator/db.py) muss auch dann psycopg wählen, wenn
    die Umgebung eine treiberlose `postgresql://`-URL liefert (Railway)."""
    import os

    import aggregator.config as config
    import aggregator.db as db

    original_url = os.environ["DATABASE_URL"]  # von conftest gesetzt (Test-DB)
    try:
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.internal:5432/railway")
        config.get_settings.cache_clear()
        importlib.reload(db)

        assert db.DATABASE_URL == "postgresql+psycopg://u:p@db.internal:5432/railway"
        assert db.engine.url.get_backend_name() == "postgresql"
        assert db.engine.url.get_driver_name() == "psycopg"
    finally:
        # Engine/Settings verbindlich auf die echte Test-DB zurücksetzen,
        # damit Folgetests nicht die Fake-URL nutzen.
        monkeypatch.setenv("DATABASE_URL", original_url)
        config.get_settings.cache_clear()
        importlib.reload(db)
