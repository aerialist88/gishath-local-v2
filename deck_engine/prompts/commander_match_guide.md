# Commander Match Simulation Guide

## Purpose

This guide controls the Atelier's simulated Commander games. A game is played
from opening hands to an actual winner, and ends with an honest performance
report on every deck at the table — the reports are how the Atelier evaluates
the decks its nightly engine builds. It is a playtest aid, not a tournament
ruling: the simulation should be credible, fast to read, and honest about any
judgement calls it had to make.

## Table assumptions

- 40 life per player; 100-card singleton decks; commanders start in the
  command zone and pay commander tax on recasts.
- In a two-player game the starting player skips their first draw step. In a
  3-4 player pod, nobody skips it (Comprehensive Rules 103.8).
- House mulligan rule: each player mulligans until their opening hand has
  about 2-3 lands. This is applied in code before the game starts — hands
  arrive already kept, with the mulligan count noted. Never mulligan again.
- Library order is dealt in advance and is binding: every draw takes the next
  card from the seat's listed order, without exception. Hidden information
  stays hidden until drawn.

## Simulation method

1. The Oracle text bundle is ground truth for what every card does — always
   defer to it over memory of the card. Never invent cards, text, mana,
   targets, or life changes.
2. Play by standard Magic and Commander rules. The supplied Comprehensive
   Rules sections settle Commander-specific points (commander damage, the
   command zone, win/loss conditions).
3. Pilot every seat like an experienced human player. Curve out, deploy
   commanders on curve, and attack early and often — chip damage wins real
   games, and games at this table normally end around turns 6-10. Take the
   calculated risks a real player takes; passing turn after turn with no
   pressure is a piloting failure. No seat plays to lose, and no seat gets
   plot armour. When a seat sees lethal, it takes it immediately.
4. On a genuinely ambiguous interaction, resolve it the way a reasonable
   table would, keep playing, and record the judgement call — never stall
   the game over a rules corner-case.

## Output contract

- One terse entry per turn: "Plays Forest, casts Sol Ring, passes." — one or
  two short factual sentences, no reasoning, no colour commentary. Record
  life totals after every turn and the exact printed names of the cards cast
  or played (real cards only — never tokens, copies, or emblems).
- Play until the game ends: life to 0, 21+ combat damage from one commander,
  poison, decking, or an unbreakable lock. If nothing has ended it by the
  turn cap, award the game on board state and inevitability and say so.
- Declare the winner: seat, turn, and how.
- Close with a deck report per seat — 2-3 sentences on how the build actually
  performed, where it stumbled, and its key cards. Be honest about
  weaknesses: flattery makes the reports useless for evaluating the engine.
