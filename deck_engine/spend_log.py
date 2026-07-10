"""
deck_engine/spend_log.py — per-run turn/cost/token logging (PRD §4f).

Every `claude -p` invocation across the whole pipeline is tagged with a single
`run_id` (one per nightly run, minted by whatever calls run_pipeline() — see
agent_pipeline.py) so `summarize_run(run_id)` can answer "what did *this*
run actually cost", not just a lifetime total. Converts the PRD's cost
*estimate* (~$0 marginal on subscription / ~$1-3/night metered) into a real,
checkable number from the first dry run.

TOKEN/CACHE BREAKDOWN (added 2026-07-01): cost_usd/num_turns alone can't
answer "is prompt caching actually helping" or "did --strict-mcp-config /
--disable-slash-commands reduce the per-call system-prompt overhead" — an
ad-hoc isolated check on Trevor's Mac that day was inconclusive precisely
because it only had cost_usd to look at plus one manually-copied `usage`
dict (see claude_cli.py's module docstring for what that check found:
cache_read_input_tokens went from 0 to >0 with the flags on, but
cache_creation_input_tokens stayed roughly the same size — mixed signal).
Every record now carries the real input/output/cache-creation/cache-read
token counts straight from the API response's `usage` block, so future cost
questions can be answered by reading this file instead of running one-off
diagnostics.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import config


def record(*, run_id: str, stage: str, model: str, cost_usd: float, num_turns: int,
           duration_ms: int, is_error: bool, session_id: str,
           input_tokens: int = 0, output_tokens: int = 0,
           cache_creation_input_tokens: int = 0, cache_read_input_tokens: int = 0,
           tools_used: list[str] | None = None,
           narration_chars: int = 0, thinking_chars: int = 0,
           thinking_est_tokens: int = 0, structured_chars: int = 0) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "stage": stage,
        "model": model,
        "cost_usd": cost_usd,
        "num_turns": num_turns,
        "duration_ms": duration_ms,
        "is_error": is_error,
        "session_id": session_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        # Tool access is unrestricted (Trevor's call, 2026-07-01 — see
        # claude_cli.py's module docstring) — this is what makes an anomaly
        # like an unexpectedly-high-turn-count call explainable after the
        # fact instead of a permanent mystery. Empty list on nearly every
        # call; only non-empty if the model reached for something.
        "tools_used": tools_used or [],
        # Output-token attribution (2026-07-10): where did the output go —
        # visible narration, extended thinking, or the structured payload?
        # Chars for the text streams; thinking_est_tokens is the redacted
        # stream's own token counter (sonnet/opus). Zeros on records logged
        # before this existed.
        "narration_chars": narration_chars,
        "thinking_chars": thinking_chars,
        "thinking_est_tokens": thinking_est_tokens,
        "structured_chars": structured_chars,
    }
    with config.SPEND_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def summarize_run(run_id: str) -> dict:
    """Roll up total cost/turns/duration/tokens for one run_id, stage by stage.

    `cache_hit_ratio` = cache_read / (cache_read + cache_creation) across the
    whole run — 0.0 means every call paid full cache-creation price for its
    system prompt (no reuse across stages); closer to 1.0 means later calls
    are mostly hitting cache from earlier ones. Missing entirely (None) for
    runs logged before this field existed, or if a run had zero of both
    (shouldn't happen for a real `claude -p` call, but avoid a ZeroDivisionError
    on old/malformed data).
    """
    result = {
        "run_id": run_id, "total_cost_usd": 0.0, "total_turns": 0,
        "total_duration_ms": 0, "had_errors": False, "stages": [],
        "total_input_tokens": 0, "total_output_tokens": 0,
        "total_cache_creation_input_tokens": 0, "total_cache_read_input_tokens": 0,
        "cache_hit_ratio": None,
        "tools_used": [],  # every distinct non-StructuredOutput tool any call in this run invoked
    }
    if not config.SPEND_LOG_PATH.exists():
        return result
    tools_used: set[str] = set()
    for line in config.SPEND_LOG_PATH.read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("run_id") != run_id:
            continue
        result["total_cost_usd"] += entry.get("cost_usd", 0.0)
        result["total_turns"] += entry.get("num_turns", 0)
        result["total_duration_ms"] += entry.get("duration_ms", 0)
        result["had_errors"] = result["had_errors"] or entry.get("is_error", False)
        result["total_input_tokens"] += entry.get("input_tokens", 0)
        result["total_output_tokens"] += entry.get("output_tokens", 0)
        result["total_cache_creation_input_tokens"] += entry.get("cache_creation_input_tokens", 0)
        result["total_cache_read_input_tokens"] += entry.get("cache_read_input_tokens", 0)
        tools_used.update(entry.get("tools_used", []) or [])
        result["stages"].append(entry)
    result["total_cost_usd"] = round(result["total_cost_usd"], 4)
    cache_total = result["total_cache_creation_input_tokens"] + result["total_cache_read_input_tokens"]
    if cache_total > 0:
        result["cache_hit_ratio"] = round(result["total_cache_read_input_tokens"] / cache_total, 4)
    result["tools_used"] = sorted(tools_used)
    return result
