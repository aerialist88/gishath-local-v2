"""
deck_engine/agent_pipeline.py — stages 2-5 of the nightly pipeline (PRD §5):
draft (×N parallel, each builds a complete deck) -> judge (picks the winner,
cherry-picks from the losers) -> validate/repair -> optimize -> re-validate
-> synergy gate/repair.

WIDEN-BACK-OUT (2026-07-06, Trevor's call): the original PRD vision of three
simultaneous agents — previously compressed to 3 quick ideate calls feeding a
single synthesize->build track — is restored as the draft stage: each of the
3 agents commits to an angle AND drafts the full 99 cards in one long call,
so the Atelier benches show three deckwrights genuinely working in parallel
for most of the run. Synthesize's pick/merge role became the judge stage
(full-deck comparison + surgical cherry-pick swaps applied in code). Roughly
cost-neutral vs the old shape: 3 big sonnet drafts replace 3 small ideates +
1 big build. Two behaviour changes to know about: web search is now blocked
at the angle-exploration stage (it rides inside the build-shaped draft call —
T5 policy — where the old standalone ideate had it enabled), and synthesize's
S2 role-quota adjustment is retired (drafts get the config defaults; the
prompt lets them argue for deviating).

Concept selection (stage 1) lives in concept_selector.py; pricing/export/
delivery (stages 6-9) live in pricing.py / export.py / emailer.py. This
module owns everything that touches Scryfall-validated decklist content.

Every `claude -p` call in here is JSON-schema-constrained (see the *_JSON_SCHEMA
dicts below) so stage output is structured data, not free text that needs
fragile parsing. Not yet exercised against a real authenticated `claude`
session (see claude_cli.py docstring) — verify the full pipeline once on
Trevor's Mac before relying on it unattended.

ORACLE-TEXT GROUNDING (added 2026-07-01, after a real run built a deck
around a hallucinated commander ability): every stage that reasons about
what a card does is handed that card's REAL printed oracle_text from the
Scryfall cache, not just its name — the model reasons from ground truth
instead of its training-data memory of the card, which can be wrong. The
`optimize` stage additionally does an explicit fact-check pass (job 1,
before any card review) comparing the build brief's claimed synergies
against that real text; if it finds the core gameplan was built on a false
premise, `run_pipeline()` retries the whole draft->optimize loop once
(config.MAX_STRATEGY_RETRIES) with a note about what went wrong, rather than
patching individual cards (a false premise isn't a card-level bug).

PRD v4 AMENDMENT (2026-07-03) — token diet + synergy grounding:
  - T2: build/optimize prompts now cap free-text reasoning at ~150 words and
    forbid enumerating the decklist there — card names appear only in the
    structured JSON output, never written out twice per call. Prompt-only
    change (prompts/build.md, prompts/optimize.md); nothing to do in this file.
  - T3: optimize now returns `swaps` (targeted deltas), not the full 99-card
    list — see _apply_swaps() below, called from _run_one_attempt().
  - T4: `card_tags` no longer comes from optimize's Opus response — see
    card_tagger.tag_cards(), called from run_pipeline() after final_cards
    settle. _oracle_text_block() is reused by card_tagger's Haiku fallback.
  - T5: build/validate_repair/optimize/card_tagger calls pass
    config.DISALLOWED_SEARCH_TOOLS; select/ideate/synthesize stay
    unrestricted (see config.py's DISALLOWED_SEARCH_TOOLS comment for why
    synthesize isn't blocked despite touching the same "ideate" model tier).
  - T6: optional --resume session chaining behind config.RESUME_SESSION_CHAINING
    (default off) — synthesize -> build -> optimize can share a session for an
    instrumented A/B experiment Trevor runs later; ideate's 3 parallel calls
    are deliberately left independent/unchained.
  - S1/S2: build prompt now receives an EDHREC candidate pool (edhrec_pool.py)
    and role-quota ranges from synthesize (see SYNTHESIZE_JSON_SCHEMA).
  - S3: after the optimize-repair loop, a code-level synergy-density gate
    (synergy_check.py) runs; on failure it routes through one more repair-style
    model pass (prompts/synergy_repair.md) rather than editing the deck itself.
"""
from __future__ import annotations

import concurrent.futures
import sys
import time
from dataclasses import dataclass, field

from . import card_tagger, claude_cli, config, edhrec_pool, prompt_helpers, scryfall_cache, synergy_check
from .concept_selector import ConceptChoice
from .scryfall_cache import ValidationResult


