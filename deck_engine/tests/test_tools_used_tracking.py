"""Regression/new-feature test for tool-use tracking, added 2026-07-01 after
Trevor decided tool access should stay fully unrestricted ("let the models
decide whenever they wanna web search") rather than gating WebSearch/WebFetch
to specific stages. That decision means a call CAN now reach for any tool at
any stage — this closes the observability gap that made the Old Rutstein
run's 8-turn build call (vs. every other run's 2-turn build calls)
unexplainable: nothing recorded what happened beyond the aggregate numbers.

Covers two things:
  1. claude_cli._track_tool_use() correctly extracts real tool names from
     content_block_start events, excludes StructuredOutput (the schema-
     response mechanism every call uses — not a meaningful signal), and
     ignores unrelated event shapes without raising.
  2. spend_log.record()/summarize_run() round-trip tools_used per call and
     roll up the UNION of tools used across every call in a run.

Run manually: cd gishath-local-v2 && python3 -m deck_engine.tests.test_tools_used_tracking
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .. import claude_cli, config, spend_log


def _tool_use_event(name: str) -> dict:
    return {
        "type": "stream_event",
        "event": {"type": "content_block_start", "content_block": {"type": "tool_use", "name": name}},
    }


def test_track_tool_use() -> list[str]:
    problems = []
    tools_used: set[str] = set()

    # A real tool call, StructuredOutput (must be excluded), and some noise
    # event shapes that must be silently ignored rather than raise.
    events = [
        _tool_use_event("WebSearch"),
        _tool_use_event("StructuredOutput"),
        _tool_use_event("Bash"),
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}},
        {"type": "system", "subtype": "init"},  # not a stream_event at all
        {},  # empty dict — no "type" key
    ]
    for event in events:
        claude_cli._track_tool_use(event, tools_used)  # noqa: SLF001 — testing the internal helper directly

    if tools_used != {"WebSearch", "Bash"}:
        problems.append(f"expected {{'WebSearch', 'Bash'}}, got {tools_used}")
    return problems


def test_spend_log_rollup() -> list[str]:
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        config.SPEND_LOG_PATH = Path(tmp) / "spend_log.jsonl"

        spend_log.record(run_id="r1", stage="ideate/1", model="sonnet", cost_usd=0.1, num_turns=2,
                          duration_ms=1000, is_error=False, session_id="s1", tools_used=["WebSearch"])
        spend_log.record(run_id="r1", stage="ideate/2", model="sonnet", cost_usd=0.1, num_turns=2,
                          duration_ms=1000, is_error=False, session_id="s2", tools_used=[])
        spend_log.record(run_id="r1", stage="build", model="sonnet", cost_usd=0.1, num_turns=2,
                          duration_ms=1000, is_error=False, session_id="s3", tools_used=["Bash", "WebSearch"])
        # A call that never passes tools_used at all (default None) — must not break the rollup.
        spend_log.record(run_id="r1", stage="optimize", model="opus", cost_usd=0.1, num_turns=2,
                          duration_ms=1000, is_error=False, session_id="s4")

        summary = spend_log.summarize_run("r1")
        if summary["tools_used"] != ["Bash", "WebSearch"]:
            problems.append(f"expected sorted union ['Bash', 'WebSearch'], got {summary['tools_used']}")

        # A run where nothing used any tool must report an empty list, not None or a crash.
        spend_log.record(run_id="r2", stage="select", model="opus", cost_usd=0.1, num_turns=2,
                          duration_ms=1000, is_error=False, session_id="s5", tools_used=[])
        empty_summary = spend_log.summarize_run("r2")
        if empty_summary["tools_used"] != []:
            problems.append(f"expected [] for a run with no tool use, got {empty_summary['tools_used']}")

    return problems


def main() -> int:
    results = {"track_tool_use": test_track_tool_use(), "spend_log_rollup": test_spend_log_rollup()}
    problems = [p for plist in results.values() for p in plist]
    if problems:
        print(f"FAILED: {problems}", file=sys.stderr)
        return 1
    print("OK: tool-use tracking round-trips through _track_tool_use() and spend_log correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
