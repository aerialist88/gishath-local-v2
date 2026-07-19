"""
deck_engine/local_llm.py — streaming client for a local OpenAI-compatible
server (LM Studio by default; anything speaking /v1/chat/completions works).

This is the "local" backend behind claude_cli.run(): when a stage's entry in
config.MODEL_TIERS is "local" (or "local:<model-id>"), claude_cli dispatches
here instead of spawning `claude -p`. All the run bookkeeping — cancel checks,
crucible cap, live-view handle, spend_log — stays in claude_cli._run_local();
this module ONLY talks HTTP and knows nothing about the pipeline, so it has no
imports from the rest of deck_engine except config.

Differences from the `claude -p` path that callers should understand:

  - NO TOOLS. The local server has no WebSearch/WebFetch/Bash — a prompt that
    invites the model to search simply gets a model that reasons from what's
    in the prompt. disallowed_tools becomes a no-op (there is nothing to
    disallow). This is fine for this pipeline: every deck-content stage is
    grounded in oracle text included in the prompt.
  - STRUCTURED OUTPUT IS GRAMMAR-ENFORCED, not a tool call. `claude -p`'s
    --json-schema lets the model narrate free text first and then call an
    internal StructuredOutput tool; an OpenAI-style response_format
    json_schema constrains the ENTIRE reply to schema-shaped JSON. The
    prompts' "narrate out loud" instructions therefore can't land in the
    reply text — a system message (below) steers that deliberation into the
    model's reasoning channel instead, which streams to the live view as
    thinking (an upgrade over sonnet/opus, whose thinking is redacted).
  - Reasoning deltas arrive as delta["reasoning_content"] (LM Studio) or
    delta["reasoning"] (some builds/servers); both are handled. Some models
    inline <think>...</think> in content instead — _extract_json() strips
    those before parsing structured output.
  - usage arrives (if at all) on the final chunk when stream_options
    include_usage is set; mapped to the anthropic-style keys spend_log
    expects, with cache fields left 0.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from . import config


class LocalLLMError(RuntimeError):
    """Raised for any failure to get a usable reply out of the local server."""


# Injected as the system message on json_schema calls — see module docstring.
_SCHEMA_SYSTEM_PROMPT = (
    "Your reply must be ONLY a JSON object matching the response schema you have "
    "been given — no prose, no markdown fences, nothing before or after it. Any "
    "instruction in the prompt to narrate, deliberate, or 'call the structured-"
    "output tool' refers to your private reasoning: think out loud there, then "
    "reply with only the JSON."
)


def resolve_model_id(tier_value: str) -> str:
    """'local' -> config.LOCAL_LLM_MODEL (may be '', meaning whatever model the
    server has loaded); 'local:foo/bar' -> 'foo/bar' (per-stage override, so a
    future small-model tier can coexist with the big drafter)."""
    if ":" in tier_value:
        return tier_value.split(":", 1)[1]
    return config.LOCAL_LLM_MODEL


def _extract_json(text: str) -> dict | list:
    """Parse a schema-constrained reply, tolerating the two decorations local
    models actually add in practice: an inline <think>...</think> block (models
    whose server doesn't split reasoning into its own field) and markdown
    fences. Raises LocalLLMError if no JSON can be recovered."""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Last resort: the outermost {...} span (a stray preamble despite the
    # grammar — seen when a server silently ignores response_format).
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise LocalLLMError(f"local model reply is not valid JSON: {cleaned[:500]}")




def _extract_trailing_json(text: str):
    """Recover a JSON object that ends a block of free text (the reasoning
    stream's tail). Walks '{' positions backwards with raw_decode, accepting
    the earliest start whose parse consumes through the end of the text —
    which is exactly the outermost object, and is immune to braces inside
    prose or JSON strings (mana symbols like {T} in card discussion).
    Returns None if no such object exists."""
    stripped = text.rstrip()
    if not stripped.endswith("}"):
        return None
    decoder = json.JSONDecoder()
    result = None
    for pos in range(len(stripped) - 1, -1, -1):
        if stripped[pos] != "{":
            continue
        try:
            obj, end = decoder.raw_decode(stripped[pos:])
        except json.JSONDecodeError:
            continue
        if pos + end == len(stripped):
            result = obj  # keep widening: an earlier '{' that also parses to the end is more complete
        elif result is not None:
            break  # past the outermost start — done
    return result



def stream_chat(
    prompt: str,
    *,
    model_id: str,
    json_schema: dict | None = None,
    timeout_s: float = 3600.0,
    base_url: str | None = None,
    on_text=None,
    on_thinking=None,
) -> dict:
    """One streaming chat-completion call. Returns
    {"text", "structured", "usage", "duration_ms", "finish_reason"} where
    `structured` is the parsed JSON (json_schema calls only, else None) and
    `usage` uses spend_log's anthropic-style key names.

    timeout_s is enforced two ways: as the socket timeout (covers a stalled
    server AND the long silent prefill before the first token — minutes on a
    30k-token draft prompt against Air-class hardware) and as a total
    wall-clock guard checked between chunks.
    """
    url = (base_url or config.LOCAL_LLM_BASE_URL).rstrip("/") + "/chat/completions"

    messages = []
    if json_schema is not None:
        messages.append({"role": "system", "content": _SCHEMA_SYSTEM_PROMPT})
    messages.append({"role": "user", "content": prompt})

    payload: dict = {
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if model_id:
        payload["model"] = model_id
    max_out = int(getattr(config, "LOCAL_LLM_MAX_OUTPUT_TOKENS", 0) or 0)
    if max_out > 0:
        payload["max_tokens"] = max_out
    # Loop-breaking penalties — see config.LOCAL_LLM_FREQUENCY_PENALTY's note.
    freq = float(getattr(config, "LOCAL_LLM_FREQUENCY_PENALTY", 0) or 0)
    if freq:
        payload["frequency_penalty"] = freq
    pres = float(getattr(config, "LOCAL_LLM_PRESENCE_PENALTY", 0) or 0)
    if pres:
        payload["presence_penalty"] = pres
    temp = getattr(config, "LOCAL_LLM_TEMPERATURE", None)
    if temp is not None:
        payload["temperature"] = float(temp)
    top_p = getattr(config, "LOCAL_LLM_TOP_P", None)
    if top_p is not None:
        payload["top_p"] = float(top_p)
    if json_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "structured_output", "strict": True, "schema": json_schema},
        }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    started = time.monotonic()
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    # Loop detector (Ziatora loop, 2026-07-17): reasoning is outside the JSON
    # grammar's jurisdiction, and sampling penalties alone don't reliably break
    # a collapsed phrase loop. Watch the last 4000 chars of the combined
    # stream; a 160-char exact window recurring 4+ times is a loop (a mono
    # deck's ~30 repeated basic-land entries span <640 chars, safely under).
    loop_window = ""
    loop_checks = 0
    finish_reason = ""
    usage_raw: dict = {}

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            for raw_line in resp:
                if time.monotonic() - started > timeout_s:
                    raise LocalLLMError(f"local call exceeded {timeout_s:.0f}s wall-clock budget")
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue  # stray non-JSON SSE noise — ignore rather than fail the call
                if chunk.get("usage"):
                    usage_raw = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}
                reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                if reasoning:
                    reasoning_parts.append(reasoning)
                    if on_thinking is not None:
                        on_thinking(reasoning)
                content = delta.get("content") or ""
                if content:
                    text_parts.append(content)
                    if on_text is not None:
                        on_text(content)
                if reasoning or content:
                    loop_window = (loop_window + reasoning + content)[-4000:]
                    loop_checks += 1
                    if loop_checks % 40 == 0 and len(loop_window) >= 1500:
                        probe = loop_window[-160:]
                        if loop_window.count(probe) >= 4:
                            raise LocalLLMError(
                                "repetition loop detected in the model's output stream — "
                                f"aborted early after ~{len(loop_window)} looping chars "
                                f"(sample: {probe[:80]!r})"
                            )
    except urllib.error.URLError as exc:
        raise LocalLLMError(
            f"cannot reach local LLM server at {url} — is LM Studio running with its "
            f"server started (and a model loaded)? underlying error: {exc}"
        ) from exc
    except TimeoutError as exc:  # socket-level timeout mid-stream
        raise LocalLLMError(f"local LLM server stalled >: no data for {timeout_s:.0f}s") from exc

    text = "".join(text_parts)
    reasoning_text = "".join(reasoning_parts)
    if not text and not reasoning_text:
        raise LocalLLMError("local model returned an empty reply (no content, no reasoning)")
    if finish_reason == "length":
        raise LocalLLMError(
            "local model hit its output-token limit mid-reply (finish_reason=length) — "
            "raise DECK_ENGINE_LOCAL_LLM_MAX_OUTPUT_TOKENS or the model's context length "
            "in LM Studio; a truncated reply would fail JSON parsing anyway"
        )

    structured = None
    if json_schema is not None:
        # LM Studio 0.4.x + Qwen thinking templates: the grammar-constrained
        # JSON is emitted inside the model's think block, so the server files
        # the ENTIRE reply under reasoning_content and content arrives empty
        # (confirmed live against qwen3.6-35b-a3b, 2026-07-17 — /no_think and
        # chat_template_kwargs don't change it). The grammar is still enforced,
        # so when content is empty the reasoning stream IS the reply: recover
        # the JSON from its tail. Tail, not head — on prompts where the model
        # genuinely deliberates first, the JSON grammar kicks in at the end.
        try:
            structured = _extract_json(text)
        except LocalLLMError:
            structured = _extract_trailing_json(reasoning_text)
            if structured is None:
                raise
            text = json.dumps(structured)

    return {
        "text": text,
        "structured": structured,
        "usage": {
            "input_tokens": int(usage_raw.get("prompt_tokens", 0) or 0),
            "output_tokens": int(usage_raw.get("completion_tokens", 0) or 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "duration_ms": int((time.monotonic() - started) * 1000),
        "finish_reason": finish_reason,
    }
