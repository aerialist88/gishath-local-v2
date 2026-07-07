"""atelier/demo.py — a scripted rehearsal commission.

Plays a realistic ~40 second night through the same RunEventLog a real run
uses — every screen state (stage gauge, three parallel drafting benches each
building a whole deck, the Adjudicator's judging pass, the Master
Deckwright's repair pass, the ledger, live cost tickers) can be exercised
without an authenticated `claude` session or a cent of API spend.
Sample text follows the design handoff's Braids exploration; if the gallery
has a real newest deck, the delivered event points at it so "open in the
gallery" lands somewhere real.
"""
from __future__ import annotations

import time

from .runner import RunEventLog

_DRAFT_STREAMS = [
    ("draft/attempt1/1",
     "...Braids wants permanents hitting the yard every turn. Sacrifice outlets first: "
     "Viscera Seer, Carrion Feeder, Woe Strider — free, repeatable, instant-speed. Then the "
     "engine's fuel: token makers that replace themselves. Ophiomancer and Bitterblossom give "
     "a body every upkeep, which turns Braids' symmetrical trigger into pure upside for us. "
     "Committing to the outlet-engine angle and drafting around it: 36 lands, heavy swamp "
     "count for Coffers lines, ten ramp pieces that double as sacrifice fodder. The token "
     "layer needs nine repeatable makers before the outlets earn their slots..."),
    ("draft/attempt1/2",
     "...the draw trigger is the real engine. Every player may sacrifice — but only WE are "
     "built to profit. Black Market Connections, Morbid Opportunist, Village Rites: the deck "
     "should draw two-plus cards a turn cycle once the board is set. Deadly Dispute is the "
     "best card in the 99 and I will die on this hill. Drafting the full list around card "
     "advantage as the wincon's fuel: twelve draw engines, a lean 35-land base since the "
     "deck refills itself, and a top end that converts a full grip into the table's problem..."),
    ("draft/attempt1/3",
     "...lean into the punishment angle: Grave Pact, Dictate of Erebos, Butcher of Malakir. "
     "Each of our free sacrifices becomes an edict for the table. Pair with recursion — "
     "Reassembling Skeleton, Nether Traitor — so our own losses are rentals, not purchases. "
     "The draft leans dense on the edict package: eight punishment effects, a recursion "
     "suite of nine, and enough token fodder that we always have something cheap to feed "
     "the machine while opponents sacrifice real cards..."),
]

# Extended-thinking rehearsal text — streamed as call_thinking BEFORE the
# visible reply, mirroring what a real run does now that the pipeline enables
# thinking (config.THINKING_BUDGET_TOKENS) and surfaces the reasoning stream.
_SELECT_THINKING = (
    "The dedupe window rules out the last 30 commanders, so the graveyard decks are mostly "
    "taken. What archetypes haven't shipped lately? No aristocrats deck in the log. Braids, "
    "Arisen Nightmare is underplayed relative to her power — symmetrical effects price in an "
    "apparent fairness the table misjudges. Checking her oracle text before committing..."
)

_DRAFT_THINKING = (
    "Start from the role quota defaults: 35-38 lands, 10-12 ramp, 8-10 draw. The sacrifice "
    "outlets need to be free and instant-speed or the edict lines don't work on other "
    "players' turns. Counting on-mechanic slots... 31 so far, above the 28 floor. Mana "
    "curve is bunching at three — trade two three-drops for Bitterblossom and Village "
    "Rites to flatten it..."
)

_JUDGE_STREAM = (
    "...three honest drafts on the table. Draft 1 is the tightest engine — the outlet count "
    "means Braids' trigger is never a real cost, and its on-mechanic count leads at 34. "
    "Draft 2's draw suite is the best six cards of the night but the 35-land base is greedy "
    "for a deck this hungry. Draft 3's edict package wins longest games yet leans on the "
    "commander surviving, which mono-black can't promise. Verdict: draft 1 takes the "
    "commission, borrowing Black Market Connections and Deadly Dispute from draft 2 — the "
    "engine deserves their fuel..."
)

_REPAIR_STREAM = (
    "...replacing the two illegal inclusions. Braids' color identity is mono-black, so "
    "Deathrite Shaman is out — Deadly Rollick fills the free-removal role within identity. "
    "For the hallucinated \"Nether Vault\", substituting Oversold Cemetery, a real card "
    "covering the same recursion line..."
)

_OPTIMIZE_STREAM = (
    "...fact-checking the premise against the oracle text: Braids triggers at the beginning "
    "of EACH player's upkeep — the deck correctly builds around giving opponents nothing to "
    "sacrifice profitably. Swapping two slow sorceries for instant-speed value: Village Rites "
    "over Sign in Blood, Malakir Rebirth over Raise Dead. Curve lands at 2.87 average..."
)


def _stream(log: RunEventLog, label: str, text: str, *, chunk: int = 10, delay: float = 0.08,
            etype: str = "call_text") -> None:
    for i in range(0, len(text), chunk):
        log.emit(etype, label=label, chunk=text[i:i + chunk])
        time.sleep(delay)


