"""Tests for the local LLM backend (local_llm.py + claude_cli's "local" tier
dispatch + config's DECK_ENGINE_LOCAL_MODE switch). All HTTP is mocked — no
LM Studio needed.

Run manually: cd gishath-local-v2 && python3 -m deck_engine.tests.test_local_llm
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

from .. import claude_cli, config, local_llm


def _sse(*events: str) -> io.BytesIO:
    """Build a fake urlopen response body from SSE data lines."""
    body = "".join(f"data: {e}\n\n" for e in events)
    return io.BytesIO(body.encode("utf-8"))


class _FakeResponse:
    """Minimal stand-in for urlopen's response: iterable lines + context manager."""

    def __init__(self, body: io.BytesIO):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._body.readlines())


def _chunk(content: str | None = None, reasoning: str | None = None,
           finish: str | None = None, usage: dict | None = None) -> str:
    delta: dict = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    payload: dict = {"choices": [{"delta": delta, "finish_reason": finish}]}
    if usage is not None:
        payload["usage"] = usage
    return json.dumps(payload)


def test_extract_json() -> list[str]:
    problems = []
    if local_llm._extract_json('{"a": 1}') != {"a": 1}:
        problems.append("plain JSON should parse")
    if local_llm._extract_json('<think>hmm\nlands...</think>\n{"a": 1}') != {"a": 1}:
        problems.append("inline <think> block should be stripped before parsing")
    if local_llm._extract_json('```json\n{"a": 1}\n```') != {"a": 1}:
        problems.append("markdown fences should be stripped")
    if local_llm._extract_json('Here you go: {"a": 1}') != {"a": 1}:
        problems.append("stray preamble before the outermost {...} should be tolerated")
    try:
        local_llm._extract_json("no json here at all")
        problems.append("garbage should raise LocalLLMError")
    except local_llm.LocalLLMError:
        pass
    return problems


def test_stream_chat_parses_sse() -> list[str]:
    problems = []
    events = [
        _chunk(reasoning="thinking about ramp"),
        _chunk(reasoning=" and lands"),
        _chunk(content='{"cards": '),
        _chunk(content='["Sol Ring"]}'),
        _chunk(finish="stop"),
        _chunk(usage={"prompt_tokens": 1200, "completion_tokens": 340}),
        "[DONE]",
    ]
    thinking_chunks: list[str] = []
    text_chunks: list[str] = []
    with mock.patch.object(local_llm.urllib.request, "urlopen",
                            return_value=_FakeResponse(_sse(*events))) as fake_open:
        reply = local_llm.stream_chat(
            "build me a deck", model_id="test-model",
            json_schema={"type": "object"}, timeout_s=30,
            on_text=text_chunks.append, on_thinking=thinking_chunks.append,
        )
    if reply["structured"] != {"cards": ["Sol Ring"]}:
        problems.append(f"structured output wrong: {reply['structured']}")
    if reply["text"] != '{"cards": ["Sol Ring"]}':
        problems.append(f"text wrong: {reply['text']!r}")
    if "".join(thinking_chunks) != "thinking about ramp and lands":
        problems.append(f"thinking callback wrong: {thinking_chunks}")
    if "".join(text_chunks) != reply["text"]:
        problems.append("text callback should see every content delta")
    if reply["usage"]["input_tokens"] != 1200 or reply["usage"]["output_tokens"] != 340:
        problems.append(f"usage mapping wrong: {reply['usage']}")

    # Request shape: schema call must carry response_format + the steering
    # system message, and the model id.
    req = fake_open.call_args[0][0]
    sent = json.loads(req.data.decode("utf-8"))
    if sent.get("model") != "test-model":
        problems.append(f"model id not sent: {sent.get('model')}")
    if sent.get("response_format", {}).get("type") != "json_schema":
        problems.append("json_schema call should set response_format")
    if sent["messages"][0]["role"] != "system":
        problems.append("json_schema call should inject the steering system message")
    if sent.get("stream_options", {}).get("include_usage") is not True:
        problems.append("stream_options.include_usage should be requested")
    if sent.get("frequency_penalty") != local_llm.config.LOCAL_LLM_FREQUENCY_PENALTY:
        problems.append("frequency_penalty should be sent (loop-breaking, 2026-07-17 draft loop)")
    return problems


