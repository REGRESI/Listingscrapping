"""Speicher-Wartung der vehicles-Tabelle.

Aufgaben:
  * inaktive Fahrzeuge löschen, die seit > N Tagen nicht mehr gesehen wurden
    (last_seen_at) — nicht mehr gebraucht, blähen nur die DB auf,
  * bereits gespeicherte, große raw-Felder nachträglich verschlanken
    (pro Quelle über slim_raw des jeweiligen Adapters),
  * VACUUM (FULL) ausführen, damit der Speicher ans Dateisystem zurückgeht,
  * Datenbank-/Tabellengröße vorher und nachher anzeigen.

Start:
    python -m aggregator.maintenance                 # löschen + slim + VACUUM
    python -m aggregator.maintenance --days 14
    python -m aggregator.maintenance --no-vacuum --no-slim
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text

from .adapters.registry import all_adapters
from .config import get_settings
from .db import SessionLocal, engine, init_db
from .logging_conf import get_logger, setup_logging
from .models import Vehicle

log = get_logger(__name__)


def sizes(conn) -> tuple[str, str]:
    """(Datenbankgröße, Größe der vehicles-Tabelle inkl. Indizes/TOAST)."""
    db = conn.execute(
        text("SELECT pg_size_pretty(pg_database_size(current_database()))")
    ).scalar_one()
    tbl = conn.execute(
        text("SELECT pg_size_pretty(pg_total_relation_size('vehicles'))")
    ).scalar_one()
    return db, tbl


def raw_stats(conn) -> list[tuple]:
    """Pro Quelle: Anzahl + durchschnittliche raw-Größe (lesbar)."""
    return conn.execute(text(
        "SELECT source, count(*) AS n, "
        "pg_size_pretty(avg(pg_column_size(raw))::bigint) AS avg_raw, "
        "pg_size_pretty(sum(pg_column_size(raw))::bigint) AS sum_raw "
        "FROM vehicles GROUP BY source ORDER BY source"
    )).all()


def delete_stale_inactive(session, older_than_days: int) -> int:
    """Inaktive Fahrzeuge löschen, die seit > N Tagen nicht gesehen wurden."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    result = session.execute(
        delete(Vehicle).where(
            Vehicle.active.is_(False),
            Vehicle.last_seen_at.is_not(None),
            Vehicle.last_seen_at < cutoff,
        )
    )
    session.commit()
    return result.rowcount or 0


def reslim_existing_raw(session) -> int:
    """Bereits gespeicherte raw-Felder pro Quelle nachträglich verschlanken."""
    slim_by_source = {a.name: a.slim_raw for a in all_adapters()}
    updated = 0
    for v in session.execute(select(Vehicle)).scalars().yield_per(500):
        fn = slim_by_source.get(v.source)
        new_raw = fn(v.raw or {}) if fn else {}
        if new_raw != (v.raw or {}):
            v.raw = new_raw
            updated += 1
    session.commit()
    return updated


def vacuum_full() -> None:
    """VACUUM (FULL, ANALYZE) — gibt Speicher ans Dateisystem zurück.
    Läuft außerhalb einer Transaktion (AUTOCOMMIT) und sperrt die Tabelle kurz."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM (FULL, ANALYZE) vehicles"))


def run(days: int, do_slim: bool = True, do_vacuum: bool = True) -> None:
    init_db()

    with engine.connect() as conn:
        db_before, tbl_before = sizes(conn)
        stats_before = raw_stats(conn)

    log.info("VORHER  DB=%s  Tabelle=%s", db_before, tbl_before)
    for src, n, avg_raw, sum_raw in stats_before:
        log.info("  raw[%s]: %d Zeilen, Ø %s, gesamt %s", src, n, avg_raw, sum_raw)

    with SessionLocal() as session:
        deleted = delete_stale_inactive(session, days)
        log.info("Gelöscht: %d inaktive Fahrzeuge (> %d Tage nicht gesehen)", deleted, days)
        if do_slim:
            slimmed = reslim_existing_raw(session)
            log.info("raw verschlankt: %d Datensätze aktualisiert", slimmed)

    if do_vacuum:
        log.info("VACUUM (FULL, ANALYZE) läuft … (Tabelle kurz gesperrt)")
        vacuum_full()

    with engine.connect() as conn:
        db_after, tbl_after = sizes(conn)
        stats_after = raw_stats(conn)

    log.info("NACHHER DB=%s  Tabelle=%s", db_after, tbl_after)
    for src, n, avg_raw, sum_raw in stats_after:
        log.info("  raw[%s]: %d Zeilen, Ø %s, gesamt %s", src, n, avg_raw, sum_raw)


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Speicher-Wartung der vehicles-Tabelle")
    parser.add_argument("--days", type=int, default=settings.inactive_retention_days,
                        help="Inaktive Fahrzeuge nach so vielen Tagen löschen.")
    parser.add_argument("--no-slim", action="store_true", help="raw NICHT nachträglich verschlanken.")
    parser.add_argument("--no-vacuum", action="store_true", help="Kein VACUUM FULL.")
    args = parser.parse_args(argv)

    setup_logging(settings.log_level)
    run(days=args.days, do_slim=not args.no_slim, do_vacuum=not args.no_vacuum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