def _log(msg: str) -> None:
    """Stage-progress print — cheap observability so a slow stage doesn't look
    identical to a hung one. Skipped when a live_view is active (deck_engine/run.py
    sets one via claude_cli.set_live_view()) since the terminal live view already
    shows per-call progress more richly — this is the fallback for a plain run."""
    if claude_cli.has_live_view():
        return
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _oracle_text_block(cache: dict, names: list[str]) -> str:
    """Formats real Scryfall oracle text for a list of card names, one per line.
    Silently skips names not found in the cache (they may be a theme/type phrase
    rather than an exact card name — best-effort grounding, not a hard requirement).
    Reused by card_tagger.py's Haiku fallback (imported lazily there to avoid a
    circular import at module load time)."""
    lines = []
    for name in names:
        card = cache.get(str(name).strip().lower())
        if card is None:
            continue
        oracle_text = (card.get("oracle_text") or "").strip() or "(no rules text — likely a land or vanilla creature)"
        lines.append(f'- {card.get("name", name)}: "{oracle_text}"')
    return "\n".join(lines) if lines else "(none of the named key cards were found in the Scryfall cache)"


def _role_quota_block(quotas: dict) -> str:
    q = {**config.ROLE_QUOTA_DEFAULTS, **(quotas or {})}
    return (
        f"- Lands: {q['land_min']}-{q['land_max']}\n"
        f"- Ramp: {q['ramp_min']}-{q['ramp_max']}\n"
        f"- Card draw: {q['draw_min']}-{q['draw_max']}\n"
        f"- Interaction/removal: {q['interaction_min']}-{q['interaction_max']}\n"
        f"- Board wipes: {q['wipes_min']}-{q['wipes_max']}\n"
        f"- On-mechanic (min): {q['on_mechanic_min']}"
    )


DRAFT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "angle_name": {"type": "string"},
        "gameplan_summary": {"type": "string"},
        "key_cards": {
            "type": "array",
            "items": {"type": "string"},
            "description": "5-10 exact, real card names this draft's gameplan depends on — the judge "
                           "gets their real oracle text as ground truth.",
        },
        "cards": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The complete decklist (commander excluded), exact printed names.",
        },
    },
    "required": ["angle_name", "gameplan_summary", "key_cards", "cards"],
}

SWAP_JSON_SCHEMA_ITEM = {
    "type": "object",
    "properties": {
        "remove": {"type": "string"},
        "add": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["remove", "add", "reason"],
}

# Synergy repair returns targeted swaps applied in code (_apply_swaps) — the
# same T3 swap-delta principle optimize follows. Until 2026-07-10 this stage
# regurgitated the complete decklist (DECKLIST_JSON_SCHEMA), which cost
# near-draft output tokens per firing and contradicted the pipeline's own rule.
SYNERGY_REPAIR_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "swaps": {
            "type": "array",
            "description": "Targeted replacements only — generic card out, on-mechanic card in.",
            "items": SWAP_JSON_SCHEMA_ITEM,
        },
    },
    "required": ["swaps"],
}

# Validate-repair, as deltas (2026-07-10). This was the last full-regurgitation
# stage (DECKLIST_JSON_SCHEMA) — in run 81f2b542 three repair regurgitations
# quietly rewrote the mana base from ~36 lands down to 23 while "fixing" a card
# count. Swaps alone can't fix a wrong count, so repair also gets cuts/adds.
REPAIR_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "swaps": {
            "type": "array",
            "description": "1-for-1 replacements for illegal/hallucinated/off-color cards.",
            "items": SWAP_JSON_SCHEMA_ITEM,
        },
        "cuts": {
            "type": "array",
            "description": "Exact card names to remove (one entry removes one copy) — for fixing an over-count.",
            "items": {"type": "string"},
        },
        "adds": {
            "type": "array",
            "description": "Exact real card names to add — for fixing an under-count or a short mana base.",
            "items": {"type": "string"},
        },
    },
    "required": ["swaps", "cuts", "adds"],
}

JUDGE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "chosen_draft": {
            "type": "integer",
            "description": "1-based number of the winning draft.",
        },
        "build_brief": {
            "type": "string",
            "description": "The winning deck's gameplan brief — the reference document the optimize "
                           "stage later fact-checks the deck against.",
        },
        "key_cards": {
            "type": "array",
            "items": {"type": "string"},
            "description": "5-10 exact, real card names the winning gameplan depends on — used to pull "
                           "real oracle text as ground truth for every later stage.",
        },
        "swaps": {
            "type": "array",
            "description": "Surgical cherry-picks applied to the WINNING draft's list (typically cards "
                           "borrowed from losing drafts). Empty list if the winner is already coherent.",
            "items": SWAP_JSON_SCHEMA_ITEM,
        },
    },
    "required": ["chosen_draft", "build_brief", "key_cards", "swaps"],
}

