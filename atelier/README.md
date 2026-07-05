# The Deckwright's Atelier — desktop/web UI for deck_engine

Implements the Claude Design handoff in
`deck_engine/AI-Crafted EDH Deck Builder.zip` (design tokens, screens, and
interactions per its README — the chosen "1a Atelier" direction).

## Run it

```bash
make atelier        # native macOS window (pywebview / WKWebView)
make atelier-web    # same app in the browser: http://127.0.0.1:5077
```

Both start the same local Flask server; the desktop window is just a WebView
over it. For priced decks, the pricing app must also be running (`make run`,
port 5003) — the home screen warns when it isn't.

## Screens (design ids in parentheses)

- **Commission / home (2a)** — type a commander or "Let the guild choose",
  knobs from Guild rules, last-delivered plaque, gallery shelf. The
  "rehearsal" link plays a scripted demo run — full live view, no API spend.
- **Workshop / live run (1a, 3b)** — streaming benches per `claude -p` call
  (three parallel apprentices at ideate, the Master Deckwright at repair),
  8-node stage gauge, ledger of finished calls, crucible spend plaque,
  Elapsed/Spend/Calls header. Reconnect-safe: reload mid-run and the view
  resumes from a snapshot.
- **Failure (3c)** — rust header, spend-by-stage bars, recommission /
  raise-cap-and-recommission / abandon actions. Persisted post-mortems
  (`deck_engine/state/atelier_runs/`) survive an app restart.
- **Finished deck (2b)** — decklist by role with dotted price leaders and
  over-cap ⚑ flags, Breakdown/Gameplan/Stats tabs mirroring the xlsx sheets,
  mana curve, priciest inclusions, Moxfield .txt / .xlsx downloads.
- **Gallery** — every past commission. Pre-Atelier decks are backfilled by
  parsing their xlsx once (cached as `*_deck.json` next to the workbook).
- **Guild rules (3d)** — purse sliders (deck budget display-only, per-card
  cap, crucible cap), bracket, nightly bell, per-stage model tiers, courier
  emails. Persists to `deck_engine/state/ui_settings.json`, which
  deck_engine's config overlays at import — **nightly runs obey these too**
  (env vars still outrank; see config.py).

## How it hooks into the engine

- `runner.AtelierView` implements the same surface as the terminal
  `LiveView` and is passed to `deck_engine.run.main(view=…)`; token streams,
  call costs, and stage transitions arrive as events on an in-memory bus,
  served to the browser via SSE (`/api/run/events`).
- The crucible cap is enforced in `claude_cli.run()` (checked before each
  call spawns); "abandon" uses the same mechanism (`request_cancel`).
- Typed commanders go through `concept_selector.select_concept(forced_commander=…)`
  — validated in code against the Scryfall cache, dedupe/price vetoes skipped
  for an explicit user choice.
- Finished runs write a `*_deck.json` record (`export.save_deck_json`) that
  backs the deck view; card art comes from the local Scryfall bulk cache's
  `image_uris`, distilled once into `state/atelier_art_index.json`.

## iPhone (next phase)

The frontend is responsive at ~390pt per the handoff's phone mocks. The
plan: serve this same app over the LAN and add it to the home screen (or a
thin Capacitor wrapper later) — the pipeline itself must keep running on the
Mac, since it drives the local `claude` CLI and scrapers.
