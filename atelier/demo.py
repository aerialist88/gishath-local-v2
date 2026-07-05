"""atelier/demo.py — a scripted rehearsal commission.

Plays a realistic ~40 second night through the same RunEventLog a real run
uses — every screen state (stage gauge, three parallel apprentice benches,
the Master Deckwright's repair pass, the ledger, live cost tickers) can be
exercised without an authenticated `claude` session or a cent of API spend.
Sample text follows the design handoff's Braids exploration; if the gallery
has a real newest deck, the delivered event points at it so "open in the
gallery" lands somewhere real.
"""
from __future__ import annotations

import time

from .runner import RunEventLog

_IDEATE_STREAMS = [
    ("ideate/attempt1/1",
     "...Braids wants permanents hitting the yard every turn. Sacrifice outlets first: "
     "Viscera Seer, Carrion Feeder, Woe Strider — free, repeatable, instant-speed. Then the "
     "engine's fuel: token makers that replace themselves. Ophiomancer and Bitterblossom give "
     "a body every upkeep, which turns Braids' symmetrical trigger into pure upside for us..."),
    ("ideate/attempt1/2",
     "...the draw trigger is the real engine. Every player may sacrifice — but only WE are "
     "built to profit. Black Market Connections, Morbid Opportunist, Village Rites: the deck "
     "should draw two-plus cards a turn cycle once the board is set. Deadly Dispute is the "
     "best card in the 99 and I will die on this hill..."),
    ("ideate/attempt1/3",
     "...lean into the punishment angle: Grave Pact, Dictate of Erebos, Butcher of Malakir. "
     "Each of our free sacrifices becomes an edict for the table. Pair with recursion — "
     "Reassembling Skeleton, Nether Traitor — so our own losses are rentals, not purchases..."),
]

_BUILD_STREAM = (
    "...locking the manabase at 36 lands with heavy swamp count for Cabal Coffers lines. "
    "Role quotas from the brief: 10 ramp, 12 draw engines, 9 sacrifice outlets, 10 edict "
    "effects, recursion package of 9. Filling the last flex slots with Pawn of Ulamog and "
    "Sifter of Skulls — Eldrazi Spawn tokens feed the outlets AND ramp us into the top of "
    "the curve..."
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


def _stream(log: RunEventLog, label: str, text: str, *, chunk: int = 10, delay: float = 0.08) -> None:
    for i in range(0, len(text), chunk):
        log.emit("call_text", label=label, chunk=text[i:i + chunk])
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

    # ── ideate: three parallel benches ───────────────────────────────────
    log.emit("stage", stage="ideate/build/validate/optimize")
    for label, _ in _IDEATE_STREAMS:
        log.emit("call_started", label=label, model="sonnet")
    # interleave the three streams so they visibly run in parallel
    texts = {label: text for label, text in _IDEATE_STREAMS}
    positions = {label: 0 for label, _ in _IDEATE_STREAMS}
    while any(positions[lbl] < len(txt) for lbl, txt in texts.items()):
        for lbl, txt in texts.items():
            pos = positions[lbl]
            if pos < len(txt):
                log.emit("call_text", label=lbl, chunk=txt[pos:pos + 10])
                positions[lbl] = pos + 10
        time.sleep(0.14)
    for i, (label, _) in enumerate(_IDEATE_STREAMS):
        log.emit("call_status", label=label, status="packaging structured output...")
        time.sleep(0.3)
        log.emit("call_finished", label=label, cost_usd=0.0312 + i * 0.004, num_turns=2,
                 duration_s=11.0 + i, is_error=False)

    # ── synthesize / build ────────────────────────────────────────────────
    log.emit("call_started", label="synthesize/attempt1", model="sonnet")
    _stream(log, "synthesize/attempt1",
            "...merging the three angles: sacrifice outlets as the spine, draw engines as the "
            "payoff, edicts as the win pressure. Role quotas set for the build bench...",
            delay=0.06)
    log.emit("call_finished", label="synthesize/attempt1", cost_usd=0.0289, num_turns=2,
             duration_s=8.0, is_error=False)
    log.emit("call_started", label="build/attempt1", model="sonnet")
    _stream(log, "build/attempt1", _BUILD_STREAM, delay=0.07)
    log.emit("call_status", label="build/attempt1", status="packaging structured output...")
    time.sleep(0.6)
    log.emit("call_finished", label="build/attempt1", cost_usd=0.1373, num_turns=2,
             duration_s=24.0, is_error=False)

    # ── validate + repair (the Master Deckwright) ─────────────────────────
    announce(f"[run {run_id[:8]}] validate: 3 flaw(s) found — repair pass 1 of 2")
    log.emit("call_started", label="validate_repair/repair-1", model="sonnet")
    _stream(log, "validate_repair/repair-1", _REPAIR_STREAM, delay=0.08)
    log.emit("call_finished", label="validate_repair/repair-1", cost_usd=0.0512, num_turns=3,
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
             moxfield_txt="", cost_usd=0.5102, turns=15)
