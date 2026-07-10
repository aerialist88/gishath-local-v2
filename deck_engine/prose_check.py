"""
deck_engine/prose_check.py — final prose consistency pass (2026-07-10).

Run 9e430ab7 shipped a gameplan naming Villainous Wealth, Dream Harvest and
Displacer Kitten — none of which were in the final deck. Two ways prose goes
stale: (1) optimize anchors on the judge's build brief, which was written
against the PRE-repair draft, so it can name cards that were already cut
before optimize ever ran; (2) the deck keeps mutating AFTER optimize writes
the prose (synergy repair, budget pass), with nothing updating the text.

This module closes both holes at the last possible moment (run.py calls it
after the budget pass, right before export): a code-level scan finds real
card names mentioned in the prose that aren't in the final deck, and only if
any exist, ONE cheap model call rewrites the prose grounded in the final
decklist. Zero model spend on a clean run; cosmetic-tier failure handling
(never raises into the pipeline — a stale sentence is better than no deck).
"""
from __future__ import annotations

import re

from . import claude_cli, config

# Card names shorter than this are skipped by the scan: prose legitimately
# uses words like "Opt" or "Ponder" in ordinary sentences, and a false match
# costs a pointless (if cheap) rewrite call. Combined with the case-sensitive
# match below, 6+ chars keeps false positives rare while still catching every
# real multi-word card reference.
_MIN_NAME_LEN = 6

PROSE_FIX_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "final_summary": {"type": "string"},
        "early_game": {"type": "string"},
        "mid_game": {"type": "string"},
        "late_game": {"type": "string"},
        "changes_made": {"type": "string"},
    },
    "required": ["final_summary", "early_game", "mid_game", "late_game", "changes_made"],
}


def _prose_fields(deck) -> dict[str, str]:
    return {
        "final_summary": deck.final_summary or "",
        "early_game": deck.early_game or "",
        "mid_game": deck.mid_game or "",
        "late_game": deck.late_game or "",
        "changes_made": deck.changes_made or "",
    }


def _mid_sentence(text: str, pos: int) -> bool:
    """True if the character stream before `pos` continues a sentence (so a
    capitalized word there is a deliberate proper noun, not just sentence
    case). Start-of-text, newlines, and sentence punctuation mean False."""
    before = text[:pos].rstrip()
    return bool(before) and before[-1] not in ".!?|:\n"


def stale_names(deck_cards: list[str], commander: str, prose_texts: list[str], cache: dict) -> list[str]:
    """Real card names mentioned in the prose that are NOT in the final deck.

    Matching is case-sensitive against the printed name ("Counterspell" the
    card vs "counterspells" mid-sentence), requires word boundaries (the card
    "Displace" must not flag off a mention of Displacer Kitten, nor "Villain"
    off Villainous Wealth — both real false positives from run 9e430ab7's
    prose), and skips names that are substrings of any name actually in the
    deck (so "Glen Elendra" doesn't flag while Glen Elendra Archmage is
    played). Pure code — scanning the whole cache is a few tens of
    milliseconds, far cheaper than any model call."""
    text = "\n".join(t for t in prose_texts if t)
    if not text.strip():
        return []
    deck_keys = {str(c).strip().lower() for c in deck_cards}
    deck_keys.add(commander.strip().lower())

    flagged: set[str] = set()
    for key, card in cache.items():
        if key in deck_keys:
            continue
        printed = card.get("name") or ""
        if len(printed) < _MIN_NAME_LEN or printed not in text:
            continue  # fast substring pre-filter; the regex below is the real test
        if (card.get("legalities") or {}).get("commander") != "legal":
            continue  # tokens/art/playtest cards ("Snakes", "Villain") — prose can't be citing these
        matches = list(re.finditer(r"(?<!\w)" + re.escape(printed) + r"(?!\w)", text))
        if not matches:
            continue  # only matched inside a longer word — not a card reference
        if " " not in printed and not any(_mid_sentence(text, m.start()) for m in matches):
            # A single-word name that only ever appears sentence-initial is far
            # more likely ordinary English than a card reference ("Overwhelm the
            # table", "Execute the raid", "Lifelink from the same trigger" — all
            # real false positives from historical runs). Multi-word names are
            # unambiguous and skip this check.
            continue
        if any(printed.lower() in dk for dk in deck_keys):
            continue  # substring of a card that IS in the deck (or the commander)
        flagged.add(printed)

    # A flagged name wholly contained in a LONGER flagged name is almost always
    # the same prose mention counted twice ("Infestation" inside a "Blowfly
    # Infestation" reference) — keep only the longest match.
    return sorted(
        name for name in flagged
        if not any(other != name and name.lower() in other.lower() for other in flagged)
    )


