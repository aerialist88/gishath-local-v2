"""Regression test for the token/cache instrumentation added to spend_log.py
(2026-07-01) so cost questions can be answered from real data instead of
one-off diagnostics. Writes to a temp file (never touches the real
deck_engine/logs/spend_log.jsonl) and checks record()/summarize_run() round-trip
the new fields correctly, including cache_hit_ratio's math and its None
fallback when a run has no token data at all.

Run manually: cd gishath-local-v2 && python3 -m deck_engine.tests.test_spend_log_token_breakdown
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .. import config, spend_log


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        config.SPEND_LOG_PATH = Path(tmp) / "spend_log.jsonl"

        # Two calls in one run: one all cache-creation (cold), one all cache-read (warm) —
        # mirrors what a real multi-stage run should look like if caching is working.
        spend_log.record(run_id="r1", stage="select", model="opus", cost_usd=0.39, num_turns=2,
                          duration_ms=9000, is_error=False, session_id="s1",
                          input_tokens=767, output_tokens=54,
                          cache_creation_input_tokens=21297, cache_read_input_tokens=0)
        spend_log.record(run_id="r1", stage="ideate/1", model="sonnet", cost_usd=0.14, num_turns=2,
                          duration_ms=9000, is_error=False, session_id="s2",
                          input_tokens=800, output_tokens=60,
                          cache_creation_input_tokens=0, cache_read_input_tokens=21297)
        # A second, unrelated run must not leak into r1's summary.
        spend_log.record(run_id="r2", stage="select", model="opus", cost_usd=1.0, num_turns=2,
                          duration_ms=9000, is_error=False, session_id="s3")

        summary = spend_log.summarize_run("r1")

        checks = [
            ("total_input_tokens", summary["total_input_tokens"] == 1567),
            ("total_output_tokens", summary["total_output_tokens"] == 114),
            ("total_cache_creation_input_tokens", summary["total_cache_creation_input_tokens"] == 21297),
            ("total_cache_read_input_tokens", summary["total_cache_read_input_tokens"] == 21297),
            ("cache_hit_ratio == 0.5", summary["cache_hit_ratio"] == 0.5),
            ("r2 didn't leak into r1's stage count", len(summary["stages"]) == 2),
        ]
        failed = [label for label, ok in checks if not ok]
        if failed:
            print(f"FAILED: {failed}\nsummary={summary}", file=sys.stderr)
            return 1

        # A run with no token fields at all (old log entries, or record() called
        # with all defaults) must report cache_hit_ratio=None, not raise or divide by zero.
        spend_log.record(run_id="r3", stage="select", model="opus", cost_usd=0.1, num_turns=1,
                          duration_ms=1000, is_error=False, session_id="s4")
        empty_summary = spend_log.summarize_run("r3")
        if empty_summary["cache_hit_ratio"] is not None:
            print(f"FAILED: expected cache_hit_ratio=None for a run with zero cache tokens, "
                  f"got {empty_summary['cache_hit_ratio']}", file=sys.stderr)
            return 1

    print("OK: token/cache fields round-trip through record()/summarize_run() correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
