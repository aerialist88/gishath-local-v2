"""
deck_engine/card_tagger.py — role/phase tags for the breakdown sheet, off Opus.

PRD v4 amendment §3.1 T4: the Opus optimize call previously ALSO returned
`card_tags` for all 99 cards every attempt — regurgitating a role/phase label
per card, for every card, on the single most expensive model tier, purely for
xlsx presentation metadata ("Tags are xlsx presentation metadata only —
mislabels are cosmetic," per the PRD, i.e. this was never the right place to
spend Opus tokens).

New approach: cheap code heuristics against the local Scryfall cache handle
the easy/obvious cases (lands, ramp, removal, draw, board wipes — pattern-
matched off oracle_text/type_line), and a single Haiku call handles whatever
heuristics can't confidently classify. Net effect: same tags on the
breakdown sheet, a fraction of the tokens, off the expensive model entirely.

Deliberately over-inclusive/crude keyword patterns below — a mistagged card
is purely cosmetic (per the PRD framing above), so false positives here are
cheap; anything ambiguous just falls through to the Haiku pass rather than
needing perfect coverage.
"""
from __future__ import annotations

from . import claude_cli, config

_RAMP_PATTERNS = (
    "search your library for a land", "search your library for a basic land",
    "search your library for a forest", "search your library for a plains",
    "search your library for a swamp", "search your library for an island",
    "search your library for a mountain", "search your library for up to",
    "basic land card", "add one mana", "add two mana",
    "add three mana", "add mana of any", "add {", "additional land",
)
_REMOVAL_PATTERNS = (
    "destroy target", "exile target", "deals damage to target creature",
    "deals damage to any target", "-x/-x", "sacrifice a creature",
    "return target creature", "counter target spell", "target creature gets -",
)
_WIPE_PATTERNS = (
    "destroy all creatures", "each creature gets -", "exile all creatures",
    "destroy all other creatures", "each player sacrifices",
)
_DRAW_PATTERNS = (
    "draw a card", "draw two cards", "draw cards", "draw an additional card",
    "draw a card for each",
)

# The single role vocabulary every tagging path must draw from — heuristics,
# the Haiku fallback, and the budget pass's swap `role` field alike. Free-text
# roles fragment the Stats sheet's role counts (run 9e430ab7 shipped
# "Land/Mana base (UG dual)" / "(creature-count ramp)" variants from the
# budget-swap model); a shared enum in every schema makes that impossible.
CANONICAL_ROLES: list[str] = [
    "Land/Mana base", "Ramp", "Card draw", "Removal", "Board wipe",
    "Interaction", "Protection", "Tutor", "Synergy piece", "Card advantage",
    "Win condition",
]

CARD_TAG_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string", "enum": CANONICAL_ROLES},
                    "phase": {"type": "string", "enum": ["early", "mid", "late"]},
                },
                "required": ["name", "role", "phase"],
            },
        },
    },
    "required": ["tags"],
}


def _heuristic_tag(card: dict) -> tuple[str, str] | None:
    """Returns (role, phase) from cheap code heuristics, or None if this card
    should fall through to the Haiku pass instead (nothing matched confidently)."""
    type_line = (card.get("type_line") or "").lower()
    oracle = (card.get("oracle_text") or "").lower()

    if "land" in type_line:
        return "Land/Mana base", "early"
    if any(p in oracle for p in _WIPE_PATTERNS):
        return "Board wipe", "mid"
    if any(p in oracle for p in _RAMP_PATTERNS):
        return "Ramp", "early"
    if any(p in oracle for p in _REMOVAL_PATTERNS):
        return "Removal", "mid"
    if any(p in oracle for p in _DRAW_PATTERNS):
        return "Card draw", "mid"
    return None


