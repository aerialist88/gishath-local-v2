"""price_history.py — local price history from your own searches.

Every /search already fetches live SGD prices across all stores; this module
snapshots the cheapest price per (card, store) per day into a small SQLite
file (state/price_history.db) so the UI can show "was SGD 3.50 a month ago"
and a sparkline. Purely passive: it only records what you search for, one
row per card/store/day (a re-search the same day just lowers the recorded
minimum). No background jobs, no network.

Connections are opened per call — writes happen once per search and reads
once per render, so connection pooling would be over-engineering.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_BASE_DIR, "state", "price_history.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    day   TEXT NOT NULL,          -- YYYY-MM-DD (local date)
    card  TEXT NOT NULL,          -- input card name as searched
    store TEXT NOT NULL,
    price REAL NOT NULL,          -- cheapest SGD listing that day
    PRIMARY KEY (day, card, store)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_card ON snapshots (card, day);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.executescript(_SCHEMA)
    return conn


def record(rows: list[dict]) -> None:
    """Snapshot today's cheapest price per (card, store) from display rows.

    Takes the same row dicts presentation.format_results() produces (including
    hidden ones). Failures are logged and swallowed — history must never break
    a search.
    """
    best: dict[tuple[str, str], float] = {}
    for r in rows:
        if r.get("is_error") or not r.get("src"):
            continue
        price = float(r.get("price_val") or 0)
        if price <= 0:
            continue
        key = (r["card"], r["src"])
        if key not in best or price < best[key]:
            best[key] = price
    if not best:
        return
    today = date.today().isoformat()
    try:
        with _connect() as conn:
            conn.executemany(
                # keep the day's minimum even across multiple searches
                "INSERT INTO snapshots (day, card, store, price) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(day, card, store) DO UPDATE SET price = MIN(price, excluded.price)",
                [(today, card, store, price) for (card, store), price in best.items()],
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("price history record failed (search unaffected): %s", exc)


def get_history(card_names: list[str], days: int = 180) -> dict[str, dict]:
    """{card: {"points": [[day, min_price_across_stores], ...] ascending}}.

    Cards with no snapshots are omitted. Today's point is included if already
    recorded (record() runs before this in /search, so it normally is).
    """
    if not card_names:
        return {}
    cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
    out: dict[str, dict] = {}
    try:
        with _connect() as conn:
            placeholders = ",".join("?" * len(card_names))
            cur = conn.execute(
                f"SELECT card, day, MIN(price) FROM snapshots "
                f"WHERE card IN ({placeholders}) AND day >= ? "
                f"GROUP BY card, day ORDER BY day",
                [*card_names, cutoff],
            )
            for card, day, price in cur:
                out.setdefault(card, {"points": []})["points"].append([day, round(price, 2)])
    except Exception as exc:  # noqa: BLE001
        log.warning("price history read failed (search unaffected): %s", exc)
        return {}
    return out
