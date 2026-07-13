"""atelier/coach.py — the LLM turn coach behind Forge's AtelierCoach hooks.

The forked Forge (third_party/forge-src, forge.ai.AtelierCoach) asks an HTTP
endpoint for a turn plan — threat ranking, cards to hold, activations to
forbid — once per seat per own-turn, and enforces it inside the heuristic AI.
This module is that endpoint: a per-match HTTP server running in the SAME
Python process as forge_engine.run_match, so deck context needs no handoff.

Division of labor (the Ms. Bumbleflower lesson, unchanged): Forge tracks all
state and enumerates all options; the LLM only RANKS players Forge named and
names cards Forge showed it. A bad coach plays worse, never illegally — the
Java side drops anything it can't match and falls back to stock AI on any
timeout or error.

Two LLM stages, both spend-logged via claude_cli:
  - standing orders (config.MODEL_TIERS["coach_orders"]): one call per deck,
    cached at state/coach_orders/<deck_id>.txt — win lines, synergy lines,
    hold/activation discipline. This is the pre-game "deck directive" layer.
  - turn plan (config.MODEL_TIERS["coach_turn"]): up to two cheap calls per
    seat per own-turn — the engine re-consults at start of combat so attack/
    reserve decisions see the post-casting board. The engine's request body
    is already a readable plain-text state block, so it goes into the prompt
    verbatim, plus an oracle-text reference block built here from the local
    Scryfall cache (the coach model must not trust its memory of card text).

Wire protocol (mirrors forge.ai.AtelierCoach): request lines
`token/turn/phase/seat/life/hand/commander[player]/board[player]`; response
lines `attack:`, `hold:`, `forbid:`, `reserve:` with " | "-separated exact
names ("*" = everything), plus `posture:` — "race" forces all-out attacks and
"pressure" forces favorable attacks (both only ever RAISE the engine's own
aggression); `reserve:` names creatures that must not attack this turn (mana
dorks, blockers, value bodies), the per-creature brake on "race". Any failure
returns an empty plan, never an error page.
"""
from __future__ import annotations

import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

from deck_engine import claude_cli, config, scryfall_cache

ORDERS_DIR = config.REPO_ROOT / "state" / "coach_orders"
# One haiku call; Java's atelier.coach.timeout_ms must exceed this. Sized from
# the 2026-07-13 enriched-state game: median 46s, p90 62s, max 72s — the old
# 90s cap cost 5 plans to timeouts.
TURN_CALL_TIMEOUT_S = 120.0
ORDERS_CALL_TIMEOUT_S = 300.0

# Both coach stages are pure text-in/text-out: the model must never touch the
# repo. Before this list existed the orders model wrote its notes to
# standing_orders_*.txt files at the repo root and returned only a summary —
# which then got cached and fed to the turn coach as the "orders".
_NO_SIDE_EFFECT_TOOLS = [
    "Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "WebSearch", "WebFetch",
]

_ORDERS_PROMPT = """You are writing pre-game strategy notes ("standing orders") for a computer \
player piloting a Commander deck. The computer plays real rules-legal Magic but has weak judgement \
about WHEN to deploy things — it tends to dump its hand and fire activated abilities pointlessly. \
Your notes will be consulted every turn by a fast turn-coach.

Reply with the notes as plain message text ONLY. Do NOT create, write, or edit any file — a note \
saved to disk is invisible to the coach; only your reply text reaches it.

Commander: {commander}
Decklist:
{decklist}

Write at most 25 short lines, no headings, no markdown: the deck's win conditions and main synergy \
lines; which cards to hold and for what moment (counterspells, board wipes, combat tricks, protection); \
which activated abilities are only worth firing under specific conditions; and the deck's natural \
posture in a 4-player game (race / develop / control); and which creatures should normally STAY \
HOME instead of attacking — mana dorks, tap-ability utility creatures, dedicated blockers, \
fragile engine pieces (one line starting exactly "STAY HOME:" listing those card names). \
End with one line starting exactly \
"RACE TRIGGER:" — the concrete board condition at which this deck should stop developing and \
attack with everything every turn until the game ends (e.g. "5+ creatures totalling 15+ power", \
"commander equipped and unblockable", "any opponent below 15 life")."""

