Commander: $commander ($color_identity)

The following decklist failed automated validation:

$repair_notes

Current decklist ($card_count cards, should be $deck_size_minus_1):
$current_decklist_block

Return a CORRECTED, complete decklist of exactly $deck_size_minus_1 card names that fixes
every issue listed above while keeping as much of the original gameplan intact as possible.
Replace only the problem cards — do not rebuild the whole deck from scratch unless the notes
require it. Respond only via the provided JSON schema — a flat list of exactly
$deck_size_minus_1 card name strings.

Before calling the structured-output tool, briefly narrate what you're fixing — which cards
are coming out, what's replacing them, and why each replacement resolves its issue. This
streams live to whoever is watching the commission. Keep it to a few sentences per fix, and
do NOT enumerate the rest of the decklist — the full list belongs only in the structured
output.