OPTIMIZE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "strategy_valid": {
            "type": "boolean",
            "description": "false if the fact-check found the core gameplan depends on an ability the "
                           "real card text doesn't support.",
        },
        "strategy_problem": {"type": "string"},
        "final_archetype": {
            "type": "string",
            "description": "Corrected (or confirmed-accurate) short archetype label — ships to Trevor "
                           "verbatim in the email/spreadsheet, replacing the original select-stage guess "
                           "if the fact-check found it described something the commander doesn't actually "
                           "do. Added 2026-07-01: the deck itself was already grounded/fact-checked, but "
                           "the displayed archetype label wasn't, so a wrong label could ship next to a "
                           "correctly-built deck (real incident: 'Izzet dice/spellslinger' label shipped "
                           "alongside a deck correctly built around offspring/power-1 synergies instead).",
        },
        "final_summary": {
            "type": "string",
            "description": "Corrected (or confirmed-accurate) 1-3 sentence description of what THIS deck "
                           "actually does, grounded in the real oracle text above — same reasoning as "
                           "final_archetype.",
        },
        # PRD v4 amendment T3: targeted swap deltas, not the full decklist — applied
        # and re-validated in code (_apply_swaps() below), never trusted as-is.
        "swaps": {
            "type": "array",
            "description": "Targeted changes only. Empty list if the deck needs no changes. Ignored if "
                           "strategy_valid is false.",
            "items": SWAP_JSON_SCHEMA_ITEM,
        },
        "changes_made": {"type": "string"},
        "early_game": {"type": "string"},
        "mid_game": {"type": "string"},
        "late_game": {"type": "string"},
    },
    "required": ["strategy_valid", "strategy_problem", "final_archetype", "final_summary", "swaps",
                 "changes_made", "early_game", "mid_game", "late_game"],
}


def _apply_swaps(cards: list[str], swaps: list[dict], strict: bool = False) -> tuple[list[str], list[str]]:
    """PRD v4 amendment T3: applies optimize's swap deltas to the existing decklist
    in code, rather than trusting a regurgitated full list. Returns (new_cards,
    warnings) — a `remove` name not found in the current list is a warning, not a
    hard failure. When strict=False (optimize/synergy), the add still happens without
    a removal; the caller re-validates via the normal Scryfall repair loop, which
    catches a resulting wrong card count. When strict=True (judge cherry-picks), the
    whole swap is skipped instead: run 81f2b542 had a judge response whose `remove`
    targets mostly missed, and the lenient path appended ~26 cards, ballooning the
    deck to 125 and sending it through count-fixing repairs that mangled the mana
    base. A dropped cherry-pick is a far smaller loss than that."""
    warnings: list[str] = []
    result = list(cards)
    lower_map = {c.strip().lower(): c for c in result}

    for swap in swaps:
        remove_name = str(swap.get("remove", "")).strip()
        add_name = str(swap.get("add", "")).strip()
        if not remove_name or not add_name:
            warnings.append(f"skipped malformed swap entry: {swap!r}")
            continue

        key = remove_name.lower()
        if key not in lower_map:
            if strict:
                warnings.append(
                    f"swap asked to remove {remove_name!r}, which isn't in the current decklist "
                    f"— skipping the swap entirely (strict mode), {add_name!r} not added."
                )
                continue
            warnings.append(
                f"optimize asked to remove {remove_name!r}, which isn't in the current decklist "
                f"(possibly already swapped, or a typo) — adding {add_name!r} anyway without a removal."
            )
            result.append(add_name)
            lower_map[add_name.strip().lower()] = add_name
            continue

        actual_name = lower_map.pop(key)
        idx = result.index(actual_name)
        result[idx] = add_name
        lower_map[add_name.strip().lower()] = add_name

    return result, warnings


def _apply_repair_deltas(cards: list[str], parsed: dict) -> tuple[list[str], list[str]]:
    """Applies a REPAIR_JSON_SCHEMA response (swaps + cuts + adds) in code.
    Swaps run strict — a repair targeting a card that isn't there is a model
    error, and blindly appending is exactly the count-inflation failure this
    schema replaces. Cuts remove one copy each (so 'cut the duplicate
    Necroskitter' works on a list with two); unknown cut targets are warned and
    ignored. Adds are appended as-is — the caller re-validates, which catches a
    hallucinated add."""
    current, warnings = _apply_swaps(cards, parsed.get("swaps", []), strict=True)

    for raw_name in parsed.get("cuts", []):
        name = str(raw_name).strip()
        key = name.lower()
        idx = next((i for i, c in enumerate(current) if c.strip().lower() == key), None)
        if idx is None:
            warnings.append(f"repair asked to cut {name!r}, which isn't in the current decklist — ignored.")
        else:
            current.pop(idx)

    for raw_name in parsed.get("adds", []):
        name = str(raw_name).strip()
        if name:
            current.append(name)

    return current, warnings


