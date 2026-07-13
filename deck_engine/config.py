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

# ── .env auto-load ────────────────────────────────────────────────────────────
# run_nightly.sh loads deck_engine/.env into the shell environment itself
# (`set -a; source deck_engine/.env; set +a`) before invoking Python — but any
# OTHER entry point (the Atelier UI, a REPL, a future script) imports this
# module directly with no shell step in between, so a value that only ever
# lived in the .env file (never actually exported into the process) was
# invisible to it — confirmed 2026-07-05: a real Atelier commission built a
# complete deck end to end, then failed at the deliver stage with
# EmailConfigError even though deck_engine/.env had a real address/App
# Password on disk the whole time. Loading it here, once, at import time,
# makes every entry point behave the same regardless of how it was launched.
def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)  # a real, already-exported env var always wins


_load_dotenv(Path(__file__).resolve().parent / ".env")

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
# GISHATH_PORT, not the generic PORT: dev harnesses inject PORT=<own port>
# into whatever process they launch, so an Atelier started that way would
# read PORT=5077 here and price against *itself* — /api/health answers,
# /search 404s, and the whole run silently ships unpriced (run 81f2b542,
# 2026-07-10). app.py honors the same GISHATH_PORT, so one var moves both.
GISHATH_APP_PORT: int = int(os.environ.get("GISHATH_PORT", 5003))
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

# Widened back out 2026-07-06 (Trevor's call — "cooler to see 3 agents working
# at the same time"): each subagent now drafts a COMPLETE deck in parallel, not
# just an angle proposal; a judge stage picks the winner. Replaces the old
# ideate×3 -> synthesize -> single-build chain (roughly cost-neutral: 3 big
# sonnet drafts ≈ 3 small ideates + 1 big build).
DRAFT_SUBAGENTS: int = 3   # complete decks drafted in parallel before the judge picks one

# Model tier per stage. Opus on `judge` and `optimize` only (2026-07-13 swap:
# judge sonnet->opus, select opus->sonnet, roughly cost-neutral). Judge is the
# highest-leverage single call — it picks among 3 complete 99s and its
# cherry-pick swaps ship straight into the final deck — and it's where the
# Forge-sim quality gap (synergy piles with zero interaction) has to be
# caught, since the quota scorecard counts roles but can't weigh them.
# Select's hard constraints (dedupe, eligibility) are enforced in code and a
# mediocre concept still builds fine, so it doesn't need the priciest tier.
# Sonnet everywhere else — Opus is ~4-5x Sonnet cost (PRD §4 constraints), and
# draft/validate_repair are grounded in real oracle text now, so the marginal
# value of Opus there is lower than on the single-call stages. `draft` runs
# DRAFT_SUBAGENTS calls in parallel, so an Opus tier there multiplies.
MODEL_TIERS: dict = {
    "select": os.environ.get("DECK_ENGINE_SELECT_MODEL", "sonnet"),
    "draft": "sonnet",
    "judge": os.environ.get("DECK_ENGINE_JUDGE_MODEL", "opus"),
    "validate_repair": "sonnet",
    "optimize": os.environ.get("DECK_ENGINE_OPTIMIZE_MODEL", "opus"),
    # PRD v4 amendment T4: card_tags/mechanic-token extraction moved OFF Opus
    # entirely — these are cheap classification tasks (xlsx presentation
    # metadata, or naming a few keywords), never the deck-content stages.
    "card_tagger": os.environ.get("DECK_ENGINE_CARD_TAGGER_MODEL", "haiku"),
    # A bounded, evidence-grounded Commander rehearsal launched from Atelier.
    "simulate": os.environ.get("DECK_ENGINE_SIMULATE_MODEL", "sonnet"),
    # Forge match coach (atelier/coach.py): "coach_orders" reads a decklist
    # once per deck (cached forever at state/coach_orders/); "coach_turn" runs
    # once per seat per own-turn during a coached match (~40-60 calls/game),
    # so it stays on the cheapest tier.
    "coach_orders": os.environ.get("DECK_ENGINE_COACH_ORDERS_MODEL", "sonnet"),
    "coach_turn": os.environ.get("DECK_ENGINE_COACH_TURN_MODEL", "haiku"),
}

MAX_VALIDATE_REPAIR_ATTEMPTS: int = 3   # give the Scryfall repair loop this many tries before failing the run
MAX_STRATEGY_RETRIES: int = 1           # if optimize's fact-check flags a broken premise, retry draft->optimize this many times