def _haiku_tag_remainder(run_id: str, commander: str, names: list[str], cache: dict) -> dict[str, dict]:
    """One Haiku call for every card the heuristics above couldn't confidently tag —
    never one call per card, never Opus. Falls back to blank tags (never raises) if
    the call fails; a missing tag is cosmetic, not worth failing a run over."""
    if not names:
        return {}

    from . import agent_pipeline  # local import: avoids a circular import at module load time

    oracle_block = agent_pipeline._oracle_text_block(cache, names)  # noqa: SLF001 — same-package helper
    role_choices = ", ".join(f'"{r}"' for r in CANONICAL_ROLES)
    prompt = (
        f"Commander: {commander}\n\n"
        f"For each of the following {len(names)} cards, give a role label — choose the closest fit "
        f"from exactly this list: {role_choices} — and the game "
        f"phase it's mainly relevant in (\"early\", \"mid\", or \"late\"). Base this on the real oracle "
        f"text below where available; use your best judgement from the name alone for anything not "
        f"listed.\n\nCards:\n" + "\n".join(f"- {n}" for n in names) +
        f"\n\nReal oracle text on file for these cards (not all will be listed):\n{oracle_block}\n\n"
        f"Respond only via the provided JSON schema — one entry per card, same exact spelling as listed "
        f"above. Keep any reasoning brief; card names belong in the structured output only."
    )
    try:
        result = claude_cli.run(
            prompt, run_id=run_id, stage="card_tagger/haiku", model_tier_key="card_tagger",
            json_schema=CARD_TAG_JSON_SCHEMA, disallowed_tools=config.DISALLOWED_SEARCH_TOOLS,
        )
        parsed = result.parsed_json()
    except claude_cli.ClaudeCLIError:
        return {}

    return {
        str(t.get("name", "")).strip().lower(): {"role": t.get("role", ""), "phase": t.get("phase", "")}
        for t in parsed.get("tags", [])
    }


def tag_cards(run_id: str, commander: str, cards: list[str], cache: dict) -> dict[str, dict]:
    """Returns {lower_name: {"role": ..., "phase": ...}} for `commander` + every
    card in `cards`. Heuristics handle lands/ramp/removal/draw/wipes directly from
    the Scryfall cache (no model call); whatever's left ambiguous goes to ONE
    Haiku call for the whole remainder. Caller (agent_pipeline.run_pipeline)
    overrides the commander's own entry to "Commander" afterward — this function
    tags it like any other card, which is usually wrong for a commander."""
    all_names = [commander] + list(cards)
    tags: dict[str, dict] = {}
    ambiguous: list[str] = []

    for name in all_names:
        key = name.strip().lower()
        if key in tags:
            continue  # duplicate name (e.g. a repeated basic land) — tag once, reuse for all copies
        card = cache.get(key)
        if card is None:
            ambiguous.append(name)  # not in the cache — let Haiku take a best-effort guess from the name
            continue
        heuristic = _heuristic_tag(card)
        if heuristic is None:
            ambiguous.append(name)
        else:
            role, phase = heuristic
            tags[key] = {"role": role, "phase": phase}

    if ambiguous:
        tags.update(_haiku_tag_remainder(run_id, commander, ambiguous, cache))

    return tags


def retag_untagged(run_id: str, commander: str, cards: list[str], tags: dict[str, dict], cache: dict) -> None:
    """Fill in role/phase for any card in `cards` that has no role in `tags`,
    mutating `tags` in place. Run after any post-tagging deck mutation (the
    budget pass's repair loop adds cards the original tag_cards() never saw —
    run 9e430ab7 shipped Morphic Pool/Sunken Hollow untagged this way).
    Heuristics first (free), one Haiku call only if anything is left ambiguous;
    never raises — a missing tag is cosmetic."""
    ambiguous: list[str] = []
    for name in cards:
        key = name.strip().lower()
        if (tags.get(key) or {}).get("role", "").strip():
            continue
        card = cache.get(key)
        heuristic = _heuristic_tag(card) if card is not None else None
        if heuristic is not None:
            role, phase = heuristic
            tags[key] = {"role": role, "phase": phase}
        else:
            ambiguous.append(name)
    if ambiguous:
        tags.update(_haiku_tag_remainder(run_id, commander, ambiguous, cache))
