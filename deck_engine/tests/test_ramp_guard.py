"""Regression tests for the 2026-07-11 ramp-collapse fixes (post-mortem of runs
7b80666e / Xanathar and 3d23a52f / Hraesvelgr, which shipped 4 and 1 structural
ramp sources against the 10-12 quota): the structural is_ramp_card matcher, the
validate_deck ramp tripwire + quota-targeting repair notes, ramp protection in
the synergy-repair path (candidate filtering + code-level swap vetoes), and the
per-stage thinking-budget resolution that gives the draft stage its
deliberation back.

Run manually: cd gishath-local-v2 && python3 -m deck_engine.tests.test_ramp_guard
"""
from __future__ import annotations

import sys
from unittest import mock

from .. import claude_cli, config, prompt_helpers
from ..agent_pipeline import _quota_scorecard, _ramp_protected_names, _veto_ramp_swaps
from ..scryfall_cache import is_ramp_card, validate_deck

_CACHE = {
    "test commander": {"name": "Test Commander", "type_line": "Legendary Creature — Elf",
                       "color_identity": ["G", "U"], "oracle_text": "Elves matter.",
                       "legalities": {"commander": "legal"}, "mana_cost": "{2}{G}{U}"},
    "forest": {"name": "Forest", "type_line": "Basic Land — Forest", "color_identity": ["G"],
               "legalities": {"commander": "legal"}, "oracle_text": ""},
    "command tower": {"name": "Command Tower", "type_line": "Land", "color_identity": [],
                      "legalities": {"commander": "legal"},
                      "oracle_text": "{T}: Add one mana of any color in your commander's color identity."},
    "sol ring": {"name": "Sol Ring", "type_line": "Artifact", "color_identity": [],
                 "legalities": {"commander": "legal"}, "oracle_text": "{T}: Add {C}{C}."},
    "llanowar elves": {"name": "Llanowar Elves", "type_line": "Creature — Elf Druid",
                       "color_identity": ["G"], "legalities": {"commander": "legal"},
                       "oracle_text": "{T}: Add {G}.", "mana_cost": "{G}"},
    "rampant growth": {"name": "Rampant Growth", "type_line": "Sorcery", "color_identity": ["G"],
                       "legalities": {"commander": "legal"},
                       "oracle_text": "Search your library for a basic land card, put that card "
                                      "onto the battlefield tapped, then shuffle."},
    "exploration": {"name": "Exploration", "type_line": "Enchantment", "color_identity": ["G"],
                    "legalities": {"commander": "legal"},
                    "oracle_text": "You may play an additional land on each of your turns."},
    "counterspell": {"name": "Counterspell", "type_line": "Instant", "color_identity": ["U"],
                     "legalities": {"commander": "legal"},
                     "oracle_text": "Counter target spell.", "mana_cost": "{U}{U}"},
    "opt": {"name": "Opt", "type_line": "Instant", "color_identity": ["U"],
            "legalities": {"commander": "legal"},
            "oracle_text": "Scry 1. Draw a card.", "mana_cost": "{U}"},
    "grizzly bears": {"name": "Grizzly Bears", "type_line": "Creature — Bear",
                      "color_identity": ["G"], "legalities": {"commander": "legal"},
                      "oracle_text": "", "mana_cost": "{1}{G}"},
    "day of judgment": {"name": "Day of Judgment", "type_line": "Sorcery", "color_identity": ["W"],
                        "legalities": {"commander": "legal"},
                        "oracle_text": "Destroy all creatures.", "mana_cost": "{2}{W}{W}"},
}


def test_is_ramp_card_classification() -> list[str]:
    problems = []
    should_be_ramp = ["sol ring", "llanowar elves", "rampant growth", "exploration"]
    for key in should_be_ramp:
        if not is_ramp_card(_CACHE[key]):
            problems.append(f"{key} must count as structural ramp")
    should_not = ["counterspell", "opt", "grizzly bears"]
    for key in should_not:
        if is_ramp_card(_CACHE[key]):
            problems.append(f"{key} must NOT count as structural ramp")
    # Lands never count as ramp, even mana-producing ones — they're the land
    # count's job (a Command Tower reading as "ramp" would let a rock-less
    # deck sail past the tripwire on its mana base alone).
    if is_ramp_card(_CACHE["command tower"]):
        problems.append("a land must never count as ramp")
    if is_ramp_card({}):
        problems.append("an empty/unknown card dict must not count as ramp")
    return problems


def test_validate_deck_ramp_tripwire_and_notes() -> list[str]:
    problems = []
    low_ramp = ["Sol Ring", "Counterspell", "Opt", "Grizzly Bears", "Forest"]
    result = validate_deck("Test Commander", low_ramp, cache=_CACHE, min_ramp=7, ramp_target=10)
    if result.ramp_count != 1:
        problems.append(f"expected 1 structural ramp counted, got {result.ramp_count}")
    if not result.too_few_ramp:
        problems.append("1 ramp source under a min_ramp=7 tripwire must flag too_few_ramp")
    notes = result.as_repair_notes()
    if "at least 10 ramp" not in notes:
        problems.append(f"repair notes should target the quota floor (10), got: {notes}")
    if "at least 7" in notes:
        problems.append("repair notes still name the tripwire (7) as the target")
    if "Do NOT cut lands" not in notes:
        problems.append("repair notes must forbid cutting lands to make room for ramp")

    healthy = ["Sol Ring", "Llanowar Elves", "Rampant Growth", "Exploration", "Counterspell"]
    result = validate_deck("Test Commander", healthy, cache=_CACHE, min_ramp=4, ramp_target=10)
    if result.too_few_ramp:
        problems.append("4 ramp sources must clear a min_ramp=4 tripwire")
    # min_ramp=0 (the default) must disable the check entirely.
    result = validate_deck("Test Commander", ["Counterspell"], cache=_CACHE)
    if result.too_few_ramp:
        problems.append("min_ramp=0 must disable the ramp check")
    return problems