def repair_prose(run_id: str, deck, stale: list[str], cache: dict) -> bool:
    """One cheap call rewriting the prose fields against the FINAL decklist.
    Mutates deck's prose fields in place on success. Returns True if the prose
    was updated; False (never raises) on any failure — prose is presentation,
    not deck content."""
    fields = _prose_fields(deck)
    prompt = (
        f"Commander: {deck.concept.commander}\n"
        f"Archetype: {deck.final_archetype}\n\n"
        f"The deck description below was written before some final card changes were applied, and it "
        f"references cards that are NOT in the finished deck: {', '.join(stale)}.\n\n"
        f"FINAL decklist (the only cards that exist — the commander, {deck.concept.commander}, "
        f"is separate):\n" + "\n".join(f"- {c}" for c in deck.cards) + "\n\n"
        f"Current text, per field:\n"
        + "\n".join(f"[{name}]\n{value}\n" for name, value in fields.items())
        + "\nRewrite each field so it describes ONLY this final deck: keep every sentence that is "
        f"still accurate as close to verbatim as possible, and rewrite just the parts that name or "
        f"rely on the missing cards — substitute the closest cards actually in the decklist above, "
        f"or drop the reference. Do not invent new strategy claims, and keep each field at roughly "
        f"its current length. In final_summary/early_game/mid_game/late_game, name only cards in the "
        f"decklist above (the commander is fine). changes_made is the exception: it narrates what was "
        f"cut or swapped, so it may keep naming removed cards — return it unchanged unless it "
        f"contradicts the decklist. Respond only via the provided JSON schema; no reasoning text needed."
    )
    try:
        result = claude_cli.run(
            prompt, run_id=run_id, stage="prose/consistency", model_tier_key="card_tagger",
            json_schema=PROSE_FIX_JSON_SCHEMA, disallowed_tools=config.DISALLOWED_SEARCH_TOOLS,
        )
        parsed = result.parsed_json()
    except claude_cli.ClaudeCLIError:
        return False

    updated = False
    for name in fields:
        new_value = str(parsed.get(name, "")).strip()
        if new_value:
            setattr(deck, name, new_value)
            updated = True
    return updated


def ensure_prose_matches_deck(run_id: str, deck, cache: dict) -> list[str]:
    """Entry point for run.py: scan, and repair only if something is stale.
    Returns the stale names found (empty list = prose was already clean).

    changes_made is deliberately NOT scanned: it narrates what was cut/swapped,
    so naming cards that are no longer in the deck is exactly what it's for
    (run 9e430ab7's changes_made correctly named Cyclonic Rift/Glissa
    Sunslayer/Seedborn Muse as cuts). It's still handed to the rewrite call for
    context when the OTHER fields are stale."""
    gameplan_fields = [deck.final_summary or "", deck.early_game or "",
                       deck.mid_game or "", deck.late_game or ""]
    stale = stale_names(deck.cards, deck.concept.commander, gameplan_fields, cache)
    if stale:
        repair_prose(run_id, deck, stale, cache)
    return stale
