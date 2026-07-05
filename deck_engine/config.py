"""
deck_engine/config.py — all tunable settings for the nightly deck engine.

Deliberately plain data (dicts/constants), not buried in code, so bracket
rules / model tiers / dedupe window can be edited without touching pipeline
logic. Matches PRD_nightly_deck_engine.md section 4.

Everything here can be overridden with an environment variable of the same
name (see `_env_override` at the bottom) so a single dry run can tweak e.g.
DECK_ENGINE_MODEL_TIER_OPTIMIZE=opus without editing this file.
"""
from __future__ import annotations

import os
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT: Path = Path(__file__).resolve().parent.parent          # gishath-local-v2/
DECK_ENGINE_DIR: Path = Path(__file__).resolve().parent            # gishath-local-v2/deck_engine/
STATE_DIR: Path = DECK_ENGINE_DIR / "state"
LOG_DIR: Path = DECK_ENGINE_DIR / "logs"
PROMPTS_DIR: Path = DECK_ENGINE_DIR / "prompts"
OUTPUT_DIR: Path = DECK_ENGINE_DIR / "output"          # xlsx files land here before email

RUN_LOG_PATH: Path = STATE_DIR / "run_log.json"
SCRYFALL_CACHE_PATH: Path = STATE_DIR / "scryfall_cards.json"
SCRYFALL_CACHE_META_PATH: Path = STATE_DIR / "scryfall_cache_meta.json"
SPEND_LOG_PATH: Path = LOG_DIR / "spend_log.jsonl"