@dataclass
class DeckResult:
    concept: ConceptChoice
    cards: list[str]                 # DECK_SIZE - 1 names, commander excluded
    validation: ValidationResult
    changes_made: str
    early_game: str
    mid_game: str
    late_game: str
    card_tags: dict[str, dict] = field(default_factory=dict)   # lower-cased name -> {role, phase}
    ideation_angles: list[dict] = field(default_factory=list)  # kept for the email/debug trail
    retried: bool = False            # True if the strategy fact-check failed once and we retried
    retry_reason: str = ""           # what optimize's fact-check flagged, if retried
    # Fact-checked, final display text — added 2026-07-01 after a real run shipped
    # concept.archetype ("Izzet dice/spellslinger") verbatim in the email/xlsx even
    # though optimize's own fact-check had determined that label was wrong and the
    # deck was actually built around the commander's real offspring/power-1 text.
    # concept.archetype/rationale are the SELECT-stage's unverified first guess;
    # these are optimize's corrected (or confirmed) version and are what export.py/
    # emailer.py should display — never concept.archetype/rationale directly.
    final_archetype: str = ""
    final_summary: str = ""
    # PRD v4 amendment diagnostics (§2 success criteria / §3.2 S1 fallback note):
    swap_warnings: list[str] = field(default_factory=list)      # T3 swap-application anomalies, if any
    edhrec_pool_used: bool = False                               # S1 — False means v3 no-pool fallback fired
    synergy_gate_fired: bool = False                             # S3 — for tracking the "<1 in 5 runs" target


def _draft(
    run_id: str, concept: ConceptChoice, attempt: int, edhrec_pool_text: str, retry_note: str = "",
) -> list[dict]:
    """Spawn DRAFT_SUBAGENTS parallel drafting agents (the 2026-07-06 widen-back-out:
    each agent commits to one angle AND builds a complete decklist in a single long
    call, so all three benches visibly work simultaneously for the bulk of the run —
    replacing the old quick ideate×3 → synthesize → single-build chain). Deliberately
    NOT session-chained even when config.RESUME_SESSION_CHAINING is on — these are
    independent parallel explorations by design.

    Search is disallowed here: this call is build-shaped (T5 — grounding comes from
    the oracle text + EDHREC pool), and 3 parallel search-enabled builds could blow
    the crucible cap on a bad night. This retires the old ideate stage's
    search-enabled policy along with the stage itself."""
    n = config.DRAFT_SUBAGENTS
    color_identity = ", ".join(concept.color_identity) or "colorless"
    quotas = dict(config.ROLE_QUOTA_DEFAULTS)

    _log(f"draft: spawning {n} parallel deck drafts (model tier: {config.MODEL_TIERS['draft']})...")

    def _one(i: int) -> dict:
        prompt = prompt_helpers.render(
            "draft.md",
            commander=concept.commander, color_identity=color_identity,
            archetype=concept.archetype, rationale=concept.rationale,
            commander_oracle_text=concept.oracle_text or "(no oracle text on file for this card)",
            angle_index=i + 1, angle_total=n, retry_note=retry_note,
            deck_size_minus_1=config.DECK_SIZE - 1,
            role_quota_block=_role_quota_block(quotas),
            on_mechanic_min=quotas["on_mechanic_min"],
            edhrec_pool_block=edhrec_pool_text,
            **prompt_helpers.bracket_rules_text(),
        )
        t0 = time.monotonic()
        result = claude_cli.run(
            prompt, run_id=run_id, stage=f"draft/attempt{attempt}/{i + 1}",
            model_tier_key="draft", json_schema=DRAFT_JSON_SCHEMA,
            disallowed_tools=config.DISALLOWED_SEARCH_TOOLS,
        )
        parsed = result.parsed_json()
        _log(f"draft {i + 1}/{n} done in {time.monotonic() - t0:.0f}s — "
             f"angle: {parsed.get('angle_name', '?')}, {len(parsed.get('cards', []))} cards")
        return parsed

    # Subprocess calls are I/O-bound (waiting on the API), so a thread pool
    # is enough — no need for asyncio here. Each future is resolved
    # individually (not via pool.map(), whose iterator raises on the first
    # exception and discards any already-successful results) so one drafter
    # failing doesn't waste the cost already spent by its siblings — the
    # judge just works with whoever survived.
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(_one, i) for i in range(n)]
        results = []
        for i, future in enumerate(futures):
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 — one drafter's failure shouldn't sink the others
                _log(f"draft {i + 1}/{n} failed: {exc} — continuing with the survivors")
    if not results:
        raise RuntimeError(f"all {n} parallel drafts failed — nothing for the judge to compare")
    if len(results) < n:
        _log(f"draft: {n - len(results)}/{n} drafter(s) failed, proceeding with {len(results)} surviving draft(s)")
    return results


