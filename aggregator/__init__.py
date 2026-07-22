"""Regresi Fahrzeug-Aggregator.

Liest Fahrzeugbestände von Autohaus-Plattformen ein (Adapter-Muster),
speichert sie in Postgres und hält den Bestand über einen idempotenten
Sync laufend aktuell. Bereitstellung für die Webseite über FastAPI.
"""

__version__ = "1.0.0"