for _d in (STATE_DIR, LOG_DIR, PROMPTS_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── gishath-local-v2 Flask app (pricing backend) ─────────────────────────────
# Must already be running (`make run`) before the nightly job starts — see
# PRD §4b / §6 risk "gishath-local-v2 not running when the job starts".
# NOTE: app.py's actual default port is 5003 (its own docstring/comments say
# 5001 — that's stale; verified against `app.py` line `port = int(os.environ
# .get("PORT", 5003))` on 2026-07-01). Always read the real default from the
# same env var app.py uses, not from stale docs.
GISHATH_APP_PORT: int = int(os.environ.get("PORT", 5003))
GISHATH_APP_BASE: str = f"http://127.0.0.1:{GISHATH_APP_PORT}"
GISHATH_HEALTH_URL: str = f"{GISHATH_APP_BASE}/api/health"
GISHATH_SEARCH_URL: str = f"{GISHATH_APP_BASE}/search"
GISHATH_STARTUP_TIMEOUT_S: float = 20.0   # how long to wait for /api/health to come up

# ── Deck shape ────────────────────────────────────────────────────────────────

DECK_SIZE: int = 100          # singleton EDH incl. commander
BRACKET: str = "3-4"          # target Commander Bracket band

# ── Bracket 3-4 house rules (PRD §4a) — edit here, not in pipeline code ──────

BRACKET_RULES: dict = {
    "game_changers_allowed": True,
    "tutors_allowed": True,
    # Two-card infinite combos may exist as a *backup* wincon only — the
    # build/optimize prompts must not lean on them as the primary gameplan.
    "two_card_infinite_combos": "backup_only",
    "mass_land_destruction_allowed": False,
    # No colour/archetype restriction — actively favour novel, unorthodox
    # builds over "solved" commanders/staple shells.
    "favour_unorthodox": True,
}

# ── Dedupe (PRD §4c) ──────────────────────────────────────────────────────────

DEDUPE_COMMANDER_DAYS: int = 30   # hard: do not repeat a commander in this window
DEDUPE_ARCHETYPE_SOFT_DAYS: int = 30  # soft: bias away from, don't hard-block

# ── Agent pipeline shape (PRD §5 / §8 step 4) ────────────────────────────────

IDEATION_SUBAGENTS: int = 3   # angles/lines explored in parallel before build picks one

# Model tier per stage. Opus on `select` and `optimize` only (2026-07-01,
# after a real run's deck was built around a hallucinated commander ability —
# see agent_pipeline.py's oracle-text grounding for the actual fix; Opus here
# is the fact-checking backstop on top of that, not a substitute for it).
# Sonnet everywhere else — Opus is ~4-5x Sonnet cost (PRD §4 constraints), and
# ideate/build/validate_repair are grounded in real oracle text now, so the
# marginal value of Opus there is lower than on the two single-call stages.
MODEL_TIERS: dict = {
    "select": os.environ.get("DECK_ENGINE_SELECT_MODEL", "opus"),
    "ideate": "sonnet",
    "build": "sonnet",
    "validate_repair": "sonnet",
    "optimize": os.environ.get("DECK_ENGINE_OPTIMIZE_MODEL", "opus"),
    # PRD v4 amendment T4: card_tags/mechanic-token extraction moved OFF Opus
    # entirely — these are cheap classification tasks (xlsx presentation
    # metadata, or naming a few keywords), never the deck-content stages.
    "card_tagger": os.environ.get("DECK_ENGINE_CARD_TAGGER_MODEL", "haiku"),
}

MAX_VALIDATE_REPAIR_ATTEMPTS: int = 3   # give the Scryfall repair loop this many tries before failing the run
MAX_STRATEGY_RETRIES: int = 1           # if optimize's fact-check flags a broken premise, retry ideate->optimize this many times

# ── PRD v4 amendment §3.1 T5 — web-search gating ─────────────────────────────
# Search stays enabled at select/ideate (2026-07-01's "let the models decide"
# call, kept there deliberately — Trevor's explicit re-confirmation 2026-07-03).
# build/validate_repair/optimize/card_tagger now pass this list explicitly as
# claude_cli.run(disallowed_tools=...) at their call sites in agent_pipeline.py
# — a global on/off switch here wouldn't work cleanly since e.g. "ideate" and
# "synthesize" share the same MODEL_TIERS key but have different search
# policies (synthesize is left unrestricted — not named in the PRD's resolved
# decision, status quo preserved).
DISALLOWED_SEARCH_TOOLS: list[str] = ["WebSearch", "WebFetch"]

# ── PRD v4 amendment §3.2 — synergy grounding ────────────────────────────────
# S2: role-quota RANGES the synthesize stage can adjust per build brief
# (defaults below); build.md is instructed to target these unless the brief
# specifically argues for deviating.
ROLE_QUOTA_DEFAULTS: dict = {
    "land_min": 35, "land_max": 38,
    "ramp_min": 10, "ramp_max": 12,
    "draw_min": 8, "draw_max": 10,
    "interaction_min": 8, "interaction_max": 10,
    "wipes_min": 2, "wipes_max": 3,
    "on_mechanic_min": 28,
}
# S3: code-level synergy-density gate threshold — deliberately low (tolerate
# false negatives; this is a backstop against egregious goodstuff piles, not
# the primary quality mechanism). Tunable without a code change.
SYNERGY_GATE_THRESHOLD: int = int(os.environ.get("DECK_ENGINE_SYNERGY_GATE_THRESHOLD", "25"))
MAX_SYNERGY_REPAIR_ATTEMPTS: int = 2

# ── PRD v4 amendment §3.4 — budget pass ──────────────────────────────────────
# Per-card cap only — Trevor's explicit call (2026-07-03): NO total-deck cap;
# the total is displayed in the email, never enforced. Cards priced above the
# cap get a targeted swap pass after stage 6 pricing (budget_pass.py); if a
# breach can't be fixed in MAX_BUDGET_REPAIR_ATTEMPTS, the deck SHIPS with an
# "over budget" flag rather than failing the run (resolved question, same
# flag-never-block philosophy as pricing itself).
MAX_CARD_PRICE_SGD: float = float(os.environ.get("DECK_ENGINE_MAX_CARD_PRICE_SGD", "150"))
MAX_BUDGET_REPAIR_ATTEMPTS: int = 2

# Whole-deck budget in SGD — DISPLAY ONLY, mirroring the "no total-deck cap"
# decision above: shown in the Atelier UI's commission knobs and available to
# prompts, never enforced anywhere in the pipeline.
DECK_BUDGET_SGD: float = float(os.environ.get("DECK_ENGINE_DECK_BUDGET_SGD", "250"))

# ── Crucible cap — max API spend (USD) per run ───────────────────────────────
# Checked by claude_cli.run() before EACH call: once the run's logged spend
# reaches this, the next call raises instead of spawning, which lands in
# run.py's normal failure path (error email + run_log entry — a halt is never
# silent). 0 disables the check entirely — the pre-Atelier behaviour.
MAX_RUN_SPEND_USD: float = float(os.environ.get("DECK_ENGINE_MAX_RUN_SPEND_USD", "0"))

# ── PRD v4 amendment §3.1 T6 — --resume session-chaining A/B experiment ─────
# Off by default. One instrumented experiment, decision from data — NOT a
# silent default change (PRD is explicit about this). Flip on to run the
# comparison; watch for context-anchoring in later stages (build/optimize
# picking up unwanted framing from an earlier stage's session) per the PRD's
# own caution.
RESUME_SESSION_CHAINING: bool = os.environ.get("DECK_ENGINE_RESUME_CHAINING", "").strip().lower() in ("1", "true", "yes")

# Claude CLI binary — override via env if `claude` isn't on PATH in the
# environment run_nightly.sh executes in (e.g. cron-like shells often have a
# thinner PATH than an interactive terminal).
CLAUDE_BIN: str = os.environ.get("DECK_ENGINE_CLAUDE_BIN", "claude")

# ── Scryfall (PRD §4e) ────────────────────────────────────────────────────────

SCRYFALL_BULK_INDEX_URL: str = "https://api.scryfall.com/bulk-data"
SCRYFALL_CACHE_MAX_AGE_DAYS: int = 7   # refresh cache if older than this
SCRYFALL_USER_AGENT: str = "GishathDeckEngine/1.0 (personal project; contact: trevorjow@hotmail.com)"

# ── Cost / spend logging (PRD §4f) ───────────────────────────────────────────

LOG_SPEND_PER_RUN: bool = True

# ── Email delivery ────────────────────────────────────────────────────────────
# SMTP + app password by default — deliberately NOT Gmail API/OAuth. This
# script runs unattended with no browser available to complete an OAuth
# consent flow; an App Password is a one-time setup step and then works
# headlessly forever. See deck_engine/emailer.py docstring for setup steps.
#
# EMAIL_FROM must be a Gmail account you control (needs an App Password —
# https://myaccount.google.com/apppasswords — 2-Step Verification required).
# No default: set DECK_ENGINE_EMAIL_FROM yourself rather than have this guess
# an address for you.
# EMAIL_TO defaults to your on-file address (trevorjow@hotmail.com) — Gmail
# SMTP can deliver to any inbox, it's only the *sending* account that must be
# Gmail.
EMAIL_FROM: str = os.environ.get("DECK_ENGINE_EMAIL_FROM", "")
EMAIL_TO: str = os.environ.get("DECK_ENGINE_EMAIL_TO", "trevorjow@hotmail.com")
SMTP_HOST: str = os.environ.get("DECK_ENGINE_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT: int = int(os.environ.get("DECK_ENGINE_SMTP_PORT", "587"))
SMTP_USERNAME: str = os.environ.get("DECK_ENGINE_SMTP_USERNAME", EMAIL_FROM)
SMTP_APP_PASSWORD_ENV: str = "DECK_ENGINE_SMTP_APP_PASSWORD"  # read at send-time, never logged

# ── Newsletter (PRD v4 amendment §3.3) ───────────────────────────────────────
# Comma-separated friend email addresses. Empty by default — the newsletter
# send is skipped entirely (see emailer.py) until this is set. Set via a
# local, gitignored .env like the other secrets in this file; never commit
# real addresses. Two sends when this is non-empty: Trevor's own copy (full
# diagnostics) is unaffected either way; friends get a SEPARATE, clean copy
# (no cost/turns/tools diagnostics) via Bcc — resolved open question #2,
# 2026-07-03.
NEWSLETTER_BCC: list[str] = [
    addr.strip() for addr in os.environ.get("DECK_ENGINE_NEWSLETTER_BCC", "").split(",") if addr.strip()
]

# ── Atelier UI settings overlay (state/ui_settings.json) ────────────────────
# The Deckwright's Atelier app ("Guild rules" screen) persists its settings to
# a JSON file so they apply to EVERY run — UI-launched and nightly alike.
# Precedence: code defaults < ui_settings.json < environment variables. The
# env check keeps every existing override path (run_nightly.sh's .env, ad-hoc
# DECK_ENGINE_* exports) winning exactly as before this file existed.
UI_SETTINGS_PATH: Path = STATE_DIR / "ui_settings.json"

# ui_settings key -> (config attribute, env var that outranks it, coercion)
_UI_SETTING_KEYS: dict = {
    "deck_budget_sgd": ("DECK_BUDGET_SGD", "DECK_ENGINE_DECK_BUDGET_SGD", float),
    "max_card_price_sgd": ("MAX_CARD_PRICE_SGD", "DECK_ENGINE_MAX_CARD_PRICE_SGD", float),
    "max_run_spend_usd": ("MAX_RUN_SPEND_USD", "DECK_ENGINE_MAX_RUN_SPEND_USD", float),
    "bracket": ("BRACKET", None, str),
    "dedupe_commander_days": ("DEDUPE_COMMANDER_DAYS", None, int),
    "resume_session_chaining": ("RESUME_SESSION_CHAINING", "DECK_ENGINE_RESUME_CHAINING", bool),
    "email_to": ("EMAIL_TO", "DECK_ENGINE_EMAIL_TO", str),
}


def _apply_ui_settings() -> None:
    if not UI_SETTINGS_PATH.exists():
        return
    try:
        import json
        settings = json.loads(UI_SETTINGS_PATH.read_text())
    except Exception:  # noqa: BLE001 — a corrupt settings file must never block a run
        return
    if not isinstance(settings, dict):
        return
    g = globals()
    for key, (attr, env_var, coerce) in _UI_SETTING_KEYS.items():
        if key not in settings or settings[key] is None:
            continue
        if env_var and os.environ.get(env_var):
            continue  # explicit env override outranks the UI file
        try:
            g[attr] = coerce(settings[key])
        except (TypeError, ValueError):
            continue
    # Per-stage model tiers — only known stages, only known tier names.
    tiers = settings.get("model_tiers")
    if isinstance(tiers, dict):
        for stage_key, tier in tiers.items():
            if stage_key in MODEL_TIERS and tier in ("haiku", "sonnet", "opus"):
                env_var = {
                    "select": "DECK_ENGINE_SELECT_MODEL",
                    "optimize": "DECK_ENGINE_OPTIMIZE_MODEL",
                    "card_tagger": "DECK_ENGINE_CARD_TAGGER_MODEL",
                }.get(stage_key)
                if env_var and os.environ.get(env_var):
                    continue
                MODEL_TIERS[stage_key] = tier
    # Newsletter BCC list (list of addresses).
    bcc = settings.get("newsletter_bcc")
    if isinstance(bcc, list) and not os.environ.get("DECK_ENGINE_NEWSLETTER_BCC"):
        g["NEWSLETTER_BCC"] = [str(a).strip() for a in bcc if str(a).strip()]


_apply_ui_settings()
