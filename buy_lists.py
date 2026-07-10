"""buy_lists.py — named, saved buy lists for the input panel.

state/buy_lists.json: {name: {"cards": [...], "updated": "YYYY-MM-DD HH:MM"}}.
Same atomic temp-file + rename write pattern as watchlist.py.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(_BASE_DIR, "state", "buy_lists.json")

MAX_LISTS = 100  # sanity cap; this is a personal tool

_lock = threading.Lock()


def _load() -> dict[str, dict]:
    try:
        data = json.loads(open(PATH, encoding="utf-8").read())
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — missing/corrupt file -> no lists
        return {}


def _save(lists: dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = f"{PATH}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(lists, fh, indent=1)
    os.replace(tmp, PATH)


def list_all() -> dict[str, dict]:
    with _lock:
        return _load()


def save(name: str, cards: list[str]) -> dict[str, dict]:
    name = name.strip()
    cards = [c.strip() for c in cards if c and c.strip()]
    if not name:
        raise ValueError("list name required")
    if not cards:
        raise ValueError("list is empty")
    with _lock:
        lists = _load()
        if name not in lists and len(lists) >= MAX_LISTS:
            raise ValueError(f"too many saved lists (max {MAX_LISTS})")
        lists[name] = {"cards": cards, "updated": datetime.now().strftime("%Y-%m-%d %H:%M")}
        _save(lists)
        return lists


def delete(name: str) -> dict[str, dict]:
    with _lock:
        lists = _load()
        lists.pop(name.strip(), None)
        _save(lists)
        return lists
