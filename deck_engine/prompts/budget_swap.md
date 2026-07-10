Commander: $commander ($color_identity)
Archetype: $archetype
What this deck does: $final_summary

Commander's ACTUAL printed ability (verified against Scryfall):
"""
$commander_oracle_text
"""

This commander's specific mechanic, distilled to a few keywords/phrases: $mechanic_tokens

This deck has already been built, validated, and priced at the cheapest Singapore stores.
The following cards exceed the per-card budget cap of SGD $max_card_price:

$over_cap_block

The current decklist, for reference — a replacement must NOT be a card that already appears
here (it would break the singleton rule; basic lands are the only exception):

$current_decklist_block

Propose ONE replacement for EACH card listed above. Every replacement must:
- Be a real Magic: The Gathering card (exact printed name), Commander-legal, within the
  commander's color identity, and not already in the deck
- Fill the SAME functional role as the card it replaces (listed above), so the deck's
  structure survives the swap
- Meaningfully interact with the commander's specific mechanic where the original did —
  a budget swap must not turn a synergy piece into generic filler
- Be a widely-printed, inexpensive card — prefer recent-reprint budget staples of the same
  function over niche old cards, since it must actually be cheap to buy in Singapore, not
  just cheaper than the original

Do NOT propose swapping any card that isn't in the list above — everything else in the deck
is already within budget. Before calling the structured-output tool, narrate briefly what
you're swapping and why — this streams live to whoever is watching the commission. Keep it
to a sentence per swap, 80 words at most in total; the full per-swap reasoning belongs in
the structured output, not the narration.

Respond only via the provided JSON schema.
