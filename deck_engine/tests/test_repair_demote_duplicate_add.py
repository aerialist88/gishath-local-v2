"""Regression tests for the run-46858f10 post-mortem (2026-07-14): a repair
swap whose `add` was already in the deck (Rugged Highlands -> Rugged Prairie)
was skipped entirely, keeping the off-color offender through the final repair
attempt and failing the run. Fixes under test: repair-mode swaps demote a
duplicate-add to a bare cut, and the repair loop's last-resort count fill
restores an undercount deck with basics instead of failing.

Run manually: cd gishath-local-v2 && python3 -m deck_engine.tests.test_repair_demote_duplicate_add
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest import mock

from .. import agent_pipeline, config
from ..agent_pipeline import _apply_repair_deltas, _apply_swaps, _top_up_basics, _validate_and_repair
from ..concept_selector import ConceptChoice


def _land(name: str, colors: list[str]) -> dict:
    return {"name": name, "type_line": "Land", "color_identity": colors,
            "legalities": {"commander": "legal"}, "oracle_text": ""}


def _cache() -> dict:
    cache = {
        "test commander": {"name": "Test Commander", "type_line": "Legendary Creature — Elf",
                           "color_identity": ["G", "U"], "oracle_text": "Elves matter.",
                           "legalities": {"commander": "legal"}, "mana_cost": "{2}{G}{U}"},
        "forest": {"name": "Forest", "type_line": "Basic Land — Forest", "color_identity": ["G"],
                   "legalities": {"commander": "legal"}, "oracle_text": ""},
        "island": {"name": "Island", "type_line": "Basic Land — Island", "color_identity": ["U"],
                   "legalities": {"commander": "legal"}, "oracle_text": ""},
        "breeding pool": _land("Breeding Pool", ["G", "U"]),
        "off-color land": _land("Off-Color Land", ["R"]),
    }
    for i in range(10):
        cache[f"ramp rock {i}"] = {"name": f"Ramp Rock {i}", "type_line": "Artifact",
                                   "color_identity": [], "legalities": {"commander": "legal"},
                                   "oracle_text": "{T}: Add {C}.", "mana_cost": "{2}"}
    for i in range(70):
        cache[f"filler spell {i}"] = {"name": f"Filler Spell {i}", "type_line": "Instant",
                                      "color_identity": ["U"], "legalities": {"commander": "legal"},
                                      "oracle_text": "Draw a card.", "mana_cost": "{1}{U}"}
    return cache


def _concept() -> ConceptChoice:
    return ConceptChoice(commander="Test Commander", archetype="Test", rationale="r",
                         color_identity=["G", "U"], oracle_text="Elves matter.",
                         mechanic_tokens=["elf"])


def test_apply_swaps_demotes_duplicate_add_to_cut() -> list[str]:
    problems = []
    cards = ["Off-Color Land", "Breeding Pool", "Filler Spell 0"]
    swaps = [{"remove": "Off-Color Land", "add": "Breeding Pool", "reason": "r"}]
    new_cards, warnings = _apply_swaps(cards, swaps, strict=True, demote_duplicate_add_to_cut=True)
    if "Off-Color Land" in new_cards:
        problems.append(f"demote mode must still cut the remove target, got {new_cards}")
    if new_cards.count("Breeding Pool") != 1:
        problems.append(f"the duplicate add must be dropped, got {new_cards}")
    if not warnings:
        problems.append("expected a warning describing the demoted swap")
    # Default mode is unchanged: skip the whole swap, keep the remove target.
    unchanged, _ = _apply_swaps(cards, swaps, strict=True)
    if unchanged != cards:
        problems.append(f"non-repair callers must keep the skip-entirely behavior, got {unchanged}")
    # Demote only fires when the remove target actually exists.
    ghost = [{"remove": "Not In Deck", "add": "Breeding Pool", "reason": "r"}]
    same, _ = _apply_swaps(cards, ghost, strict=True, demote_duplicate_add_to_cut=True)
    if same != cards:
        problems.append(f"a duplicate-add swap with a missing remove target must be a no-op, got {same}")
    return problems


def test_apply_repair_deltas_uses_demote() -> list[str]:
    problems = []
    cards = ["Off-Color Land", "Breeding Pool", "Filler Spell 0"]
    parsed = {"swaps": [{"remove": "Off-Color Land", "add": "Breeding Pool", "reason": "r"}],
              "cuts": [], "adds": []}
    new_cards, _ = _apply_repair_deltas(cards, parsed)
    if "Off-Color Land" in new_cards:
        problems.append(f"repair deltas must demote the duplicate-add swap, got {new_cards}")
    return problems


def test_top_up_basics_fill_to_count_ignores_land_quota() -> list[str]:
    problems = []
    land_min = int(config.ROLE_QUOTA_DEFAULTS["land_min"])
    cache = _cache()
    # Lands already at quota, deck 2 short: the normal top-up must not touch it,
    # the fill_to_count last resort must fill to DECK_SIZE-1.
    cards = ["Forest"] * land_min + ["Filler Spell 0", "Filler Spell 1"]
    cards += [f"Filler Spell {i}" for i in range(2, (config.DECK_SIZE - 1) - len(cards))]
    _, added_normal = _top_up_basics(_concept(), cards, cache)
    if added_normal:
        problems.append(f"normal top-up must respect the land quota, added {added_normal}")
    filled, added = _top_up_basics(_concept(), cards, cache, fill_to_count=True)
    if len(filled) != config.DECK_SIZE - 1:
        problems.append(f"fill_to_count must reach DECK_SIZE-1, got {len(filled)}")
    if len(added) != (config.DECK_SIZE - 1) - len(cards):
        problems.append(f"unexpected fill size: {added}")
    return problems


def test_validate_and_repair_recovers_run_46858f10_shape() -> list[str]:
    """End-to-end replay: one off-color land, lands one over quota, and the
    only repair attempt proposes swapping the offender for a dual already in
    the deck. Old behavior: swap skipped, offender kept, run fails. Fixed
    behavior: demote-to-cut removes the offender, the last-resort count fill
    tops the 98-card deck back to 99, and validation passes."""
    problems = []
    land_min = int(config.ROLE_QUOTA_DEFAULTS["land_min"])
    cache = _cache()
    lands = ["Breeding Pool", "Off-Color Land"] + ["Forest"] * (land_min - 10) + ["Island"] * 9
    ramp = [f"Ramp Rock {i}" for i in range(10)]
    filler_count = (config.DECK_SIZE - 1) - len(lands) - len(ramp)
    cards = lands + ramp + [f"Filler Spell {i}" for i in range(filler_count)]
    if len(cards) != config.DECK_SIZE - 1:
        return [f"test setup bug: deck has {len(cards)} cards"]

    fake_repair = SimpleNamespace(parsed_json=lambda: {
        "swaps": [{"remove": "Off-Color Land", "add": "Breeding Pool", "reason": "r"}],
        "cuts": [], "adds": []})
    with mock.patch.object(agent_pipeline.claude_cli, "run", return_value=fake_repair):
        final_cards, validation = _validate_and_repair(
            "test-run", _concept(), cards, cache, stage_prefix="test", max_attempts=1)

    if "Off-Color Land" in final_cards:
        problems.append("the off-color offender survived the repair loop")
    if len(final_cards) != config.DECK_SIZE - 1:
        problems.append(f"deck must be refilled to {config.DECK_SIZE - 1}, got {len(final_cards)}")
    if not validation.is_valid:
        problems.append(f"validation must pass after demote + count fill, got: {validation.as_repair_notes()}")
    return problems


def main() -> int:
    tests = [
        test_apply_swaps_demotes_duplicate_add_to_cut,
        test_apply_repair_deltas_uses_demote,
        test_top_up_basics_fill_to_count_ignores_land_quota,
        test_validate_and_repair_recovers_run_46858f10_shape,
    ]
    all_problems: dict[str, list[str]] = {}
    for test in tests:
        problems = test()
        if problems:
            all_problems[test.__name__] = problems

    if all_problems:
        for name, problems in all_problems.items():
            print(f"FAILED: {name}", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
        return 1

    print(f"OK: all {len(tests)} repair demote/count-fill checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
