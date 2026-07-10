# 3vor Fetch

A personal Magic: The Gathering toolkit built around Commander (EDH), combining three linked apps: a multi-store price scraper/dashboard, an AI nightly deck-building engine, and a Forge-powered match simulator.

Originally built as **Gishath Fetch**, a scraper for Singapore's local game stores (LGS); this repo is the v2 rewrite, renamed 3vor Fetch, that grew the pricing tool into the full toolkit described below.

## What's here

### 3vor Fetch — price scraper dashboard
Takes a buy list and searches Singapore's local game stores (LGS) in parallel, pricing everything in SGD. A Go engine hits most stores directly over HTTP, while a persistent Playwright/Chromium browser handles the BinderPOS/Shopify stores that block plain requests. Results are merged into one price-comparison table with card art, price history, xlsx export, a price watchlist with email alerts, saved buy lists, and a Collection view for pricing an entire Moxfield collection in batches. Card Kingdom reference pricing (MTGJSON-sourced) fills in when a local store is missing a card.

### The Deckwright's Atelier — AI deck-building UI
A desktop (pywebview) or browser front end over `deck_engine`, a pipeline that has Claude draft a full budget-aware Commander deck:

1. Three parallel "deckwright" drafts, each grounded in an EDHREC synergy pool
2. An Adjudicator judge that merges the best of each draft
3. A validation/repair loop
4. Price optimization against the price-scraper's own search
5. A synergy-density gate/repair pass

The run streams live with a cost ledger and spend cap, and a deck can also be commissioned on demand from a typed commander. Finished decks get an xlsx (Moxfield/Breakdown/Gameplan/Stats sheets), a plain-text Moxfield import, and a gallery of past runs.

### Commander match simulator
Runs real games between saved decks on the actual Forge rules engine (bundled as a portable JDK + Forge jar, gitignored) rather than an LLM guessing legality. Claude only writes the narrative/report on top of Forge's real game log.

### Supporting pieces
- A public, read-only static gallery of finished decks, re-baked nightly and deployable anywhere
- A nightly entrypoint (`run_nightly.sh`) that generates a deck and emails a newsletter

## Stack

Python/Flask + a Go pricing engine, Playwright for scraping, headless `claude -p` for the AI stages, Forge (Java) for match simulation, vanilla JS/HTML for both dashboards.

## Running it

See the `Makefile` for targets (`make run`, `make atelier`, `make atelier-web`, `make gallery`, `make ck-refresh`, `make watchlist-check`). Each app's own README (`atelier/README.md`, `deck_engine/README.md`) has more detail on that piece.
