"""refresh_ck_prices.py — manually force a refresh of the Card Kingdom
reference-price cache, right now, on demand.

You normally don't need to run this: app.py starts a background thread
(ck_price.start_background_refresher()) that checks the cache's age every
hour and refreshes automatically whenever it's missing or >20h old — this
runs whenever the app happens to be up, with no dependency on a fixed
overnight time (a cron/launchd job was considered and dropped specifically
because the laptop isn't reliably on overnight; see PRD_card_kingdom_price.md
§13 and ck_price.py's module docstring for the full reasoning).

This script exists for the cases the background refresher doesn't cover:
  - You want fresh prices RIGHT NOW, not whenever the hourly check next fires.
  - You want to build/verify the cache without starting the whole Flask app
    (engine subprocess + Playwright browser).
  - Debugging — running it standalone gives you the full log output inline.

Downloads MTGJSON's AllPricesToday + AllPrintings, rebuilds the cheapest-CK-
listing-per-card-name index (ck_price.refresh_cache()), and writes it to
state/ck_prices.json. AllPrintings is a large streamed download — this takes
a few minutes.

Usage:
    cd gishath-local-v2
    source venv/bin/activate
    python refresh_ck_prices.py
    # or: make ck-refresh

Safe to run any time, including while app.py is already running (each writer
uses its own PID-suffixed temp file before the atomic rename — see
ck_price.py's _write_cache). A crash or Ctrl-C mid-refresh can never corrupt
the existing cache; /search just keeps serving the last good one until a
refresh succeeds. The Flask app does NOT need to be running for this script
to work — a running app.py picks up the fresh cache automatically on its next
search (ck_price.py checks the file's mtime, no restart needed).
"""
from __future__ import annotations

import logging
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("refresh_ck_prices")

import ck_price  # noqa: E402


def main() -> int:
    started = time.monotonic()
    log.info("Starting Card Kingdom price refresh (MTGJSON AllPricesToday + AllPrintings)...")
    try:
        summary = ck_price.refresh_cache()
    except Exception:
        log.exception("Card Kingdom price refresh failed")
        return 1

    elapsed = time.monotonic() - started
    log.info(
        "Done in %.1fs — %d cards indexed, price date %s, synced at %s",
        elapsed, summary["entries"], summary["priceDate"], summary["syncedAt"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