def test_stream_chat_failure_modes() -> list[str]:
    problems = []
    # finish_reason=length must raise (truncated JSON is useless downstream).
    events = [_chunk(content='{"cards": ["Sol'), _chunk(finish="length"), "[DONE]"]
    with mock.patch.object(local_llm.urllib.request, "urlopen",
                            return_value=_FakeResponse(_sse(*events))):
        try:
            local_llm.stream_chat("prompt", model_id="m", json_schema={"type": "object"}, timeout_s=30)
            problems.append("finish_reason=length should raise LocalLLMError")
        except local_llm.LocalLLMError as exc:
            if "LOCAL_LLM_MAX_OUTPUT_TOKENS" not in str(exc):
                problems.append(f"length error should name the knob to turn: {exc}")

    # Empty reply must raise, not return a blank ClaudeResult downstream.
    with mock.patch.object(local_llm.urllib.request, "urlopen",
                            return_value=_FakeResponse(_sse("[DONE]"))):
        try:
            local_llm.stream_chat("prompt", model_id="m", timeout_s=30)
            problems.append("empty reply should raise LocalLLMError")
        except local_llm.LocalLLMError:
            pass

    # Server down → LocalLLMError naming the URL, not a raw URLError.
    with mock.patch.object(local_llm.urllib.request, "urlopen",
                            side_effect=local_llm.urllib.error.URLError("connection refused")):
        try:
            local_llm.stream_chat("prompt", model_id="m", timeout_s=30)
            problems.append("unreachable server should raise LocalLLMError")
        except local_llm.LocalLLMError as exc:
            if "LM Studio" not in str(exc):
                problems.append(f"unreachable-server error should mention LM Studio: {exc}")
    return problems


def test_repetition_loop_aborts_early() -> list[str]:
    """A phrase loop in the (grammar-free) reasoning channel must abort with a
    clean LocalLLMError, not stream to the token cap (Ziatora loop, 2026-07-17).
    A legitimate mono-color lands array (~30 repeated basic names) must NOT trip it."""
    problems = []
    phrase = "I will swap Ziatora's Putrid Rats for Ziatora, the Incinerator. "
    events = [_chunk(reasoning=phrase) for _ in range(120)] + ["[DONE]"]
    with mock.patch.object(local_llm.urllib.request, "urlopen",
                            return_value=_FakeResponse(_sse(*events))):
        try:
            local_llm.stream_chat("prompt", model_id="m", json_schema={"type": "object"}, timeout_s=30)
            problems.append("phrase loop should raise LocalLLMError")
        except local_llm.LocalLLMError as exc:
            if "repetition loop" not in str(exc):
                problems.append(f"unexpected error for loop: {exc}")

    # 30 Mountains inside an otherwise-normal reply: legitimate, must pass.
    lands = ", ".join(['"Mountain"'] * 30)
    body = '{"lands": [' + lands + '], "nonlands": ["Lightning Bolt"]}'
    events = ([_chunk(reasoning="planning the burn deck... ")] * 30
              + [_chunk(content=body[i:i+40]) for i in range(0, len(body), 40)]
              + [_chunk(finish="stop"), "[DONE]"])
    with mock.patch.object(local_llm.urllib.request, "urlopen",
                            return_value=_FakeResponse(_sse(*events))):
        try:
            reply = local_llm.stream_chat("prompt", model_id="m", json_schema={"type": "object"}, timeout_s=30)
            if reply["structured"] is None or len(reply["structured"]["lands"]) != 30:
                problems.append(f"mono-basics reply mangled: {reply['structured']}")
        except local_llm.LocalLLMError as exc:
            problems.append(f"mono-basics lands array false-positived the loop detector: {exc}")
    return problems


