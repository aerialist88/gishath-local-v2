Commander: $commander ($color_identity)

The following decklist failed automated validation:

$repair_notes

Current decklist ($card_count cards, should be $deck_size_minus_1):
$current_decklist_block

Fix ONLY the issues listed above, as targeted deltas — do NOT return the whole decklist:

- **swaps** — replace a problem card 1-for-1 (hallucinated, banned, off-color) with a real,
  Commander-legal card inside the commander's color identity. `remove` must match a name in
  the decklist above exactly.
- **cuts** — remove cards when the deck is over $deck_size_minus_1. Cut the weakest,
  most off-plan cards first, and never cut lands or ramp to fix a count problem. Each entry
  removes one copy, so a duplicate is fixed by cutting the name once.
- **adds** — add cards when the deck is under $deck_size_minus_1, or when the notes say the
  mana base is short. Basic lands in the commander's colors are always safe adds.

When the notes say the deck is short on RAMP, fix it with **swaps**: replace the weakest
non-ramp, nonland cards with efficient mana rocks, mana dorks, or land-ramp spells in the
commander's color identity — never cut lands to make room for ramp.

Make the arithmetic work: $card_count current + adds − cuts must equal exactly
$deck_size_minus_1 (swaps don't change the count).

Before calling the structured-output tool, briefly narrate what you're fixing — which cards
are coming out, what's replacing them, and why each change resolves its issue. This streams
live to whoever is watching the commission. Keep it to a few sentences per fix, and do NOT
enumerate the rest of the decklist.
