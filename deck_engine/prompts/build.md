Commander: $commander ($color_identity)
Archetype: $archetype
Build brief: $build_brief

Commander's ACTUAL printed ability (verified against Scryfall — the build brief above must be
read through this text, not through what you recall about the card; if they conflict, this
text wins):
"""
$commander_oracle_text
"""

Real printed text for the key cards this build brief depends on (verified against Scryfall):
$key_cards_oracle_text

Bracket $bracket house rules:
- Game Changers: $game_changers
- Tutors: $tutors
- Two-card infinite combos: $combo_rule (backup wincon only — do not build around a combo as
  the primary win condition)
- Mass land destruction: $mld

Build a complete $deck_size_minus_1-card decklist (the commander, $commander, is separate and
NOT included in this list — this is the other $deck_size_minus_1 cards only) for a singleton
Commander deck in $commander's color identity ($color_identity). Every card must:
- Be a real Magic: The Gathering card (exact printed name)
- Be legal in the Commander format and not on the banned list
- Be within $commander's color identity (colorless is always fine)
- Appear only once (singleton), except basic lands or cards that explicitly allow multiples

Target role-count ranges for this build (adjusted from the defaults by the synthesize stage for
this specific brief — treat these as targets, not hard limits, if the brief genuinely calls for
a different shape):
$role_quota_block

Include an appropriate mana base, ramp, card draw, interaction/removal, and win conditions
consistent with the build brief and bracket house rules.

This deck must be built around $commander's specific mechanic (per the oracle text above), not
just its color identity — at least $on_mechanic_min of your card choices should meaningfully
interact with that mechanic: enable it, trigger it more often, or capitalize on what it produces.
Generic ramp/removal/draw fills out the supporting shell but should not be the majority of what
defines this deck. If you're including a card mainly because it's "solid in these colors" rather
than because it interacts with the commander's actual mechanic, look for a more synergistic
alternative first — only fall back to the generic pick if nothing on-mechanic fits.

Candidate synergy pool for $commander (from EDHREC, ranked roughly by how often other pilots play
these cards with this commander) — a CANDIDATE LIST, not a required or exhaustive list. Aim to
pull roughly 60 of your card choices from this pool where they genuinely fit the build brief and
bracket rules; off-pool inclusions are fine and often better — the house rule to favour
unorthodox builds still applies, and this pool is consensus data, not a ceiling on creativity. Use
the build brief above to decide WHICH part of the pool to lean into, not to copy the pool
wholesale.

$edhrec_pool_block

Respond only via the provided JSON schema — a flat list of exactly $deck_size_minus_1 card name
strings. Before calling the structured-output tool, narrate your reasoning out loud — this streams
live to whoever is watching the commission, so make it genuinely worth reading: your read on the
build brief, how you're shaping the manabase and curve, which packages/synergies you're leaning on
and why, any tensions you're weighing. Write a few real paragraphs, not one line. Do NOT enumerate
the decklist, or any large portion of it, in that reasoning text — the actual card names belong
ONLY in the structured JSON output, not written out twice.
