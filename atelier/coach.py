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
  - turn plan (config.MODEL_TIERS["coach_turn"]): one cheap call per seat per
    own-turn (~10-15 per seat per game). The engine's request body is already
    a readable plain-text state block, so it goes into the prompt verbatim.

Wire protocol (mirrors forge.ai.AtelierCoach): request lines
`token/turn/phase/seat/life/hand/commander[player]/board[player]`; response
lines `attack:`, `hold:`, `forbid:` with " | "-separated exact names
("*" = everything). Any failure returns an empty plan, never an error page.
"""
from __future__ import annotations

import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

from deck_engine import claude_cli, config

ORDERS_DIR = config.REPO_ROOT / "state" / "coach_orders"
TURN_CALL_TIMEOUT_S = 90.0   # one haiku call; Java's atelier.coach.timeout_ms must exceed this
ORDERS_CALL_TIMEOUT_S = 300.0

_ORDERS_PROMPT = """You are writing pre-game strategy notes ("standing orders") for a computer \
player piloting a Commander deck. The computer plays real rules-legal Magic but has weak judgement \
about WHEN to deploy things — it tends to dump its hand and fire activated abilities pointlessly. \
Your notes will be consulted every turn by a fast turn-coach.

Commander: {commander}
Decklist:
{decklist}

Write at most 25 short lines, no headings, no markdown: the deck's win conditions and main synergy \
lines; which cards to hold and for what moment (counterspells, board wipes, combat tricks, protection); \
which activated abilities are only worth firing under specific conditions; and the deck's natural \
posture in a 4-player game (race / develop / control)."""

_TURN_PROMPT = """You are the turn coach for one seat in a 4-player Commander game played by a rules \
engine. Rank threats and set this turn's discipline. Standing orders for this deck:
{orders}

Current game state (from the engine; "seat" is you, "hand" is your hand):
{state}

Reply with EXACTLY these three lines and nothing else. Use the exact player/card names shown above; \
separate multiple names with " | "; leave a line's value empty if nothing applies. "*" means everything.
attack: <opponents you should attack, most threatening first — always rank ALL living opponents>
hold: <cards from your hand or command zone NOT to cast this turn>
forbid: <cards whose activated abilities to skip this turn>"""

_LINE_RE = re.compile(r"^(attack|hold|forbid)\s*:\s*(.*)$", re.I | re.M)


def standing_orders(deck: dict, run_id: str) -> str:
    """The cached per-deck directive sheet, generating it on first use."""
    deck_id = str(deck.get("id") or deck.get("deck_id") or "unknown")
    ORDERS_DIR.mkdir(parents=True, exist_ok=True)
    cache = ORDERS_DIR / f"{deck_id}.txt"
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
        disallowed_tools=config.DISALLOWED_SEARCH_TOOLS if hasattr(config, "DISALLOWED_SEARCH_TOOLS") else None,
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
            return plan
        except Exception:
            self.failures += 1
            return ""

    def _plan(self, seat: int, state: str) -> str:
        result = claude_cli.run(
            _TURN_PROMPT.format(orders=self._orders.get(seat) or "(none)", state=state),
            run_id=self.run_id, stage=f"coach/turn/seat{seat}",
            model_tier_key="coach_turn", timeout_s=TURN_CALL_TIMEOUT_S,
        )
        lines = []
        for match in _LINE_RE.finditer(result.text or ""):
            lines.append(f"{match.group(1).lower()}: {match.group(2).strip()}")
        return "\n".join(lines)
