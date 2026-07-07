"""deck_engine — nightly EDH deck-generation pipeline.

Extends gishath-local-v2: reuses its running Flask app (/search endpoint)
for pricing rather than duplicating any scraping code. See
PRD_nightly_deck_engine.md (repo root's parent folder) for the full spec.

Pipeline (config.PIPELINE_STAGES order):
    select -> draft (×3 parallel whole-deck drafts) -> judge -> validate -> optimize -> price -> export -> deliver
"""
