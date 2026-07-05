"""
deck_engine/concept_selector.py — stage 1: pick tonight's commander/archetype.

PRD §5 step 1. Enforces the hard 30-day commander dedupe as a code-level
check, not just a prompt instruction — the prompt asks the model to avoid
blocked commanders, but LLMs are not reliable rule-followers against a long
exclusion list, so this module re-verifies against run_log in code and
retries (reinforcing the blocklist each time) rather than trusting
compliance. Also re-verifies the pick actually exists and is
commander-eligible against the Scryfall cache (§4e) before accepting it —
catches hallucinated or non-legendary picks before they reach the build stage.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import claude_cli, config, prompt_helpers, run_log, scryfall_cache, synergy_check

SELECT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "commander": {"type": "string"},
        "archetype": {
            "type": "string",
            "description": "short label, e.g. 'Group hug voltron' or 'Mono-black reanimator'",
        },
        "rationale": {
            "type": "string",
            "description": "1-2 sentences on why this is a novel/unorthodox pick for tonight",
        },
    },
    "required": ["commander", "archetype", "rationale"],
}


@dataclass
class ConceptChoice:
    commander: str
    archetype: str
    rationale: str
    color_identity: list[str]
    # Real printed ability text from the Scryfall cache — threaded through
    # every downstream prompt (ideate/build/optimize) so those stages reason
    # from ground truth instead of the model's memory of what the card does.
    # Added 2026-07-01 after a real run built a deck around a hallucinated
    # commander ability (see agent_pipeline.py docstring).
    oracle_text: str = ""
    # PRD v4 amendment §3.2 S3: 3-5 mechanic keywords extracted from oracle_text at
    # select-time (one cheap Haiku call), fed to the code-level synergy-density gate
    # in agent_pipeline.py. Empty list means the gate is a no-op for this run — a
    # missing token extraction should never block a night's deck.
    mechanic_tokens: list[str] = field(default_factory=list)


def _build_prompt(blocked_commanders: set[str], soft_avoid_archetypes: set[str]) -> str:
    return prompt_helpers.render(
        "select.md",
        dedupe_days=config.DEDUPE_COMMANDER_DAYS,
        blocked_list=", ".join(sorted(blocked_commanders)) or "(none yet)",
        soft_list=", ".join(sorted(soft_avoid_archetypes)) or "(none yet)",
        **prompt_helpers.bracket_rules_text(),
    )


def _forced_concept(run_id: str, forced_commander: str, cache: dict) -> ConceptChoice:
    """Atelier commission path: the user typed a specific commander, so the
    select stage skips picking (and skips the dedupe/price checks — an explicit
    user choice is honoured, not vetoed) and only asks the model to name the
    archetype/rationale for the fixed commander. Eligibility is still verified
    in code against the Scryfall cache — a typo'd or non-legendary name should
    fail loudly here, not surface as a hallucination mid-build."""
    key = forced_commander.strip().lower()
    card = cache.get(key)
    if card is None:
        raise claude_cli.ClaudeCLIError(
            f"select_concept: commander not found on Scryfall: {forced_commander!r} — check the spelling."
        )
    type_line = (card.get("type_line") or "").lower()
    oracle_text_lower = (card.get("oracle_text") or "").lower()
    if not (("legendary" in type_line and "creature" in type_line)
            or "can be your commander" in oracle_text_lower):
        raise claude_cli.ClaudeCLIError(
            f"select_concept: {card.get('name', forced_commander)} isn't commander-eligible "
            f"({card.get('type_line')})."
        )

    name = card.get("name", forced_commander)
    oracle_text = card.get("oracle_text") or ""
    rules = prompt_helpers.bracket_rules_text()
    prompt = (
        f"Tonight's EDH (Commander) deck commission has a FIXED commander, chosen by the user: {name}.\n"
        f"Its real printed rules text:\n{oracle_text or '(no rules text cached)'}\n\n"
        f"House rules: Commander Bracket {rules['bracket']}; game changers {rules['game_changers']}; "
        f"tutors {rules['tutors']}; two-card infinite combos {rules['combo_rule']}; "
        f"mass land destruction {rules['mld']}.\n\n"
        "Propose the most interesting archetype to build this commander around tonight — favour a "
        "novel, unorthodox line grounded in the commander's actual rules text over a solved staple "
        "shell. Return the commander name unchanged."
    )
    result = claude_cli.run(
        prompt, run_id=run_id, stage="select/commissioned",
        model_tier_key="select", json_schema=SELECT_JSON_SCHEMA,
    )
    choice = result.parsed_json()
    mechanic_tokens = synergy_check.extract_mechanic_tokens(run_id, name, oracle_text)
    return ConceptChoice(
        commander=name,
        archetype=str(choice.get("archetype", "")).strip(),
        rationale=str(choice.get("rationale", "")).strip(),
        color_identity=card.get("color_identity", []),
        oracle_text=oracle_text,
        mechanic_tokens=mechanic_tokens,
    )


def select_concept(
    run_id: str, cache: dict | None = None, max_attempts: int = 3,
    price_check=None, forced_commander: str | None = None,
) -> ConceptChoice:
    """Pick tonight's commander/archetype. Raises ClaudeCLIError if no valid pick lands in max_attempts.

    price_check (PRD v4 amendment §3.4): optional callable(commander_name) -> float | None,
    normally budget_pass.commander_price_sgd wired in by run.py. An over-cap commander is
    re-picked HERE — it can't be swapped after the deck is built around it. None (pricing
    unavailable / no hits) skips the check, fail-soft: a pricing hiccup must never block
    the night's deck, and budget_pass.enforce_card_cap() re-checks the commander later and
    flags it if it turns out over-cap.

    forced_commander (Atelier): a user-typed commander name — skips the model's
    pick and the dedupe/price vetoes entirely; see _forced_concept()."""
    cache = cache if cache is not None else scryfall_cache.load_cache()
    if forced_commander and forced_commander.strip():
        return _forced_concept(run_id, forced_commander, cache)
    blocked = set(run_log.recent_commanders())
    soft_avoid = run_log.recent_archetypes()

    last_error = "no attempts made"
    for attempt in range(1, max_attempts + 1):
        prompt = _build_prompt(blocked, soft_avoid)
        result = claude_cli.run(
            prompt, run_id=run_id, stage=f"select/{attempt}",
            model_tier_key="select", json_schema=SELECT_JSON_SCHEMA,
        )
        choice = result.parsed_json()
        commander = str(choice.get("commander", "")).strip()
        archetype = str(choice.get("archetype", "")).strip()
        rationale = str(choice.get("rationale", "")).strip()
        key = commander.lower()

        if key in blocked:
            last_error = f"model repeated a blocked commander: {commander}"
            blocked.add(key)  # reinforce for the retry, in case the model tries again
            continue

        card = cache.get(key)
        if card is None:
            last_error = f"commander not found on Scryfall (likely hallucinated): {commander}"
            continue

        type_line = (card.get("type_line") or "").lower()
        oracle_text = (card.get("oracle_text") or "").lower()
        is_legendary_creature = "legendary" in type_line and "creature" in type_line
        can_be_commander = "can be your commander" in oracle_text
        if not (is_legendary_creature or can_be_commander):
            last_error = f"card isn't commander-eligible: {commander} ({card.get('type_line')})"
            continue

        # §3.4 commander price cap — checked BEFORE the (costlier) mechanic-token
        # extraction so a rejected pick doesn't pay for a Haiku call it won't use.
        if price_check is not None:
            price = price_check(card.get("name", commander))
            if price is not None and price > config.MAX_CARD_PRICE_SGD:
                last_error = (
                    f"commander over the per-card cap: {commander} prices at SGD {price:.2f} "
                    f"(cap SGD {config.MAX_CARD_PRICE_SGD:.0f})"
                )
                blocked.add(key)  # don't let the retry pick the same expensive card again
                continue

        oracle_text = card.get("oracle_text") or ""
        # PRD v4 amendment S3, resolved open question #3: mechanic tokens extracted
        # HERE, at select-time, once per run — not re-derived per ideation angle or
        # per build attempt. One cheap Haiku call; [] (gate no-op) on any failure.
        mechanic_tokens = synergy_check.extract_mechanic_tokens(run_id, card.get("name", commander), oracle_text)

        return ConceptChoice(
            commander=card.get("name", commander),
            archetype=archetype,
            rationale=rationale,
            color_identity=card.get("color_identity", []),
            oracle_text=oracle_text,
            mechanic_tokens=mechanic_tokens,
        )

    raise claude_cli.ClaudeCLIError(
        f"select_concept: no valid pick after {max_attempts} attempts. Last error: {last_error}"
    )
