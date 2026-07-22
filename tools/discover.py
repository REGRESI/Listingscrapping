"""
Discovery tool for the bhg-mobile / Alphartis vehicle search.

Opens the Next.js search page in headless Chromium, records all network
traffic, and heuristically identifies the JSON response(s) that carry the
vehicle result list. Prints the request URL, query parameters, HTTP method,
any POST body, and the top-level structure of the JSON so field names can be
read from the REAL response (never guessed).

Usage:
    python -m tools.discover
    python tools/discover.py --url "https://www.bhg-mobile.de/de/fahrzeugsuche?page=0"

Notes on the sandboxed/proxied environment:
  * If HTTPS_PROXY is set (agent proxy), Chromium is pointed at it and the
    proxy CA bundle is trusted (or TLS errors are ignored for discovery only).
  * A 403 on CONNECT means the egress policy blocks the target host. That is a
    policy decision, not a code bug -- allowlist the host and re-run.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

from playwright.async_api import async_playwright

DEFAULT_URL = "https://www.bhg-mobile.de/de/fahrzeugsuche?page=0"

CHROMIUM_CANDIDATES = [
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium-1193/chrome-linux/chrome",
]

# Keys that strongly suggest a record is a vehicle.
VEHICLE_HINT_KEYS = {
    "make", "model", "price", "mileage", "firstRegistration", "fuel",
    "hersteller", "modell", "preis", "kilometer", "erstzulassung", "kraftstoff",
    "vin", "fin", "power", "leistung", "transmission", "getriebe",
}


def _find_chromium() -> str | None:
    for path in CHROMIUM_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def _looks_like_vehicle(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    keys = {k.lower() for k in obj.keys()}
    return len(keys & {k.lower() for k in VEHICLE_HINT_KEYS}) >= 2


def _score_body(body: Any) -> tuple[int, str]:
    """Return (number_of_vehicle_like_records, path_where_found)."""
    best = (0, "")

    def walk(node: Any, path: str):
        nonlocal best
        if isinstance(node, list):
            hits = sum(1 for x in node if _looks_like_vehicle(x))
            if hits > best[0]:
                best = (hits, path or "$")
            for i, x in enumerate(node[:3]):
                walk(x, f"{path}[{i}]")
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k)

    walk(body, "")
    return best


def _describe(obj: Any, depth: int = 0, max_depth: int = 3) -> Any:
    """Return a shape description (types + a sample) rather than full data."""
    if depth >= max_depth:
        return f"<{type(obj).__name__}>"
    if isinstance(obj, dict):
        return {k: _describe(v, depth + 1, max_depth) for k, v in obj.items()}
    if isinstance(obj, list):
        if not obj:
            return []
        return [_describe(obj[0], depth + 1, max_depth), f"... ({len(obj)} items)"]
    if isinstance(obj, str):
        return f"str:{obj[:60]!r}"
    return type(obj).__name__


async def run(url: str, out_path: str) -> None:
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    ca_bundle = "/root/.ccr/ca-bundle.crt"

    async with async_playwright() as p:
        launch_kwargs: dict[str, Any] = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage"],
        }
        exe = _find_chromium()
        if exe:
            launch_kwargs["executable_path"] = exe
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}

        browser = await p.chromium.launch(**launch_kwargs)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            # Discovery only: ignore MITM TLS errors from the agent proxy.
            ignore_https_errors=bool(proxy) or os.path.exists(ca_bundle),
        )
        page = await ctx.new_page()

        captured: list[dict[str, Any]] = []

        async def on_response(resp):
            try:
                ct = resp.headers.get("content-type", "")
                if "json" not in ct and not resp.url.endswith(".json"):
                    return
                try:
                    body = await resp.json()
                except Exception:
                    return
                captured.append(
                    {
                        "url": resp.url,
                        "status": resp.status,
                        "method": resp.request.method,
                        "content_type": ct,
                        "post_data": resp.request.post_data,
                        "body": body,
                    }
                )
            except Exception:
                pass

        page.on("response", on_response)

        print(f"[discover] navigating to {url}")
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(4000)
        try:
            await page.mouse.wheel(0, 4000)
            await page.wait_for_timeout(2500)
        except Exception:
            pass
        await browser.close()

    # Rank captured responses by how many vehicle-like records they contain.
    ranked = []
    for c in captured:
        hits, path = _score_body(c["body"])
        ranked.append((hits, path, c))
    ranked.sort(key=lambda t: t[0], reverse=True)

    print(f"\n=== Captured {len(captured)} JSON responses ===")
    for hits, path, c in ranked:
        flag = "  <-- VEHICLES" if hits else ""
        print(f"[{hits:>3} veh @ {path or '-'}] {c['method']} {c['status']} {c['url']}{flag}")

    if ranked and ranked[0][0] > 0:
        hits, path, c = ranked[0]
        print("\n=== BEST CANDIDATE (vehicle list) ===")
        print(f"URL     : {c['url']}")
        print(f"Method  : {c['method']}")
        if c["post_data"]:
            print(f"POST    : {c['post_data'][:1000]}")
        print(f"Vehicles: {hits} record(s) at JSON path '{path}'")
        print("\n--- Top-level response shape ---")
        print(json.dumps(_describe(c["body"]), indent=2, ensure_ascii=False)[:6000])
    else:
        print("\n[!] No vehicle-bearing JSON detected. "
              "Either the host is blocked (see 403 above), the data is inlined "
              "in __NEXT_DATA__, or it uses a different endpoint. Full dump saved.")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(captured, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n[discover] full capture -> {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="bhg-mobile network discovery")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--out", default="discovery_capture.json")
    args = ap.parse_args()
    asyncio.run(run(args.url, args.out))


if __name__ == "__main__":
    main()
