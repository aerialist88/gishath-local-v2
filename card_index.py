"""card_index.py — card-name autocomplete for the buy-list box.

Mirrors atelier/commanders.py, but indexes *all* paper card names (not just
commander-eligible ones) because 3vor Fetch is a buy list — any real card is a
valid line. Reuses the same 67 MB local Scryfall bulk cache the deck engine
already maintains, distilling it once (on a background thread) into a small
name/color index so we never re-parse the whole cache on a keystroke.

Tokens, emblems and art-series entries are excluded: they aren't things a
store's search or a buyer would look up by name.
"""
from __future__ import annotations

import json
import os
import threading

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRYFALL_CACHE_PATH = os.path.join(_BASE_DIR, "deck_engine", "state", "scryfall_cards.json")
INDEX_PATH = os.path.join(_BASE_DIR, "deck_engine", "state", "card_name_index.json")

_EXCLUDE_LAYOUTS = {"token", "double_faced_token", "emblem", "art_series"}

_lock = threading.Lock()
_index: list[dict] | None = None
_building = False


def _is_buyable(card: dict) -> bool:
    name = card.get("name") or ""
    if not name:
        return False
    # "A-" prefixes Alchemy rebalanced cards — digital-only, never sold in paper.
    if name.startswith("A-"):
        return False
    if (card.get("layout") or "") in _EXCLUDE_LAYOUTS:
        return False
    if "Token" in (card.get("type_line") or ""):
        return False
    return True


def _build_index() -> None:
    global _index, _building
    try:
        raw = json.loads(open(SCRYFALL_CACHE_PATH, encoding="utf-8").read())
        # The cache is keyed by lowercased name, so names are already unique.
        entries = [
            {"name": card["name"], "colors": card.get("color_identity", [])}
            for card in raw.values()
            if _is_buyable(card)
        ]
        entries.sort(key=lambda e: e["name"])
        with open(INDEX_PATH, "w", encoding="utf-8") as fh:
            json.dump(entries, fh)
        with _lock:
            _index = entries
    except Exception:  # noqa: BLE001 — no cache / bad cache -> no suggestions, never an error
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
        if os.path.exists(INDEX_PATH):
            try:
                _index = json.loads(open(INDEX_PATH, encoding="utf-8").read())
                return _index
            except Exception:  # noqa: BLE001 — rebuild below
                pass
        if not _building:
            _building = True
            threading.Thread(target=_build_index, daemon=True, name="card-name-index").start()
        return None


def search(query: str, limit: int = 8) -> list[dict]:
    """Name/color matches for `query` (a name prefix/substring), ranked
    prefix-matches-first. Empty list if the query is too short, the index is
    still warming up, or nothing matches — never an error."""
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
        # Keep scanning for prefix hits until we have enough; substring hits
        # backfill afterwards. Cap the substring bucket so a common fragment
        # (e.g. "the") doesn't build a huge list we immediately trim.
        if len(starts) >= limit:
            break
        if len(contains) >= limit * 4:
            break
    return (starts + contains)[:limit]