_TURN_PROMPT = """You are the turn coach for one seat in a 4-player Commander game played by a rules \
engine. Rank threats and set this turn's discipline. Standing orders for this deck:
{orders}

Current game state (from the engine; "seat" is you, "hand" is your hand; "untapped_mana" is each \
player's open mana right now):
{state}

Real rules text of the cards in play and in your hand (trust this over memory — do not guess what \
a card does):
{reference}

Read the phase line: on a pre-combat consult (UPKEEP/MAIN1), focus your hold/forbid calls — what \
to cast now vs later, in what order the synergy demands (enablers and lords BEFORE the pieces that \
profit from them; the payoff AFTER its engine). On a combat-phase consult you are re-planning the \
attack with the board as it stands AFTER casting — focus attack/reserve/posture.
Open mana is interaction risk: attacking or overextending into a seat holding 2+ untapped mana \
(especially one whose colors/deck suggest removal, counterspells or combat tricks) can cost you a \
key creature. Weigh that in reserve (keep the piece home), hold (don't cast the payoff into open \
counter mana if it can wait a turn), and posture. But do not freeze: passing forever loses too.

THE GAME CLOCK OVERRIDES THE STANDING ORDERS. Commander pods should be deciding by turn 8 and over \
by turn 12 — a slow perfect plan loses to the table ending the game first. Your posture call:
- posture "race": the engine attacks with EVERYTHING, ignoring bad blocks. Call it when your board \
can kill an opponent within a swing or two (compare your total power to their life), when you are \
clearly ahead, or on turn 10+ unless attacking would leave you dead to the counterswing.
- posture "pressure": the engine attacks whenever a decent attack exists. This is the DEFAULT from \
turn 6 on, and earlier whenever you have any board.
- posture empty: engine's own judgement. Only right in the first few turns or when you must turtle.
Hold discipline follows posture: hold only cards with a clearly better moment; when racing, hold \
nothing except instant-speed protection or removal. If an opponent is within kill range of your \
board, rank them FIRST on the attack line regardless of who is "scariest" long-term.
"reserve" is the per-creature brake on posture: creatures listed there NEVER attack this turn even \
under race. Reserve your mana dorks, tap-ability utility creatures, and any blocker you need alive \
— but never reserve your real damage; racing with an empty reserve line beats a cautious list.

Reply with EXACTLY these five lines and nothing else. Use the exact player/card names shown above; \
separate multiple names with " | "; leave a line's value empty if nothing applies. "*" means everything.
attack: <opponents you should attack, most threatening first — always rank ALL living opponents>
hold: <cards from your hand or command zone NOT to cast this turn>
forbid: <cards whose activated abilities to skip this turn>
reserve: <your creatures that must NOT attack this turn — dorks, needed blockers, utility>
posture: <race, pressure, or empty>"""

# [ \t]* and not \s* around the colon: \s matches newlines, so an EMPTY value
# line ("forbid:") would swallow the whole next line into its value.
_LINE_RE = re.compile(r"^(attack|hold|forbid|reserve|posture)[ \t]*:[ \t]*(.*)$", re.I | re.M)

# ── Oracle-text grounding for the turn coach ─────────────────────────────────
# The engine's state block is names-only; a haiku-tier coach misremembers what
# half the cards do. Every unique card name in the state gets its REAL rules
# text appended from the local Scryfall bulk cache (face-aware via
# scryfall_cache.oracle_text_of — the Ms. Bumbleflower lesson applies to the
# coach too). Token creatures and basics simply miss the cache and are skipped.
_STATE_CARD_LINE = re.compile(r"^(?:hand|commander\[[^\]]*\]|board\[[^\]]*\])\s*:\s*(.*)$", re.M)
_PT_SUFFIX = re.compile(r"\s*\(\d+/\d+(?:, tapped)?\)\s*$")
_REF_CARD_CHARS = 260     # per-card oracle budget — first sentences carry the mechanics
_REF_TOTAL_CHARS = 9000   # whole block budget; hand/commanders come first so they never get cut

_scryfall: dict | None = None