def _judge(
    run_id: str, concept: ConceptChoice, drafts: list[dict], cache: dict, attempt: int,
) -> tuple[int, list[str], str, list[str], list[str], str]:
    """Judge the parallel drafts: pick the winning deck, cherry-pick strict upgrades
    from the losers (applied in code via _apply_swaps, same trust model as optimize's
    T3 swaps), and write the build brief + key_cards that every later stage works
    from. Replaces the old synthesize stage. Returns (chosen_index, winning_cards,
    build_brief, key_cards, swap_warnings, session_id) — session_id is only used if
    config.RESUME_SESSION_CHAINING is on (T6 scaffolding)."""
    tokens = concept.mechanic_tokens
    blocks = []
    for i, d in enumerate(drafts):
        cards = d.get("cards", [])
        # Code-computed on-mechanic signal per draft — same counter the S3 synergy
        # gate uses later, handed to the judge as evidence rather than asking it
        # to eyeball 3×99 names for mechanic overlap.
        _, match_count, _ = synergy_check.gate_passes(cards, tokens, cache)
        blocks.append(
            f"Draft {i + 1} — {d.get('angle_name', '?')}\n"
            f"Gameplan: {d.get('gameplan_summary', '')}\n"
            f"On-mechanic count (code-computed): {match_count} of {len(cards)} cards\n"
            f"Key cards' real printed text:\n{_oracle_text_block(cache, d.get('key_cards', []))}\n"
            f"Decklist ({len(cards)} cards):\n" + "\n".join(f"- {c}" for c in cards)
        )
    prompt = prompt_helpers.render(
        "judge.md",
        commander=concept.commander, color_identity=", ".join(concept.color_identity) or "colorless",
        archetype=concept.archetype,
        commander_oracle_text=concept.oracle_text or "(no oracle text on file for this card)",
        draft_total=len(drafts), drafts_block="\n\n".join(blocks),
    )
    _log(f"judge: comparing {len(drafts)} drafts (model tier: {config.MODEL_TIERS['judge']})...")
    t0 = time.monotonic()
    result = claude_cli.run(prompt, run_id=run_id, stage=f"judge/attempt{attempt}",
                             model_tier_key="judge", json_schema=JUDGE_JSON_SCHEMA)
    parsed = result.parsed_json()

    chosen = parsed.get("chosen_draft", 1)
    if not isinstance(chosen, int) or not (1 <= chosen <= len(drafts)):
        chosen = 1  # a nonsense index must never crash the run — fall back to draft 1
    winning_cards = list(drafts[chosen - 1].get("cards", []))
    winning_cards, swap_warnings = _apply_swaps(winning_cards, parsed.get("swaps", []), strict=True)
    _log(f"judge done in {time.monotonic() - t0:.0f}s — chose draft {chosen} "
         f"({drafts[chosen - 1].get('angle_name', '?')}), {len(parsed.get('swaps', []))} cherry-pick(s)")
    return (chosen, winning_cards, parsed.get("build_brief", ""), parsed.get("key_cards", []),
            swap_warnings, result.session_id)


def _validate_and_repair(
    run_id: str, concept: ConceptChoice, cards: list[str], cache: dict,
    stage_prefix: str, max_attempts: int,
) -> tuple[list[str], ValidationResult]:
    """Run scryfall_cache.validate_deck(); if invalid, loop repair prompts up to max_attempts.

    Repair responses are swap/cut/add deltas applied in code (_apply_repair_deltas) —
    never a regurgitated full list. The land floor sits a little under the draft
    quota's land_min: it's a tripwire for a stage destroying the mana base, not
    quota enforcement, and a deck legitimately one land light shouldn't burn a
    repair call."""
    min_lands = max(0, int(config.ROLE_QUOTA_DEFAULTS.get("land_min", 0)) - 2)
    current = cards
    validation = scryfall_cache.validate_deck(concept.commander, current, cache=cache, min_lands=min_lands)
    _log(f"{stage_prefix}: validate — {'PASSED' if validation.is_valid else 'failed, entering repair loop'}")

    for attempt in range(1, max_attempts + 1):
        if validation.is_valid:
            break
        _log(f"{stage_prefix}: repair attempt {attempt}/{max_attempts}...")
        prompt = prompt_helpers.render(
            "validate_repair.md",
            commander=concept.commander, color_identity=", ".join(concept.color_identity) or "colorless",
            repair_notes=validation.as_repair_notes(),
            card_count=len(current), deck_size_minus_1=config.DECK_SIZE - 1,
            current_decklist_block="\n".join(f"- {c}" for c in current),
        )
        t0 = time.monotonic()
        result = claude_cli.run(
            prompt, run_id=run_id, stage=f"{stage_prefix}/repair-{attempt}",
            model_tier_key="validate_repair", json_schema=REPAIR_JSON_SCHEMA,
            disallowed_tools=config.DISALLOWED_SEARCH_TOOLS,  # T5: repair is pure Scryfall-legality fixing, no search needed
        )
        current, repair_warnings = _apply_repair_deltas(current, result.parsed_json())
        for warning in repair_warnings:
            _log(f"{stage_prefix}: repair {attempt} — {warning}")
        validation = scryfall_cache.validate_deck(concept.commander, current, cache=cache, min_lands=min_lands)
        _log(f"{stage_prefix}: repair {attempt}/{max_attempts} done in {time.monotonic() - t0:.0f}s — "
             f"{'PASSED' if validation.is_valid else 'still failing'}")

    return current, validation


