"""print_index.py — print-identity resolver for the Collection section.

A Moxfield collection export identifies each card as (set code, collector
number), but store listings only carry free text ("[The Lost Caverns of
Ixalan] [V1 - Borderless]"). This module bridges the two: an index keyed
"SETCODE|number" holding the card's name, the set's full name, and the
print's variant traits (borderless / showcase / extended / etched / retro /
fullart / promo), distilled from MTGJSON AllPrintings.

The index is built as a free rider on ck_price.refresh_cache()'s existing
AllPrintings streaming pass (see the collect_set() hook it calls per set) —
no second download, no extra memory profile beyond one set at a time. It
lands at state/print_index.json and reloads lazily on mtime change, same
pattern as ck_price's cache.

Trait vocabulary is deliberately small and store-facing: only traits that
SG store listings actually put into titles are worth matching on. Each trait
maps to the text markers collection.py's matcher looks for.
"""
from __future__ import annotations

import json
import logging
import os
import threading

log = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(_BASE_DIR, "state", "print_index.json")

# frameEffects / other MTGJSON fields -> our trait names
_FRAME_EFFECT_TRAITS = {
    "showcase": "showcase",
    "extendedart": "extended",
    "etched": "etched",
    "shatteredglass": "showcase",   # stores label these "showcase" if at all
    "inverted": "showcase",
}

# Traits a listing might carry as text. Used by collection.py both to confirm
# a trait is present and to detect a *conflicting* special version when the
# owned print is the plain one.
TRAIT_MARKERS: dict[str, list[str]] = {
    "borderless": ["borderless"],
    "showcase": ["showcase", "shattered glass", "inverted"],
    "extended": ["extended art", "extended-art", "extendedart", "extended"],
    "etched": ["etched"],
    "retro": ["retro"],
    "fullart": ["full art", "full-art", "fullart"],
    "serialized": ["serialized", "serialised"],
    "promo": ["promo", "prerelease", "pre-release"],
}


def _traits_for(card: dict) -> list[str]:
    traits: set[str] = set()
    for fe in card.get("frameEffects") or []:
        trait = _FRAME_EFFECT_TRAITS.get(fe)
        if trait:
            traits.add(trait)
    if card.get("borderColor") == "borderless":
        traits.add("borderless")
    if card.get("frameVersion") in ("1993", "1997"):
        traits.add("retro")
    if card.get("isFullArt"):
        traits.add("fullart")
    if card.get("isPromo"):
        traits.add("promo")
    if "serialized" in (card.get("promoTypes") or []):
        traits.add("serialized")
    if "etched" in (card.get("finishes") or []) and len(card.get("finishes") or []) == 1:
        traits.add("etched")  # etched-only prints; foil/etched mixes handled by finishes
    return sorted(traits)


# ── Build path (called from ck_price's AllPrintings streaming pass) ───────────

_collected: dict[str, dict] = {}
_ck_prices_by_uuid: dict[str, dict] = {}


def collect_begin(ck_prices_by_uuid: dict[str, dict] | None = None) -> None:
    """ck_prices_by_uuid: {uuid: {"normal": usd, "foil": usd}} from
    AllPricesToday — the same map ck_price already fetched. Lets each print
    entry carry its own exact-print CK reference price."""
    global _ck_prices_by_uuid
    _collected.clear()
    _ck_prices_by_uuid = ck_prices_by_uuid or {}


def _ck_for(card: dict) -> dict | None:
    """Compact per-print CK block: {n: normal_usd, f: foil_usd, u: url,
    uf: foil_url}, empty keys omitted. None when CK doesn't stock the print."""
    prices = _ck_prices_by_uuid.get(card.get("uuid") or "")
    if not prices:
        return None
    urls = card.get("purchaseUrls") or {}
    out: dict = {}
    if prices.get("normal", 0) > 0:
        out["n"] = round(prices["normal"], 2)
        if urls.get("cardKingdom"):
            out["u"] = urls["cardKingdom"]
    if prices.get("foil", 0) > 0:
        out["f"] = round(prices["foil"], 2)
        if urls.get("cardKingdomFoil") or urls.get("cardKingdom"):
            out["uf"] = urls.get("cardKingdomFoil") or urls["cardKingdom"]
    return out or None


