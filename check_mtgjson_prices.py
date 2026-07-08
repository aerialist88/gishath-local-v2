"""check_mtgjson_prices.py — feasibility check #3 for Card Kingdom pricing:
confirms MTGJSON's AllPricesToday.json.bz2 is reachable, downloadable, and
parses the way upstream's Go code (gateway/cardkingdom/mtgjson_fetch.go)
expects, before we write the full streaming pipeline (which also needs the
much larger AllPrintings.json.bz2).

Deliberately only fetches AllPricesToday (smaller of the two files) — this is
the cheap half of the check. It downloads the whole thing into memory (this
file is not the one that needs streaming; AllPrintings is), decompresses,
and parses just enough to confirm:
  - the domain/URL is reachable and returns real data (not an HTML block page)
  - the JSON shape matches what mtgjson_fetch.go expects:
      data[<uuid>].paper.cardkingdom.retail.normal / .foil  (date -> price maps)
  - roughly how many UUIDs actually carry a Card Kingdom retail price
  - real download size, so we know what AllPrintings (bigger) is likely to cost

This does NOT touch the running app or write any cache files.

Usage:
    cd gishath-local-v2
    source venv/bin/activate
    python check_mtgjson_prices.py

Paste the full output back.
"""
from __future__ import annotations

import bz2
import json
import sys
import time

ALL_PRICES_TODAY_URL = "https://mtgjson.com/api/v5/AllPricesToday.json.bz2"
HTTP_TIMEOUT_SECONDS = 120


def main() -> int:
    try:
        import httpx
    except ImportError:
        print("httpx not installed (it's already in requirements.txt — check your venv is active)")
        return 1

    print(f"Downloading {ALL_PRICES_TODAY_URL} ...")
    started = time.monotonic()
    try:
        resp = httpx.get(
            ALL_PRICES_TODAY_URL,
            headers={"Accept": "application/octet-stream"},
            timeout=HTTP_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"download failed: {exc}")
        return 1

    download_elapsed = time.monotonic() - started
    print(f"status:            {resp.status_code}")
    print(f"content-type:      {resp.headers.get('content-type', '?')}")
    print(f"compressed bytes:  {len(resp.content):,}  ({download_elapsed:.1f}s)")

    if resp.status_code != 200:
        print(f"\n=== RESULT: non-200 status, first 300B: {resp.content[:300]!r} ===")
        return 1

    print("\nDecompressing (bz2)...")
    decompress_started = time.monotonic()
    try:
        raw = bz2.decompress(resp.content)
    except Exception as exc:  # noqa: BLE001
        print(f"bz2 decompress failed: {exc}")
        print(f"first 300B of raw response: {resp.content[:300]!r}")
        return 1
    print(f"decompressed bytes: {len(raw):,}  ({time.monotonic() - decompress_started:.1f}s)")

    print("\nParsing JSON...")
    parse_started = time.monotonic()
    try:
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"json.loads failed: {exc}")
        print(f"first 300B of decompressed body: {raw[:300]!r}")
        return 1
    print(f"json.loads took {time.monotonic() - parse_started:.1f}s")

    meta_date = (payload.get("meta") or {}).get("date", "?")
    data = payload.get("data") or {}
    print(f"\nmeta.date: {meta_date}")
    print(f"total UUIDs in data: {len(data):,}")

    ck_count = 0
    sample = None
    for uuid, entry in data.items():
        paper = entry.get("paper") if isinstance(entry, dict) else None
        ck = (paper or {}).get("cardkingdom") if isinstance(paper, dict) else None
        if not ck:
            continue
        retail = ck.get("retail") or {}
        if not retail.get("normal") and not retail.get("foil"):
            continue
        ck_count += 1
        if sample is None:
            sample = (uuid, retail)

    print(f"UUIDs with a Card Kingdom retail price: {ck_count:,}")
    if sample:
        uuid, retail = sample
        print(f"sample uuid: {uuid}")
        print(f"sample retail dict (date -> price, per finish): {json.dumps(retail, indent=2)[:600]}")

    print(f"\n=== RESULT: {'SUCCESS' if ck_count > 0 else 'PARSED BUT NO CK PRICES FOUND'} ===")
    print(f"For scale planning: compressed={len(resp.content):,}B, decompressed={len(raw):,}B for AllPricesToday alone.")
    print("AllPrintings.json.bz2 (not fetched by this script) is the much larger file — this ratio is a rough guide to what streaming it will need to handle.")
    return 0 if ck_count > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
