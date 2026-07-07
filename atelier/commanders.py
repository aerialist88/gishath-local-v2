"""atelier/commanders.py — commander-name autocomplete for the commission box.

Distills the same local Scryfall bulk cache scryfall_cache.py already loads
into a small name/color-identity index, once, on a background thread — same
lazy-build pattern as art.py, for the same reason (parsing 67 MB of JSON on
every keystroke would be silly). Eligibility mirrors concept_selector.py's
own check exactly (legendary creature, or oracle text says "can be your
commander") so a suggestion here is guaranteed to pass select-time
validation later.
"""
from __future__ import annotations

import json
import threading

from deck_engine import config

INDEX_PATH = config.STATE_DIR / "atelier_commander_index.json"

_lock = threading.Lock()
_index: list[dict] | None = None
_building = False


def _is_commander_eligible(card: dict) -> bool:
    type_line = (card.get("type_line") or "").lower()
    oracle_text = (card.get("oracle_text") or "").lower()
    return ("legendary" in type_line and "creature" in type_line) or "can be your commander" in oracle_text


def _build_index() -> None:
    global _index, _building
    try:
        raw = json.loads(config.SCRYFALL_CACHE_PATH.read_text())
        entries = []
        for card in raw.values():
            if not _is_commander_eligible(card):
                continue
            name = card.get("name")
            if not name:
                continue
            entries.append({"name": name, "colors": card.get("color_identity", [])})
        entries.sort(key=lambda e: e["name"])
        INDEX_PATH.write_text(json.dumps(entries))
        with _lock:
            _index = entries
    except Exception:  # noqa: BLE001 — no cache / bad cache -> no suggestions, never an error page
        with _lock:
            _index = []
    finally:
        _building = False


def _ensure_index() -> list[dict] | None:
    """Returns the index, or None while it's still being built."""
    global _index, _building
    with _lock:
        if _index is not None:
            return _index
        if INDEX_PATH.exists():
            try:
                _index = json.loads(INDEX_PATH.read_text())
                return _index
            except Exception:  # noqa: BLE001 — rebuild below
                pass
        if not _building:
            _building = True
            threading.Thread(target=_build_index, daemon=True, name="atelier-commander-index").start()
        return None


def search(query: str, limit: int = 8) -> list[dict]:
    """Name/color matches for `query` (a name prefix/substring), ranked
    prefix-matches-first. Empty list if the query is too short, the index
    is still warming up, or nothing matches — never an error."""
    query = query.strip().lower()
    if len(query) < 2:
        return []
    index = _ensure_index()
    if not index:
        return []
    starts, contains = [], []
    for entry in index:
        name_lower = entry["name"].lower()
        if name_lower.startswith(query):
            starts.append(entry)
        elif query in name_lower:
            contains.append(entry)
        if len(starts) >= limit:
            break
    results = (starts + contains)[:limit]
    return results
