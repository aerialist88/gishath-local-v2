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

NEVER swap out lands, mana rocks, mana dorks, land-ramp spells, or extra-land-drop effects —
they read as "generic" to a keyword check precisely because ramp serves every deck, but a deck
that can't cast its commander on time loses before synergy ever matters. (Swaps that remove a
ramp source without adding one back are vetoed in code and wasted.)

Respond only via the provided JSON schema: a `swaps` list of targeted changes, each with the
exact printed name of the card to `remove` (as it appears in the decklist above), the exact
printed name of the card to `add`, and a short `reason`. Do NOT return the rest of the
decklist — only the swaps. Every card you don't name stays untouched.

Before calling the structured-output tool, narrate briefly what you're tightening and why —
this streams live to whoever is watching the commission. Keep it to one short paragraph,
80 words at most: the theme of the changes, not a card-by-card walkthrough (the per-swap
reasons already live in the structured output).
