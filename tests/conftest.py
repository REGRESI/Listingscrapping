"""Test-Fixtures. Nutzt eine echte Postgres-DB (DATABASE_URL/TEST_DATABASE_URL),
weil Upsert (ON CONFLICT), JSONB und der xmax-Trick Postgres-spezifisch sind.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import text

# Test-DB-URL VOR dem Import der App-Module setzen (Settings sind gecacht).
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://aggregator:aggregator@127.0.0.1:5433/aggregator",
    ),
)

from aggregator.db import Base, engine, SessionLocal  # noqa: E402
from aggregator import models  # noqa: E402,F401


@pytest.fixture()
def db_session():
    # Saubere Tabellen pro Test.
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS vehicles CASCADE"))
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
