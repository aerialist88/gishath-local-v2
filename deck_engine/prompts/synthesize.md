Commander: $commander ($color_identity)
Archetype: $archetype

Commander's ACTUAL printed ability (verified against Scryfall):
"""
$commander_oracle_text
"""

$angle_total build angles were explored in parallel for tonight's deck:

$angles_block

Pick the single most promising angle (or merge the best elements of two, if they combine
cleanly), and write a short build brief for the deck-builder to work from: the core gameplan,
5-10 key cards or card types to prioritize, and what "winning" looks like. Favour the more
unorthodox/novel angle when angles are close in quality — per house rules, this deck should
not be another generic staples pile. Reject or fix any angle whose gameplan depends on an
ability the commander's actual text above doesn't support.

Also weigh how tightly each angle actually exploits the commander's specific mechanic, not just
its colors or a loosely-related theme — prefer the angle that most directly showcases what this
commander uniquely does over one that's really just generic goodstuff in the right colors with
the commander attached. The build brief you write should make that mechanic the deck's central
identity, not a footnote.

Also return `key_cards`: a flat list of the SPECIFIC, real card names (exact printed names,
not card types or themes) that this gameplan actually depends on — these will have their real
oracle text pulled and handed to the builder as ground truth, so name the load-bearing pieces
precisely (5-10 cards, not the whole deck).

Also return `role_quotas`: role-count RANGES for the builder to target. Defaults are 35-38 lands,
10-12 ramp, 8-10 card draw, 8-10 interaction/removal, 2-3 board wipes, and at least 28 cards
meaningfully on-mechanic with the commander — repeat these defaults unless something about THIS
specific build brief genuinely argues for a different shape (e.g. a low-curve aggressive plan
wanting fewer lands and more spells, or a control-leaning plan wanting more interaction). Don't
adjust the ranges just to seem thorough — only move them if the brief calls for it.

Respond only via the provided JSON schema.
