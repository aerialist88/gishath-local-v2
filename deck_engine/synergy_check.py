"""
deck_engine/synergy_check.py — S3 (PRD v4 amendment §3.2): code-level synergy-density gate.

A deliberately crude, cheap backstop against the "goodstuff pile in the right
colors with the commander attached" failure mode — implemented structurally,
in code, rather than trusted to a prompt instruction alone (three rounds of
prompt-only patches had already underdelivered on this per the PRD's problem
statement). This gate never edits the deck itself: on failure it routes back
through a repair-style model pass (prompts/synergy_repair.md) that's told
which cards read as generic filler — same division of labour as the
Scryfall validate/repair loop (a gate decides pass/fail; a separate model
call does the actual editing).

Threshold (config.SYNERGY_GATE_THRESHOLD, default 25 nonland matches) is
deliberately LOW — this tolerates real synergy expressed in vocabulary the
keyword match doesn't catch (false negatives are fine; it's a backstop, not
the primary quality mechanism — that's S1's EDHREC pool + the build/optimize
prompt instructions). It exists to catch egregious goodstuff piles, not to
adjudicate borderline cases.
"""
from __future__ import annotations

from . import claude_cli, config, scryfall_cache

MECHANIC_TOKEN_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "tokens": {
            "type": "array",
            "description": "3-5 short keywords/phrases that a card's oracle text or type line would "
                           "contain if it meaningfully interacts with this commander's specific "
                           "mechanic (not generic goodstuff terms like 'draw a card' or 'destroy target "
                           "creature'). Lowercase, single words or short phrases exactly as they'd "
                           "appear in real Magic card text, e.g. ['proliferate', 'counter on', "
                           "'+1/+1 counter'].",
            "items": {"type": "string"},
        },
    },
    "required": ["tokens"],
}


def extract_mechanic_tokens(run_id: str, commander: str, oracle_text: str) -> list[str]:
    """One cheap Haiku call (at select-time, per the PRD's resolved open question #3)
    naming 3-5 mechanic tokens from the commander's real oracle text. Returns []
    (gate becomes a no-op — never blocks a run) on any failure."""
    if not oracle_text.strip():
        return []
    prompt = (
        f"Commander: {commander}\n\n"
        f"Real printed oracle text (verified against Scryfall):\n\"\"\"\n{oracle_text}\n\"\"\"\n\n"
        f"Name 3-5 short keywords/phrases (lowercase, as they'd appear in real Magic card text) that "
        f"identify this commander's SPECIFIC mechanic — not generic goodstuff terms every deck has. "
        f"Respond only via the provided JSON schema."
    )
    try:
        result = claude_cli.run(
            prompt, run_id=run_id, stage="select/mechanic-tokens", model_tier_key="card_tagger",
            json_schema=MECHANIC_TOKEN_JSON_SCHEMA, disallowed_tools=config.DISALLOWED_SEARCH_TOOLS,
        )
        tokens = result.parsed_json().get("tokens", [])
        return [str(t).strip().lower() for t in tokens if str(t).strip()]
    except claude_cli.ClaudeCLIError:
        return []


def count_synergy_matches(cards: list[str], tokens: list[str], cache: dict) -> tuple[int, list[str]]:
    """Counts how many NONLAND cards in `cards` have oracle_text/type_line matching
    at least one mechanic token. Returns (match_count, generic_card_names) —
    generic_card_names lists every nonland card matching none of the tokens, fed to
    the repair prompt as swap candidates."""
    if not tokens:
        return len(cards), []  # no tokens extracted -> gate can't meaningfully fire; treat as a pass

    match_count = 0
    generic: list[str] = []
    for name in cards:
        card = cache.get(name.strip().lower())
        if card is None:
            continue  # unknown card (shouldn't happen post-validation) — don't count either way
        type_line = (card.get("type_line") or "").lower()
        if "land" in type_line:
            continue  # lands are exempt from the synergy count entirely
        # oracle_text_of, not the top-level field: MDFC/transform/adventure text
        # lives in card_faces, and an empty haystack read every flip-Saga and
        # modal card as generic filler (2026-07-11 audit — the Satsuki
        # Saga-recursion deck's own theme pieces counted as off-mechanic).
        haystack = f"{type_line} {scryfall_cache.oracle_text_of(card).lower()}"
        if any(tok in haystack for tok in tokens):
            match_count += 1
        else:
            generic.append(name)
    return match_count, generic


def gate_passes(
    cards: list[str], tokens: list[str], cache: dict, threshold: int | None = None,
) -> tuple[bool, int, list[str]]:
    """Returns (passes, match_count, generic_card_names). threshold defaults to
    config.SYNERGY_GATE_THRESHOLD (resolved at call time, not import time, so tests
    can override config.SYNERGY_GATE_THRESHOLD directly). No tokens extracted ->
    always passes (the gate can't meaningfully fire without tokens to match
    against — must never block a run over a failed/empty extraction), regardless
    of how that compares to the threshold."""
    if not tokens:
        return True, len(cards), []
    if threshold is None:
        threshold = config.SYNERGY_GATE_THRESHOLD
    match_count, generic = count_synergy_matches(cards, tokens, cache)
    return match_count >= threshold, match_count, generic
