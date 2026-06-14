"""
filters.py — shared card-matching, filtering, and normalisation helpers.

Single source of truth for logic that used to be duplicated between
presentation.py (filters Go engine results) and playwright_scraper.py
(filters BinderPOS/Playwright results). Both paths must agree on what counts
as a "real" MTG single, what counts as a name match, and how foil/quality
fields are normalised — otherwise the two result sets drift apart as new
stores are added.

Contents:
    Name matching
        _name_matches(card_name, result_name) -> bool

    Accessory / non-MTG filtering
        _ACCESSORY_KEYWORDS          frozenset
        _NON_MTG_NAME_KEYWORDS        frozenset
        _NON_MTG_SET_KEYWORDS         frozenset
        _MTG_NON_SINGLE_KEYWORDS      frozenset
        _is_accessory(card)          -> bool   (engine-result dict: name/extraInfo)
        _is_non_mtg(name, extra_info) -> bool  (Playwright path: separate fields)

    Quality / foil normalisation
        QUALITY_MAP                   dict
        FOIL_KEYWORDS                 tuple
        _normalise_quality(raw)       -> str
        _is_foil(title)               -> bool
"""
from __future__ import annotations

import re

# ── Name matching ───────────────────────────────────────────────────────────


def _name_matches(card_name: str, result_name: str) -> bool:
    """Return True if result_name is a plausible match for card_name.

    Two-stage check:
      1. Fast path: card_name is a literal substring of result_name
         (case-insensitive, whole-word bounded). Handles the common case:
         "Lightning Bolt" in "Lightning Bolt (M10)".
      2. Slow path: every word-token in card_name appears as a whole word in
         result_name (word-boundary regex, punctuation stripped from tokens).
         Handles stores that return reformatted names, e.g. "Jace, the Mind
         Sculptor" -> tokens ["jace", "the", "mind", "sculptor"].

    Whole-word matching prevents false positives like "The One Ring" matching
    "One Ring to Rule Them All" — the token "the" is not a standalone word in
    "them", so \\bthe\\b correctly fails to match.
    """
    cn = card_name.lower()
    rn = result_name.lower()

    if re.search(r"\b" + re.escape(cn) + r"\b", rn):
        return True

    tokens = re.findall(r"[a-z0-9']+", cn)
    if not tokens:
        return False
    return all(re.search(r"\b" + re.escape(t) + r"\b", rn) for t in tokens)


# ── Accessory / non-MTG keyword sets ──────────────────────────────────────────
# No real MTG card name contains any of these strings, so any match is safe
# to drop regardless of which scraper produced the result.

_ACCESSORY_KEYWORDS: frozenset[str] = frozenset([
    "sleeve",           # catches "sleeves", "sleeved", "matte sleeve", etc.
    "deck box",
    "deckbox",
    "playmat",
    "play mat",
    "binder",
    "booster box",
    "booster pack",
    "life counter",
    "life pad",
    "dice tower",
    "card storage",
    "storage box",
    "prerelease kit",
    "prerelease pack",
])

# Multi-TCG stores (Hideout, Games Haven, Grey Ogre) carry MTG + other games.
# Checked against the card NAME field.
_NON_MTG_NAME_KEYWORDS: frozenset[str] = frozenset([
    "pokémon", "pokemon", "yu-gi-oh", "yugioh", "ygo",
    "one piece", "digimon", "dragon ball", "cardfight",
    "flesh and blood", "shadowverse", "weiß schwarz", "weiss schwarz",
    # NOTE: "force of will" is intentionally NOT here — it's a real MTG card name.
    # The Force of Will TCG is caught via _NON_MTG_SET_KEYWORDS (set name field).
    "grand archive", "lorcana", "union arena",
    "battle spirits", "my hero academia", "gundam",
])

