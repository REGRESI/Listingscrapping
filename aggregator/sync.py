"""Sync-Engine — das Kernstück des laufenden Abgleichs.

Pro Adapter:
  1. fetch()   -> alle Rohdatensätze (mit Netzwerk-Retries im Adapter)
  2. normalize + validieren + upsert, PRO DATENSATZ in try/except
     (ein krummer Satz kippt nie den ganzen Lauf)
  3. Deaktivierung verschwundener Fahrzeuge (active=false, sold_at),
     abgesichert gegen Massen-Fehldeaktivierung bei Teilausfällen.

Der Lauf ist idempotent: zweimal mit gleichen Quelldaten laufen lassen führt
zum gleichen DB-Zustand.

Start als Einmal-Lauf:
    python -m aggregator.sync
    python -m aggregator.sync --source bhg
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .adapters.base import SourceAdapter
from .adapters.registry import get_adapters
from .config import get_settings
from .db import SessionLocal, init_db
from .logging_conf import get_logger, setup_logging
from .repository import count_active, deactivate_missing, upsert_vehicle

log = get_logger(__name__)


@dataclass
class SourceResult:
    source: str
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    deactivated: int = 0
    errors: int = 0
    skipped_deactivation: bool = False
    error_message: str | None = None

    def summary(self) -> str:
        base = (
            f"[{self.source}] fetched={self.fetched} inserted={self.inserted} "
            f"updated={self.updated} deactivated={self.deactivated} errors={self.errors}"
        )
        if self.skipped_deactivation:
            base += " (Deaktivierung übersprungen: Sicherheitsnetz)"
        if self.error_message:
            base += f" FEHLER={self.error_message}"
        return base


@dataclass
class SyncReport:
    started_at: datetime
    results: list[SourceResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.error_message is None for r in self.results)


class SyncEngine:
    def __init__(self) -> None:
        self.settings = get_settings()

    def run(self, sources: list[str] | None = None) -> SyncReport:
        init_db()
        run_ts = datetime.now(timezone.utc)
        report = SyncReport(started_at=run_ts)

        names = sources or (
            [s for s in self.settings.sync_sources.split(",") if s.strip()] or None
        )
        adapters = get_adapters(names)
        if not adapters:
            log.warning("Keine passenden Adapter gefunden (Filter=%s)", names)
            return report

        for adapter in adapters:
            report.results.append(self._sync_source(adapter, run_ts))

        for r in report.results:
            log.info(r.summary())
        log.info("Sync fertig in %.1fs, ok=%s",
                 (datetime.now(timezone.utc) - run_ts).total_seconds(), report.ok)
        return report

    def _sync_source(self, adapter: SourceAdapter, run_ts: datetime) -> SourceResult:
        res = SourceResult(source=adapter.name)
        log.info("=== Sync Quelle '%s' startet ===", adapter.name)

        # 1) Fetch (Netzwerk-Retries stecken im Adapter)
        try:
            raw_records = adapter.fetch()
        except Exception as exc:
            res.error_message = f"fetch fehlgeschlagen: {exc!r}"
            log.error("[%s] %s", adapter.name, res.error_message)
            return res
        res.fetched = len(raw_records)

        session = SessionLocal()
        try:
            prev_active = count_active(session, adapter.name)

            # 2) Normalisieren + Upsert, pro Datensatz abgesichert
            seen_ok = 0
            for raw in raw_records:
                try:
                    with session.begin_nested():
                        vehicle = adapter.normalize_validated(raw)
                        action = upsert_vehicle(session, vehicle, run_ts)
                    if action == "inserted":
                        res.inserted += 1
                    else:
                        res.updated += 1
                    seen_ok += 1
                except Exception as exc:
                    res.errors += 1
                    rid = raw.get("objectID") or raw.get("id") if isinstance(raw, dict) else "?"
                    log.warning("[%s] Datensatz übersprungen (id=%s): %r",
                                adapter.name, rid, exc)
            session.commit()

            # 3) Deaktivierung verschwundener Fahrzeuge — mit Sicherheitsnetz
            if self._deactivation_safe(res, prev_active, seen_ok):
                res.deactivated = deactivate_missing(session, adapter.name, run_ts)
                session.commit()
            else:
                res.skipped_deactivation = True
                log.warning(
                    "[%s] Deaktivierung ÜBERSPRUNGEN: nur %d/%d (< %.0f%%) gesehen — "
                    "möglicher Teilausfall, Bestand bleibt unverändert.",
                    adapter.name, seen_ok, prev_active,
                    self.settings.min_fetch_ratio * 100,
                )
        except Exception as exc:
            session.rollback()
            res.error_message = f"DB-Fehler: {exc!r}"
            log.exception("[%s] unerwarteter DB-Fehler", adapter.name)
        finally:
            session.close()

        return res

    def _deactivation_safe(self, res: SourceResult, prev_active: int, seen_ok: int) -> bool:
        """Nur deaktivieren, wenn genug Fahrzeuge gesehen wurden."""
        if seen_ok == 0:
            return False
        if prev_active <= 0:
            return True  # erster Lauf / leere Quelle: nichts zu verlieren
        return seen_ok >= self.settings.min_fetch_ratio * prev_active


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regresi Fahrzeug-Sync (Einmal-Lauf)")
    parser.add_argument(
        "--source", action="append", dest="sources",
        help="Nur diese Quelle syncen (mehrfach möglich). Standard: alle.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level)

    engine = SyncEngine()
    report = engine.run(args.sources)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
