"""Regression test for standard-printing preference in the cache trim
(2026-07-15): the gallery's hover preview and the Atelier's commander plaques
hotlink the image_uris cached in scryfall_cards.json, and the old
first-printing-seen-wins dedupe meant whichever printing Scryfall's bulk file
happened to list first claimed the name key — frequently a showcase,
borderless, or Secret Lair treatment. Trevor wants the standard version of
each card on hover, never an alt-art variant.

Fixed in scryfall_cache._trim_bulk_cards() by scoring every candidate printing
(_printing_score) and letting the best one win the key: real card data first,
direct names over MDFC aliases (both pre-existing rules, see
test_art_series_shadowing), then plainest standard paper printing — non-digital,
black border, not full-art/textless/oversized, no special frame effects, not a
promo/variation, from a normal set.

Run manually: cd gishath-local-v2 && python3 -m deck_engine.tests.test_standard_printing_preference
"""
from __future__ import annotations

import sys

from .. import scryfall_cache

_COMMON = {
    "type_line": "Enchantment",
    "oracle_text": "If an effect would create one or more tokens under your control, it creates twice that many of those tokens instead.",
    "color_identity": ["W"],
    "legalities": {"commander": "legal"},
    "layout": "normal",
    "oracle_id": "fake-oracle-procession",
    "highres_image": True,
}

_SHOWCASE = {
    **_COMMON,
    "name": "Anointed Procession",
    "set_type": "expansion",
    "frame_effects": ["showcase"],
    "image_uris": {"normal": "https://img.scryfall.test/showcase.jpg"},
}

_BORDERLESS = {
    **_COMMON,
    "name": "Anointed Procession",
    "set_type": "expansion",
    "border_color": "borderless",
    "full_art": True,
    "image_uris": {"normal": "https://img.scryfall.test/borderless.jpg"},
}

_SECRET_LAIR = {
    **_COMMON,
    "name": "Anointed Procession",
    "set_type": "funny",
    "border_color": "black",
    "image_uris": {"normal": "https://img.scryfall.test/secret-lair.jpg"},
}

_STANDARD = {
    **_COMMON,
    "name": "Anointed Procession",
    "set_type": "expansion",
    "border_color": "black",
    # An ordinary frame marker (legendary crown, miracle, ...) must NOT count
    # as a special treatment — plenty of standard printings carry one.
    "frame_effects": ["legendary"],
    "image_uris": {"normal": "https://img.scryfall.test/standard.jpg"},
}

_DIGITAL = {
    **_COMMON,
    "name": "Anointed Procession",
    "set_type": "expansion",
    "border_color": "black",
    "digital": True,
    "image_uris": {"normal": "https://img.scryfall.test/mtgo.jpg"},
}

# An MDFC whose front face shares a name with a distinct single-faced card:
# even a pristine standard MDFC printing must not steal the plain card's key.
_MDFC_STANDARD = {
    "name": "Standard Bearer // Standard Bearer's Banner",
    "type_line": "Creature — Human Flagbearer // Artifact",
    "oracle_text": None,
    "color_identity": ["W"],
    "legalities": {"commander": "legal"},
    "layout": "modal_dfc",
    "set_type": "expansion",
    "oracle_id": "fake-oracle-mdfc",
    "border_color": "black",
    "highres_image": True,
    "card_faces": [{"name": "Standard Bearer", "image_uris": {"normal": "https://img.scryfall.test/mdfc-front.jpg"}}],
}

_PLAIN_CARD_UGLY_PRINTING = {
    "name": "Standard Bearer",
    "type_line": "Creature — Human Flagbearer",
    "oracle_text": "All Aura and Equipment spells...",
    "color_identity": ["W"],
    "legalities": {"commander": "legal"},
    "layout": "normal",
    "set_type": "promo",
    "oracle_id": "fake-oracle-plain",
    "border_color": "borderless",
    "full_art": True,
    "promo": True,
    "image_uris": {"normal": "https://img.scryfall.test/plain-promo.jpg"},
}


def main() -> int:
    printings = [_SHOWCASE, _BORDERLESS, _SECRET_LAIR, _DIGITAL, _STANDARD,
                 _MDFC_STANDARD, _PLAIN_CARD_UGLY_PRINTING]
    cache = scryfall_cache._trim_bulk_cards(printings)  # noqa: SLF001 — test-only introspection
    cache_reversed = scryfall_cache._trim_bulk_cards(list(reversed(printings)))  # noqa: SLF001

    checks = []
    for label, c in (("specials-first order", cache), ("standard-first order", cache_reversed)):
        procession = c.get("anointed procession") or {}
        bearer = c.get("standard bearer") or {}
        checks += [
            (f"{label}: standard printing wins the key",
             (procession.get("image_uris") or {}).get("normal") == "https://img.scryfall.test/standard.jpg"),
            (f"{label}: direct plain card beats a prettier MDFC alias",
             bearer.get("layout") == "normal"),
            (f"{label}: MDFC still reachable under its full name",
             "standard bearer // standard bearer's banner" in c),
        ]

    failed = [label for label, ok in checks if not ok]
    if failed:
        print(f"FAILED: {failed}", file=sys.stderr)
        return 1

    print("OK: standard black-border printing wins the name key over showcase/"
          "borderless/Secret Lair/digital printings, without breaking the "
          "MDFC-alias and direct-name rules.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