def collect_set(set_code: str, set_obj: dict) -> None:
    """One set's worth of printings, called mid-stream by ck_price. Never
    raises: a surprise in one set's data must not break the CK price refresh
    this rides on."""
    try:
        set_name = set_obj.get("name") or set_code
        for card in set_obj.get("cards") or []:
            number = (card.get("number") or "").strip()
            name = (card.get("name") or "").strip()
            if not number or not name:
                continue
            side = (card.get("side") or "").strip()
            if side and side != "a":
                continue  # index each physical printing once, by its front face
            key = f"{set_code.upper()}|{number.lower()}"
            entry = {
                "n": name,
                "s": set_name,
                "v": _traits_for(card),
                "f": card.get("finishes") or [],
            }
            ck = _ck_for(card)
            if ck:
                entry["ck"] = ck
            _collected[key] = entry
    except Exception:  # noqa: BLE001
        log.exception("print_index: failed to collect set %s (skipped)", set_code)


def collect_finish() -> None:
    """Writes the index atomically. Also never raises past itself."""
    try:
        os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
        tmp = f"{INDEX_PATH}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_collected, fh)
        os.replace(tmp, INDEX_PATH)
        log.info("print_index: wrote %d printings to %s", len(_collected), INDEX_PATH)
    except Exception:  # noqa: BLE001
        log.exception("print_index: failed to write index")
    finally:
        _collected.clear()


# ── Lookup path ───────────────────────────────────────────────────────────────

_lock = threading.Lock()
_index: dict[str, dict] | None = None
_index_mtime: float | None = None


def _load() -> dict[str, dict]:
    global _index, _index_mtime
    with _lock:
        try:
            mtime = os.path.getmtime(INDEX_PATH)
        except OSError:
            return {}
        if _index is not None and _index_mtime == mtime:
            return _index
        try:
            _index = json.loads(open(INDEX_PATH, encoding="utf-8").read())
            _index_mtime = mtime
        except Exception:  # noqa: BLE001
            log.exception("print_index: failed to load %s", INDEX_PATH)
            return {}
        return _index


def available() -> bool:
    return bool(_load())


_set_names_cache: tuple[float | None, frozenset[str]] = (None, frozenset())


def set_names() -> frozenset[str]:
    """All known set names, lowercased — used by the collection matcher to
    reject substring collisions ("Ixalan" inside "The Lost Caverns of
    Ixalan"). Recomputed only when the index file changes."""
    global _set_names_cache
    index = _load()
    if not index:
        return frozenset()
    if _set_names_cache[0] == _index_mtime:
        return _set_names_cache[1]
    names = frozenset((e.get("s") or "").lower() for e in index.values() if e.get("s"))
    _set_names_cache = (_index_mtime, names)
    return names


def lookup(set_code: str, collector_number: str) -> dict | None:
    """{n: name, s: set_name, v: [traits], f: [finishes]} or None."""
    if not set_code or not collector_number:
        return None
    return _load().get(f"{set_code.strip().upper()}|{collector_number.strip().lower()}")


_count_cache: tuple[float | None, dict[str, int], dict[str, set]] = (None, {}, {})


def _name_maps() -> tuple[dict[str, int], dict[str, set]]:
    """(printing counts, set codes) per card name — built in one pass,
    invalidated when the index file changes."""
    global _count_cache
    index = _load()
    if not index:
        return {}, {}
    if _count_cache[0] != _index_mtime:
        counts: dict[str, int] = {}
        sets: dict[str, set] = {}
        for key, entry in index.items():
            name = entry["n"]
            counts[name] = counts.get(name, 0) + 1
            sets.setdefault(name, set()).add(key.split("|", 1)[0])
        _count_cache = (_index_mtime, counts, sets)
    return _count_cache[1], _count_cache[2]


def printing_count(name: str) -> int:
    """How many distinct printings exist for this card name across all sets.
    A count of 1 lets the collection matcher upgrade a bare-name listing to
    an exact match — there is no other print the listing could be."""
    return _name_maps()[0].get(name.strip(), 0)


def set_codes_for(name: str) -> set[str]:
    """Set codes this card name was ever printed in. When there's exactly one,
    a bare-name listing still proves the SET (only the variant stays unknown),
    letting the matcher upgrade 'any' to 'same set'."""
    return _name_maps()[1].get(name.strip(), set())