def _optimize(
    run_id: str, concept: ConceptChoice, build_brief: str, cards: list[str],
    key_cards_oracle_text: str, attempt: int, resume_session_id: str | None = None,
) -> tuple[dict, str]:
    prompt = prompt_helpers.render(
        "optimize.md",
        commander=concept.commander, color_identity=", ".join(concept.color_identity) or "colorless",
        archetype=concept.archetype, rationale=concept.rationale, build_brief=build_brief,
        commander_oracle_text=concept.oracle_text or "(no oracle text on file for this card)",
        key_cards_oracle_text=key_cards_oracle_text,
        current_decklist_block="\n".join(f"- {c}" for c in cards),
        deck_size_minus_1=config.DECK_SIZE - 1,
        **prompt_helpers.bracket_rules_text(),
    )
    _log(f"optimize: fact-check + finishing pass (model tier: {config.MODEL_TIERS['optimize']})...")
    t0 = time.monotonic()
    result = claude_cli.run(
        prompt, run_id=run_id, stage=f"optimize/attempt{attempt}",
        model_tier_key="optimize", json_schema=OPTIMIZE_JSON_SCHEMA,
        disallowed_tools=config.DISALLOWED_SEARCH_TOOLS,  # T5: optimize reasons from the decklist + oracle text already in hand
        resume_session_id=resume_session_id if config.RESUME_SESSION_CHAINING else None,  # T6 scaffolding
    )
    parsed = result.parsed_json()
    _log(f"optimize done in {time.monotonic() - t0:.0f}s — "
         f"strategy_valid={parsed.get('strategy_valid')}, {len(parsed.get('swaps', []))} swap(s)")
    return parsed, result.session_id


def _synergy_gate_and_repair(
    run_id: str, concept: ConceptChoice, cards: list[str], existing_validation: ValidationResult,
    cache: dict, stage_prefix: str,
) -> tuple[list[str], ValidationResult, bool]:
    """S3: code-level synergy-density gate + repair route (PRD v4 amendment §3.2).
    Returns (final_cards, final_validation, gate_fired). If the gate passes
    immediately, `cards`/`existing_validation` are returned unchanged (no extra
    model call, no extra re-validation) — this only spends anything when the gate
    actually fires."""
    tokens = concept.mechanic_tokens
    passes, match_count, generic = synergy_check.gate_passes(cards, tokens, cache)
    _log(f"{stage_prefix}: synergy gate — {match_count} on-mechanic matches "
         f"(threshold {config.SYNERGY_GATE_THRESHOLD}), {'PASSED' if passes else 'firing repair'}")
    if passes:
        return cards, existing_validation, False

    current = cards
    validation = existing_validation
    for attempt in range(1, config.MAX_SYNERGY_REPAIR_ATTEMPTS + 1):
        prompt = prompt_helpers.render(
            "synergy_repair.md",
            commander=concept.commander, color_identity=", ".join(concept.color_identity) or "colorless",
            commander_oracle_text=concept.oracle_text or "(no oracle text on file for this card)",
            mechanic_tokens=", ".join(tokens) or "(none extracted)",
            generic_cards_block="\n".join(f"- {c}" for c in generic) or "(none listed)",
            current_decklist_block="\n".join(f"- {c}" for c in current),
            deck_size_minus_1=config.DECK_SIZE - 1,
        )
        t0 = time.monotonic()
        result = claude_cli.run(
            prompt, run_id=run_id, stage=f"{stage_prefix}/synergy-repair-{attempt}",
            model_tier_key="validate_repair", json_schema=SYNERGY_REPAIR_JSON_SCHEMA,
            disallowed_tools=config.DISALLOWED_SEARCH_TOOLS,
        )
        # T3 (2026-07-10): swap deltas applied in code, not a regurgitated full
        # list. Malformed swaps are warnings, and the Scryfall repair loop below
        # re-validates the applied result either way.
        current, swap_warnings = _apply_swaps(current, result.parsed_json().get("swaps", []))
        for warning in swap_warnings:
            _log(f"{stage_prefix}: synergy repair {attempt} — {warning}")
        current, validation = _validate_and_repair(
            run_id, concept, current, cache,
            stage_prefix=f"{stage_prefix}/synergy-repair-{attempt}", max_attempts=2,
        )
        _log(f"{stage_prefix}: synergy repair {attempt}/{config.MAX_SYNERGY_REPAIR_ATTEMPTS} done in "
             f"{time.monotonic() - t0:.0f}s")
        if not validation.is_valid:
            continue  # Scryfall-invalid result from this repair attempt — try again within budget
        passes, match_count, generic = synergy_check.gate_passes(current, tokens, cache)
        _log(f"{stage_prefix}: synergy repair {attempt} — {match_count} matches, "
             f"{'PASSED' if passes else 'still below threshold'}")
        if passes:
            return current, validation, True

    # Budget exhausted — ship the best attempt rather than failing the whole run.
    # This is a backstop, not a hard quality gate: the PRD is explicit about
    # tolerating false negatives here over blocking a night's deck entirely.
    return current, validation, True


