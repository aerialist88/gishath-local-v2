"""Regression tests for the 2026-07-10 deck-quality fixes (post-mortem of run
9e430ab7 / Maralen): draft normalization + judge structural counts, the
land-target repair message + deterministic basics top-up, duplicate-add guards
in swap application and the budget pass, post-budget retagging, the CK
price-sanity quarantine, and the prose-consistency scan.

Run manually: cd gishath-local-v2 && python3 -m deck_engine.tests.test_deck_quality_fixes
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest import mock

from .. import card_tagger, config, prose_check
from ..agent_pipeline import _apply_swaps, _normalize_draft_cards, _top_up_basics
from ..concept_selector import ConceptChoice
from ..pricing import PricingOutcome, _suspicious_prices, cheapest_by_card, deck_price_summary
from ..scryfall_cache import validate_deck

# Minimal cache: enough shape for type_line/mana_cost/oracle_text lookups.
_CACHE = {
    "test commander": {"name": "Test Commander", "type_line": "Legendary Creature — Elf",
                       "color_identity": ["G", "U"], "oracle_text": "Elves matter.",
                       "legalities": {"commander": "legal"}, "mana_cost": "{2}{G}{U}"},
    "forest": {"name": "Forest", "type_line": "Basic Land — Forest", "color_identity": ["G"],
               "legalities": {"commander": "legal"}, "oracle_text": ""},
    "island": {"name": "Island", "type_line": "Basic Land — Island", "color_identity": ["U"],
               "legalities": {"commander": "legal"}, "oracle_text": ""},
    "breeding pool": {"name": "Breeding Pool", "type_line": "Land — Forest Island",
                      "color_identity": ["G", "U"], "legalities": {"commander": "legal"}, "oracle_text": ""},
    "llanowar elves": {"name": "Llanowar Elves", "type_line": "Creature — Elf Druid",
                       "color_identity": ["G"], "legalities": {"commander": "legal"},
                       "oracle_text": "{T}: Add {G}.", "mana_cost": "{G}"},
    "counterspell": {"name": "Counterspell", "type_line": "Instant", "color_identity": ["U"],
                     "legalities": {"commander": "legal"},
                     "oracle_text": "Counter target spell.", "mana_cost": "{U}{U}"},
    "craterhoof behemoth": {"name": "Craterhoof Behemoth", "type_line": "Creature — Beast",
                            "color_identity": ["G"], "legalities": {"commander": "legal"},
                            "oracle_text": "Haste. When this creature enters...", "mana_cost": "{5}{G}{G}{G}"},
    "villainous wealth": {"name": "Villainous Wealth", "type_line": "Sorcery",
                          "color_identity": ["B", "G", "U"], "legalities": {"commander": "legal"},
                          "oracle_text": "Target opponent exiles the top X cards...", "mana_cost": "{X}{B}{G}{U}"},
    "glen elendra": {"name": "Glen Elendra", "type_line": "Land", "color_identity": [],
                     "legalities": {"commander": "legal"}, "oracle_text": ""},
    "glen elendra archmage": {"name": "Glen Elendra Archmage", "type_line": "Creature — Faerie Wizard",
                              "color_identity": ["U"], "legalities": {"commander": "legal"},
                              "oracle_text": "Flying. Sacrifice: counter target noncreature spell.",
                              "mana_cost": "{3}{U}"},
}


def _concept(colors=("G", "U")) -> ConceptChoice:
    return ConceptChoice(
        commander="Test Commander", archetype="Test", rationale="r",
        color_identity=list(colors), oracle_text="Elves matter.", mechanic_tokens=["elf"],
    )


def test_normalize_draft_cards_dedupes_but_keeps_basics() -> list[str]:
    problems = []
    raw = ["Llanowar Elves", "llanowar elves", "Forest", "Forest", "  ", "Counterspell",
           "Breeding Pool", "Breeding Pool"]
    clean, dupes = _normalize_draft_cards(raw, _CACHE)
    if clean.count("Forest") != 2:
        problems.append(f"basics must survive dedup, got {clean}")
    if sum(1 for c in clean if c.lower() == "llanowar elves") != 1:
        problems.append(f"expected one Llanowar Elves, got {clean}")
    if clean.count("Breeding Pool") != 1:
        problems.append(f"expected duplicate Breeding Pool removed, got {clean}")
    if dupes != 2:
        problems.append(f"expected 2 duplicates counted (Elves + Breeding Pool), got {dupes}")
    return problems


def test_top_up_basics_fills_undercount_to_land_min() -> list[str]:
    problems = []
    land_min = int(config.ROLE_QUOTA_DEFAULTS["land_min"])
    # 90 cards, 30 lands -> room for 9, need land_min-30: top-up adds min(9, land_min-30).
    cards = (["Forest"] * 15 + ["Island"] * 15 + ["Llanowar Elves"] * 1 + ["Counterspell"] * 1
             + ["Craterhoof Behemoth"] * 1)
    cards += ["Glen Elendra Archmage"] * (90 - len(cards))
    new_cards, added = _top_up_basics(_concept(), cards, _CACHE)
    expected = min((config.DECK_SIZE - 1) - 90, land_min - 30)
    if len(added) != expected:
        problems.append(f"expected {expected} basics added, got {len(added)}: {added}")
    if any(b not in ("Forest", "Island") for b in added):
        problems.append(f"added basics outside the GU identity: {added}")
    if len(new_cards) != 90 + expected:
        problems.append(f"wrong total after top-up: {len(new_cards)}")
    # Full deck: never add past DECK_SIZE-1 even when lands are short.
    full = ["Counterspell"] * (config.DECK_SIZE - 1)
    _, added_full = _top_up_basics(_concept(), full, _CACHE)
    if added_full:
        problems.append(f"top-up must not exceed the deck size, added {added_full}")
    # Healthy mana base: no-op even with room.
    healthy = ["Forest"] * land_min + ["Counterspell"] * 10
    _, added_healthy = _top_up_basics(_concept(), healthy, _CACHE)
    if added_healthy:
        problems.append(f"top-up fired on a healthy mana base: {added_healthy}")
    return problems


def test_repair_notes_target_quota_not_tripwire() -> list[str]:
    problems = []
    cards = ["Forest"] * 20 + ["Counterspell"] * 79
    result = validate_deck("Test Commander", cards, cache=_CACHE, min_lands=33, land_target=35)
    notes = result.as_repair_notes()
    if "at least 35 lands" not in notes:
        problems.append(f"repair notes should target the quota floor (35), got: {notes}")
    if "at least 33" in notes:
        problems.append("repair notes still name the tripwire (33) as the target")
    return problems


def test_apply_swaps_rejects_duplicate_add() -> list[str]:
    problems = []
    cards = ["Counterspell", "Llanowar Elves", "Breeding Pool"]
    swaps = [{"remove": "Llanowar Elves", "add": "Breeding Pool", "reason": "r"}]
    new_cards, warnings = _apply_swaps(cards, swaps)
    if new_cards != cards:
        problems.append(f"duplicate-add swap must be skipped entirely, got {new_cards}")
    if not warnings:
        problems.append("expected a warning for the skipped duplicate-add swap")
    # Basics are exempt: swapping something for a Forest already present is fine.
    swaps = [{"remove": "Counterspell", "add": "Forest", "reason": "r"}]
    new_cards, warnings = _apply_swaps(["Counterspell", "Forest"], swaps)
    if new_cards != ["Forest", "Forest"]:
        problems.append(f"basic-land adds must stay allowed even when present, got {new_cards}")
    return problems


def test_budget_vetting_rejects_add_already_in_deck() -> list[str]:
    problems = []
    from .. import budget_pass
    from ..agent_pipeline import DeckResult
    from ..scryfall_cache import ValidationResult

    deck = DeckResult(
        concept=_concept(), cards=["Expensive Card", "Breeding Pool", "Counterspell"],
        validation=ValidationResult(commander="Test Commander", card_count=4),
        changes_made="", early_game="", mid_game="", late_game="",
    )
    for c in deck.cards:
        deck.card_tags[c.lower()] = {"role": "Synergy piece", "phase": "mid"}
    assignments = [SimpleNamespace(card="Expensive Card", price=500.0, store="S")]
    pricing = PricingOutcome(plan=SimpleNamespace(strategy_a=SimpleNamespace(all_assignments=assignments)))

    # Model proposes a swap into a card already in the deck — must be vetoed in
    # code, deck untouched, and the veto noted.
    proposed = [{"remove": "Expensive Card", "add": "Breeding Pool", "reason": "cheap dual",
                 "role": "Land/Mana base", "phase": "early"}]
    with mock.patch.object(budget_pass, "_swap_call", return_value=proposed), \
         mock.patch.object(budget_pass, "_validate_and_repair",
                           side_effect=AssertionError("vetoed swap must never reach validation")), \
         mock.patch.object(budget_pass.card_tagger, "retag_untagged"):
        outcome = budget_pass.enforce_card_cap("test-run", deck, pricing, _CACHE)
    if "Expensive Card" not in deck.cards:
        problems.append("deck was mutated despite the only swap being vetoed")
    if not any("already in the deck" in n for n in outcome.notes):
        problems.append(f"expected an 'already in the deck' veto note, got {outcome.notes}")
    if not outcome.over_budget or outcome.over_budget[0][0] != "Expensive Card":
        problems.append(f"unswappable over-cap card should ship flagged, got {outcome.over_budget}")
    return problems


def test_budget_outcome_reports_unpriced_cards() -> list[str]:
    problems = []
    from .. import budget_pass
    from ..agent_pipeline import DeckResult
    from ..scryfall_cache import ValidationResult

    deck = DeckResult(
        concept=_concept(), cards=["Counterspell", "Mystery Card"],
        validation=ValidationResult(commander="Test Commander", card_count=3),
        changes_made="", early_game="", mid_game="", late_game="",
    )
    assignments = [SimpleNamespace(card="Counterspell", price=2.0, store="S")]
    pricing = PricingOutcome(plan=SimpleNamespace(strategy_a=SimpleNamespace(all_assignments=assignments)))
    with mock.patch.object(budget_pass.card_tagger, "retag_untagged"):
        outcome = budget_pass.enforce_card_cap("test-run", deck, pricing, _CACHE)
    if outcome.unpriced != ["Mystery Card"]:
        problems.append(f"expected Mystery Card reported unpriced, got {outcome.unpriced}")
    return problems


def test_retag_untagged_uses_heuristics_without_model() -> list[str]:
    problems = []
    tags = {"counterspell": {"role": "Interaction", "phase": "mid"}}
    with mock.patch.object(card_tagger, "_haiku_tag_remainder",
                           side_effect=AssertionError("lands must be tagged heuristically")) as _:
        card_tagger.retag_untagged("test-run", "Test Commander",
                                   ["Counterspell", "Breeding Pool"], tags, _CACHE)
    if tags.get("breeding pool", {}).get("role") != "Land/Mana base":
        problems.append(f"expected heuristic Land/Mana base tag, got {tags.get('breeding pool')}")
    if tags["counterspell"]["role"] != "Interaction":
        problems.append("existing tags must not be overwritten")
    return problems


def test_ck_sanity_quarantines_bogus_price() -> list[str]:
    problems = []
    assignments = [
        SimpleNamespace(card="Bayou", price=0.45, store="Bad Store"),
        SimpleNamespace(card="Counterspell", price=2.0, store="Good Store"),
    ]
    plan = SimpleNamespace(strategy_a=SimpleNamespace(all_assignments=assignments))
    ck = {"bayou": {"priceUsd": 229.99}, "counterspell": {"priceUsd": 2.49}}
    suspicious = _suspicious_prices(plan, ck)
    if "bayou" not in suspicious:
        problems.append("SGD 0.45 against a USD 229.99 CK reference must be quarantined")
    if "counterspell" in suspicious:
        problems.append("a normal price must not be quarantined")
    pricing = PricingOutcome(plan=plan, ck_prices=ck, suspicious=suspicious)
    cheapest = cheapest_by_card(pricing)
    if "bayou" in cheapest:
        problems.append("quarantined price leaked into cheapest_by_card()")
    summary = deck_price_summary(pricing, ["Bayou", "Counterspell"])
    if summary["unpriced_count"] != 1 or summary["total"] != 2.0:
        problems.append(f"quarantined card must count as unpriced/excluded, got {summary}")
    return problems


def test_prose_scan_flags_missing_cards_only() -> list[str]:
    problems = []
    deck_cards = ["Glen Elendra Archmage", "Counterspell", "Forest"]
    prose = ["Convert a big turn with Villainous Wealth while Glen Elendra Archmage protects the "
             "engine; hold up counterspells."]
    stale = prose_check.stale_names(deck_cards, "Test Commander", prose, _CACHE)
    if stale != ["Villainous Wealth"]:
        problems.append(f"expected only Villainous Wealth flagged, got {stale}")
    # "Glen Elendra" (a real land) is a substring of a deck card — must not flag;
    # lowercase "counterspells" mid-sentence must not flag the card Counterspell
    # in a deck that lacks it... but here it IS in the deck anyway; check the
    # case-sensitivity guard directly:
    stale = prose_check.stale_names(["Forest"], "Test Commander",
                                    ["hold up counterspells and develop"], _CACHE)
    if stale:
        problems.append(f"lowercase prose words must not flag card names, got {stale}")
    clean = prose_check.stale_names(deck_cards, "Test Commander",
                                    ["Glen Elendra Archmage counters things."], _CACHE)
    if clean:
        problems.append(f"expected clean prose to flag nothing, got {clean}")
    # Single-word names: sentence-initial is ordinary English ("Counterspell the
    # win attempt"), mid-sentence is a card reference ("hold up Counterspell").
    initial = prose_check.stale_names(["Forest"], "Test Commander",
                                      ["Counterspell the win attempt."], _CACHE)
    if initial:
        problems.append(f"sentence-initial single word must not flag, got {initial}")
    mid = prose_check.stale_names(["Forest"], "Test Commander",
                                  ["hold up Counterspell for the crack-back."], _CACHE)
    if mid != ["Counterspell"]:
        problems.append(f"mid-sentence card reference must flag, got {mid}")
    return problems


def main() -> int:
    tests = [
        test_normalize_draft_cards_dedupes_but_keeps_basics,
        test_top_up_basics_fills_undercount_to_land_min,
        test_repair_notes_target_quota_not_tripwire,
        test_apply_swaps_rejects_duplicate_add,
        test_budget_vetting_rejects_add_already_in_deck,
        test_budget_outcome_reports_unpriced_cards,
        test_retag_untagged_uses_heuristics_without_model,
        test_ck_sanity_quarantines_bogus_price,
        test_prose_scan_flags_missing_cards_only,
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

    print(f"OK: all {len(tests)} deck-quality fix checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
