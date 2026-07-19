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

Code-computed role counts for this decklist against the deck's role quotas (crude keyword
counts — treat as evidence, not gospel):
$role_scorecard

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

   Use the role scorecard above as hard evidence here: a deck sitting OVER the land/ramp
   quotas while UNDER the draw/interaction/wipe quotas is flooded — it will draw mana and
   nothing to do with it, and it cannot answer three opponents' boards. Rebalance with your
   swaps: trade the most redundant excess-mana slots (the third and fourth copy of the same
   effect) for the missing draw, removal, or sweepers. Respect the deck's mechanic while doing
   so — in a lands-matter deck, extra land drops are theme, so cut the most generic surplus
   first, and prefer replacements that serve double duty with the commander's mechanic.

   Also check for ORPHANED support cards: any card whose payoff depends on a package this
   deck does not actually contain — Food/Treasure/Blood/Clue payoffs without producers,
   discard payoffs without discard outlets, typal payoffs with too few creatures of the type,
   "whenever you sacrifice" cards without sacrifice fodder or outlets. One incidental enabler
   is not a package. Swap orphans for cards whose support genuinely exists in this list; note
   them in changes_made as orphan cuts.

   Also check WIN CONDITION DENSITY: name to yourself at least TWO distinct lines this deck
   can take to actually END a game from a stable board — not "accrue value" or "control the
   table", but close it out. A single win condition with no tutor that finds it is a single
   point of failure: one counterspell, theft, or a forced discard and the deck can lock a
   table it is structurally unable to beat (a real shipped political-control deck had exactly
   one wincon, no tutor for it, and a wheel effect that could discard it). If the deck has
   fewer than two realistic closing lines, swap the weakest value slot for a compact second
   win condition (or a tutor that reliably finds the existing one) that fits the deck's theme;
   note it in changes_made as a wincon-density fix.

   Also check for SELF-DEFEATING cards: any card whose symmetric or downside text hits THIS
   deck's own engine harder than it hits opponents — symmetric damage/destruction sweepers in
   a deck whose plan is a board of small tokens, wheel/discard-all effects in a deck with one
   irreplaceable card it must hold, "each player" gifts in a deck with no way to profit from
   them. Judge against this deck's actual gameplan, not the card's general playability: a
   fine card in the abstract can be a liability in this exact list (a real shipped token deck
   carried three symmetric sweepers that each destroyed its own 1/1 fleet). Keep at most one
   true board reset; swap the rest for one-sided or token-sparing alternatives, and note them
   in changes_made as self-defeating cuts.

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