# Checked against the SET NAME / extraInfo field.
_NON_MTG_SET_KEYWORDS: frozenset[str] = frozenset([
    # Pokémon
    "scarlet & violet", "sword & shield", "sun & moon", "black & white",
    "x & y", "diamond & pearl", "heartgold", "soulsilver",
    # Yu-Gi-Oh
    "phantom nightmare", "maze of memories", "battles of legend",
    "legacy of destruction", "age of overlord", "infinite forbidden",
    "terminal world", "duel overload",
    # One Piece
    "romance dawn", "paramount war", "pillars of strength",
    "kingdom of intrigue", "awakening of the new era", "wings of captain",
    # Digimon
    "digimon card", "release special",
    # Dragon Ball Super
    "galactic battle", "union force", "cross worlds",
    # Flesh and Blood
    "welcome to rathe", "arcane rising", "crucible of war",
    # Lorcana
    "the first chapter", "rise of the floodborn", "into the inklands",
    # Force of Will TCG (individual FoW cards have names like "Alice, the White
    # Witch"; the game name appears in the set/extraInfo field, not the card name)
    "force of will:",   # matches "Force of Will: Crimson Moon's Fairy Tale" etc.
])

# MTG products that are not playable singles — art cards, tokens, oversized, etc.
# Checked against the card NAME only (these terms never appear in real card names).
_MTG_NON_SINGLE_KEYWORDS: frozenset[str] = frozenset([
    "art card",         # e.g. "Ancient Copper Dragon Art Card (Gold-Stamped Signature)"
    "art series",       # older branding for the same product type
    "oversized",        # jumbo cards, commander decks etc.
    "double-faced token",
    "checklist card",   # DFC placeholder cards
])


def _is_accessory(card: dict) -> bool:
    """Return True if an engine-result dict looks like a physical accessory.

    Used on the Go-engine / merged-results path, where each result is a dict
    with "name" and "extraInfo" keys. Belt-and-suspenders: playwright_scraper
    filters its own results early via _is_non_mtg(), but engine results pass
    through here too.
    """
    name_l = card.get("name", "").lower()
    extra_l = card.get("extraInfo", "").lower()
    return any(kw in name_l or kw in extra_l for kw in _ACCESSORY_KEYWORDS)


def _is_non_mtg(name: str, extra_info: str) -> bool:
    """Return True if this result looks like a non-MTG card or an MTG non-single.

    Used on the Playwright/BinderPOS path, where name and extraInfo are
    available as separate strings before being packed into a result dict.
    """
    name_l = name.lower()
    extra_l = extra_info.lower()
    if any(kw in name_l for kw in _NON_MTG_NAME_KEYWORDS):
        return True
    if any(kw in extra_l for kw in _NON_MTG_NAME_KEYWORDS):
        return True
    if any(kw in extra_l for kw in _NON_MTG_SET_KEYWORDS):
        return True
    if any(kw in name_l for kw in _MTG_NON_SINGLE_KEYWORDS):
        return True
    if any(kw in name_l for kw in _ACCESSORY_KEYWORDS):
        return True
    return False


# ── Quality / foil normalisation ──────────────────────────────────────────────

QUALITY_MAP: dict[str, str] = {
    "NM":    "Near Mint",
    "NM/M":  "Near Mint",
    "M":     "Near Mint",
    "LP":    "Lightly Played",
    "EX":    "Lightly Played",
    "EX+":   "Lightly Played",
    "EX/EX+": "Lightly Played",
    "MP":    "Moderately Played",
    "VG":    "Moderately Played",
    "HP":    "Heavily Played",
    "PL":    "Heavily Played",
    "DM":    "Damaged",
    "D":     "Damaged",
}

FOIL_KEYWORDS = ("foil", "etched", "galaxy", "surge", "halo", "gilded")


def _normalise_quality(raw: str) -> str:
    key = raw.strip().upper()
    return QUALITY_MAP.get(key, raw.strip())


def _is_foil(title: str) -> bool:
    return any(kw in title.lower() for kw in FOIL_KEYWORDS)
