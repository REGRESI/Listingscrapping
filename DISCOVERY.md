# Schritt 1 – Discovery (bhg-mobile / Alphartis)

Ziel: Die JSON-Schnittstelle finden, über die die Next.js-Fahrzeugsuche ihre
Trefferliste nachlädt – **echte** Request-URL, Query-Parameter und
Antwort-Struktur, damit die Feldnamen aus der echten Antwort gelesen (nicht
geraten) werden.

## Status

Die Discovery selbst benötigt ausgehenden Netzwerkzugriff auf:

```
www.bhg-mobile.de
www.ahg-mobile.de
images.ahg-mobile.de
```

In einer Umgebung mit restriktiver Egress-Policy antwortet der Proxy auf
CONNECT zu diesen Hosts mit **403 (policy denial)**. In diesem Fall die
Hosts in der Netzwerk-Policy der Umgebung freigeben und eine neue Session
starten – die Policy lässt sich nicht innerhalb einer laufenden Session
ändern.

Prüfen, ob der Zugriff frei ist:

```bash
curl -sS -o /dev/null -w "%{http_code}\n" --max-time 20 \
  https://www.bhg-mobile.de/de/fahrzeugsuche
# 200/3xx = frei, 000 mit "CONNECT tunnel failed, response 403" = geblockt
```

## Discovery ausführen

```bash
python -m venv .venv && . .venv/bin/activate
pip install playwright httpx
# Chromium ist in dieser Umgebung vorinstalliert; sonst: playwright install chromium

python -m tools.discover --url "https://www.bhg-mobile.de/de/fahrzeugsuche?page=0"
```

Das Tool:
- öffnet die Seite in headless Chromium (nutzt automatisch `HTTPS_PROXY`, falls gesetzt),
- schneidet alle JSON-Responses mit,
- erkennt heuristisch die fahrzeugtragende Antwort (Felder wie make/model/price/…),
- gibt Request-URL, Methode, evtl. POST-Body und die Top-Level-Struktur aus,
- speichert die volle Aufnahme in `discovery_capture.json`.

Mit dem Discovery-Ergebnis wird anschließend der bhg-Adapter (`fetch()` +
`normalize()`) exakt an die echten Feldnamen angebunden.
