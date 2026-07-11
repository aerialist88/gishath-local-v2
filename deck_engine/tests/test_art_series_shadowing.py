"""Regression test for the art-series cache-shadowing bug found live on
Trevor's Mac (2026-07-11): Scryfall's default_cards bulk file contains
art-series collector cards (layout "art_series") named "X // X" with a blank
type_line ("Card // Card") and no oracle text. When one appeared BEFORE the
real card in the bulk file, its MDFC front-face alias claimed the real card's
key and the first-printing-wins dedupe then dropped the real card entirely —
the real Pantlaza, Sun-Favored (LCI legendary Dinosaur) was completely absent
from the cache, and a deck was built and validated against the blank art card
(commander stored under the doubled name, empty oracle grounding for sims).

The same live scan found the sibling bug: reversible_card printings
(double-sided Secret Lair reprints, also named "X // X" with all game data
nested in card_faces) shadowed 13 real cards including "Anointed Procession".

Fixed in scryfall_cache._trim_bulk_cards() by (a) skipping non-deck layouts
and memorabilia set_types outright, and (b) never letting an entry with no
type_line hold a key against one with real card data.

Run manually: cd gishath-local-v2 && python3 -m deck_engine.tests.test_art_series_shadowing
"""
from __future__ import annotations

import sys

from .. import scryfall_cache

_ART_CARD = {
    # Shape matches the real LCI art-series entry that caused the incident.
    "name": "Pantlaza, Sun-Favored // Pantlaza, Sun-Favored",
    "type_line": "Card // Card",
    "oracle_text": None,
    "color_identity": [],
    "legalities": {"commander": "not_legal"},
    "layout": "art_series",
    "set_type": "memorabilia",
    "oracle_id": None,
}

_REAL_CARD = {
    "name": "Pantlaza, Sun-Favored",
    "type_line": "Legendary Creature — Dinosaur",
    "oracle_text": "Whenever Pantlaza or another Dinosaur you control enters, discover X, where X is that creature's toughness. Do this only once each turn.",
    "color_identity": ["G", "R", "W"],
    "legalities": {"commander": "legal"},
    "layout": "normal",
    "set_type": "expansion",
    "oracle_id": "fake-oracle-pantlaza",
}

_REVERSIBLE_PRINTING = {
    # Secret Lair double-sided reprint shape: doubled name, game data only in
    # card_faces, top-level type_line/oracle_text null.
    "name": "Anointed Procession // Anointed Procession",
    "type_line": None,
    "oracle_text": None,
    "color_identity": ["W"],
    "legalities": {"commander": "legal"},
    "layout": "reversible_card",
    "set_type": "funny",
    "oracle_id": "fake-oracle-procession",
    "card_faces": [{"name": "Anointed Procession", "type_line": "Enchantment"}],
}

_REVERSIBLE_REAL = {
    "name": "Anointed Procession",
    "type_line": "Enchantment",
    "oracle_text": "If an effect would create one or more tokens under your control, it creates twice that many of those tokens instead.",
    "color_identity": ["W"],
    "legalities": {"commander": "legal"},
    "layout": "normal",
    "set_type": "expansion",
    "oracle_id": "fake-oracle-procession",
}

_TOKEN = {
    "name": "Wolf",
    "type_line": "Token Creature — Wolf",
    "oracle_text": "",
    "color_identity": ["G"],
    "legalities": {"commander": "not_legal"},
    "layout": "token",
    "set_type": "token",
    "oracle_id": None,
}

_EMBLEM = {
    "name": "Wrenn and Seven Emblem",
    "type_line": "Emblem — Wrenn",
    "oracle_text": "(emblem text)",
    "color_identity": [],
    "legalities": {"commander": "not_legal"},
    "layout": "emblem",
    "set_type": "token",
    "oracle_id": None,
}


def main() -> int:
    # The incident ordering: blank collector cards listed BEFORE the real ones.
    cache = scryfall_cache._trim_bulk_cards(  # noqa: SLF001 — test-only introspection
        [_ART_CARD, _REVERSIBLE_PRINTING, _TOKEN, _EMBLEM, _REAL_CARD, _REVERSIBLE_REAL]
    )
    # ...and the reverse ordering must yield the same winners.
    cache_reversed = scryfall_cache._trim_bulk_cards(  # noqa: SLF001
        [_REAL_CARD, _REVERSIBLE_REAL, _ART_CARD, _REVERSIBLE_PRINTING, _TOKEN, _EMBLEM]
    )

    checks = []
    for label, c in (("art-first order", cache), ("real-first order", cache_reversed)):
        pantlaza = c.get("pantlaza, sun-favored") or {}
        procession = c.get("anointed procession") or {}
        checks += [
            (f"{label}: real Pantlaza wins its key",
             pantlaza.get("layout") == "normal" and pantlaza.get("type_line") == _REAL_CARD["type_line"]),
            (f"{label}: real Pantlaza has oracle text for sim grounding",
             bool(pantlaza.get("oracle_text"))),
            (f"{label}: art card claims no key at all",
             all(v.get("layout") != "art_series" for v in c.values())),
            (f"{label}: real Anointed Procession beats its reversible printing",
             procession.get("type_line") == "Enchantment"),
            (f"{label}: token skipped", "wolf" not in c),
            (f"{label}: emblem skipped", "wrenn and seven emblem" not in c),
        ]

    failed = [label for label, ok in checks if not ok]
    if failed:
        print(f"FAILED: {failed}", file=sys.stderr)
        return 1

    # End to end: the commander must resolve to the real card, so color
    # identity is enforced from G/R/W — not the art card's empty identity
    # (which would have flagged every colored card) or a missing commander.
    result = scryfall_cache.validate_deck(
        commander="Pantlaza, Sun-Favored",
        decklist=["Anointed Procession"],  # short deck — only unknown/identity flags matter here
        cache=cache,
    )
    if "Pantlaza, Sun-Favored" in result.unknown_cards:
        print("FAILED: real commander still flagged unknown", file=sys.stderr)
        return 1
    if "Anointed Procession" in result.color_identity_violations:
        print("FAILED: W card flagged outside a G/R/W identity — commander "
              "identity must come from the real card, not the blank art card",
              file=sys.stderr)
        return 1

    print("OK: art-series/token/emblem cards skipped, reversible printings "
          "yield to real cards, and validate_deck() grounds on the real commander.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