# ── PRD v4 amendment §3.1 T5 — web-search gating ─────────────────────────────
# Search stays enabled at select only. It WAS also enabled at the old
# standalone ideate stage (2026-07-01's "let the models decide" call, Trevor's
# re-confirmation 2026-07-03), but the 2026-07-06 widen-back-out folded
# ideation into the build-shaped parallel draft calls, which follow build's
# no-search policy — 3 parallel search-enabled builds could blow the crucible
# cap on a bad night. draft/validate_repair/optimize/card_tagger pass this
# list explicitly as claude_cli.run(disallowed_tools=...) at their call sites
# in agent_pipeline.py; judge reasons only over material already in its prompt.
DISALLOWED_SEARCH_TOOLS: list[str] = ["WebSearch", "WebFetch"]

# ── PRD v4 amendment §3.2 — synergy grounding ────────────────────────────────
# S2: role-quota RANGES rendered into every parallel draft prompt (draft.md),
# which is instructed to target them unless its angle genuinely argues for
# deviating. (S2's original per-brief adjustment lived in the synthesize
# stage, retired by the 2026-07-06 widen-back-out — drafts build before any
# brief exists, so the defaults ARE the targets now.)
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
# 2 -> 1 (2026-07-10 token diet) -> back to 2 (2026-07-11): the diet cut this
# to 1 while the gate was firing on 3 of 7 runs, i.e. repair capacity halved
# exactly when it was needed most, and post-diet decks shipped goodstuff-adjacent
# (Xanathar review). Post-T3 an attempt is swap-deltas, not a full regurgitation
# (~$0.10-0.20, not draft-scale), so the second attempt is cheap insurance.
MAX_SYNERGY_REPAIR_ATTEMPTS: int = int(os.environ.get("DECK_ENGINE_MAX_SYNERGY_REPAIR_ATTEMPTS", "2"))

# ── PRD v4 amendment §3.4 — budget pass ──────────────────────────────────────
# Per-card cap only — Trevor's explicit call (2026-07-03): NO total-deck cap;
# the total is displayed in the email, never enforced. Cards priced above the
# cap get a targeted swap pass after stage 6 pricing (budget_pass.py); if a
# breach can't be fixed in MAX_BUDGET_REPAIR_ATTEMPTS, the deck SHIPS with an
# "over budget" flag rather than failing the run (resolved question, same
# flag-never-block philosophy as pricing itself).
MAX_CARD_PRICE_SGD: float = float(os.environ.get("DECK_ENGINE_MAX_CARD_PRICE_SGD", "150"))
MAX_BUDGET_REPAIR_ATTEMPTS: int = 2

# Price-sanity quarantine (2026-07-10, run 9e430ab7): a local store price under
# PRICE_SANITY_RATIO of the Card Kingdom USD reference — when that reference is
# at least PRICE_SANITY_CK_MIN_USD — is treated as a bad match (art card/proxy/
# wrong product) and quarantined as unpriced rather than trusted. A Bayou
# "priced" SGD 0.45 against a USD 229.99 CK reference slid under the per-card
# cap this way. See pricing._suspicious_prices().
PRICE_SANITY_CK_MIN_USD: float = float(os.environ.get("DECK_ENGINE_PRICE_SANITY_CK_MIN_USD", "5"))
PRICE_SANITY_RATIO: float = float(os.environ.get("DECK_ENGINE_PRICE_SANITY_RATIO", "0.10"))

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

# ── Extended thinking — live-view richness ───────────────────────────────────
# When > 0, every `claude -p` subprocess is spawned with
# MAX_THINKING_TOKENS=<this> in its environment, which turns on extended
# thinking for the call. What the live view can show depends on the model
# tier (confirmed against a real sonnet capture, 2026-07-07 — see
# claude_cli._feed_view): haiku streams its actual reasoning text; sonnet and
# opus stream REDACTED thinking (empty text + a running estimated_tokens
# counter), which the benches surface as a ticking "thinking it through…
# ~N tokens" status instead of raw prose. The readable in-progress text on
# the big sonnet/opus benches comes from the prompts' narrate-out-loud
# instructions, not from thinking. Thinking tokens bill as OUTPUT tokens
# either way, so a non-zero budget raises per-run cost; 0 disables entirely
# (the pre-2026-07-05 behaviour: no thinking, quieter and cheaper calls).
THINKING_BUDGET_TOKENS: int = int(os.environ.get("DECK_ENGINE_THINKING_BUDGET_TOKENS", "6000"))

