"""
deck_engine/run.py — top-level orchestrator, ties together stages 1-9 (PRD §5).

Entry point invoked by run_nightly.sh. Always attempts to email something —
a finished deck on success, or a structured Error Report on failure (PRD
§2.5) — and always exits non-zero on failure so run_nightly.sh's own exit
code (and any future monitoring around it) can tell the two apart, even
though the email is the primary signal Trevor actually reads.

Not yet exercised end-to-end against a real authenticated `claude` session
or a running gishath-local-v2 app (both unavailable in this sandbox) — this
is exactly what PRD §8 step 8 (dry run) on Trevor's Mac is for.
"""
from __future__ import annotations

import sys
import uuid

from . import (
    agent_pipeline,
    budget_pass,
    claude_cli,
    concept_selector,
    config,
    emailer,
    export,
    live_view,
    pricing,
    run_log,
    scryfall_cache,
    spend_log,
)


def main(view=None, forced_commander: str | None = None, run_id: str | None = None) -> int:
    """Run the whole pipeline once.

    view: anything implementing live_view.LiveView's surface (__enter__/__exit__,
    .console.print, .start_call). Defaults to the rich terminal view. The
    Atelier UI passes its own event-bus view here; hooks beyond the LiveView
    surface (set_stage / concept_chosen / run_delivered / run_failed) are
    OPTIONAL and looked up with getattr — the terminal view never needs them.

    forced_commander: a user-typed commander name (Atelier commission box) —
    threaded to concept_selector.select_concept(), which validates it in code
    and skips the model's own pick. None keeps the nightly "guild's choice"
    behaviour exactly as before.

    run_id: caller-supplied id (Atelier pre-keys its event log and cancel flag
    on it before the thread starts); None generates one, as before.
    """
    run_id = run_id or str(uuid.uuid4())
    stage = "startup"
    view = view if view is not None else live_view.LiveView()
    claude_cli.set_live_view(view)

    def _notify(method: str, **kwargs) -> None:
        # Optional UI hooks — silently skipped for views that don't implement
        # them (the rich terminal view), and never allowed to fail a run.
        fn = getattr(view, method, None)
        if callable(fn):
            try:
                fn(**kwargs)
            except Exception:  # noqa: BLE001
                pass

    def _announce(msg: str) -> None:
        # Route through the live view's own console rather than bare print() —
        # rich's Live redraws a managed region, and un-routed print() while
        # it's active can visually corrupt the display (see live_view.py).
        view.console.print(msg)

    # Set inside the try below, printed via a plain print() only AFTER `with
    # view:` exits — see the note at the bottom of this function for why.
    exit_code = 1
    final_line: str | None = None

    try:
        with view:
            try:
                _notify("run_started", run_id=run_id, forced_commander=forced_commander)
                stage = "scryfall_cache"
                _notify("set_stage", stage=stage)
                try:
                    scryfall_cache.refresh_if_stale()
                except Exception as exc:  # noqa: BLE001
                    if not config.SCRYFALL_CACHE_PATH.exists():
                        raise RuntimeError(
                            f"No Scryfall cache available and the refresh attempt also failed: {exc}. "
                            "Run `python -m deck_engine.scryfall_cache --refresh` manually first."
                        ) from exc
                    _announce(f"[warn] Scryfall cache refresh failed, continuing with existing "
                              f"(possibly stale) cache: {exc}")
                cache = scryfall_cache.load_cache()

                stage = "select"
                _notify("set_stage", stage=stage)
                # §3.4: commander priced at select-time (budget_pass.commander_price_sgd) —
                # an over-cap commander is re-picked here since it can't be swapped post-build.
                concept = concept_selector.select_concept(
                    run_id, cache=cache, price_check=budget_pass.commander_price_sgd,
                    forced_commander=forced_commander,
                )
                _announce(f"[run {run_id[:8]}] concept: {concept.commander} — {concept.archetype}")
                _notify("concept_chosen", commander=concept.commander, archetype=concept.archetype,
                        rationale=concept.rationale, colors=concept.color_identity)

                stage = "ideate/build/validate/optimize"
                _notify("set_stage", stage=stage)
                deck = agent_pipeline.run_pipeline(run_id, concept, cache=cache)
                _announce(f"[run {run_id[:8]}] deck validated: {deck.validation.is_valid} "
                          f"({len(deck.cards)} cards + commander)")

                stage = "price"
                _notify("set_stage", stage=stage)
                pricing_outcome = pricing.fetch_prices([deck.concept.commander] + deck.cards)
                if not pricing_outcome.available:
                    _announce(f"[warn] pricing unavailable this run: {pricing_outcome.error}")

                stage = "budget"
                _notify("set_stage", stage=stage)
                # §3.4 stage 6b: per-card cap (mutates deck.cards/card_tags in place and
                # overlays re-prices onto pricing_outcome; ships flagged, never fails).
                budget_outcome = budget_pass.enforce_card_cap(run_id, deck, pricing_outcome, cache)
                if budget_outcome.swaps_made:
                    _announce(f"[run {run_id[:8]}] budget pass: {len(budget_outcome.swaps_made)} swap(s), "
                              f"{len(budget_outcome.over_budget)} still over cap")
                elif budget_outcome.over_budget:
                    _announce(f"[warn] budget pass: {len(budget_outcome.over_budget)} card(s) over "
                              f"SGD {budget_outcome.cap:.0f}, shipping flagged")

                stage = "export"
                _notify("set_stage", stage=stage)
                xlsx_path = export.save_deck_excel(deck, pricing_outcome, run_id, cache=cache,
                                                   budget=budget_outcome)
                moxfield_txt_path = export.save_moxfield_txt(deck, run_id)
                _announce(f"[run {run_id[:8]}] wrote {xlsx_path}")

                stage = "deliver"
                _notify("set_stage", stage=stage)
                spend_summary = spend_log.summarize_run(run_id)
                # Machine-readable record for the Atelier UI's gallery/deck view —
                # written even when no view is attached, so nightly runs stay
                # browsable in the app the next morning.
                deck_json_path = export.save_deck_json(
                    deck, pricing_outcome, run_id, cache=cache, budget=budget_outcome,
                    spend_summary=spend_summary,
                    xlsx_path=xlsx_path, moxfield_txt_path=moxfield_txt_path,
                )
                emailer.send_success_email(
                    deck=deck, xlsx_path=xlsx_path, moxfield_txt_path=moxfield_txt_path,
                    spend_summary=spend_summary, pricing=pricing_outcome, cache=cache,
                    budget=budget_outcome,
                )

                run_log.append_record(run_log.RunRecord.now(
                    # final_archetype (optimize's fact-checked label), not concept.archetype
                    # (select's unverified first guess) — more accurate signal for the
                    # soft archetype-repeat dedupe in run_log.recent_archetypes().
                    commander=deck.concept.commander, archetype=deck.final_archetype,
                    status="success", colors=deck.concept.color_identity,
                ))
                cache_ratio = spend_summary.get("cache_hit_ratio")
                cache_note = f"cache_hit_ratio={cache_ratio:.2f}" if cache_ratio is not None else "cache_hit_ratio=n/a"
                tools_used = spend_summary.get("tools_used") or []
                tools_note = f"tools_used={tools_used}" if tools_used else "tools_used=none"
                final_line = (f"[run {run_id[:8]}] done. cost=${spend_summary['total_cost_usd']:.4f} "
                              f"turns={spend_summary['total_turns']} {cache_note} {tools_note}")
                _notify("run_delivered", run_id=run_id, deck_json=str(deck_json_path),
                        xlsx=str(xlsx_path), moxfield_txt=str(moxfield_txt_path),
                        spend_summary=spend_summary)
                exit_code = 0

            except Exception as exc:  # noqa: BLE001 — top-level: must never crash silently, must always try to email
                _announce(f"[run {run_id[:8]}] FAILED at stage '{stage}': {exc}")
                try:
                    run_log.append_record(run_log.RunRecord.now(
                        commander="", archetype="", status="error", error_summary=f"[{stage}] {exc}",
                    ))
                except Exception:  # noqa: BLE001 — never let run-log bookkeeping mask the real failure
                    pass
                try:
                    emailer.send_error_from_exception(exc, stage=stage, run_id=run_id)
                except Exception as email_exc:  # noqa: BLE001 — emailer already falls back to a local file
                    _announce(f"[run {run_id[:8]}] additionally failed to send the error report: {email_exc}")
                final_line = f"[run {run_id[:8]}] FAILED at stage '{stage}': {exc}"
                _notify("run_failed", run_id=run_id, stage=stage, error=str(exc),
                        spend_summary=spend_log.summarize_run(run_id))
                exit_code = 1
    finally:
        # Always clear the module-level view, even on an unhandled exception —
        # claude_cli.run() checks has_live_view() on every call, and a leaked
        # reference would silently misroute output if this process were ever
        # reused (e.g. imported into a longer-lived caller, or a future test
        # harness that calls main() more than once).
        claude_cli.set_live_view(None)

    # Printed with a bare print(), not _announce()/view.console — `with view:`
    # has already exited by this point, so rich's Live region is no longer
    # managing the screen and a normal print() is both safe and, more to the
    # point, guaranteed to be the true last line on screen. Previously this
    # went through view.console.print() *inside* the `with` block, which
    # rich renders above the still-live panel — meaning it scrolled out of
    # view by the time the run actually finished, and the last thing visible
    # was run_nightly.sh's generic "finished with exit code N" line instead
    # of the cost Trevor actually wants to see (caught live, 2026-07-01).
    if final_line is not None:
        print(final_line)
    return exit_code


if __name__ == "__main__":
    # `python -m deck_engine.run --commander "Braids, Arisen Nightmare"` — same
    # forced-commander path the Atelier commission box uses; no flag keeps the
    # nightly guild's-choice behaviour.
    forced = None
    if "--commander" in sys.argv:
        idx = sys.argv.index("--commander")
        if idx + 1 < len(sys.argv):
            forced = sys.argv[idx + 1]
    sys.exit(main(forced_commander=forced))

