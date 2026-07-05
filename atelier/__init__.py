"""The Deckwright's Atelier — desktop/web UI for the deck_engine pipeline.

Implements the "design_handoff_deckwright_atelier" handoff (see
deck_engine/AI-Crafted EDH Deck Builder.zip): a warm brass/parchment workshop
where commissions are placed, apprentice benches stream their work live, and
finished decks are browsed in the gallery.

Layout:
    runner.py    — background pipeline thread + AtelierView event bus
    archive.py   — deck records (JSON written by new runs, xlsx backfill for old)
    settings.py  — "Guild rules" persistence (state/ui_settings.json)
    art.py       — Scryfall art-crop index from the local bulk cache
    server.py    — Flask API + SSE + static frontend
    desktop.py   — pywebview native-window launcher

Run it:  python -m atelier.server   (browser)
         python -m atelier.desktop  (native macOS window)
"""
