"""watchlist.py — cards you're waiting on a price for.

A tiny JSON store (state/watchlist.json): card name + target SGD price, plus
alert bookkeeping so the nightly check (check_watchlist.py) emails once per
dip rather than every night the price stays low. Written atomically via a
temp file + rename, same pattern as ck_price.py's cache writes.

Alert rule (applied by check_watchlist.py via should_alert):
  - alert when best price <= target AND
  - we haven't alerted before, or the price dropped further since the last
    alert, or the price went back above target in between (state reset).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(_BASE_DIR, "state", "watchlist.json")

_lock = threading.Lock()


def _load() -> list[dict]:
    try:
        data = json.loads(open(PATH, encoding="utf-8").read())
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001 — missing/corrupt file -> empty list
        return []


def _save(entries: list[dict]) -> None:
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = f"{PATH}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=1)
    os.replace(tmp, PATH)


def list_entries() -> list[dict]:
    with _lock:
        return _load()


def add(card: str, target_sgd: float) -> list[dict]:
    """Add or update a watch (card names are unique, case-insensitive)."""
    card = card.strip()
    if not card:
        raise ValueError("card name required")
    target = round(float(target_sgd), 2)
    if target <= 0:
        raise ValueError("target price must be positive")
    with _lock:
        entries = _load()
        for e in entries:
            if e["card"].lower() == card.lower():
                e["target_sgd"] = target
                e["last_alert_price"] = None  # re-arm on target change
                break
        else:
            entries.append({
                "card": card,
                "target_sgd": target,
                "added": datetime.now().strftime("%Y-%m-%d"),
                "last_alert_price": None,
                "last_alert_at": None,
            })
        _save(entries)
        return entries


def remove(card: str) -> list[dict]:
    with _lock:
        entries = [e for e in _load() if e["card"].lower() != card.strip().lower()]
        _save(entries)
        return entries


def should_alert(entry: dict, best_price: float) -> bool:
    if best_price > entry["target_sgd"]:
        return False
    last = entry.get("last_alert_price")
    return last is None or best_price < float(last) - 0.005


def mark_alerted(card: str, price: float) -> None:
    with _lock:
        entries = _load()
        for e in entries:
            if e["card"].lower() == card.strip().lower():
                e["last_alert_price"] = round(price, 2)
                e["last_alert_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        _save(entries)


def reset_alert(card: str) -> None:
    """Price back above target — re-arm so the next dip alerts again."""
    with _lock:
        entries = _load()
        for e in entries:
            if e["card"].lower() == card.strip().lower() and e.get("last_alert_price") is not None:
                e["last_alert_price"] = None
        _save(entries)
