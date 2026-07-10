"""
deck_engine/claude_cli.py — thin wrapper around headless `claude -p`.

JSON envelope schema confirmed against a REAL authenticated call on Trevor's
Mac (2026-07-01, Claude Code CLI 2.1.197, --output-format stream-json
--include-partial-messages --verbose --json-schema '...'). Full transcript
saved at deck_engine/tests/real_stream_capture.jsonl and used as a test
fixture. Key findings from that capture, which changed this module:

  - `stream-json` requires `--verbose` alongside it (the CLI errors without
    it) — confirmed by trial on Trevor's Mac.
  - `--json-schema` does NOT constrain the reply text directly — the model
    writes a normal free-text response first, THEN calls an internal
    `StructuredOutput` tool whose `input` is the actual schema-shaped data.
    This costs 2 turns, not 1, for what looks like a single call — expect
    num_turns >= 2 even for the simplest structured-output request. The
    free-text response is exactly the "thinking steps" surfaced in
    live_view.py.
  - The final `{"type":"result",...}` envelope has a top-level
    `structured_output` field — ALREADY a parsed dict/list, not a string
    needing json.loads(). Preferred in ClaudeResult.parsed_json().
  - Cost driver worth knowing: a fresh `claude -p` subprocess with no prior
    session pays ~21K `cache_creation_input_tokens` for its own system
    prompt / tool definitions (28 tools, 5 MCP servers, memory paths, etc.)
    on the FIRST call of that process — and since every stage in this
    pipeline is an independent subprocess (no --resume, no shared session),
    each of the ~8-10 calls per run pays this independently. Mitigated below
    with --strict-mcp-config and --disable-slash-commands (both auth-
    agnostic). NOT using --bare: its help text is explicit that it "strictly"
    requires ANTHROPIC_API_KEY/apiKeyHelper and "OAuth and keychain are
    never read" — Trevor authenticates via Claude Pro subscription (OAuth),
    so --bare would break every call outright. The exact token savings from
    --strict-mcp-config/--disable-slash-commands are STILL INCONCLUSIVE after
    a real isolated check on Trevor's Mac (2026-07-01): a single flagged call
    showed cache_creation_input_tokens=21297 (essentially unchanged from the
    21028 baseline captured without the flags) but ALSO
    cache_read_input_tokens=20491 (zero in the unflagged baseline) — some
    caching is clearly being reused across independent `claude -p`
    subprocesses within the same session window (not something we expected;
    server-side prompt caching apparently isn't strictly tied to --resume),
    but the flags don't obviously shrink the fixed per-call overhead the way
    the original hypothesis assumed. Not enough data from one ad-hoc call to
    conclude anything cleanly — spend_log now captures the full usage/cache
    breakdown per call (see spend_log.py) specifically so this can be
    measured properly across a real run instead of guessed at.

`--dangerously-skip-permissions` is used deliberately: this runs unattended
overnight with no human present to approve tool-use permission prompts.

TOOL USE IS UNRESTRICTED, DELIBERATELY (decided 2026-07-01): nothing here
passes --allowedTools/--disallowedTools, so WebSearch/WebFetch (core tools,
not MCP-server-based — --strict-mcp-config doesn't touch them) are available
at every stage if the model decides to use them, including build/optimize
where the shipped card list is decided. Trevor was offered a narrower option
(web search only at select/ideate for inspiration, hard-blocked everywhere
else) and explicitly chose "let the models decide whenever they wanna web
search" instead. Given that, TOOLS ACTUALLY USED PER CALL ARE NOW TRACKED
(see tools_used below) specifically so this isn't a silent black box — the
8-turn build call on the Old Rutstein run (2026-07-01, vs. every other run's
2-turn build calls) is exactly the kind of anomaly this should have been
able to explain and couldn't, because nothing recorded what happened beyond
the aggregate turn/cost numbers.

STREAMING: run() now always requests stream-json + --include-partial-messages
and reads the subprocess's stdout line by line (one JSON object per line),
rather than blocking for one final blob (the old --output-format json
behaviour). This is what makes the live terminal view (live_view.py)
possible, and is also just a better subprocess pattern regardless of the
view — stdout and stderr are drained concurrently on separate threads to
avoid the exact pipe-deadlock bug already hit once in this project's history
(see project-gishath memory, "P5" / gishath-local-v2's engine subprocess).
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, spend_log

_active_view = None  # set via set_live_view() by deck_engine/run.py; None means "no view, behave as before"


def set_live_view(view) -> None:
    """Register (or clear, with None) the LiveView every run() call should report to."""
    global _active_view
    _active_view = view


def has_live_view() -> bool:
    return _active_view is not None


# Runs the Atelier UI has asked to abandon. Checked (like the crucible cap)
# before EACH call spawns — the in-flight call finishes normally, then the
# next one raises into run.py's standard failure path. A set, not a bool,
# so a stale cancel from a finished run can never bleed into the next one.
_cancelled_runs: set[str] = set()


def request_cancel(run_id: str) -> None:
    _cancelled_runs.add(run_id)


def cancel_requested(run_id: str) -> bool:
    return run_id in _cancelled_runs


class ClaudeCLIError(RuntimeError):
    """Raised for any failure to get a usable result out of `claude -p`."""


@dataclass
class ClaudeResult:
    text: str          # `result` field — raw text, or a JSON string if json_schema was supplied
    is_error: bool
    num_turns: int
    cost_usd: float
    duration_ms: int
    session_id: str
    raw: dict
    usage: dict         # raw `usage` block from the final envelope — input/output/cache tokens (see spend_log.py)
    tools_used: list[str]  # every distinct tool the model invoked this call, EXCLUDING StructuredOutput
                            # (that's the schema-response mechanism every call uses, not a meaningful signal)

    def parsed_json(self) -> dict:
        """Structured output from a json_schema=... call. Prefers the envelope's
        top-level `structured_output` field (already parsed by Claude Code from
        the internal StructuredOutput tool call — confirmed real via a live
        capture, see module docstring); falls back to parsing `text` as JSON
        for older CLI versions or a --json-schema-less call that still
        happens to return JSON."""
        structured = self.raw.get("structured_output")
        if structured is not None:
            return structured
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as exc:
            raise ClaudeCLIError(f"Expected JSON output but got: {self.text[:500]}") from exc


def _feed_view(handle, event: dict) -> None:
    """Translates one parsed stream-json line into a live_view update. Silently
    ignores event shapes it doesn't recognise — this must never be the reason
    a real pipeline run fails."""
    try:
        if event.get("type") != "stream_event":
            return
        inner = event.get("event", {})
        itype = inner.get("type")
        if itype == "content_block_delta":
            delta = inner.get("delta", {})
            if delta.get("type") == "text_delta":
                handle.append_text(delta.get("text", ""))
            elif delta.get("type") == "thinking_delta":
                # Extended-thinking stream (enabled via MAX_THINKING_TOKENS in
                # run() when config.THINKING_BUDGET_TOKENS > 0). What arrives
                # depends on the model — confirmed with a real sonnet capture on
                # Trevor's Mac, 2026-07-07:
                #   - haiku streams the actual reasoning text in delta["thinking"]
                #   - sonnet/opus stream REDACTED deltas: delta["thinking"] is
                #     always "" and only delta["estimated_tokens"] (a running
                #     counter) is populated — the raw reasoning of Claude 4/5-era
                #     models is never sent over the wire, so a text stream for
                #     those tiers is structurally impossible, not a plumbing bug.
                # For the redacted case, surface the counter as a ticking status
                # line so a long think (build calls have sat >12 min in real
                # runs) reads as visible progress instead of a hang.
                chunk = delta.get("thinking", "")
                if chunk:
                    handle.append_thinking(chunk)
                else:
                    est = delta.get("estimated_tokens")
                    if est:
                        handle.set_status(f"thinking it through... ~{est:,} tokens")
        elif itype == "content_block_start":
            block = inner.get("content_block", {})
            if block.get("type") == "thinking":
                handle.set_status("thinking it through...")
            elif block.get("type") == "text":
                # First visible-reply block (often right after a thinking block
                # ends) — flip the status so the foot line tracks the shift.
                handle.set_status("drafting...")
            elif block.get("type") == "tool_use":
                name = block.get("name")
                if name == "StructuredOutput":
                    handle.set_status("packaging structured output...")
                elif name:
                    # Any other tool (WebSearch, WebFetch, Bash, etc.) — tool use is
                    # unrestricted here (see module docstring), so make it visible
                    # rather than a silent black box when it happens.
                    handle.set_status(f"using tool: {name}...")
    except Exception:  # noqa: BLE001 — a display glitch must never break the actual pipeline call
        pass


def _track_tool_use(event: dict, tools_used: set[str]) -> None:
    """Records every distinct tool the model invoked during this call. Runs
    unconditionally (unlike _feed_view, which only matters when a live view is
    attached) so spend_log has this data for every call, live view or not —
    this is what would have explained the Old Rutstein run's 8-turn build
    call (see module docstring) if it had existed at the time."""
    try:
        if event.get("type") != "stream_event":
            return
        inner = event.get("event", {})
        if inner.get("type") != "content_block_start":
            return
        block = inner.get("content_block", {})
        if block.get("type") != "tool_use":
            return
        name = block.get("name")
        if name and name != "StructuredOutput":
            tools_used.add(name)
    except Exception:  # noqa: BLE001 — diagnostics must never be the reason a real call fails
        pass


def _track_stream_stats(event: dict, stats: dict) -> None:
    """Splits a call's output into its three components — visible narration
    text, extended thinking, and structured output — so spend_log can say
    WHERE output tokens went, not just how many there were (2026-07-10: the
    narration-vs-thinking attribution question was unanswerable from
    output_tokens alone). Chars, not tokens, for the text streams (deltas
    don't carry token counts); thinking_est_tokens comes from the redacted
    stream's own running estimated_tokens counter, so for sonnet/opus it IS
    a token figure."""
    try:
        if event.get("type") != "stream_event":
            return
        inner = event.get("event", {})
        if inner.get("type") != "content_block_delta":
            return
        delta = inner.get("delta", {})
        dtype = delta.get("type")
        if dtype == "text_delta":
            stats["narration_chars"] += len(delta.get("text", ""))
        elif dtype == "thinking_delta":
            stats["thinking_chars"] += len(delta.get("thinking", ""))
            est = delta.get("estimated_tokens")
            if est:
                # A running counter, not an increment — keep the latest (max).
                stats["thinking_est_tokens"] = max(stats["thinking_est_tokens"], int(est))
    except Exception:  # noqa: BLE001 — diagnostics must never be the reason a real call fails
        pass


def run(
    prompt: str,
    *,
    run_id: str,
    stage: str,
    model_tier_key: str,
    json_schema: dict | None = None,
    cwd: Path | None = None,
    timeout_s: float = 900.0,
    disallowed_tools: list[str] | None = None,
    resume_session_id: str | None = None,
) -> ClaudeResult:
    """Invoke `claude -p` headlessly once (streaming under the hood) and return a parsed ClaudeResult.

    Args:
        run_id:         one nightly run's identifier (threaded through every
                         stage so spend_log.summarize_run(run_id) can total
                         actual cost/turns for that run — see PRD §4f).
        stage:          free-text label for the spend log AND the live-view
                         panel title, e.g. "draft/attempt1/2".
        model_tier_key: key into config.MODEL_TIERS (e.g. "draft", "optimize")
                         — NOT a raw model name, so retuning a stage's model
                         is a one-line config edit, not a call-site hunt.
        json_schema:    optional JSON Schema dict — passed to `--json-schema`
                         for structured output. Use ClaudeResult.parsed_json().
        disallowed_tools: PRD v4 amendment T5 — passed to `--disallowedTools`
                         when set (e.g. config.DISALLOWED_SEARCH_TOOLS at
                         build/validate_repair/optimize/card_tagger call
                         sites). None/empty means no restriction, unchanged
                         from pre-v4 behaviour. NOT yet verified against the
                         real CLI's exact flag syntax from this sandbox (no
                         authenticated `claude` session here, same class of
                         gap as claude_cli.py's other real-CLI-shape notes
                         above) — confirm on Trevor's Mac before trusting this
                         beyond the mocked tests.
        resume_session_id: PRD v4 amendment T6 — passed to `--resume` when
                         set. Scaffolding for an instrumented A/B experiment
                         (config.RESUME_SESSION_CHAINING); callers should gate
                         actually passing a non-None value behind that flag so
                         default behaviour (independent subprocesses, no
                         --resume) is completely unchanged unless Trevor
                         explicitly opts into the experiment.
    """
    model = config.MODEL_TIERS.get(model_tier_key)
    if not model:
        raise ValueError(f"Unknown model tier key: {model_tier_key!r} (check config.MODEL_TIERS)")

    if cancel_requested(run_id):
        raise ClaudeCLIError(f"[{stage}] run cancelled by user — halting before this call")

    # Crucible cap (Atelier "Guild rules"): halt BEFORE spawning the next call
    # once this run's logged spend reaches config.MAX_RUN_SPEND_USD. Raising
    # here lands in run.py's normal failure path — error email + run_log entry
    # — so a halt is never silent. 0 (the default) disables the check.
    cap = float(getattr(config, "MAX_RUN_SPEND_USD", 0) or 0)
    if cap > 0:
        spent = spend_log.summarize_run(run_id)["total_cost_usd"]
        if spent >= cap:
            raise ClaudeCLIError(
                f"[{stage}] crucible cap reached: ${spent:.4f} already spent this run "
                f">= ${cap:.2f} cap — halting before this call"
            )

    cmd = [
        config.CLAUDE_BIN, "-p", prompt,
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--model", model,
        "--dangerously-skip-permissions",
        "--strict-mcp-config",       # skip loading Trevor's other MCP servers (IBKR/Calendar/Drive/365/Gmail) — never needed here
        "--disable-slash-commands",  # skip skill loading — this pipeline never invokes a skill
    ]
    if json_schema is not None:
        cmd += ["--json-schema", json.dumps(json_schema)]
    if disallowed_tools:
        cmd += ["--disallowedTools", *disallowed_tools]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]

    handle = _active_view.start_call(stage, model) if _active_view is not None else None

    # Extended thinking: the CLI reads MAX_THINKING_TOKENS from its environment
    # (no dedicated flag) — set it per-subprocess so nothing leaks into the
    # parent process or any other tool this app shells out to. Budget is
    # per-model (config.THINKING_BUDGET_BY_MODEL): sonnet/opus default 0 —
    # their thinking is redacted on the wire, so it billed as output tokens
    # while showing only a ticking counter next to the narration the prompts
    # already demand. ALWAYS exported, "0" included: the old
    # `env=None when budget == 0` path inherited the parent environment, so a
    # configured 0 deferred to the CLI's own default instead of reliably
    # disabling thinking.
    budgets = getattr(config, "THINKING_BUDGET_BY_MODEL", None) or {}
    thinking_budget = int(budgets.get(model, getattr(config, "THINKING_BUDGET_TOKENS", 0) or 0))
    env = {**os.environ, "MAX_THINKING_TOKENS": str(max(0, thinking_budget))}

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,  # never let claude block waiting on stdin in an unattended run
            text=True,
            bufsize=1,  # line-buffered, so streamed events arrive promptly rather than batched
        )
    except FileNotFoundError as exc:
        raise ClaudeCLIError(
            f"[{stage}] '{config.CLAUDE_BIN}' not found. If run_nightly.sh runs under a "
            "thinner PATH than an interactive shell, set DECK_ENGINE_CLAUDE_BIN to the "
            "absolute path (see `which claude`)."
        ) from exc

    # Drain stderr on its own thread — reading only stdout in the main loop
    # below would deadlock if claude writes enough to stderr to fill its OS
    # pipe buffer while we're not reading it (this exact class of bug already
    # hit gishath-local-v2's own engine subprocess — see app.py's
    # _ensure_engine_healthy() history in project-gishath memory).
    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_chunks.append(line)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    timer = threading.Timer(timeout_s, proc.kill)
    timer.start()
    timed_out = False
    final_envelope: dict | None = None
    tools_used: set[str] = set()
    stream_stats = {"narration_chars": 0, "thinking_chars": 0, "thinking_est_tokens": 0}
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # stray non-JSON noise on stdout — ignore rather than fail the whole call
            if event.get("type") == "result":
                final_envelope = event
                continue
            _track_tool_use(event, tools_used)
            _track_stream_stats(event, stream_stats)
            if handle is not None:
                _feed_view(handle, event)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        # threading.Timer.finished is set BOTH when cancel() is called and when
        # the timer's own function (proc.kill) has already fired — checking it
        # here, before we call cancel() ourselves, is what distinguishes the
        # two: if it's already set, the timeout genuinely fired on its own.
        timed_out = timer.finished.is_set()
        timer.cancel()
        stderr_thread.join(timeout=5)

    stderr_output = "".join(stderr_chunks)

    if final_envelope is None:
        if handle is not None:
            handle.finish(cost_usd=0.0, num_turns=0, duration_s=0.0, is_error=True)
        if timed_out:
            raise ClaudeCLIError(f"[{stage}] claude -p timed out after {timeout_s:.0f}s")
        raise ClaudeCLIError(
            f"[{stage}] claude -p produced no final result event (exit {proc.returncode}). "
            f"stderr: {stderr_output[:500]}"
        )

    usage = final_envelope.get("usage") or {}

    result = ClaudeResult(
        text=final_envelope.get("result", ""),
        is_error=bool(final_envelope.get("is_error", False)),
        num_turns=int(final_envelope.get("num_turns", 0) or 0),
        cost_usd=float(final_envelope.get("total_cost_usd", 0.0) or 0.0),
        duration_ms=int(final_envelope.get("duration_ms", 0) or 0),
        session_id=final_envelope.get("session_id", ""),
        raw=final_envelope,
        usage=usage,
        tools_used=sorted(tools_used),
    )

    if handle is not None:
        handle.finish(cost_usd=result.cost_usd, num_turns=result.num_turns,
                       duration_s=result.duration_ms / 1000, is_error=result.is_error)

    if config.LOG_SPEND_PER_RUN:
        spend_log.record(
            run_id=run_id, stage=stage, model=model, cost_usd=result.cost_usd,
            num_turns=result.num_turns, duration_ms=result.duration_ms,
            is_error=result.is_error, session_id=result.session_id,
            # Full token/cache breakdown — added 2026-07-01 specifically to stop
            # guessing whether --strict-mcp-config/--disable-slash-commands (or
            # any future cost change) actually helps. See module docstring for
            # the inconclusive one-off check that motivated this.
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            # Added 2026-07-01 alongside the "let the models decide" call on tool
            # access — see module docstring. Empty list on every call unless the
            # model actually reached for something beyond StructuredOutput.
            tools_used=result.tools_used,
            # Output-token attribution (2026-07-10): narration vs thinking vs
            # structured output, so per-stage diet changes are measurable from
            # the log instead of inferred. See _track_stream_stats.
            narration_chars=stream_stats["narration_chars"],
            thinking_chars=stream_stats["thinking_chars"],
            thinking_est_tokens=stream_stats["thinking_est_tokens"],
            structured_chars=len(json.dumps(final_envelope.get("structured_output")))
            if final_envelope.get("structured_output") is not None else 0,
        )

    if result.is_error:
        raise ClaudeCLIError(f"[{stage}] claude -p returned is_error=true: {result.text[:500]}")

    return result
