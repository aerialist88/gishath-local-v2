"""check_ck_pricelist_playwright.py — feasibility check #2 for the Card Kingdom
pricelist API, using a real (headless) Chromium browser instead of an HTTP client.

Why this exists: check_ck_pricelist.py (plain httpx + curl_cffi) got a hard
Cloudflare "Just a moment..." 403 on every attempt. That's the same failure
mode this project already hit with Agora Hobby — curl_cffi's TLS impersonation
defeats fingerprint-based blocking but can't execute a JS challenge. This
script reuses the stealth-Chromium pattern from playwright_scraper.py (the
one that already works for Agora Hobby / BinderPOS) to see whether a real
browser gets past Card Kingdom's challenge too.

This does NOT touch the running app, write cache files, or require the app to
be running — it launches its own throwaway browser instance.

Usage:
    cd gishath-local-v2
    source venv/bin/activate
    python check_ck_pricelist_playwright.py

If Playwright's browsers aren't installed yet: `playwright install chromium`
(they should already be, since the main app uses Playwright too).

Paste the full output back.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

PRICELIST_URL = "https://api.cardkingdom.com/api/v2/pricelist"
NAV_TIMEOUT_MS = 60_000
CHALLENGE_SETTLE_WAIT_S = 20

# Same stealth setup as playwright_scraper.py's _new_stealth_page() — masks
# the headless-Chromium signals (navigator.webdriver, empty plugins list,
# missing window.chrome) that Cloudflare's bot management checks for.
_STEALTH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    window.chrome = {runtime: {}};
"""


async def main() -> int:
    from playwright.async_api import async_playwright

    responses = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        page = await browser.new_page(user_agent=_STEALTH_UA)
        await page.add_init_script(_STEALTH_SCRIPT)

        # Capture every response for this exact URL — the first one will
        # likely be Cloudflare's 403 challenge; if CF auto-solves and
        # reloads, a later one should be the real 200 JSON response.
        page.on("response", lambda resp: responses.append(resp) if resp.url.startswith(PRICELIST_URL) else None)

        print(f"Navigating to {PRICELIST_URL} (headless Chromium, stealth mode)...")
        try:
            await page.goto(PRICELIST_URL, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            print(f"goto failed: {exc}")
            await browser.close()
            return 1

        # Cloudflare's managed/auto challenge resolves (or doesn't) within a
        # few seconds — give it up to CHALLENGE_SETTLE_WAIT_S before giving up.
        deadline = time.monotonic() + CHALLENGE_SETTLE_WAIT_S
        title = await page.title()
        while "just a moment" in title.lower() and time.monotonic() < deadline:
            await page.wait_for_timeout(1000)
            title = await page.title()

        print(f"final page title:  {title!r}")
        print(f"responses seen for this URL: {len(responses)}")
        for i, resp in enumerate(responses):
            print(f"  [{i}] status={resp.status}  content-type={resp.headers.get('content-type', '?')}")

        ok_response = next((r for r in reversed(responses) if r.status == 200), None)
        if ok_response is None:
            print("\n=== RESULT: never got a 200 for this URL — Playwright (headless, stealth) is also blocked. ===")
            if responses:
                body = await responses[-1].body()
                print(f"last response, first 300B: {body[:300]!r}")
            print("=== Next move: pivot to the MTGJSON option (Option A in the PRD). ===")
            await browser.close()
            return 1

        body = await ok_response.body()
        print(f"\n200 response body bytes: {len(body):,}")

        try:
            payload = json.loads(body)
        except Exception as exc:  # noqa: BLE001
            print(f"\n=== RESULT: got a 200 but body isn't valid JSON ({exc}) ===")
            print(f"first 300B: {body[:300]!r}")
            await browser.close()
            return 1

        products = payload if isinstance(payload, list) else payload.get("data", [])
        print(f"\n=== RESULT: SUCCESS — valid JSON, {len(products):,} products ===")
        if products:
            print(f"sample entry:\n{json.dumps(products[0], indent=2)[:500]}")
            names = {p.get("name") for p in products if isinstance(p, dict)}
            print(f"unique names: {len(names):,}  (cheapest-by-name index would be roughly this many rows)")
        print("\n=== Playwright can fetch this endpoint. ck_price.py will route its nightly refresh through the browser, same as Agora Hobby. ===")

        await browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
