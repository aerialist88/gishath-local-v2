Commander: $commander ($color_identity)
Archetype: $archetype
Selector's rationale: $rationale

Commander's ACTUAL printed ability (verified against Scryfall — base every claim on this text,
not on what you recall about the card from memory, which may be wrong or from a different
card entirely):
"""
$commander_oracle_text
"""

Bracket $bracket house rules:
- Game Changers: $game_changers
- Tutors: $tutors
- Two-card infinite combos: $combo_rule (backup wincon only, never the primary gameplan)
- Mass land destruction: $mld
$retry_note
You are drafter $angle_index of $angle_total working in parallel tonight — each of you commits
to ONE distinct build angle and drafts a complete deck around it. A judge will compare the
finished drafts and pick a winner, so a bold, coherent take beats a hedged consensus list:
make your angle genuinely different from the generic "obvious" build of this commander.

First, commit to your angle: a specific way to lean into this commander+archetype pairing —
a particular subtheme, synergy package, or unusual line other pilots might not take. Ground it
in the commander's specific mechanic above, not just its colors. If a generic same-colored
commander could pilot your list just as well, the angle isn't using what makes THIS commander
distinct — a pile of good ramp/removal/draw in the right colors is not a build angle. Every
synergy you rely on must follow from the commander's actual printed text above; if the
archetype label suggests an ability the card doesn't have, build around what it really does.

Then draft the deck: a complete $deck_size_minus_1-card decklist (the commander, $commander,
is separate and NOT included in this list — this is the other $deck_size_minus_1 cards only)
for a singleton Commander deck in $commander's color identity ($color_identity). Every card must:
- Be a real Magic: The Gathering card (exact printed name)
- Be legal in the Commander format and not on the banned list
- Be within $commander's color identity (colorless is always fine)
- Appear only once (singleton), except basic lands or cards that explicitly allow multiples

Target role-count ranges for this draft (treat as targets, not hard limits, if your angle
genuinely calls for a different shape — e.g. a low-curve aggressive plan wanting fewer lands):
$role_quota_block

Include an appropriate mana base, ramp, card draw, interaction/removal, and win conditions
consistent with your angle and the bracket house rules.

Avoid habit includes — cards that need support this deck doesn't actually have. Path of
Ancestry is the canonical trap: a tapped land whose scry needs a real typal overlap with the
commander, which most decks don't have — take an untapped land or a basic instead. The same
test applies to any "value" land or support card: token-type payoffs (Food/Treasure/Clue)
without producers, discard payoffs without outlets, typal payoffs without the tribe. If its
value depends on a package you aren't running, it's filler, not value.

This deck must be built around $commander's specific mechanic (per the oracle text above), not
just its color identity — at least $on_mechanic_min of your card choices should meaningfully
interact with that mechanic: enable it, trigger it more often, or capitalize on what it produces.
Generic ramp/removal/draw fills out the supporting shell but should not be the majority of what
defines this deck. If you're including a card mainly because it's "solid in these colors" rather
than because it interacts with the commander's actual mechanic, look for a more synergistic
alternative first — only fall back to the generic pick if nothing on-mechanic fits.

Candidate synergy pool for $commander (from EDHREC, ranked roughly by how often other pilots play
these cards with this commander) — a CANDIDATE LIST, not a required or exhaustive list. Aim to
pull roughly 60 of your card choices from this pool where they genuinely fit your angle and
bracket rules; off-pool inclusions are fine and often better — the house rule to favour
unorthodox builds still applies, and this pool is consensus data, not a ceiling on creativity. Use
your angle to decide WHICH part of the pool to lean into, not to copy the pool wholesale.

$edhrec_pool_block

Respond only via the provided JSON schema: your angle's name, a short gameplan summary, the 5-10
key_cards your gameplan actually depends on (exact printed names — the judge gets their real
oracle text as ground truth), then the decklist in TWO parts: `lands` — exactly 36 lands, each
listed individually (basics repeat: writing 'Mountain' six times is six Mountains; nonbasic
lands are singletons) — and `nonlands` — exactly 63 distinct nonland cards. Together they are
your $deck_size_minus_1; budget the 36 land slots as part of your manabase thinking.
Before calling the structured-output tool, narrate your reasoning out loud — this streams live to
whoever is watching the commission, alongside the other drafters working in parallel, so make it
genuinely worth reading: the angle you're committing to, how you're shaping the manabase and
curve, which packages/synergies you're leaning on and why, any tensions you're weighing. Write a
few real paragraphs, not one line. Do NOT enumerate the decklist, or any large portion of it, in
that reasoning text — the actual card names belong ONLY in the structured JSON output, not
written out twice.
