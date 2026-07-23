"""SQLAlchemy-Engine, Session-Factory und Basis."""
from __future__ import annotations

import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings, normalize_database_url

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_settings = get_settings()

# Zentrale, verbindliche Normalisierung der DB-URL: JEDE Engine in diesem
# Projekt entsteht hier. Managed-Anbieter (u.a. Railway) liefern die URL als
# `postgres://` / `postgresql://`; ohne expliziten Treiber würde SQLAlchemy
# psycopg2 wählen (nicht installiert -> ModuleNotFoundError). Wir schreiben
# deshalb hier – unabhängig von der Config – zuverlässig auf
# `postgresql+psycopg://` um. API und Sync-Job importieren beide diese Engine
# und nutzen damit exakt denselben normalisierten Wert.
DATABASE_URL = normalize_database_url(_settings.database_url)

engine = create_engine(
    DATABASE_URL,
    echo=_settings.db_echo,
    pool_pre_ping=True,   # tote Verbindungen automatisch erkennen
    future=True,
)
log.info("DB-Engine erstellt (Treiber=%s)", engine.url.get_backend_name() + "+" + (engine.url.get_driver_name() or "?"))

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Legt die Tabellen an, falls noch nicht vorhanden (idempotent)."""
    from . import models  # noqa: F401  (Registrierung der Modelle)

    Base.metadata.create_all(bind=engine)
