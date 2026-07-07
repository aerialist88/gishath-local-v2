"""Regression test for the "cost line should be the true last thing printed"
fix (2026-07-01): previously `run.py` printed the "done. cost=..." summary
through `view.console.print()` *inside* the `with view:` block, so rich's
still-live panel rendered below it and the line scrolled out of sight by the
time the run actually finished. Fixed by deferring that print to a bare
print() call after `with view:` has exited. This test monkeypatches every
pipeline dependency `run.py` calls (so it never touches a real `claude`
process, gishath-local-v2 app, or SMTP) and checks that the final summary
line is literally the last non-empty line of captured stdout, for both the
success and failure paths.

Run manually: cd gishath-local-v2 && python3 -m deck_engine.tests.test_run_final_line_ordering
"""
from __future__ import annotations

import contextlib
import io
import sys

from .. import (
    agent_pipeline, concept_selector, emailer, export, pricing, run, run_log, scryfall_cache, spend_log,
)
from ..concept_selector import ConceptChoice
from ..pricing import PricingOutcome
from ..scryfall_cache import ValidationResult


def _fake_concept() -> ConceptChoice:
    return ConceptChoice(
        commander="Fake Commander", archetype="Fake Archetype", rationale="test fixture",
        color_identity=["G", "W"], oracle_text="(test fixture oracle text)",
    )


def _fake_deck(concept: ConceptChoice) -> agent_pipeline.DeckResult:
    validation = ValidationResult(commander=concept.commander, card_count=100)  # no violations -> is_valid True
    return agent_pipeline.DeckResult(
        concept=concept, cards=["Plains"] * 99, validation=validation,
        changes_made="", early_game="", mid_game="", late_game="",
    )


def _patch_common(monkeypatch_targets: dict) -> None:
    scryfall_cache.refresh_if_stale = lambda *a, **k: False
    scryfall_cache.load_cache = lambda *a, **k: {}
    export.save_deck_excel = lambda *a, **k: "/tmp/fake_deck.xlsx"
    pricing.fetch_prices = lambda *a, **k: PricingOutcome(plan=None, available=False, error="test: pricing skipped")
    emailer.send_success_email = lambda *a, **k: None
    emailer.send_error_from_exception = lambda *a, **k: None
    run_log.append_record = lambda *a, **k: None
    spend_log.summarize_run = lambda run_id: {
        "run_id": run_id, "total_cost_usd": 1.2345, "total_turns": 9, "cache_hit_ratio": 0.42,
    }


def _last_nonempty_line(captured: str) -> str:
    # Strip rich's ANSI/control-code noise crudely — we only need to confirm
    # ORDERING (that our plain text appears after the live view's output),
    # not validate rich's rendering, so a rough strip is enough here.
    import re
    plain = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", captured)
    lines = [ln.strip() for ln in plain.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def test_success_path() -> bool:
    _patch_common({})
    # **k absorbs price_check (§3.4) and any future keyword-only wiring from run.py.
    concept_selector.select_concept = lambda run_id, cache, **k: _fake_concept()
    agent_pipeline.run_pipeline = lambda run_id, concept, cache=None: _fake_deck(concept)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exit_code = run.main()
    out = buf.getvalue()
    last_line = _last_nonempty_line(out)

    ok = exit_code == 0 and "done. cost=$1.2345" in last_line and "cache_hit_ratio=0.42" in last_line
    if not ok:
        print(f"FAILED success path: exit_code={exit_code} last_line={last_line!r}", file=sys.stderr)
    return ok


def test_failure_path() -> bool:
    _patch_common({})
    # **k absorbs price_check (§3.4) and any future keyword-only wiring from run.py.
    concept_selector.select_concept = lambda run_id, cache, **k: _fake_concept()

    def _boom(run_id, concept, cache=None):
        raise RuntimeError("simulated pipeline failure for test")
    agent_pipeline.run_pipeline = _boom

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exit_code = run.main()
    out = buf.getvalue()
    last_line = _last_nonempty_line(out)

    ok = exit_code == 1 and "FAILED at stage 'draft/judge/validate/optimize'" in last_line
    if not ok:
        print(f"FAILED failure path: exit_code={exit_code} last_line={last_line!r}", file=sys.stderr)
    return ok


def main() -> int:
    results = {"success_path": test_success_path(), "failure_path": test_failure_path()}
    if not all(results.values()):
        print(f"FAILED: {results}", file=sys.stderr)
        return 1
    print(f"OK: final summary line is the true last line of output in both cases — {results}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
