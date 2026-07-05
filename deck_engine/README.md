# deck_engine — Nightly EDH Deck Generation Engine

Implements `PRD_nightly_deck_engine.md` (v3) as amended by
`PRD_deck_engine_v4_amendment.md` (token diet, synergy grounding, newsletter
— 2026-07-03). Extends `gishath-local-v2` — reuses its running Flask app for
pricing rather than duplicating any scraping code.

## What's here

| Module | Stage(s) | Purpose |
|---|---|---|
| `config.py` | — | All tunable settings: bracket rules, dedupe window, model tiers, role-quota defaults, synergy-gate threshold, newsletter BCC, paths, email config. Edit this, not the pipeline code. |
| `run_log.py` | — | JSON history of past runs; backs the 30-day commander dedupe / soft archetype-avoid, and (v4) the newsletter's issue number + "last 7 decks" list. |
| `scryfall_cache.py` | validate | Local Scryfall bulk-data cache + legality/color-identity/singleton validation. Also caches `cmc`/`mana_cost`/`rarity`/`image_uris`/`card_faces` (v4) for export's stats/columns and the email's commander image. |
| `claude_cli.py` | — | Thin wrapper around headless `claude -p`, JSON-schema structured output, spend logging. v4: `disallowed_tools` (T5 search gating) and `resume_session_id` (T6 experiment scaffolding) parameters. |
| `spend_log.py` | — | Per-run cost/turn logging (PRD §4f). |
| `prompt_helpers.py` + `prompts/*.md` | — | Editable prompt templates (PRD: "prompts/bracket rules as editable config, not hardcoded"). `prompts/synergy_repair.md` (v4) is new. |
| `concept_selector.py` | 1. select | Picks tonight's commander/archetype; enforces dedupe + commander-eligibility in code. v4: also extracts the commander's 3-5 mechanic tokens (one Haiku call, `synergy_check.py`) for the S3 gate. |
| `agent_pipeline.py` | 2-5. ideate/build/validate/optimize/synergy-gate | Parallel ideation subagents → synthesize (now also proposes role-quota ranges) → build (now grounded in an EDHREC candidate pool) → validate/repair loop → optimize (now returns swap deltas, applied in code) → re-validate → synergy-density gate/repair. |
| `card_tagger.py` (v4) | tag | T4: role/phase tags for the Breakdown sheet via code heuristics + one Haiku call — off Opus entirely (was part of optimize's schema in v3). |
| `edhrec_pool.py` (v4) | build | S1: fetches/caches an EDHREC per-commander synergy candidate pool (unofficial endpoint, cached ≥7 days, degrades to v3 no-pool behaviour on any failure). |
| `synergy_check.py` (v4) | select, synergy-gate | S3: extracts mechanic tokens at select-time; code-level keyword match against the final decklist decides whether the synergy-repair pass fires. |
| `pricing.py` | 6. price | HTTP client of gishath-local-v2's own `/search` endpoint (**not** `engine_client.search_many()` directly — see the correction note in the module docstring: that path only reaches 6 of 15 stores). v4: `cheapest_by_card()`/`deck_price_summary()` shared by export.py and emailer.py. |
| `export.py` | 7. export | Builds the `.xlsx` — Moxfield sheet (basics grouped), Breakdown sheet (qty/price/store/role/phase/CMC/type/rarity, sorted by role then price), Gameplan sheet (now with priced total), Stats sheet (v4: mana curve, colour pips, role counts). `write_moxfield_txt()`/`save_moxfield_txt()` (v4) write a plain-text Moxfield import alongside the xlsx. |
| `emailer.py` | 9. deliver | Gmail SMTP + App Password. v4: SGD price headline in the subject, commander image, top-5 priciest cards, last-7-decks list, and — when `config.NEWSLETTER_BCC` is set — a **second, separate send**: a clean copy (no cost/turn/tool diagnostics) Bcc'd to friends, while Trevor's own copy keeps full diagnostics. Falls back to a local file in `logs/` if sending itself fails. |
| `run.py` | orchestrator | Ties stages 1-9 together; always emails or falls back to a local file; never crashes silently. |
| `run_nightly.sh` (repo root) | entrypoint | `caffeinate ./run_nightly.sh` — checks/starts `gishath-local-v2` if not already running, loads `.env`, runs the pipeline. |

## PRD v4 amendment — status (2026-07-03)

Built this session: T2 (token-diet prompt instructions), T3 (swap-delta
optimize schema + code application), T4 (card tagging off Opus), T5
(`--disallowedTools` at build/repair/optimize/card_tagger), T6 (`--resume`
scaffolding, off by default — `DECK_ENGINE_RESUME_CHAINING`), S1 (EDHREC
pool), S2 (role-quota ranges), S3 (synergy-density gate), and the §3.3
newsletter/output plumbing (BCC list, two-send, SGD headline, image, top-5,
last-7, Moxfield `.txt`, xlsx Stats sheet). Verified via
`deck_engine/tests/test_v4_amendment.py` plus the existing suite (all pass).

**Not yet done — needs a real run on Trevor's Mac, not buildable from this
sandbox:**
- Step 4 (verify token diet ≥5 real runs vs. the 8-run baseline)
- Step 8 (run synergy grounding ≥7 nights, review deck quality)
- Step 11 (T1 ideation-collapse trial — gated on step 8 completing)
- Friends' email addresses for `DECK_ENGINE_NEWSLETTER_BCC` (plumbing is
  live; newsletter send is a no-op until this is set)
