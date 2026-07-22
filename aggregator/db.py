"""SQLAlchemy-Engine, Session-Factory und Basis."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    echo=_settings.db_echo,
    pool_pre_ping=True,   # tote Verbindungen automatisch erkennen
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Legt die Tabellen an, falls noch nicht vorhanden (idempotent)."""
    from . import models  # noqa: F401  (Registrierung der Modelle)

    Base.metadata.create_all(bind=engine)