# Per-model thinking budgets (2026-07-10 token diet). Since sonnet/opus
# thinking is REDACTED on the wire (see above), their budget bought only the
# ticking counter while billing at output-token prices ON TOP of the visible
# narration the prompts already demand — paying twice for reasoning on every
# expensive call. Haiku keeps the global budget: its thinking streams as real
# readable text on the live benches, and haiku calls are the cheap ones.
# claude_cli.run() resolves the model's entry here, falling back to
# THINKING_BUDGET_TOKENS for a model name not listed.
THINKING_BUDGET_BY_MODEL: dict = {
    "haiku": int(os.environ.get("DECK_ENGINE_THINKING_HAIKU_TOKENS", str(THINKING_BUDGET_TOKENS))),
    "sonnet": int(os.environ.get("DECK_ENGINE_THINKING_SONNET_TOKENS", "0")),
    "opus": int(os.environ.get("DECK_ENGINE_THINKING_OPUS_TOKENS", "0")),
    # fable must be listed explicitly: an unlisted model falls back to the
    # global 6000, which would silently buy redacted thinking at the most
    # expensive tier's output prices.
    "fable": int(os.environ.get("DECK_ENGINE_THINKING_FABLE_TOKENS", "0")),
}

# Per-STAGE thinking budgets, keyed by model tier key — outranks the per-model
# entry above (claude_cli.run resolves stage -> model -> global). Added
# 2026-07-11: the diet's blanket sonnet/opus thinking cut collapsed each
# drafter's output from 22-27k tokens to 3.5-5k, and the five post-diet
# commissions all showed the cost of that lost deliberation — every run burned
# draft-repair rounds (pre-diet often none), the synergy gate fired on 3 of 5
# (vs 4 of 25 before), and two runs died in validation outright. Draft is
# where thinking demonstrably bought deck quality, so it alone gets a budget
# back. Other stages stay on the per-model (diet) rules.
# 10000 -> 6000 (2026-07-11, run b5b10134): 10k overshot — drafts emitted
# 24-30k output tokens, MORE than the pre-diet era, and the run cost $3.51 vs
# the $2.88 baseline. 6000 is the budget the pre-diet drafts actually ran on;
# it bought the zero-repair structural soundness without the overage.
THINKING_BUDGET_BY_STAGE: dict = {
    "draft": int(os.environ.get("DECK_ENGINE_THINKING_DRAFT_TOKENS", "6000")),
    # judge moved to opus 2026-07-13 for deeper deliberation over the three
    # drafts; thinking on by default so the upgrade actually buys deliberation,
    # not just a bigger model reading the same prompt. Single call per run, so
    # the worst case is ~6k output-billed tokens (~$0.15 at opus rates).
    "judge": int(os.environ.get("DECK_ENGINE_THINKING_JUDGE_TOKENS", "6000")),
}

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
            if stage_key in MODEL_TIERS and tier in ("haiku", "sonnet", "opus", "fable"):
                env_var = {
                    "select": "DECK_ENGINE_SELECT_MODEL",
                    "judge": "DECK_ENGINE_JUDGE_MODEL",
                    "optimize": "DECK_ENGINE_OPTIMIZE_MODEL",
                    "card_tagger": "DECK_ENGINE_CARD_TAGGER_MODEL",
                }.get(stage_key)
                if env_var and os.environ.get(env_var):
                    continue
                MODEL_TIERS[stage_key] = tier
    # Per-stage thinking budgets (0 = off). Lands in THINKING_BUDGET_BY_STAGE,
    # the top of claude_cli's stage > model > global resolution, so a UI value
    # decides the stage outright. An explicit env var — the same
    # DECK_ENGINE_THINKING_<STAGE>_TOKENS pattern draft already uses — outranks
    # the UI file, consistent with everything else here.
    thinking = settings.get("thinking_by_stage")
    if isinstance(thinking, dict):
        for stage_key, tokens in thinking.items():
            if stage_key not in MODEL_TIERS:
                continue
            if os.environ.get(f"DECK_ENGINE_THINKING_{stage_key.upper()}_TOKENS"):
                continue
            try:
                THINKING_BUDGET_BY_STAGE[stage_key] = max(0, int(tokens))
            except (TypeError, ValueError):
                continue
    # Newsletter BCC list (list of addresses).
    bcc = settings.get("newsletter_bcc")
    if isinstance(bcc, list) and not os.environ.get("DECK_ENGINE_NEWSLETTER_BCC"):
        g["NEWSLETTER_BCC"] = [str(a).strip() for a in bcc if str(a).strip()]


_apply_ui_settings()
