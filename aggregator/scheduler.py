"""Eingebauter Scheduler (APScheduler) für den stündlichen Sync.

Alternative zum System-Cron: ein langlaufender Prozess, der den Sync nach
einem Cron-Ausdruck triggert. Auf eurem Server mit offenem Internet starten:

    python -m aggregator.scheduler

Der Sync selbst braucht Netzwerkzugang — daher gehört dieser Prozess auf den
Server mit offenem Internet, nicht in eine abgeschottete Umgebung.
"""
from __future__ import annotations

import signal

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import get_settings
from .logging_conf import get_logger, setup_logging
from .sync import SyncEngine

log = get_logger(__name__)


def _run_sync() -> None:
    try:
        SyncEngine().run()
    except Exception:  # Scheduler darf nie an einem Lauf sterben
        log.exception("Sync-Lauf mit unerwartetem Fehler abgebrochen")


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    scheduler = BlockingScheduler(timezone="UTC")
    trigger = CronTrigger.from_crontab(settings.schedule_cron, timezone="UTC")
    scheduler.add_job(
        _run_sync,
        trigger=trigger,
        id="vehicle_sync",
        max_instances=1,       # keine überlappenden Läufe
        coalesce=True,         # verpasste Läufe zusammenfassen
        misfire_grace_time=300,
    )
    log.info("Scheduler gestartet, Cron='%s' (UTC)", settings.schedule_cron)

    if settings.run_on_start:
        log.info("run_on_start=true -> initialer Sync")
        _run_sync()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: scheduler.shutdown(wait=False))

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):  # pragma: no cover
        log.info("Scheduler beendet")


if __name__ == "__main__":
    main()