def test_schema_json_recovered_from_reasoning_channel() -> list[str]:
    """LM Studio + Qwen thinking templates file the grammar-constrained JSON
    under reasoning_content with empty content (live capture 2026-07-17) —
    the reply must be recovered from the reasoning tail, including when the
    deliberation prose itself contains braces (mana symbols)."""
    problems = []
    events = [
        _chunk(reasoning='Weighing ramp: {T}: Add {G} is a classic... '),
        _chunk(reasoning='{"cards": ["Sol'),
        _chunk(reasoning=' Ring"]}'),
        _chunk(finish="stop"),
        _chunk(usage={"prompt_tokens": 50, "completion_tokens": 30}),
        "[DONE]",
    ]
    with mock.patch.object(local_llm.urllib.request, "urlopen",
                            return_value=_FakeResponse(_sse(*events))):
        reply = local_llm.stream_chat("prompt", model_id="m",
                                       json_schema={"type": "object"}, timeout_s=30)
    if reply["structured"] != {"cards": ["Sol Ring"]}:
        problems.append(f"should recover JSON from reasoning tail: {reply['structured']}")

    # Reasoning with NO trailing JSON must still raise, not return junk.
    events = [_chunk(reasoning="just thinking, no json"), _chunk(finish="stop"), "[DONE]"]
    with mock.patch.object(local_llm.urllib.request, "urlopen",
                            return_value=_FakeResponse(_sse(*events))):
        try:
            local_llm.stream_chat("prompt", model_id="m", json_schema={"type": "object"}, timeout_s=30)
            problems.append("reasoning without JSON should raise LocalLLMError")
        except local_llm.LocalLLMError:
            pass
    return problems


