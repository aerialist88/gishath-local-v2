"""atelier/settings.py — "Guild rules" persistence.

Reads/writes deck_engine/state/ui_settings.json, the overlay deck_engine's
config.py applies at import (precedence: code defaults < ui_settings.json <
env vars). GET merges the file with the *effective* config values so the
screen always shows what a run would actually use; PUT validates, writes, and
re-applies to the live config module so the very next UI-launched run obeys
the new rules without a restart.

Some fields are UI-only (nightly bell time/toggles) — the nightly schedule
itself lives outside this app (run_nightly.sh + whatever invokes it), so they
are stored and displayed but change nothing in the engine.
"""
from __future__ import annotations

import json

from deck_engine import config

_TIER_STAGES = ["select", "draft", "judge", "validate_repair", "optimize", "card_tagger", "simulate"]
_VALID_TIERS = ("haiku", "sonnet", "opus", "fable")
_VALID_BRACKETS = ("1", "2", "3", "3-4", "4", "5")


def _effective_thinking(stage: str) -> int:
    """The budget the next call at this stage would actually use — same
    stage > model > global resolution as claude_cli.run()."""
    from deck_engine.claude_cli import _resolve_thinking_budget
    return _resolve_thinking_budget(stage, config.MODEL_TIERS.get(stage, ""))


def _read_file() -> dict:
    try:
        data = json.loads(config.UI_SETTINGS_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — missing/corrupt file just means defaults
        return {}


def current() -> dict:
    """Effective settings as the next run would see them."""
    stored = _read_file()
    return {
        "deck_budget_sgd": config.DECK_BUDGET_SGD,
        "max_card_price_sgd": config.MAX_CARD_PRICE_SGD,
        "max_run_spend_usd": config.MAX_RUN_SPEND_USD,
        "bracket": config.BRACKET,
        "dedupe_commander_days": config.DEDUPE_COMMANDER_DAYS,
        "resume_session_chaining": config.RESUME_SESSION_CHAINING,
        "model_tiers": {k: config.MODEL_TIERS.get(k, "sonnet") for k in _TIER_STAGES},
        "thinking_by_stage": {k: _effective_thinking(k) for k in _TIER_STAGES},
        "thinking_default_tokens": int(config.THINKING_BUDGET_TOKENS or 6000),
        "email_to": config.EMAIL_TO,
        "newsletter_bcc": list(config.NEWSLETTER_BCC),
        # UI-only fields (stored, displayed, not engine-applied):
        "nightly_enabled": bool(stored.get("nightly_enabled", True)),
        "nightly_time": str(stored.get("nightly_time", "02:00")),
        "exclude_recent_commanders": bool(stored.get("exclude_recent_commanders", True)),
    }


def save(updates: dict) -> dict:
    """Validate + persist + apply. Returns the new effective settings.
    Unknown keys are ignored; invalid values raise ValueError with a message
    the UI can show verbatim."""
    stored = _read_file()

    def _num(key: str, lo: float, hi: float) -> None:
        if key in updates and updates[key] is not None:
            try:
                val = float(updates[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number") from None
            if not (lo <= val <= hi):
                raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
            stored[key] = val

    _num("deck_budget_sgd", 0, 100000)
    _num("max_card_price_sgd", 1, 10000)
    _num("max_run_spend_usd", 0, 100)          # 0 = cap disabled
    _num("dedupe_commander_days", 0, 365)

    if "bracket" in updates:
        bracket = str(updates["bracket"]).strip()
        if bracket not in _VALID_BRACKETS:
            raise ValueError(f"bracket must be one of {', '.join(_VALID_BRACKETS)}")
        stored["bracket"] = bracket

    if "resume_session_chaining" in updates:
        stored["resume_session_chaining"] = bool(updates["resume_session_chaining"])

    if "model_tiers" in updates and isinstance(updates["model_tiers"], dict):
        tiers = dict(stored.get("model_tiers") or {})
        for stage, tier in updates["model_tiers"].items():
            if stage not in _TIER_STAGES:
                continue
            if tier not in _VALID_TIERS:
                raise ValueError(f"model tier for {stage} must be one of {', '.join(_VALID_TIERS)}")
            tiers[stage] = tier
        stored["model_tiers"] = tiers

    if "thinking_by_stage" in updates and isinstance(updates["thinking_by_stage"], dict):
        thinking = dict(stored.get("thinking_by_stage") or {})
        for stage, tokens in updates["thinking_by_stage"].items():
            if stage not in _TIER_STAGES:
                continue
            if isinstance(tokens, bool):  # UI toggle: on = the global default budget
                tokens = int(config.THINKING_BUDGET_TOKENS or 6000) if tokens else 0
            try:
                tokens = int(tokens)
            except (TypeError, ValueError):
                raise ValueError(f"thinking budget for {stage} must be a number of tokens (0 = off)") from None
            if not (0 <= tokens <= 32000):
                raise ValueError(f"thinking budget for {stage} must be between 0 and 32000 tokens")
            thinking[stage] = tokens
        stored["thinking_by_stage"] = thinking

    if "email_to" in updates:
        email = str(updates["email_to"]).strip()
        if email and "@" not in email:
            raise ValueError("email_to doesn't look like an email address")
        stored["email_to"] = email

    if "newsletter_bcc" in updates and isinstance(updates["newsletter_bcc"], list):
        cleaned = []
        for addr in updates["newsletter_bcc"]:
            addr = str(addr).strip()
            if not addr:
                continue
            if "@" not in addr:
                raise ValueError(f"newsletter address doesn't look right: {addr}")
            cleaned.append(addr)
        stored["newsletter_bcc"] = cleaned

    for key in ("nightly_enabled", "exclude_recent_commanders"):
        if key in updates:
            stored[key] = bool(updates[key])
    if "nightly_time" in updates:
        stored["nightly_time"] = str(updates["nightly_time"]).strip()[:5]

    config.UI_SETTINGS_PATH.write_text(json.dumps(stored, indent=1))
    config._apply_ui_settings()  # noqa: SLF001 — same module, deliberate re-apply for the live process
    return current()
