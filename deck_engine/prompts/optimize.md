Commander: $commander ($color_identity)
Archetype: $archetype
Build brief: $build_brief

Commander's ACTUAL printed ability (verified against Scryfall):
"""
$commander_oracle_text
"""

Real printed text for the key cards this build brief depends on (verified against Scryfall):
$key_cards_oracle_text

This decklist has passed legality/singleton/color-identity validation:
$current_decklist_block

You have two jobs this pass, in order:

1. FACT-CHECK FIRST. Compare the build brief's claimed gameplan/synergies against the real
   printed text above. This deck was built from a brief that may have described an ability the
   commander (or a key card) doesn't actually have — check specifically for that before doing
   anything else. If the core gameplan depends on something that isn't actually true of the
   real card text, the deck does not work as intended, regardless of how well-constructed it
   looks otherwise.

2. ONLY IF the fact-check passes: review as a finishing pass — mana curve, ramp package, card
   draw, interaction/removal density, and win conditions, per Bracket $bracket house rules
   (two-card infinite combos are backup wincons only, never the primary gameplan; mass land
   destruction excluded). Make small targeted swaps only where they clearly improve the deck's
   consistency or power-level fit for the bracket — do not rebuild wholesale.

   Also review SYNERGY DENSITY as part of this pass: does this decklist actually lean into
   $commander's specific mechanic (per the oracle text above), or does it read more like a
   generic goodstuff pile in these colors with the commander attached? A deck that a
   same-colored commander could pilot just as well hasn't used what makes this one distinct.
   If synergy density is low, swap out the weakest generic filler (cards included mainly
   because they're "solid in these colors" rather than because they interact with the
   commander's actual mechanic) for more on-mechanic alternatives — same small-targeted-swaps
   discipline as the rest of this pass, not a wholesale rebuild. Note any such swaps in
   changes_made specifically as synergy-density fixes, distinct from ordinary curve/consistency
   tuning.

The original archetype label and rationale this commander was picked under, written BEFORE this
deck existed and BEFORE anyone checked it against real card text, were:
  Archetype: "$archetype"
  Rationale: "$rationale"
These ship to Trevor verbatim as the deck's headline and description unless you correct them
here — this is the only stage that sees the fully-built, fact-checked deck, so it's the only
place that can catch a stale or wrong headline before it goes out. If your fact-check in job 1
found the label/rationale above describes something the commander doesn't actually do (a
misleading mechanic, a wrong colour, an invented synergy), do NOT let that wrong description
reach the email/spreadsheet unchanged — write a corrected one in final_archetype/final_summary
that matches what the deck you're holding actually does. If the original was already accurate,
you may repeat it near-verbatim in final_archetype/final_summary — don't invent a difference
that isn't there.

Return:
- strategy_valid: false if the fact-check in job 1 found the core gameplan depends on an
  ability the real card text doesn't support; true otherwise. If false, skip job 2 entirely —
  just set strategy_valid=false, explain the problem in strategy_problem, and return an empty
  `swaps` list (the deck will be discarded and rebuilt, not shipped).
- strategy_problem: empty string if strategy_valid is true; otherwise a precise explanation of
  what the brief got wrong about the card's actual ability.
- final_archetype: the corrected (or confirmed-accurate) short archetype label — this is what
  ships to Trevor, not the original "$archetype" above unless it's already right.
- final_summary: the corrected (or confirmed-accurate) 1-3 sentence description of what this
  specific deck actually does, grounded in the real oracle text above — this is what ships to
  Trevor, not the original rationale unless it's already right.
- swaps: a list of TARGETED changes only — {remove: "<exact card name currently in the list>",
  add: "<exact real card name to replace it with>", reason: "<short reason>"} — one entry per
  card you're changing. Return an EMPTY list if the deck needs no changes at all. Do NOT return
  the full decklist here; the caller applies your swaps to the existing list in code and
  re-validates the result, so only the deltas matter.
- changes_made: a short plain-English note on what you changed and why (or "no changes") —
  should read consistently with the swaps list above, not describe changes not reflected there.
- early_game / mid_game / late_game: 1-3 sentences each describing what this deck is doing at
  that stage of a typical game

In final_summary, changes_made and early_game/mid_game/late_game, name ONLY cards that actually
appear in the decklist above (the commander is fine too). The build brief may reference cards
that were cut before this pass ever ran — the decklist above is the sole authority on what is
in the deck, and any card you name that isn't on it will reach Trevor as a description of a
deck that doesn't exist.

Respond only via the provided JSON schema. Before calling the structured-output tool, narrate your
reasoning out loud — this streams live to whoever is watching the commission. Walk through your
fact-check finding in full, then explain what you're swapping and why, and how the synergy-density
review went. Write a few real paragraphs, not one line. Do NOT enumerate the decklist, or re-list
more than a handful of specific cards, in that reasoning text — the swaps list in the structured
output is the only place card names need to appear.
