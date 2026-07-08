"""
deck_engine/budget_pass.py — stage 6b: per-card budget cap (PRD v4 amendment §3.4).

Runs AFTER stage 6 pricing — the only point in the pipeline where prices
exist (no earlier stage sees a price, which is exactly how the Arcades run
shipped an SGD 1,023.86 deck on 2026-07-03). Rule, per Trevor's explicit
calls that day: no single card over config.MAX_CARD_PRICE_SGD (default 150);
deliberately NO total-deck cap (the total is displayed, never enforced); an
unfixable breach ships FLAGGED rather than failing the run — same
flag-never-block philosophy as pricing itself (PRD §2.4).

Mechanism mirrors T3's swap-delta discipline: one Sonnet call per attempt
receives ONLY the over-cap cards (name, SGD price, role, phase) plus the
commander's oracle text / mechanic tokens / fact-checked summary, and
returns targeted `swaps` — applied in code via agent_pipeline._apply_swaps(),
Scryfall re-validated via the existing repair loop, then ONLY the swapped-in
cards are re-priced through gishath-local-v2's /search (never the whole deck
again — a full re-scrape costs minutes for no reason). Max
config.MAX_BUDGET_REPAIR_ATTEMPTS (2) loops in case a substitute itself
breaches the cap.

SYNERGY INTERACTION (PRD §3.4): after the swaps settle, the S3 synergy gate
is RE-CHECKED (code-only, free) so a cheap-but-generic substitute can't
silently drop the deck below the density threshold. Deliberately check-only
here — no repair loop: budget-swap → synergy-repair → new expensive card →
budget-swap ping-pong has no natural fixpoint, so a post-budget synergy dip
is reported in the email (BudgetOutcome.synergy_note), not silently
re-edited. The primary defence is the prompt: substitutes must stay
on-mechanic.

COMMANDER: cannot be swapped here — replacing the commander invalidates the
entire deck. It's checked at select-time instead (commander_price_sgd() is
passed into concept_selector.select_concept() as price_check by run.py) so
an over-cap commander is re-picked before anything is built. If that check
was unavailable at select time (pricing app not yet reachable — fail-soft)
and the commander still prices over cap here, it goes straight onto the
over_budget flag list.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import claude_cli, config, prompt_helpers, pricing as pricing_mod, synergy_check
from .agent_pipeline import DeckResult, _apply_swaps, _validate_and_repair

BUDGET_SWAP_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "swaps": {
            "type": "array",
            "description": "One entry per over-cap card. remove = the exact over-cap card name; "
                           "add = the budget replacement (exact printed name).",
            "items": {
                "type": "object",
                "properties": {
                    "remove": {"type": "string"},
                    "add": {"type": "string"},
                    "reason": {"type": "string"},
                    "role": {"type": "string",
                             "description": "functional role of the replacement, e.g. 'Ramp', 'Removal'"},
                    "phase": {"type": "string", "enum": ["early", "mid", "late"]},
                },
                "required": ["remove", "add", "reason", "role", "phase"],
            },
        },
    },
    "required": ["swaps"],
}


@dataclass
class BudgetOutcome:
    """What the budget pass did, for the email/xlsx callouts (PRD §3.4)."""
    ran: bool = False                # False = no pricing available, pass skipped entirely
    cap: float = 0.0
    # (removed_card, removed_price, added_card, added_price_or_None, reason)
    swaps_made: list[tuple] = field(default_factory=list)
    # (card, price) — still over cap after the attempt budget; shipped flagged
    over_budget: list[tuple] = field(default_factory=list)
    synergy_note: str = ""           # non-empty if the post-swap synergy re-check dipped below threshold
    notes: list[str] = field(default_factory=list)   # swap-application warnings etc., for diagnostics


def _log(msg: str) -> None:
    if claude_cli.has_live_view():
        return
    import sys
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def commander_price_sgd(commander: str) -> float | None:
    """Price ONE card (the commander) via the existing /search path — used as the
    select-stage price_check (PRD §3.4: an over-cap commander must be re-picked
    before anything is built, since it can't be swapped afterward). Returns None
    (check skipped, fail-soft) if pricing is unavailable or the card gets no hits."""
    outcome = pricing_mod.fetch_prices([commander])
    if not outcome.available or outcome.plan is None:
        return None
    info = pricing_mod.cheapest_by_card(outcome).get(commander.strip().lower())
    return info[0] if info else None


def _violations(cards: list[str], cheapest: dict[str, tuple[float, str]], cap: float) -> list[tuple[str, float]]:
    """(card, price) for every card in `cards` priced strictly above the cap.
    Unpriced cards are NOT violations — the cap can't be evaluated for them and
    they're already flagged as unpriced elsewhere (never assumed cheap OR expensive)."""
    out = []
    for card in cards:
        info = cheapest.get(card.strip().lower())
        if info is not None and info[0] > cap:
            out.append((card, info[0]))
    return out


def _swap_call(run_id: str, deck: DeckResult, violations: list[tuple[str, float]], attempt: int) -> list[dict]:
    over_cap_block = "\n".join(
        f"- {card} — SGD {price:.2f} (role: {deck.card_tags.get(card.strip().lower(), {}).get('role') or 'unknown'}, "
        f"phase: {deck.card_tags.get(card.strip().lower(), {}).get('phase') or 'unknown'})"
        for card, price in violations
    )
    prompt = prompt_helpers.render(
        "budget_swap.md",
        commander=deck.concept.commander,
        color_identity=", ".join(deck.concept.color_identity) or "colorless",
        archetype=deck.final_archetype,
        final_summary=deck.final_summary,
        commander_oracle_text=deck.concept.oracle_text or "(no oracle text on file)",
        mechanic_tokens=", ".join(deck.concept.mechanic_tokens) or "(none extracted)",
        max_card_price=f"{config.MAX_CARD_PRICE_SGD:.0f}",
        over_cap_block=over_cap_block,
    )
    result = claude_cli.run(
        prompt, run_id=run_id, stage=f"budget/swap-{attempt}",
        model_tier_key="validate_repair", json_schema=BUDGET_SWAP_JSON_SCHEMA,
        disallowed_tools=config.DISALLOWED_SEARCH_TOOLS,
    )
    return result.parsed_json().get("swaps", [])


def enforce_card_cap(
    run_id: str, deck: DeckResult, pricing: pricing_mod.PricingOutcome, cache: dict,
) -> BudgetOutcome:
    """Stage 6b. Mutates deck.cards/card_tags in place (swapped cards), appends
    re-priced assignments into `pricing` (extra_assignments) so every downstream
    consumer — export, email headline, breakdown sheet — prices the FINAL deck.
    Never raises for budget reasons: unfixable breaches come back in
    BudgetOutcome.over_budget and ship flagged (PRD §3.4)."""
    cap = config.MAX_CARD_PRICE_SGD
    outcome = BudgetOutcome(cap=cap)

    if not pricing.available or pricing.plan is None:
        outcome.notes.append("pricing unavailable — budget pass skipped (cap can't be evaluated)")
        return outcome
    outcome.ran = True

    cheapest = pricing_mod.cheapest_by_card(pricing)

    # Commander first: can't be swapped post-build (see module docstring) — if it's
    # over cap despite the select-time check, it goes straight onto the flag list.
    commander_info = cheapest.get(deck.concept.commander.strip().lower())
    if commander_info is not None and commander_info[0] > cap:
        outcome.over_budget.append((deck.concept.commander, commander_info[0]))
        outcome.notes.append(
            f"commander {deck.concept.commander} is over the cap (SGD {commander_info[0]:.2f}) and cannot "
            "be swapped post-build — the select-stage price check must have been unavailable this run"
        )

    for attempt in range(1, config.MAX_BUDGET_REPAIR_ATTEMPTS + 1):
        violations = _violations(deck.cards, cheapest, cap)
        if not violations:
            break
        _log(f"budget: {len(violations)} card(s) over SGD {cap:.0f} — swap attempt {attempt}/"
             f"{config.MAX_BUDGET_REPAIR_ATTEMPTS}...")

        proposed = _swap_call(run_id, deck, violations, attempt)

        # Only over-cap cards may be removed — the model must never touch a
        # compliant card, whatever it returns (same trust-nothing posture as T3).
        violation_keys = {c.strip().lower() for c, _ in violations}
        vetted = [s for s in proposed if str(s.get("remove", "")).strip().lower() in violation_keys]
        skipped = len(proposed) - len(vetted)
        if skipped:
            outcome.notes.append(f"attempt {attempt}: ignored {skipped} swap(s) targeting non-violating cards")
        if not vetted:
            outcome.notes.append(f"attempt {attempt}: model proposed no usable swaps")
            continue

        new_cards, warnings = _apply_swaps(deck.cards, vetted)
        outcome.notes.extend(warnings)

        # Scryfall re-validate (real card, legality, color identity, singleton) via
        # the existing repair loop — a budget swap must clear the same quality bar
        # as every other edit in this pipeline.
        new_cards, validation = _validate_and_repair(
            run_id, deck.concept, new_cards, cache,
            stage_prefix=f"budget/attempt{attempt}", max_attempts=2,
        )
        if not validation.is_valid:
            outcome.notes.append(
                f"attempt {attempt}: swapped list failed Scryfall re-validation and could not be repaired — "
                "keeping the previous (valid, over-budget) list instead"
            )
            continue  # deck.cards untouched; next attempt (or ship flagged)

        deck.cards = new_cards
        deck.validation = validation

        # Re-price ONLY the swapped-in cards (never the whole deck again).
        added = [str(s["add"]).strip() for s in vetted]
        reprice = pricing_mod.fetch_prices(added)
        added_prices = pricing_mod.cheapest_by_card(reprice) if reprice.available else {}
        for key, (price, store) in added_prices.items():
            pricing.extra_assignments.append((key, price, store))
            cheapest[key] = (price, store)
        # Carry the swapped-in cards' CK reference prices too, so the export's
        # CK column doesn't just go blank for anything the budget pass touched.
        pricing.ck_prices.update(reprice.ck_prices)

        # Record swaps + carry role/phase tags over so the breakdown sheet stays complete.
        for s in vetted:
            removed, added_name = str(s["remove"]).strip(), str(s["add"]).strip()
            removed_price = dict(violations).get(removed)
            if removed_price is None:  # match case-insensitively if exact-case lookup missed
                removed_price = next((p for c, p in violations if c.strip().lower() == removed.lower()), 0.0)
            added_info = added_prices.get(added_name.strip().lower())
            outcome.swaps_made.append(
                (removed, removed_price, added_name, added_info[0] if added_info else None,
                 str(s.get("reason", "")).strip())
            )
            deck.card_tags.pop(removed.strip().lower(), None)
            deck.card_tags[added_name.strip().lower()] = {
                "role": str(s.get("role", "")).strip(), "phase": str(s.get("phase", "")).strip(),
            }

    # Whatever's still over cap after the attempt budget ships FLAGGED (Trevor's
    # explicit call: never fail a run over a pricing rule).
    outcome.over_budget.extend(_violations(deck.cards, cheapest, cap))

    # S3 synergy re-check — check-only, no repair loop here (see module docstring
    # for why: budget<->synergy repair ping-pong has no fixpoint).
    if outcome.swaps_made and deck.concept.mechanic_tokens:
        passes, match_count, _generic = synergy_check.gate_passes(
            deck.cards, deck.concept.mechanic_tokens, cache,
        )
        if not passes:
            outcome.synergy_note = (
                f"Heads-up: after the budget swaps, only {match_count} nonland cards match the commander's "
                f"mechanic keywords (threshold {config.SYNERGY_GATE_THRESHOLD}) — the budget substitutes may "
                "have thinned the synergy package; worth a manual look."
            )

    if outcome.swaps_made:
        _log(f"budget: {len(outcome.swaps_made)} swap(s) applied; "
             f"{len(outcome.over_budget)} card(s) still over cap (shipping flagged)" if outcome.over_budget
             else f"budget: {len(outcome.swaps_made)} swap(s) applied; all cards now within cap")
    return outcome