def _card_reference(state: str) -> str:
    """The oracle-text block for every recognizable card name in a state
    request, hand and commanders first. Empty string when the Scryfall cache
    is unavailable — the coach then runs names-only, as before."""
    global _scryfall
    if _scryfall is None:
        try:
            _scryfall = scryfall_cache.load_cache()
        except Exception:
            _scryfall = {}
    if not _scryfall:
        return ""
    names: list[str] = []
    seen: set[str] = set()
    for match in _STATE_CARD_LINE.finditer(state):
        for raw in match.group(1).split("|"):
            name = _PT_SUFFIX.sub("", raw).strip()
            key = name.lower()
            if name and key not in seen:
                seen.add(key)
                names.append(name)
    lines: list[str] = []
    total = 0
    for name in names:
        card = _scryfall.get(name.lower())
        if card is None:
            continue
        type_line = str(card.get("type_line") or "")
        if type_line.startswith("Basic Land"):
            continue   # reminder text only — the coach knows what a Forest does
        text = scryfall_cache.oracle_text_of(card).replace("\n", " ").strip()
        if not text:
            continue
        if len(text) > _REF_CARD_CHARS:
            text = text[: _REF_CARD_CHARS - 1].rstrip() + "…"
        line = f"- {card.get('name', name)} [{card.get('type_line', '')}]: {text}"
        if total + len(line) > _REF_TOTAL_CHARS:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def standing_orders(deck: dict, run_id: str) -> str:
    """The cached per-deck directive sheet, generating it on first use.

    The cache name carries a version suffix: bump it whenever _ORDERS_PROMPT
    changes materially, so every deck regenerates under the new prompt instead
    of serving stale directives forever (v2: RACE TRIGGER line)."""
    deck_id = str(deck.get("id") or deck.get("deck_id") or "unknown")
    ORDERS_DIR.mkdir(parents=True, exist_ok=True)
    cache = ORDERS_DIR / f"{deck_id}.v2.txt"
    if cache.exists() and cache.stat().st_size > 0:
        return cache.read_text()
    commander = str(deck.get("commander") or "")
    decklist = "\n".join(
        str(row.get("name") or "") for row in (deck.get("cards") or []) if row.get("name")
    )
    result = claude_cli.run(
        _ORDERS_PROMPT.format(commander=commander, decklist=decklist),
        run_id=run_id, stage=f"coach/orders/{commander[:24]}",
        model_tier_key="coach_orders", timeout_s=ORDERS_CALL_TIMEOUT_S,
        disallowed_tools=_NO_SIDE_EFFECT_TOOLS,
    )
    text = (result.text or "").strip()
    if text:
        cache.write_text(text)
    return text


class CoachServer:
    """Per-match coach endpoint. start() binds an ephemeral port; the caller
    passes it to Forge as -Datelier.coach.url=http://127.0.0.1:<port>/coach."""

    def __init__(self, decks_by_seat: dict[int, dict], run_id: str | None = None):
        self.decks_by_seat = decks_by_seat
        self.run_id = run_id or f"coach-{uuid.uuid4().hex[:8]}"
        self.token = uuid.uuid4().hex
        self.calls = 0
        self.failures = 0
        # Every plan served, in order: {turn, seat, plan} — persisted with the
        # match receipts so coaching decisions can be audited after the game.
        self.plans: list[dict] = []
        self._orders: dict[int, str] = {}
        self._server: HTTPServer | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> int:
        """Generate/load standing orders for every seat, then serve. Returns the port."""
        for seat, deck in self.decks_by_seat.items():
            try:
                self._orders[seat] = standing_orders(deck, self.run_id)
            except Exception:
                self._orders[seat] = ""   # coach still works, just less deck-aware
        coach = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8", "replace")
                payload = coach._respond(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self._server.server_address[1]

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    # -- request handling ----------------------------------------------------
    def _respond(self, body: str) -> str:
        try:
            fields = {}
            for line in body.splitlines():
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()
            if self.token and fields.get("token") != self.token:
                return ""   # not our engine process — empty plan
            seat_match = re.match(r"Ai\((\d+)\)-", fields.get("seat", ""))
            seat = int(seat_match.group(1)) if seat_match else 0
            plan = self._plan(seat, body)
            self.calls += 1
            self.plans.append({"turn": fields.get("turn", ""), "phase": fields.get("phase", ""),
                               "seat": seat, "plan": plan})
            return plan
        except Exception:
            self.failures += 1
            return ""

    def _plan(self, seat: int, state: str) -> str:
        result = claude_cli.run(
            _TURN_PROMPT.format(orders=self._orders.get(seat) or "(none)", state=state,
                                reference=_card_reference(state) or "(no card reference available)"),
            run_id=self.run_id, stage=f"coach/turn/seat{seat}",
            model_tier_key="coach_turn", timeout_s=TURN_CALL_TIMEOUT_S,
            disallowed_tools=_NO_SIDE_EFFECT_TOOLS,
        )
        lines = []
        for match in _LINE_RE.finditer(result.text or ""):
            lines.append(f"{match.group(1).lower()}: {match.group(2).strip()}")
        return "\n".join(lines)