def test_ramp_protection_and_swap_vetoes() -> list[str]:
    problems = []
    # 4 ramp cards, ramp_max=12 -> no surplus -> all protected.
    cards = ["Sol Ring", "Llanowar Elves", "Rampant Growth", "Exploration",
             "Counterspell", "Grizzly Bears"]
    protected = _ramp_protected_names(cards, _CACHE)
    if protected != {"sol ring", "llanowar elves", "rampant growth", "exploration"}:
        problems.append(f"expected the full ramp suite protected, got {protected}")

    # A ramp surplus beyond ramp_max lifts protection entirely.
    surplus = ["Sol Ring"] + [f"Fake Rock {i}" for i in range(int(config.ROLE_QUOTA_DEFAULTS["ramp_max"]))]
    fat_cache = dict(_CACHE)
    for i in range(int(config.ROLE_QUOTA_DEFAULTS["ramp_max"])):
        fat_cache[f"fake rock {i}"] = {"name": f"Fake Rock {i}", "type_line": "Artifact",
                                       "oracle_text": "{T}: Add {C}.",
                                       "legalities": {"commander": "legal"}, "color_identity": []}
    if _ramp_protected_names(surplus, fat_cache):
        problems.append("a deck with ramp surplus beyond ramp_max must protect nothing")

    # Veto: ramp out / non-ramp in is dropped; ramp-for-ramp and nonramp swaps survive.
    swaps = [
        {"remove": "Sol Ring", "add": "Grizzly Bears", "reason": "r"},
        {"remove": "Llanowar Elves", "add": "Rampant Growth", "reason": "r"},
        {"remove": "Counterspell", "add": "Opt", "reason": "r"},
    ]
    kept, warnings = _veto_ramp_swaps(cards, swaps, _CACHE)
    if len(kept) != 2 or any(s["remove"] == "Sol Ring" for s in kept):
        problems.append(f"expected only the Sol Ring swap vetoed, kept {kept}")
    if len(warnings) != 1 or "Sol Ring" not in warnings[0]:
        problems.append(f"expected one veto warning naming Sol Ring, got {warnings}")
    return problems


def test_quota_scorecard_counts_and_renders() -> list[str]:
    problems = []
    cards = ["Forest", "Sol Ring", "Counterspell", "Opt", "Day of Judgment", "Grizzly Bears",
             "Unknown Mystery Card"]
    scorecard = _quota_scorecard(cards, _CACHE)
    q = config.ROLE_QUOTA_DEFAULTS
    expectations = [
        f"- Lands: 1 (quota {q['land_min']}-{q['land_max']})",
        f"- Ramp: 1 (quota {q['ramp_min']}-{q['ramp_max']})",
        f"- Card draw: 1 (quota {q['draw_min']}-{q['draw_max']})",
        f"- Interaction/removal: 1 (quota {q['interaction_min']}-{q['interaction_max']})",
        f"- Board wipes: 1 (quota {q['wipes_min']}-{q['wipes_max']})",
    ]
    for line in expectations:
        if line not in scorecard:
            problems.append(f"missing/incorrect scorecard line {line!r} in:\n{scorecard}")
    # optimize.md must actually consume the $role_scorecard placeholder.
    rendered = prompt_helpers.render("optimize.md", role_scorecard=scorecard)
    if "$role_scorecard" in rendered or "- Board wipes: 1" not in rendered:
        problems.append("optimize.md did not substitute the role scorecard")
    return problems


def test_thinking_budget_stage_resolution() -> list[str]:
    problems = []
    with mock.patch.object(config, "THINKING_BUDGET_BY_STAGE", {"draft": 10000}), \
         mock.patch.object(config, "THINKING_BUDGET_BY_MODEL", {"haiku": 6000, "sonnet": 0, "opus": 0}), \
         mock.patch.object(config, "THINKING_BUDGET_TOKENS", 6000):
        if claude_cli._resolve_thinking_budget("draft", "sonnet") != 10000:
            problems.append("draft stage must get the per-stage budget, not sonnet's 0")
        if claude_cli._resolve_thinking_budget("judge", "sonnet") != 0:
            problems.append("non-draft sonnet stages must stay on the diet (0)")
        if claude_cli._resolve_thinking_budget("card_tagger", "haiku") != 6000:
            problems.append("haiku stages must keep the per-model budget")
        if claude_cli._resolve_thinking_budget("select", "unknown-model") != 6000:
            problems.append("an unlisted model must fall back to the global budget")
    with mock.patch.object(config, "THINKING_BUDGET_BY_STAGE", {"draft": -5}):
        if claude_cli._resolve_thinking_budget("draft", "sonnet") != 0:
            problems.append("a negative configured budget must clamp to 0")
    return problems


def main() -> int:
    tests = [
        test_is_ramp_card_classification,
        test_validate_deck_ramp_tripwire_and_notes,
        test_ramp_protection_and_swap_vetoes,
        test_quota_scorecard_counts_and_renders,
        test_thinking_budget_stage_resolution,
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

    print(f"OK: all {len(tests)} ramp-guard checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
