"""atelier/art.py — card art lookups for the UI.

The design handoff's art areas are striped placeholders to be replaced with
Scryfall image URIs (art_crop for shelves/plaques, normal for big cards),
hotlinked per Scryfall's image guidelines. The 67 MB bulk cache already
stores image_uris per card (and per face for MDFCs), so this module distills
it once into a small name -> {art_crop, normal} index kept at
state/atelier_art_index.json — parsing 67 MB of JSON on every lookup (or
holding it resident in the server) would be silly.

The index build is lazy and runs on a background thread on first request;
lookups before it's ready return None and the frontend keeps its parchment
placeholder — art is decoration here, never load-bearing.
"""
from __future__ import annotations

import json
import threading

from deck_engine import config

INDEX_PATH = config.STATE_DIR / "atelier_art_index.json"

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
        raw = json.loads(config.SCRYFALL_CACHE_PATH.read_text())
        index = {}
        for key, card in raw.items():
            entry = _extract(card)
            if entry:
                index[key] = entry
        INDEX_PATH.write_text(json.dumps(index))
        with _lock:
            _index = index
    except Exception:  # noqa: BLE001 — no cache / bad cache -> no art, never an error page
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
        if INDEX_PATH.exists():
            try:
                _index = json.loads(INDEX_PATH.read_text())
                return _index
            except Exception:  # noqa: BLE001 — rebuild below
                pass
        if not _building:
            _building = True
            threading.Thread(target=_build_index, daemon=True, name="atelier-art-index").start()
        return None


def lookup(name: str) -> dict | None:
    """{art_crop, normal} for a card name, or None (unknown card / index warming up)."""
    index = _ensure_index()
    if index is None:
        return None
    return index.get(name.strip().lower())
