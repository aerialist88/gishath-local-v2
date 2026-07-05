Commander: $commander ($color_identity)

Commander's ACTUAL printed ability (verified against Scryfall):
"""
$commander_oracle_text
"""

This commander's specific mechanic, distilled to a few keywords/phrases: $mechanic_tokens

A code-level check found this decklist reads as more "goodstuff in these colors" than tightly
built around that specific mechanic — the following cards didn't match any of the keywords above
in their own oracle text or type line:
$generic_cards_block

Current decklist ($deck_size_minus_1 cards):
$current_decklist_block

Swap out some of the weakest, most generic cards from the list above for real, legal,
in-color-identity replacements that meaningfully enable, trigger, or pay off the commander's
specific mechanic. Keep whatever's already working (mana base, genuinely load-bearing generic
support like removal/draw the deck still needs) — this is a targeted tightening pass, not a
wholesale rebuild. Not every generic card needs replacing; use judgement on how many swaps
actually improve the deck without gutting its functional shell.

Return the complete, corrected $deck_size_minus_1-card decklist. Respond only via the provided
JSON schema — a flat list of exactly $deck_size_minus_1 card name strings. Before calling the
structured-output tool, narrate your reasoning out loud — this streams live to whoever is watching
the commission: which generic cards you're targeting and why, what you're bringing in instead and
how it plays into the commander's mechanic. Write a few real paragraphs, not one line — but the
actual card names for the full decklist belong only in the structured output, not repeated
card-by-card in your narration.
