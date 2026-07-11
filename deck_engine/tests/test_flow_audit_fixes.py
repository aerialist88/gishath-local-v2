"""Regression tests for the 2026-07-11 flow-audit fixes: face-aware oracle text
(oracle_text_of — MDFC/transform/adventure text lives in card_faces and every
consumer used to read the empty top-level field), the synergy-gate and
post-optimize revert-over-raise policies (run 33606bc6 died with a valid deck
in hand), the judge's ramp evidence, and the EDHREC front-face slug.

Run manually: cd gishath-local-v2 && python3 -m deck_engine.tests.test_flow_audit_fixes
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest import mock

from .. import agent_pipeline, card_tagger, config, edhrec_pool, synergy_check
from ..concept_selector import ConceptChoice, _forced_concept
from ..scryfall_cache import ValidationResult, is_ramp_card, oracle_text_of

# An adventure (no land face — a spell//LAND MDFC is deliberately counted as a
# land, never ramp, by validate_deck's mutually-exclusive branches).
_MDFC_RAMP = {
    "name": "Meadow Giant // Fertile Steps", "type_line": "Creature — Giant // Sorcery — Adventure",
    "color_identity": ["G"], "legalities": {"commander": "legal"}, "oracle_text": "",
    "card_faces": [
        {"name": "Meadow Giant", "oracle_text": "Trample."},
        {"name": "Fertile Steps",
         "oracle_text": "Search your library for a basic land card, put it onto the battlefield, then shuffle."},
    ],
}
_FLIP_SAGA = {
    "name": "Tale of Elves // Elvish Relic", "type_line": "Enchantment — Saga // Enchantment Creature — Elf",
    "color_identity": ["G"], "legalities": {"commander": "legal"}, "oracle_text": "",
    "card_faces": [
        {"name": "Tale of Elves", "oracle_text": "I — Create a 1/1 green Elf Warrior creature token."},
        {"name": "Elvish Relic", "oracle_text": "Elves you control get +1/+1."},
    ],
}
_MDFC_DRAW = {
    "name": "Insight // Hindsight", "type_line": "Instant // Sorcery",
    "color_identity": ["U"], "legalities": {"commander": "legal"}, "oracle_text": "",
    "card_faces": [
        {"name": "Insight", "oracle_text": "Draw two cards."},
        {"name": "Hindsight", "oracle_text": "Return target card from your graveyard to your hand."},
    ],
}
_DFC_WALKER_COMMANDER = {
    "name": "Valin, the Unbound // Valin Ascended",
    "type_line": "Legendary Planeswalker — Valin // Legendary Planeswalker — Valin",
    "color_identity": ["U"], "legalities": {"commander": "legal"}, "oracle_text": "",
    "mana_cost": "{2}{U}",
    "card_faces": [
        {"name": "Valin, the Unbound", "oracle_text": "+1: Scry 2."},
        {"name": "Valin Ascended", "oracle_text": "Valin Ascended can be your commander.\n-2: Draw a card."},
    ],
}
_PLAIN = {"name": "Counterspell", "type_line": "Instant", "color_identity": ["U"],
          "legalities": {"commander": "legal"}, "oracle_text": "Counter target spell."}


def test_oracle_text_of_joins_faces() -> list[str]:
    problems = []
    if oracle_text_of(_PLAIN) != "Counter target spell.":
        problems.append("single-face cards must pass their top-level text through unchanged")
    joined = oracle_text_of(_MDFC_RAMP)
    if "Search your library for a basic land card" not in joined or "Trample." not in joined:
        problems.append(f"multi-face text must include every face, got: {joined!r}")
    if "Meadow Giant:" not in joined:
        problems.append("face text should be labeled with the face name")
    if oracle_text_of({}) != "":
        problems.append("a card with no text anywhere must yield an empty string")
    return problems


def test_face_aware_consumers() -> list[str]:
    problems = []
    if not is_ramp_card(_MDFC_RAMP):
        problems.append("an MDFC whose faces are land-ramp + a mana ability must count as ramp")
    cache = {"tale of elves // elvish relic": _FLIP_SAGA, "counterspell": _PLAIN}
    matches, generic = synergy_check.count_synergy_matches(
        ["Tale of Elves // Elvish Relic", "Counterspell"], ["elf"], cache)
    if matches != 1 or "Tale of Elves // Elvish Relic" in generic:
        problems.append(f"a flip-Saga with on-mechanic face text must match, got matches={matches}, generic={generic}")
    role_phase = card_tagger._heuristic_tag(_MDFC_DRAW)
    if role_phase is None or role_phase[0] != "Card draw":
        problems.append(f"tagger heuristics must read face text, got {role_phase}")
    return problems


def test_dfc_commander_eligibility_and_grounding() -> list[str]:
    problems = []
    cache = {"valin, the unbound // valin ascended": _DFC_WALKER_COMMANDER}
    fake_result = SimpleNamespace(parsed_json=lambda: {"commander": "x", "archetype": "a", "rationale": "r"},
                                  session_id="s")
    with mock.patch.object(agent_pipeline.claude_cli, "run", return_value=fake_result), \
         mock.patch.object(synergy_check, "extract_mechanic_tokens", return_value=["scry"]) as tok:
        concept = _forced_concept("test-run", "Valin, the Unbound // Valin Ascended", cache)
    if "can be your commander" not in concept.oracle_text:
        problems.append("a back-face 'can be your commander' must make a DFC walker eligible "
                        "and its full text must reach ConceptChoice.oracle_text")
    if "+1: Scry 2." not in concept.oracle_text:
        problems.append(f"front-face text missing from the concept grounding: {concept.oracle_text!r}")
    if "Scry 2" not in str(tok.call_args):
        problems.append("mechanic-token extraction must receive the face-joined oracle text")
    return problems


def test_synergy_gate_reverts_to_valid_deck() -> list[str]:
    problems = []
    concept = ConceptChoice(commander="Test Commander", archetype="t", rationale="r",
                            color_identity=["U"], oracle_text="Elves matter.", mechanic_tokens=["elf"])
    cache = {"counterspell": _PLAIN}
    entry_cards = ["Counterspell"]  # 1 match < threshold -> gate fires
    entry_validation = ValidationResult(commander="Test Commander", card_count=2)
    invalid = ValidationResult(commander="Test Commander", card_count=2, wrong_card_count=True)
    fake_result = SimpleNamespace(parsed_json=lambda: {"swaps": []}, session_id="s")

    with mock.patch.object(agent_pipeline.claude_cli, "run", return_value=fake_result), \
         mock.patch.object(agent_pipeline, "_validate_and_repair",
                           return_value=(["Bogus Card"], invalid)):
        cards, validation, fired = agent_pipeline._synergy_gate_and_repair(
            "test-run", concept, entry_cards, entry_validation, cache, stage_prefix="synergy/test",
        )
    if not fired:
        problems.append("gate must report it fired")
    if cards != entry_cards or not validation.is_valid:
        problems.append(f"exhausted invalid repairs must revert to the valid entry deck, got {cards}")
    return problems


def test_optimize_failure_reverts_not_raises() -> list[str]:
    problems = []
    concept = ConceptChoice(commander="Test Commander", archetype="t", rationale="r",
                            color_identity=["U"], oracle_text="x", mechanic_tokens=[])
    good_cards = ["Counterspell"]
    valid = ValidationResult(commander="Test Commander", card_count=2)
    invalid = ValidationResult(commander="Test Commander", card_count=2, wrong_card_count=True)
    draft = {"angle_name": "a", "gameplan_summary": "g", "key_cards": [], "cards": good_cards}
    optimize_response = {"strategy_valid": True, "strategy_problem": "", "final_archetype": "t",
                         "final_summary": "s", "swaps": [{"remove": "Counterspell", "add": "Bogus", "reason": "r"}],
                         "changes_made": "", "early_game": "", "mid_game": "", "late_game": ""}

    with mock.patch.object(agent_pipeline.edhrec_pool, "pool_block", return_value=("(none)", False)), \
         mock.patch.object(agent_pipeline, "_draft", return_value=[draft]), \
         mock.patch.object(agent_pipeline, "_judge",
                           return_value=(1, good_cards, "brief", [], [], "sess")), \
         mock.patch.object(agent_pipeline, "_optimize", return_value=(optimize_response, "sess2")), \
         mock.patch.object(agent_pipeline, "_validate_and_repair",
                           side_effect=[(good_cards, valid), (["Bogus"], invalid)]), \
         mock.patch.object(agent_pipeline, "_synergy_gate_and_repair",
                           side_effect=lambda run_id, c, cards, v, cache, stage_prefix: (cards, v, False)):
        try:
            (_, final_cards, final_validation, _, swap_warnings, _, _) = agent_pipeline._run_one_attempt(
                "test-run", concept, {"counterspell": _PLAIN}, 1, "",
            )
        except agent_pipeline.claude_cli.ClaudeCLIError as exc:
            return [f"an unrepairable optimize swap must revert, not fail the run: {exc}"]
    if final_cards != good_cards or not final_validation.is_valid:
        problems.append(f"expected the pre-optimize deck back, got {final_cards}")
    if not any("reverted to the pre-optimize decklist" in w for w in swap_warnings):
        problems.append(f"expected a revert warning for the email trail, got {swap_warnings}")
    return problems


def test_edhrec_slug_uses_front_face() -> list[str]:
    problems = []
    if edhrec_pool._slugify("Eddie Brock // Venom, Lethal Protector") != "eddie-brock":
        problems.append(f"'//' names must slug by front face, got "
                        f"{edhrec_pool._slugify('Eddie Brock // Venom, Lethal Protector')!r}")
    if edhrec_pool._slugify("Zinnia, Valley's Voice") != "zinnia-valleys-voice":
        problems.append("single-face slugs must be unchanged")
    return problems


def main() -> int:
    tests = [
        test_oracle_text_of_joins_faces,
        test_face_aware_consumers,
        test_dfc_commander_eligibility_and_grounding,
        test_synergy_gate_reverts_to_valid_deck,
        test_optimize_failure_reverts_not_raises,
        test_edhrec_slug_uses_front_face,
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

    print(f"OK: all {len(tests)} flow-audit fix checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
