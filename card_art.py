"""card_art.py — card image lookups for the results table.

Mirrors atelier/art.py and shares the SAME distilled index file
(deck_engine/state/atelier_art_index.json): name -> {art_crop, normal}
Scryfall image URIs, hotlinked per Scryfall's image guidelines. The browser
lazy-loads the images straight from Scryfall's CDN — this module only hands
out URL strings, so a search with images costs the laptop nothing beyond
what the scrape already does.

Like atelier/art.py, the index build is lazy on a background thread; lookups
before it's ready return None and the UI simply shows no thumbnail — art is
decoration here, never load-bearing.
"""
from __future__ import annotations

import json
import os
import threading

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRYFALL_CACHE_PATH = os.path.join(_BASE_DIR, "deck_engine", "state", "scryfall_cards.json")
INDEX_PATH = os.path.join(_BASE_DIR, "deck_engine", "state", "atelier_art_index.json")

_lock = threading.Lock()
_index: dict[str, dict] | None = None
_building = False


def _extract(card: dict) -> dict | None:
    uris = card.get("image_uris")
    if not uris and card.get("card_faces"):
        for face in card["card_faces"]:
            if isinstance(face, dict) and face.get("image_uris"):
                uris = face["image_uris"]
                break
    if not isinstance(uris, dict):
        return None
    out = {}
    if uris.get("art_crop"):
        out["art_crop"] = uris["art_crop"]
    if uris.get("normal"):
        out["normal"] = uris["normal"]
    return out or None


def _build_index() -> None:
    global _index, _building
    try:
        raw = json.loads(open(SCRYFALL_CACHE_PATH, encoding="utf-8").read())
        index = {}
        for key, card in raw.items():
            entry = _extract(card)
            if entry:
                index[key] = entry
        with open(INDEX_PATH, "w", encoding="utf-8") as fh:
            json.dump(index, fh)
        with _lock:
            _index = index
    except Exception:  # noqa: BLE001 — no cache / bad cache -> no art, never an error
        with _lock:
            _index = {}
    finally:
        _building = False


def _ensure_index() -> dict | None:
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
            threading.Thread(target=_build_index, daemon=True, name="card-art-index").start()
        return None


def lookup(name: str) -> dict | None:
    """{art_crop, normal} for a card name, or None (unknown / index warming up).

    The index is keyed by full lowercased Scryfall names, so an MDFC typed as
    just its front face ("Malakir Rebirth") falls back to a "front //" prefix
    scan ("malakir rebirth // malakir mire").
    """
    index = _ensure_index()
    if not index:
        return None
    key = name.strip().lower()
    entry = index.get(key)
    if entry is not None:
        return entry
    prefix = key + " //"
    for full_name, candidate in index.items():
        if full_name.startswith(prefix):
            return candidate
    return None


def get_for_names(names: list[str]) -> dict[str, dict]:
    """{input_name: {art_crop, normal}} for every name that has art. Names
    with no art (or while the index warms up) are simply omitted."""
    out: dict[str, dict] = {}
    for name in names:
        entry = lookup(name)
        if entry:
            out[name] = entry
    return out
