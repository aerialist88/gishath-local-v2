"""Regression test for the MDFC front-face lookup bug found live on Trevor's
Mac (2026-07-01): a decklist referencing "The Fall of Lord Konda" (the front
face of the real Kamigawa: Neon Dynasty MDFC "The Fall of Lord Konda //
Fragment of Konda") was spuriously flagged as unknown/hallucinated by
validate_deck(), because refresh_cache() only indexed Scryfall's combined
"Front // Back" name. Fixed by also indexing the front face alone. This test
builds the cache with the real _trim_bulk_cards() (from a raw Scryfall-shaped
card list) rather than hand-building the trimmed dict, so it actually
exercises the indexing fix, not just validate_deck()'s lookup.

Run manually: cd gishath-local-v2 && python3 -m deck_engine.tests.test_mdfc_front_face_lookup
"""
from __future__ import annotations

import sys

from .. import scryfall_cache

# Shape matches real Scryfall bulk `default_cards` entries closely enough to
# exercise refresh_cache()'s indexing logic (name/color_identity/legalities/
# layout/oracle_text/type_line/oracle_id — the fields refresh_cache() keeps).
_FAKE_RAW_CARDS = [
    {
        "name": "The Fall of Lord Konda // Fragment of Konda",
        "type_line": "Enchantment — Saga // Enchantment Creature — Human Noble",
        "oracle_text": "(front face saga text)",
        "color_identity": ["W"],
        "legalities": {"commander": "legal"},
        "layout": "transform",
        "oracle_id": "fake-oracle-id-1",
    },
    {
        "name": "Satsuki, the Living Lore",
        "type_line": "Legendary Creature — Fox Wizard",
        "oracle_text": "commander test fixture",
        "color_identity": ["G", "W"],
        "legalities": {"commander": "legal"},
        "layout": "normal",
        "oracle_id": "fake-oracle-id-2",
    },
    {
        "name": "Plains",
        "type_line": "Basic Land — Plains",
        "oracle_text": "({T}: Add {W}.)",
        "color_identity": [],
        "legalities": {"commander": "legal"},
        "layout": "normal",
        "oracle_id": "fake-oracle-id-3",
    },
]


def main() -> int:
    # The real trimming/indexing function, no network involved. (This used to
    # mirror refresh_cache()'s inline loop and had already drifted from it —
    # the loop is now extracted precisely so tests exercise the real thing.)
    cache = scryfall_cache._trim_bulk_cards(_FAKE_RAW_CARDS)  # noqa: SLF001 — test-only introspection

    checks = [
        ("full combined name still resolves", "the fall of lord konda // fragment of konda" in cache),
        ("front-face-only name now resolves", "the fall of lord konda" in cache),
    ]
    failed = [label for label, ok in checks if not ok]
    if failed:
        print(f"FAILED: {failed}", file=sys.stderr)
        return 1

    # The actual regression: validate_deck() with a decklist using the
    # front-face-only name must NOT flag it as unknown/hallucinated.
    result = scryfall_cache.validate_deck(
        commander="Satsuki, the Living Lore",
        decklist=["The Fall of Lord Konda"] + ["Plains"] * 98,  # pad to DECK_SIZE - 1 to avoid an unrelated count flag
        cache=cache,
    )
    if "The Fall of Lord Konda" in result.unknown_cards:
        print(f"FAILED: front-face name still flagged unknown: {result.unknown_cards}", file=sys.stderr)
        return 1

    print("OK: front-face-only MDFC name resolves and validate_deck() no longer false-flags it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
