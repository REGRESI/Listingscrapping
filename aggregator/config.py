"""Zentrale Konfiguration, geladen aus Umgebung / .env (12-Factor)."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(url: str) -> str:
    """SQLAlchemy-URL auf den psycopg-(v3)-Treiber normalisieren.

    Managed-Postgres-Anbieter (u.a. Railway) liefern DATABASE_URL als
    ``postgres://...`` oder ``postgresql://...``. SQLAlchemy würde daraus den
    psycopg2-Dialekt ableiten — wir installieren aber psycopg v3. Deshalb das
    Schema auf ``postgresql+psycopg://`` umschreiben, falls noch kein Treiber
    explizit angegeben ist. Reine Verbindungs-Normalisierung, keine Logik.
    """
    if not url:
        return url
    url = url.strip()
    # Schema case-insensitiv erkennen, Rest der URL unangetastet lassen.
    lower = url.lower()
    if lower.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif lower.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Datenbank -------------------------------------------------------
    # SQLAlchemy-URL. Standard nutzt psycopg (v3) als Treiber.
    # Vorrang: echte Umgebungsvariable > .env-Datei > dieser Default
    # (pydantic-settings-Reihenfolge). Auf Railway zählt also DATABASE_URL
    # aus der Umgebung, der localhost-Default greift nur lokal ohne Env.
    database_url: str = Field(
        default="postgresql+psycopg://aggregator:aggregator@localhost:5432/aggregator",
        description="SQLAlchemy-Verbindungs-URL zur Postgres-Datenbank.",
    )

    @field_validator("database_url", mode="after")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        return normalize_database_url(v)
    db_echo: bool = Field(default=False, description="SQL-Statements loggen (Debug).")

    # --- Sync ------------------------------------------------------------
    # Welche Adapter der Sync-Lauf abarbeitet (Kommagetrennt). Leer = alle.
    sync_sources: str = Field(
        default="",
        description="Kommagetrennte Adapter-Namen; leer = alle registrierten.",
    )
    # Sicherheitsnetz gegen Fehl-Deaktivierung: liefert ein Lauf weniger als
    # dieser Anteil des zuletzt aktiven Bestands, wird NICHT deaktiviert.
    min_fetch_ratio: float = Field(
        default=0.5,
        description="Untergrenze fetched/zuvor-aktiv, sonst Deaktivierung überspringen.",
    )
    fetch_max_retries: int = Field(default=4, description="Retries bei Netzwerkfehlern.")
    fetch_retry_backoff: float = Field(
        default=2.0, description="Basis für exponentielles Backoff (Sekunden)."
    )

    # --- Scheduler -------------------------------------------------------
    schedule_cron: str = Field(
        default="0 * * * *",
        description="Cron-Ausdruck für den eingebauten APScheduler (stündlich).",
    )
    run_on_start: bool = Field(
        default=True, description="Beim Start des Schedulers sofort einen Lauf machen."
    )

    # --- API -------------------------------------------------------------
    api_default_limit: int = Field(default=50, description="Standard-Seitengröße /vehicles.")
    api_max_limit: int = Field(default=500, description="Maximale Seitengröße /vehicles.")

    # --- Logging ---------------------------------------------------------
    log_level: str = Field(default="INFO")


@lru_cache
def get_settings() -> Settings:
    """Gecachte Settings-Instanz (einmal pro Prozess)."""
    return Settings()