def play(log: RunEventLog) -> None:  # noqa: PLR0915 — a script reads top to bottom
    run_id = log.run_id

    def announce(text: str) -> None:
        log.emit("announce", text=text)

    log.emit("run_started", run_id=run_id, forced_commander=None, demo=True)
    log.emit("stage", stage="scryfall_cache")
    announce("[demo] rehearsal commission — no API calls are made, nothing is emailed")
    time.sleep(0.8)

    # ── select ────────────────────────────────────────────────────────────
    log.emit("stage", stage="select")
    log.emit("call_started", label="select/1", model="opus")
    log.emit("call_status", label="select/1", status="thinking it through...")
    _stream(log, "select/1", _SELECT_THINKING, delay=0.03, etype="call_thinking")
    log.emit("call_status", label="select/1", status="drafting...")
    _stream(log, "select/1",
            "...scanning the last 30 nights' commanders to stay clear of repeats. Tonight wants "
            "something the table underestimates: Braids, Arisen Nightmare — a symmetrical "
            "sacrifice engine everyone reads as fair until the edicts start...",
            delay=0.06)
    log.emit("call_status", label="select/1", status="packaging structured output...")
    time.sleep(0.7)
    log.emit("call_finished", label="select/1", cost_usd=0.0512, num_turns=2,
             duration_s=9.0, is_error=False)
    log.emit("concept", commander="Braids, Arisen Nightmare",
             archetype="Mono-black aristocrats",
             rationale="A symmetrical-sacrifice engine, drafted in black — every permanent feeds the engine.",
             colors=["B"])
    announce(f"[run {run_id[:8]}] concept: Braids, Arisen Nightmare — Mono-black aristocrats")
    time.sleep(0.5)

    # ── draft: three whole decks, three parallel benches ─────────────────
    log.emit("stage", stage="draft/judge/validate/optimize")
    for label, _ in _DRAFT_STREAMS:
        log.emit("call_started", label=label, model="sonnet")
        log.emit("call_status", label=label, status="thinking it through...")
    # a shared thinking beat first, then interleave the three visible streams
    # so the benches visibly run in parallel
    for label, _ in _DRAFT_STREAMS:
        _stream(log, label, _DRAFT_THINKING, chunk=18, delay=0.01, etype="call_thinking")
        log.emit("call_status", label=label, status="drafting...")
    texts = {label: text for label, text in _DRAFT_STREAMS}
    positions = {label: 0 for label, _ in _DRAFT_STREAMS}
    while any(positions[lbl] < len(txt) for lbl, txt in texts.items()):
        for lbl, txt in texts.items():
            pos = positions[lbl]
            if pos < len(txt):
                log.emit("call_text", label=lbl, chunk=txt[pos:pos + 10])
                positions[lbl] = pos + 10
        time.sleep(0.12)
    for i, (label, _) in enumerate(_DRAFT_STREAMS):
        log.emit("call_status", label=label, status="packaging structured output...")
        time.sleep(0.3)
        log.emit("call_finished", label=label, cost_usd=0.4212 + i * 0.031, num_turns=2,
                 duration_s=41.0 + 3 * i, is_error=False)

    # ── judge: the adjudicator weighs the drafts ──────────────────────────
    log.emit("call_started", label="judge/attempt1", model="sonnet")
    _stream(log, "judge/attempt1", _JUDGE_STREAM, delay=0.05)
    log.emit("call_status", label="judge/attempt1", status="packaging structured output...")
    time.sleep(0.5)
    log.emit("call_finished", label="judge/attempt1", cost_usd=0.1922, num_turns=2,
             duration_s=14.0, is_error=False)
    announce(f"[run {run_id[:8]}] judge: draft 1 wins — 2 cherry-pick(s) from the losing benches")

    # ── validate + repair (the Master Deckwright) ─────────────────────────
    announce(f"[run {run_id[:8]}] validate: 3 flaw(s) found — repair pass 1 of 2")
    log.emit("call_started", label="draft/attempt1/repair-1", model="sonnet")
    _stream(log, "draft/attempt1/repair-1", _REPAIR_STREAM, delay=0.08)
    log.emit("call_finished", label="draft/attempt1/repair-1", cost_usd=0.0512, num_turns=3,
             duration_s=13.0, is_error=False)
    announce(f"[run {run_id[:8]}] deck validated: True (99 cards + commander)")

    # ── optimize ──────────────────────────────────────────────────────────
    log.emit("call_started", label="optimize/attempt1", model="opus")
    _stream(log, "optimize/attempt1", _OPTIMIZE_STREAM, delay=0.07)
    log.emit("call_finished", label="optimize/attempt1", cost_usd=0.2104, num_turns=2,
             duration_s=19.0, is_error=False)

    # ── price / budget / export / deliver ────────────────────────────────
    log.emit("stage", stage="price")
    announce(f"[run {run_id[:8]}] pricing 100 cards across the store scrapers...")
    time.sleep(1.6)
    log.emit("stage", stage="budget")
    announce("[demo] budget pass: 1 swap, 2 cards over cap — shipping flagged")
    log.emit("budget_swaps", swaps=[
        {"remove": "Ashnod's Altar", "removed_price": 18.60, "add": "Phyrexian Tower",
         "added_price": 6.40, "reason": "same free-sac role, well under the per-card cap"},
    ])
    time.sleep(0.8)
    log.emit("stage", stage="export")
    announce(f"[run {run_id[:8]}] wrote output/(demo — nothing actually written)")
    time.sleep(0.6)
    log.emit("stage", stage="deliver")
    time.sleep(0.5)

    # Point "open in the gallery" at the newest real deck if there is one.
    deck_id = ""
    try:
        from . import archive
        decks = archive.list_decks()
        if decks:
            deck_id = decks[0]["id"]
    except Exception:  # noqa: BLE001
        pass
    log.emit("delivered", run_id=run_id, deck_id=deck_id, deck_json="", xlsx="",
             moxfield_txt="", cost_usd=1.8622, turns=15)
