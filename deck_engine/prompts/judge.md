Commander: $commander ($color_identity)
Archetype: $archetype

Commander's ACTUAL printed ability (verified against Scryfall):
"""
$commander_oracle_text
"""

$draft_total complete draft decks were built in parallel for tonight's commission, each
committed to a different angle. Your job is to judge them: pick the single strongest draft as
the base deck, then optionally cherry-pick individual cards from the losing drafts where they
are a strict upgrade for the winning gameplan.

Each draft below lists its angle, gameplan, key cards (with their REAL printed oracle text —
the only authority on what those cards do), its full decklist, and a code-computed count of how
many of its cards mechanically match the commander's extracted mechanic keywords (a rough
on-mechanic signal, not the whole story):

$drafts_block

Judging criteria, in order:
1. Reject any draft whose gameplan depends on an ability the commander's actual text above
   doesn't support — a coherent deck built on a false premise loses to a rougher honest one.
2. Prefer the draft that most tightly exploits the commander's specific mechanic — not just its
   colors or a loosely-related theme. The on-mechanic counts above are evidence, but read the
   lists yourself; a generic goodstuff pile with the commander stapled on should not win.
3. Per house rules, favour the more unorthodox/novel angle when drafts are close in quality —
   this deck should not be another generic staples pile.
4. Structural soundness: a sane manabase, curve, and enough ramp/draw/interaction to actually
   function. Use the code-computed structural counts in each draft's header as hard evidence
   here: a draft that is far off the required card count, or far under the land quota, will
   burn repair passes that cut cards blindly after your decision — treat that as a real defect
   when comparing otherwise-close drafts, not a cosmetic one.

Return, via the provided JSON schema:
- `chosen_draft`: the 1-based number of the winning draft.
- `build_brief`: a short brief describing the winning deck's core gameplan, the key synergy
  packages, and what "winning" looks like — written as the reference document the later
  optimization pass will fact-check the deck against.
- `key_cards`: a flat list of the SPECIFIC, real card names (exact printed names, not card types
  or themes) the winning gameplan actually depends on — 5-10 cards, not the whole deck. These
  will have their real oracle text pulled as ground truth for every later stage.
- `swaps`: cherry-picks applied to the WINNING draft's decklist — each removes one card from the
  winning list and adds one card (typically from a losing draft) that serves the winning gameplan
  strictly better. Keep this surgical: an empty list is the right answer if the winning draft is
  already coherent; never restructure the deck's identity through swaps. The swapped-in cards
  must respect color identity and singleton rules.

Before calling the structured-output tool, narrate your deliberation out loud — this streams
live to whoever is watching: what each draft got right, what sank the losers, why the winner
wins. A few short paragraphs. Do not enumerate full decklists in that narration.
