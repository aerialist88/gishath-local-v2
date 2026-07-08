"""check_ck_pricelist.py — one-off feasibility check for the Card Kingdom pricelist API.

Run this on your Mac (not in a sandbox — api.cardkingdom.com is not reachable from
Claude's sandbox network). It does NOT touch the running app or write any cache
files; it just tells us whether GET https://api.cardkingdom.com/api/v2/pricelist
is reachable and what it looks like, so we know before building anything whether
plain HTTP is enough or whether we need curl_cffi's browser-TLS impersonation
(already a project dependency, used for the BinderPOS stores).

Usage:
    cd gishath-local-v2
    source venv/bin/activate
    python check_ck_pricelist.py

Paste the full output back — that's what decides how ck_price.py gets built.
"""
from __future__ import annotations

import json
import sys
import time

PRICELIST_URL = "https://api.cardkingdom.com/api/v2/pricelist"
ATTEMPTS = 3
TIMEOUT_SECONDS = 60


def _looks_like_cloudflare_challenge(body: bytes) -> bool:
    prefix = body[:512].decode("utf-8", errors="ignore").lower()
    return "just a moment" in prefix or "cloudflare" in prefix


def _summarize(label: str, status: int, headers: dict, body: bytes) -> None:
    print(f"\n--- {label} ---")
    print(f"status:       {status}")
    print(f"content-type: {headers.get('content-type', headers.get('Content-Type', '?'))}")
    print(f"body bytes:   {len(body):,}")

    if _looks_like_cloudflare_challenge(body):
        print("shape:        Cloudflare interstitial/challenge page (not JSON)")
        print(f"first 300B:   {body[:300]!r}")
        return

    try:
        payload = json.loads(body)
    except Exception as exc:  # noqa: BLE001 — diagnostic script, want to see anything
        print(f"shape:        NOT valid JSON ({exc})")
        print(f"first 300B:   {body[:300]!r}")
        return

    products = payload if isinstance(payload, list) else payload.get("data", [])
    print(f"shape:        valid JSON, {len(products):,} products")
    if products:
        sample = products[0]
        print(f"sample entry: {json.dumps(sample, indent=2)[:500]}")
        # Rough estimate of what the cheapest-by-name cache would cost on disk.
        names = {p.get("name") for p in products if isinstance(p, dict)}
        print(f"unique names: {len(names):,}  (cheapest-by-name index would be roughly this many rows)")


def try_plain_httpx() -> bool:
    """Attempt 1: plain httpx GET, like a normal API client. This is what most
    of the codebase already uses (see app.py's engine health check)."""
    try:
        import httpx
    except ImportError:
        print("httpx not installed — skipping plain-httpx attempt (pip install httpx)")
        return False

    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    }

    for attempt in range(1, ATTEMPTS + 1):
        try:
            resp = httpx.get(PRICELIST_URL, headers=headers, timeout=TIMEOUT_SECONDS, follow_redirects=True)
            _summarize(f"httpx attempt {attempt}/{ATTEMPTS}", resp.status_code, dict(resp.headers), resp.content)
            if resp.status_code == 200 and not _looks_like_cloudflare_challenge(resp.content):
                return True
        except Exception as exc:  # noqa: BLE001
            print(f"\n--- httpx attempt {attempt}/{ATTEMPTS} ---\nerror: {exc}")
        if attempt < ATTEMPTS:
            time.sleep(attempt * 2)
    return False


def try_curl_cffi() -> bool:
    """Attempt 2: curl_cffi with Chrome TLS/JA3 impersonation — the same
    technique playwright_scraper.py falls back to for BinderPOS stores when
    Cloudflare blocks a plain HTTP client."""
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        print("curl_cffi not installed — skipping (pip install curl-cffi)")
        return False

    for attempt in range(1, ATTEMPTS + 1):
        try:
            resp = cffi_requests.get(
                PRICELIST_URL,
                headers={"Accept": "application/json"},
                impersonate="chrome",
                timeout=TIMEOUT_SECONDS,
            )
            _summarize(f"curl_cffi attempt {attempt}/{ATTEMPTS}", resp.status_code, dict(resp.headers), resp.content)
            if resp.status_code == 200 and not _looks_like_cloudflare_challenge(resp.content):
                return True
        except Exception as exc:  # noqa: BLE001
            print(f"\n--- curl_cffi attempt {attempt}/{ATTEMPTS} ---\nerror: {exc}")
        if attempt < ATTEMPTS:
            time.sleep(attempt * 2)
    return False


def main() -> int:
    print(f"Checking {PRICELIST_URL} ...")

    plain_ok = try_plain_httpx()
    if plain_ok:
        print("\n=== RESULT: plain httpx works. No curl_cffi needed for this endpoint. ===")
        return 0

    print("\nPlain httpx did not get a clean 200 JSON response — trying curl_cffi (Chrome impersonation)...")
    cffi_ok = try_curl_cffi()
    if cffi_ok:
        print("\n=== RESULT: curl_cffi (Chrome impersonation) works; plain httpx does not. ===")
        print("=== ck_price.py will need to use curl_cffi, same as the BinderPOS scraper. ===")
        return 0

    print("\n=== RESULT: neither plain httpx nor curl_cffi got a clean response. ===")
    print("=== Paste this full output back — we may need different headers, a proxy, or to fall back to MTGJSON (Option A). ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