def _run_one_attempt(
    run_id: str, concept: ConceptChoice, cache: dict, attempt: int, retry_note: str,
) -> tuple[dict, list[str], ValidationResult, list[dict], list[str], bool, bool]:
    """One full draft×N->judge->validate/repair->optimize->synergy-gate pass.
    Returns (optimized_response, final_cards, final_validation, ideation_angles,
    swap_warnings, edhrec_pool_used, synergy_gate_fired) — caller checks
    optimized_response["strategy_valid"] to decide whether to accept or retry."""
    # S1: EDHREC candidate pool, now fetched BEFORE the drafts since every parallel
    # drafter builds from it — never on the critical path (pool_block() degrades to
    # a "no pool" placeholder + pool_used=False on any fetch failure or thin-data
    # commander; see edhrec_pool.py).
    edhrec_pool_text, edhrec_pool_used = edhrec_pool.pool_block(concept.commander, cache)

    drafts = _draft(run_id, concept, attempt, edhrec_pool_text, retry_note=retry_note)
    chosen, raw_cards, build_brief, key_cards, judge_swap_warnings, judge_session_id = _judge(
        run_id, concept, drafts, cache, attempt,
    )
    key_cards_oracle_text = _oracle_text_block(cache, key_cards)

    # Kept under the old name for the email/debug trail — same shape as the old
    # ideate angles, plus which draft won.
    angles = [
        {"angle_name": d.get("angle_name", "?"), "gameplan_summary": d.get("gameplan_summary", ""),
         "key_cards_or_themes": d.get("key_cards", []), "chosen": (i + 1 == chosen)}
        for i, d in enumerate(drafts)
    ]

    cards, validation = _validate_and_repair(
        run_id, concept, raw_cards, cache,
        stage_prefix=f"draft/attempt{attempt}", max_attempts=config.MAX_VALIDATE_REPAIR_ATTEMPTS,
    )
    if not validation.is_valid:
        raise claude_cli.ClaudeCLIError(
            "Deck failed validation after the draft-repair loop and could not be recovered:\n"
            f"{validation.as_repair_notes()}"
        )

    # T6 scaffolding: the old chain was synthesize -> build -> optimize; its
    # equivalent now is judge -> optimize (the judge has already seen every
    # draft and wrote the brief optimize fact-checks against).
    optimized, optimize_session_id = _optimize(
        run_id, concept, build_brief, cards, key_cards_oracle_text, attempt,
        resume_session_id=judge_session_id,
    )

    # T3: apply optimize's swap deltas in code rather than trusting a regurgitated
    # full list. Skipped entirely if the fact-check already failed — no point
    # applying swaps to a decklist built on a premise we're about to discard.
    swap_warnings: list[str] = list(judge_swap_warnings)
    if optimized.get("strategy_valid", True):
        optimized_cards, more_warnings = _apply_swaps(cards, optimized.get("swaps", []))
        swap_warnings.extend(more_warnings)
    else:
        optimized_cards = cards

    # optimize() can itself introduce a new problem (a swap that breaks color
    # identity, a hallucinated replacement, etc.) — re-validate rather than
    # trusting "finalizes" at face value. Shorter repair budget here since
    # this path should rarely trigger if optimize behaves. Skipped entirely
    # if the fact-check already failed — no point Scryfall-validating a
    # decklist built on a premise we're about to discard.
    if optimized.get("strategy_valid", True):
        final_cards, final_validation = _validate_and_repair(
            run_id, concept, optimized_cards, cache, stage_prefix=f"optimize/attempt{attempt}", max_attempts=2,
        )
        if not final_validation.is_valid:
            raise claude_cli.ClaudeCLIError(
                "Deck failed validation after the optimize-repair loop and could not be recovered:\n"
                f"{final_validation.as_repair_notes()}"
            )
    else:
        final_cards, final_validation = cards, validation

    # S3: code-level synergy-density gate. Skipped if the strategy fact-check
    # already failed — same reasoning as the re-validate step above.
    synergy_gate_fired = False
    if optimized.get("strategy_valid", True):
        final_cards, final_validation, synergy_gate_fired = _synergy_gate_and_repair(
            run_id, concept, final_cards, final_validation, cache, stage_prefix=f"synergy/attempt{attempt}",
        )
        if not final_validation.is_valid:
            raise claude_cli.ClaudeCLIError(
                "Deck failed validation after the synergy-gate repair loop and could not be recovered:\n"
                f"{final_validation.as_repair_notes()}"
            )

    return optimized, final_cards, final_validation, angles, swap_warnings, edhrec_pool_used, synergy_gate_fired