def test_run_dispatches_local_tier() -> list[str]:
    """claude_cli.run() with a 'local' tier must route to local_llm, never spawn
    a subprocess, and log a cost-0 spend record with the same field shape as a
    cloud call."""
    problems = []
    fake_reply = {
        "text": '{"winner": 2}',
        "structured": {"winner": 2},
        "usage": {"input_tokens": 900, "output_tokens": 120,
                   "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        "duration_ms": 4200,
        "finish_reason": "stop",
    }
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(config, "SPEND_LOG_PATH", Path(tmp) / "spend.jsonl"), \
             mock.patch.dict(config.MODEL_TIERS, {"judge": "local:qwen3.6-35b"}), \
             mock.patch.object(local_llm, "stream_chat", return_value=fake_reply) as fake_chat, \
             mock.patch.object(claude_cli.subprocess, "Popen",
                                side_effect=AssertionError("local tier must not spawn a subprocess")):
            result = claude_cli.run("pick a winner", run_id="t-local", stage="judge/attempt1",
                                     model_tier_key="judge", json_schema={"type": "object"})
        if result.parsed_json() != {"winner": 2}:
            problems.append(f"parsed_json wrong: {result.parsed_json()}")
        if result.cost_usd != 0.0:
            problems.append(f"local call must cost $0, got {result.cost_usd}")
        if fake_chat.call_args.kwargs.get("model_id") != "qwen3.6-35b":
            problems.append(f"'local:<id>' tier should pass the id through: {fake_chat.call_args.kwargs}")
        # Effective timeout must be the larger of caller's (900 default) and config floor.
        if fake_chat.call_args.kwargs.get("timeout_s") != max(900.0, config.LOCAL_LLM_TIMEOUT_S):
            problems.append(f"timeout floor not applied: {fake_chat.call_args.kwargs.get('timeout_s')}")
        entries = [json.loads(l) for l in (Path(tmp) / "spend.jsonl").read_text().splitlines()]
        if len(entries) != 1:
            problems.append(f"expected exactly one spend record, got {len(entries)}")
        else:
            e = entries[0]
            checks = [
                ("model", e["model"] == "local:qwen3.6-35b"),
                ("cost_usd", e["cost_usd"] == 0.0),
                ("input_tokens", e["input_tokens"] == 900),
                ("output_tokens", e["output_tokens"] == 120),
                ("structured_chars", e["structured_chars"] == len(json.dumps({"winner": 2}))),
            ]
            failed = [label for label, ok in checks if not ok]
            if failed:
                problems.append(f"spend record fields wrong: {failed} in {e}")
    return problems


def test_run_local_error_becomes_claude_cli_error() -> list[str]:
    problems = []
    with mock.patch.dict(config.MODEL_TIERS, {"judge": "local"}), \
         mock.patch.object(config, "LOG_SPEND_PER_RUN", False), \
         mock.patch.object(local_llm, "stream_chat",
                            side_effect=local_llm.LocalLLMError("server not running")):
        try:
            claude_cli.run("prompt", run_id="t-err", stage="judge/attempt1", model_tier_key="judge")
            problems.append("LocalLLMError should surface as ClaudeCLIError")
        except claude_cli.ClaudeCLIError as exc:
            if "judge/attempt1" not in str(exc):
                problems.append(f"error should carry the stage label: {exc}")
    return problems


def test_rebalance_mana_enforces_ceilings() -> list[str]:
    """Run 45813889 shipped 47 lands + 24 ramp ("a flood machine" — Trevor's
    review, 2026-07-17): _rebalance_mana must trim to the quota ceilings and
    refill from the pool, never changing the card count."""
    import json as _json
    from types import SimpleNamespace
    from .. import agent_pipeline as ap
    problems = []
    cache = _json.loads((config.SCRYFALL_CACHE_PATH).read_text())
    concept = SimpleNamespace(commander="Korvold, Fae-Cursed King", color_identity=["B", "G", "R"])
    deck = (["Swamp"] * 16 + ["Mountain"] * 15 + ["Forest"] * 16   # 47 lands, all basics
            + ["Worn Powerstone", "Commander's Sphere", "Thought Vessel", "Mind Stone",
               "Fellwar Stone", "Wayfarer's Bauble", "Cultivate", "Kodama's Reach",
               "Rampant Growth", "Farseek", "Nature's Lore", "Three Visits",
               "Sol Ring", "Arcane Signet", "Llanowar Elves", "Birds of Paradise"]  # 16 ramp
            + ["Mayhem Devil", "Viscera Seer", "Grim Haruspex", "Bitterblossom",
               "Ophiomancer", "Pitiless Plunderer", "Skullclamp", "Blood Artist",
               "Zulaport Cutthroat", "Judith, the Scourge Diva", "Korvold's Fury",
               "Dictate of Erebos", "Grave Pact", "Deathreap Ritual", "Moldervine Reclamation",
               "Beast Within", "Chaos Warp", "Assassin's Trophy", "Abrupt Decay",
               "Terminate", "Vandalblast", "Toxic Deluge", "Blasphemous Act",
               "Village Rites", "Deadly Dispute", "Costly Plunder", "Vampiric Tutor",
               "Demonic Tutor", "Eternal Witness", "Reanimate", "Victimize",
               "Living Death", "Meren of Clan Nel Toth", "Mazirek, Kraul Death Priest",
               "Savra, Queen of the Golgari", "Poison-Tip Archer"])  # 36 nonland spells
    fake_pool = ["Nadier's Nightblade", "Marionette Master", "Reckless Fireweaver",
                 "Jadar, Ghoulcaller of Nephalia", "Tendershoot Dryad", "Pawn of Ulamog",
                 "Sifter of Skulls", "Yawgmoth, Thran Physician", "Woe Strider",
                 "Ayara, First of Locthwain", "Chittering Witch", "Ogre Slumlord",
                 "Mahadi, Emporium Master", "Juri, Master of the Revue", "Bastion of Remembrance",
                 "Mirkwood Bats", "Hissing Iguanar", "Vindictive Vampire",
                 "Falkenrath Noble", "Cruel Celebrant", "Elas il-Kor, Sadistic Pilgrim",
                 "Braids, Arisen Nightmare", "Chatterfang, Squirrel General", "Academy Manufactor"]
    with mock.patch.object(ap.edhrec_pool, "fetch_pool", return_value=fake_pool):
        out, notes = ap._rebalance_mana(concept, deck, cache)
    land_max = config.ROLE_QUOTA_DEFAULTS["land_max"]
    ramp_max = config.ROLE_QUOTA_DEFAULTS["ramp_max"]
    lands = [c for c in out if "Land" in (cache.get(c.lower()) or {}).get("type_line", "")]
    from .. import scryfall_cache as sc
    ramp = [c for c in out if c.lower() in cache and c not in lands and sc.is_ramp_card(cache[c.lower()])]
    if len(out) != len(deck):
        problems.append(f"card count changed: {len(deck)} -> {len(out)}")
    if len(lands) > land_max:
        problems.append(f"lands still over ceiling: {len(lands)} > {land_max}")
    if len([c for c in ramp if c in ap._RAMP_CUT_ORDER]) and len(ramp) > ramp_max:
        problems.append(f"generic ramp left while over ceiling: {len(ramp)}")
    if "Sol Ring" not in out or "Llanowar Elves" not in out:
        problems.append("auto-keeps (Sol Ring / dorks) must never be cut")
    if not any(c in out for c in fake_pool):
        problems.append("freed slots should refill from the pool")
    return problems


def test_local_mode_switch() -> list[str]:
    """DECK_ENGINE_LOCAL_MODE=1 flips deck-content stages to 'local', leaves
    simulate/coach_* alone, and respects explicit per-stage env overrides."""
    problems = []
    original = dict(config.MODEL_TIERS)
    try:
        with mock.patch.dict(os.environ, {"DECK_ENGINE_LOCAL_MODE": "1",
                                            "DECK_ENGINE_JUDGE_MODEL": "opus"}):
            config._apply_local_mode()
            for stage in ("select", "draft", "validate_repair", "optimize", "card_tagger"):
                if config.MODEL_TIERS[stage] != "local":
                    problems.append(f"{stage} should flip to local, got {config.MODEL_TIERS[stage]}")
            if config.MODEL_TIERS["judge"] != original["judge"]:
                problems.append("explicit DECK_ENGINE_JUDGE_MODEL should survive local mode")
            for stage in ("simulate", "coach_orders", "coach_turn"):
                if config.MODEL_TIERS[stage] != original[stage]:
                    problems.append(f"{stage} must stay on its cloud tier in local mode")
        # Switch off → no-op even with tiers reset.
        config.MODEL_TIERS.update(original)
        with mock.patch.dict(os.environ, {"DECK_ENGINE_LOCAL_MODE": ""}):
            config._apply_local_mode()
            if config.MODEL_TIERS != original:
                problems.append("local mode off should change nothing")
    finally:
        config.MODEL_TIERS.clear()
        config.MODEL_TIERS.update(original)
    return problems


def main() -> int:
    tests = [
        test_extract_json,
        test_stream_chat_parses_sse,
        test_stream_chat_failure_modes,
        test_schema_json_recovered_from_reasoning_channel,
        test_repetition_loop_aborts_early,
        test_run_dispatches_local_tier,
        test_run_local_error_becomes_claude_cli_error,
        test_rebalance_mana_enforces_ceilings,
        test_local_mode_switch,
    ]
    failures = 0
    for t in tests:
        problems = t()
        if problems:
            failures += 1
            print(f"FAILED {t.__name__}:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
        else:
            print(f"ok {t.__name__}")
    if failures:
        return 1
    print("OK: local backend dispatch, SSE parsing, spend logging, and local-mode switch all behave.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
