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
is already within budget. Keep any reasoning text brief (a short note per swap, not an essay);
the swap data belongs only in the structured output.

Respond only via the provided JSON schema.