- EDHREC's real response shape has never been fetched live (unofficial
  endpoint, not reachable from this sandbox) — `edhrec_pool._extract_card_names()`
  is a best-effort guess at the JSON schema; verify against a real commander
  page and adjust if EDHREC's actual shape differs.
- `--disallowedTools`' exact CLI flag syntax is unverified against a real
  authenticated `claude` session (same sandbox limitation as everything else
  CLI-shaped in this project) — confirm `claude_cli.py`'s `cmd += ["--disallowedTools", *tools]` matches the real flag before trusting it beyond the mocked tests.

## What was verified in this sandbox, and how

This sandbox has no authenticated `claude` session, no internet access to
`api.scryfall.com` (blocked by the network allowlist, confirmed), no running
`gishath-local-v2` instance, and no SMTP credentials — so nothing here has
been run against the real outside world yet. What *was* verified:

- **`claude -p` JSON envelope schema** — captured live against the real
  `claude` binary installed in this sandbox (`Claude Code CLI 2.1.197`).
  Unauthenticated, so only the error path was observed, but the field names
  (`result`, `is_error`, `num_turns`, `total_cost_usd`, `session_id`) are
  confirmed real, not guessed.
- **Every module's internal logic** — Scryfall validation (banned cards,
  color-identity subset check, singleton exemptions for basic lands and
  "any number of cards named X" effects, hallucination detection), the
  concept selector's dedupe/retry loop, the full agent pipeline
  (ideate → synthesize → build → validate/repair → optimize → re-validate)
  with a deliberately-broken build response to confirm the repair loop
  actually fires and recovers, the pricing integration against the *real*
  `optimizer.py` (confirmed hidden/rank>5 rows are excluded and the cheapest
  price is picked correctly), the xlsx export (read back and checked cell by
  cell), and the emailer's fail-closed behavior (no credentials → falls back
  to a local file instead of losing the report) — all exercised with
  `unittest.mock` standing in for the real `claude` calls / network calls,
  and a full `run.main()` dry run exercising every stage end to end.
- **NOT verified**: an actual `claude -p` call succeeding (needs
  authentication), an actual Scryfall bulk-data download (needs the
  sandbox's network restriction lifted, i.e. runs fine off-sandbox),
  actual pricing against a running `gishath-local-v2`, actual email
  delivery.

This mirrors the pattern already established in `project-gishath` memory —
several pieces of that project also had to be smoke-tested on Trevor's Mac
because the sandbox can't reach the real network or build Go binaries.

## Pre-flight checklist before the first real dry run (PRD §8 step 8)

1. **Clear sandbox test residue** (safe to delete — all gitignored):
   - `deck_engine/state/scryfall_cards.json` / `scryfall_cache_meta.json` — a
     synthetic 5-card fake cache used for testing, **not real Scryfall
     data**. Must be replaced before real use (step 2 does this).
   - `deck_engine/state/test_run_log.json`, `deck_engine/state/sel_test_log.json`
   - `deck_engine/logs/UNSENT_*.txt`, `deck_engine/logs/spend_log.jsonl`
   - `deck_engine/output/*.xlsx` (test artifacts)
   - Two stray files from a sandbox path mistake I couldn't delete remotely
     (this sandbox's mount doesn't allow deletes): `gishath-local-v2/gishath-local-v2/`
     (an empty nested duplicate folder) and `gishath-local-v2/gishath-local-v2-nul`
     (a 1-byte junk file), both at the repo root, both harmless and unreferenced
     by any code — safe to `rm -rf` whenever convenient.
   - `deck_engine/state/run_log.json` has already been reset to `[]` — real.
2. **Refresh the real Scryfall cache**: `python -m deck_engine.scryfall_cache --refresh`
   (needs real internet; will not work from this sandbox).
3. **Set up email**: copy `deck_engine/.env.example` to `deck_engine/.env`,
   fill in a Gmail address + App Password
   (https://myaccount.google.com/apppasswords, needs 2-Step Verification on
   first).
4. **Confirm `claude` is authenticated** in the shell you'll run this from
   (`claude /login` if needed) — `run_nightly.sh` runs it with
   `--dangerously-skip-permissions` since there's no human present overnight
   to approve tool-use prompts.
5. **Run it**: `caffeinate ./run_nightly.sh` from the `gishath-local-v2/`
   directory. Watch the console output; check `deck_engine/logs/spend_log.jsonl`
   afterward for actual cost/turns.
6. Confirm: the emailed `.xlsx` imports cleanly into Moxfield, the deck is
   legal (Moxfield will also flag illegal decks, as a second check beyond
   the Scryfall gate), and the breakdown sheet's prices look sane.

## Known limitations / follow-ups

- **Fixed by the v4 amendment**: card tags used to come from optimize's own
  response, so a card added by the optimize-repair or synergy-repair loops
  (after optimize already returned) could ship untagged. `card_tagger.py` now
  runs on `final_cards` — after every repair loop, including the new synergy
  gate — so every shipped card gets a tag regardless of which stage last
  touched it.
- `run_nightly.sh` starts `gishath-local-v2` in the background if it isn't
  already running, but never stops it — matches how Trevor already runs it
  day to day (a standing local tool), not a one-shot subprocess.
- Cost logging is per-stage/per-run (`spend_log.jsonl`); there's no rollup
  dashboard yet — `spend_log.summarize_run(run_id)` gives the numbers, but
  reading the raw JSONL by hand is the only way to see trends across many
  nights right now.