def run_pipeline(run_id: str, concept: ConceptChoice, cache: dict | None = None) -> DeckResult:
    """Runs stages 2-5: draft (×N parallel) -> judge -> validate/repair -> optimize
    -> re-validate -> synergy gate/repair -> card tagging.

    If optimize's fact-check pass finds the deck was built on a hallucinated
    ability (strategy_valid=false), retries the whole draft->optimize loop
    once (config.MAX_STRATEGY_RETRIES) with a note about what went wrong,
    rather than patching individual cards — a false premise invalidates the
    whole build brief, not just a few cards. Raises claude_cli.ClaudeCLIError
    if the deck still isn't valid (structurally or strategically) after that
    — this must surface as the PRD §2.5 structured error-report email, never
    a silently-shipped illegal/hallucinated deck (PRD's core quality bar).
    """
    cache = cache if cache is not None else scryfall_cache.load_cache()

    retry_note = ""
    optimized = final_cards = final_validation = angles = swap_warnings = None
    edhrec_pool_used = synergy_gate_fired = False
    retried = False
    retry_reason = ""

    for attempt in range(1, config.MAX_STRATEGY_RETRIES + 2):  # e.g. MAX_STRATEGY_RETRIES=1 -> attempts 1, 2
        optimized, final_cards, final_validation, angles, swap_warnings, edhrec_pool_used, synergy_gate_fired = (
            _run_one_attempt(run_id, concept, cache, attempt, retry_note)
        )
        if optimized.get("strategy_valid", True):
            break

        strategy_problem = optimized.get("strategy_problem", "(no explanation given)")
        _log(f"optimize: strategy fact-check FAILED — {strategy_problem}")
        if attempt > config.MAX_STRATEGY_RETRIES:
            raise claude_cli.ClaudeCLIError(
                "Deck's core gameplan failed the oracle-text fact-check and the retry budget "
                f"(config.MAX_STRATEGY_RETRIES={config.MAX_STRATEGY_RETRIES}) is exhausted. "
                f"Last problem reported: {strategy_problem}"
            )
        retried = True
        retry_reason = strategy_problem
        retry_note = (
            f"\nNOTE: a previous attempt at this commander built a deck around this claim, which "
            f"turned out NOT to match the card's real printed text: \"{strategy_problem}\" — do not "
            f"repeat that mistake; base this angle strictly on the oracle text given above.\n"
        )
        _log(f"retrying draft->optimize (attempt {attempt + 1}/{config.MAX_STRATEGY_RETRIES + 1})...")

    # T4: role/phase tags built off Opus entirely — cheap heuristics + one Haiku
    # call for the ambiguous remainder (card_tagger.py), not part of optimize's
    # schema anymore. Commander's own entry is overridden to "Commander" right
    # after — tag_cards() would otherwise tag it like any other card, which is
    # usually wrong (e.g. a commander with removal-shaped text reading as "Removal").
    card_tags = card_tagger.tag_cards(run_id, concept.commander, final_cards, cache)
    commander_key = concept.commander.strip().lower()
    card_tags[commander_key] = {"role": "Commander", "phase": card_tags.get(commander_key, {}).get("phase", "early")}

    return DeckResult(
        concept=concept,
        cards=final_cards,
        validation=final_validation,
        changes_made=optimized.get("changes_made", ""),
        early_game=optimized.get("early_game", ""),
        mid_game=optimized.get("mid_game", ""),
        late_game=optimized.get("late_game", ""),
        card_tags=card_tags,
        ideation_angles=angles,
        retried=retried,
        retry_reason=retry_reason,
        # Fall back to the select-stage originals only if optimize's response is somehow
        # missing them (older cached response shape, empty string, etc.) — should not
        # happen now that both are `required` in OPTIMIZE_JSON_SCHEMA, but never display
        # a blank headline over a stale-but-present one.
        final_archetype=optimized.get("final_archetype") or concept.archetype,
        final_summary=optimized.get("final_summary") or concept.rationale,
        swap_warnings=swap_warnings or [],
        edhrec_pool_used=edhrec_pool_used,
        synergy_gate_fired=synergy_gate_fired,
    )
