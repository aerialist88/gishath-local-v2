"""Full simulated Commander games for the Atelier — played to a winner.

Reworked 2026-07-10 (Trevor's spec) from a bounded one-circuit rehearsal into
a whole game: seeded house-rule mulligans (~2-3 lands, applied in code), the
entire library dealt in deterministic draw order, terse play-by-play turns, a
declared winner, and a per-deck performance report — the point is evaluating
the decks the engine builds. Still not a rules engine: the model plays from
standard MTG knowledge grounded by the real Oracle text of every card, and
code validates that every card played actually exists in that seat's list
(the old exact-excerpt citation validator can't scale past one circuit).
"""
from __future__ import annotations

import json
import random
import re
import threading
import uuid
from datetime import datetime, timezone

from deck_engine import claude_cli, config, scryfall_cache

from . import archive, forge_engine, rules_reference

GUIDE_PATH = config.PROMPTS_DIR / "commander_match_guide.md"
SIMULATION_DIR = config.STATE_DIR / "simulations"
SIMULATION_DIR.mkdir(parents=True, exist_ok=True)
_SPACE_RE = re.compile(r"\s+")

# Oracle text is full of typographic quotes/dashes; a model quoting it back
# with straight ASCII equivalents would fail the exact-substring citation
# check on pure typography — normalise both sides before comparing.
_TYPOGRAPHY = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "—": "-", "–": "-", " ": " "})

# The rehearsal's grounding contract is "use only the supplied sources", so
# unlike the deck pipeline (tool use deliberately unrestricted, Trevor's
# 2026-07-01 call) this blocks local reading too — otherwise the model could
# Read/Bash the full comprehensive-rules file or the Scryfall cache straight
# off disk and quietly reason from text the citation validator never sees.
_DISALLOWED_TOOLS = [*config.DISALLOWED_SEARCH_TOOLS, "Bash", "Read", "Grep", "Glob", "Edit", "Write", "NotebookEdit", "Task"]


def _clean(text: str) -> str:
    return _SPACE_RE.sub(" ", str(text or "").translate(_TYPOGRAPHY)).strip().lower()


def _oracle_text(card: dict) -> str:
    """Use Scryfall's printed text, falling back to its individual MDFC faces."""
    if text := str(card.get("oracle_text") or "").strip():
        return text
    chunks = []
    for face in card.get("card_faces") or []:
        name, text = str(face.get("name") or "").strip(), str(face.get("oracle_text") or "").strip()
        if text:
            chunks.append(f"{name}: {text}" if name else text)
    return "\n".join(chunks)


def _card_texts(deck: dict, cache: dict[str, dict]) -> dict[str, str]:
    texts, missing = {}, []
    names = [str(row.get("name") or "").strip() for row in deck.get("cards") or []]
    names.append(str(deck.get("commander") or "").strip())
    for name in filter(None, names):
        card = cache.get(name.lower())
        if card is None:
            missing.append(name)
        else:
            texts[name] = _oracle_text(card)
    if missing:
        raise ValueError("These cards are absent from the local Scryfall cache: " + ", ".join(sorted(set(missing))))
    return texts


MAX_MULLIGANS = 3  # house-rule mulligans are free (no hand-size penalty), but bounded so dealing always terminates


def _seeded_cards(deck: dict, seed: int, player: int, land_names: set[str]) -> dict:
    """Deal a seat's opening hand and FULL library order, deterministically.

    House rule (Trevor, 2026-07-10): each player mulligans until the opening
    hand has about 2-3 lands. Applied here in code — seeded and reproducible —
    rather than left to the model: attempt 0, 1, 2... reshuffles until a hand
    shows 2-3 lands; if none does within MAX_MULLIGANS, the closest-to-3 hand
    is kept. Mulligans are free (fresh 7 each time, no penalty).

    The commander is excluded from the deal — it starts in the command zone
    (previously it was shuffled into the library like any other card, so it
    could dead-draw into the opening hand)."""
    commander = str(deck.get("commander") or "").strip()
    names = [
        n for row in deck.get("cards") or []
        if (n := str(row.get("name") or "").strip()) and not row.get("is_commander") and n != commander
    ]
    if len(names) < 40:
        raise ValueError(f"{commander or 'Selected deck'} does not contain enough cards to play a match.")

    best: tuple[int, list[str], int] | None = None
    for attempt in range(MAX_MULLIGANS + 1):
        rng = random.Random(f"{seed}:{player}:{commander}:{attempt}")
        order = list(names)
        rng.shuffle(order)
        lands = sum(1 for n in order[:7] if n.strip().lower() in land_names)
        if 2 <= lands <= 3:
            best = (attempt, order, lands)
            break
        if best is None or abs(lands - 3) < abs(best[2] - 3):
            best = (attempt, order, lands)
    mulligans, order, lands = best
    return {
        "opening_hand": order[:7],
        "library": order[7:],  # the seat's exact draw order for the whole game
        "mulligans": mulligans,
        "lands_in_hand": lands,
    }


def build_grounding(deck_ids: list[str], seed: int) -> dict:
    """Assemble every permitted source before asking the narrator to reason."""
    if not 2 <= len(deck_ids) <= 4:
        raise ValueError("Choose between two and four saved decks.")
    if len(set(deck_ids)) != len(deck_ids):
        raise ValueError("Choose each deck only once.")
    try:
        cache = scryfall_cache.load_cache()
    except FileNotFoundError as exc:
        raise ValueError("The local Scryfall cache is required before rehearsals can run. Refresh it, then try again.") from exc
    guide = GUIDE_PATH.read_text().strip() if GUIDE_PATH.exists() else ""
    if not guide:
        raise ValueError("The Commander Match Simulation Guide is missing or empty.")
    rules = rules_reference.bundle()

    players, all_texts = [], {}
    for position, deck_id in enumerate(deck_ids, start=1):
        deck = archive.get_deck(deck_id)
        if deck is None:
            raise ValueError("One of the selected decks could not be found.")
        all_texts.update(_card_texts(deck, cache))
        # Land names for the house mulligan rule — from the Scryfall cache's
        # type_line (authoritative), falling back to the deck record's own.
        land_names = set()
        for row in deck.get("cards") or []:
            name = str(row.get("name") or "").strip()
            card = cache.get(name.lower()) or {}
            type_line = str(card.get("type_line") or row.get("type_line") or "")
            if "land" in type_line.lower():
                land_names.add(name.lower())
        players.append({
            "seat": position, "deck_id": deck_id, "commander": deck.get("commander", ""),
            "archetype": deck.get("archetype", ""), "cards": _seeded_cards(deck, seed, position, land_names),
        })
    return {
        "seed": seed,
        "guidebook": {"path": str(GUIDE_PATH.relative_to(config.REPO_ROOT)), "text": guide},
        "rules": rules, "players": players, "oracle_text": all_texts,
        "cache_meta": scryfall_cache._cache_meta() or {},  # provenance only
    }


TURN_CAP = 12  # per-player hard stop; real games at this table end turns 6-10 (Trevor), so past 12 the game is adjudicated on board state (see _prompt)

_SCHEMA = {
    "type": "object", "required": ["opening_note", "turns", "winner", "deck_reports", "unresolved_questions"],
    "properties": {
        "opening_note": {"type": "string", "description": "One or two sentences setting the table — no rules lecture."},
        "turns": {"type": "array", "items": {"type": "object", "required": ["turn", "seat", "play", "life", "cards_played"], "properties": {
            "turn": {"type": "integer"}, "seat": {"type": "integer"},
            "play": {"type": "string", "description": "1-2 terse sentences: what the seat did, e.g. 'Plays Forest, casts Sol Ring, passes.' No reasoning, no flavour."},
            "life": {"type": "array", "items": {"type": "integer"}, "description": "Every seat's life total after this turn, in seat order."},
            "cards_played": {"type": "array", "items": {"type": "string"},
                             "description": "Exact printed names of cards cast or played this turn. Real cards only — never tokens or copies."},
        }}},
        "winner": {"type": "object", "required": ["seat", "turn", "method"], "properties": {
            "seat": {"type": "integer"}, "turn": {"type": "integer"},
            "method": {"type": "string", "description": "How the game was won — combat damage, commander damage, poison, decking, lock, or adjudicated on board state at the turn cap."},
        }},
        "deck_reports": {"type": "array", "items": {"type": "object", "required": ["seat", "verdict"], "properties": {
            "seat": {"type": "integer"},
            "verdict": {"type": "string", "description": "2-3 sentences: how this deck performed, where it stumbled — the Atelier uses this to judge its own builds."},
            "key_cards": {"type": "array", "items": {"type": "string"}},
        }}},
        "unresolved_questions": {"type": "array", "items": {"type": "string"},
                                 "description": "Judgement calls made on genuinely ambiguous interactions."},
    }
}


def _prompt(grounding: dict) -> str:
    players = []
    for player in grounding["players"]:
        cards = player["cards"]
        players.append(
            f"Player {player['seat']}: {player['commander']} — {player['archetype']}\n"
            f"Mulligans taken (house rule, ~2-3 lands): {cards.get('mulligans', 0)} · "
            f"lands in kept hand: {cards.get('lands_in_hand', '?')}\n"
            f"Opening hand (7): {', '.join(cards['opening_hand'])}\n"
            f"Library in EXACT draw order, top first ({len(cards['library'])}): {'; '.join(cards['library'])}"
        )
    oracle = "\n\n".join(f"[{name}]\n{text or '(no Oracle text)'}" for name, text in grounding["oracle_text"].items())
    players_text = "\n\n".join(players)
    rules = "\n\n".join(f"[{label}]\n{text}" for label, text in grounding["rules"]["sections"].items())
    pod_rule = ("This is a two-player game: the starting player skips their first draw step."
                if len(grounding["players"]) == 2
                else "This is a multiplayer Commander pod: every player, including the starting player, takes their first draw step.")
    return f"""You are the referee and scorekeeper for a full simulated Commander game, played to a finish.

GROUND TRUTH. The Oracle text bundle below is the real printed text of every card in this game — always defer to it over memory of the card. Play by standard Magic: The Gathering and Commander rules; the Comprehensive Rules excerpts below settle the Commander-specific points. If a genuinely ambiguous interaction comes up, resolve it the way a reasonable table would, keep the game moving, and record the judgement call in unresolved_questions — never stall.

SETUP. 40 life each; commanders start in the command zone (commander tax applies on recasts). Opening hands were already mulliganed by house rule — do not mulligan again. Each seat's library order is fully predetermined above: every draw MUST take the next card from that list, in order, no exceptions. {pod_rule}

PLAY. Simulate every turn until the game actually ends: opponents' life to 0, 21+ combat damage from a single commander, poison, drawing from an empty library, or a genuinely unbreakable lock. Pilot every seat like an experienced human player, not a cautious engine:
- Curve out and apply pressure EARLY. Commanders come down on curve and attack. Creatures attack whenever the math is favourable or the defender can't punish it — chip damage wins real games.
- Real Commander games at this table end around turns 6-10. If no life total has moved by turn 4, the seats are being piloted too passively — fix the piloting, don't drift.
- Take calculated risks: run threats into open mana sometimes, force awkward blocks, use evasion and haste aggressively. Passing with no board development several turns in a row is a piloting failure.
- When a seat sees lethal — direct damage, trample, commander damage, anything — it TAKES it immediately, never holding back for style.
If no one has won by turn {TURN_CAP}, stop and award the game on board state and inevitability, saying so in the winner's method — but treat reaching the cap as a failure of aggression, not a normal outcome.

NARRATION. One entry per turn, terse and factual: 'Plays Forest, casts Kudo, King Among Bears; attacks with Ayula for 3 (Player 2 to 37). Passes.' 1-2 short sentences — no reasoning, no colour commentary, no rules explanations. After each turn record every seat's life total, and list in cards_played the exact printed names of the cards cast or played that turn (real cards only, never tokens/copies/emblems).

SCORING. When the game ends: winner (seat, turn, method), then a deck_report per seat — 2-3 sentences on how the build actually performed and where it stumbled, plus its key cards. The Atelier uses these reports to evaluate the decks it forged, so be honest about weaknesses.

GUIDEBOOK\n{grounding['guidebook']['text']}

COMPREHENSIVE RULES REFERENCE\n{rules}

PLAYERS\n{players_text}

ORACLE TEXT BUNDLE\n{oracle}
"""


def validate_result(result: dict, grounding: dict) -> list[str]:
    """Code-level checks fit for a full game (2026-07-10 — replaces the old
    per-turn exact-excerpt citation validator, which can't scale past a
    bounded circuit): every card a seat plays must exist in that seat's real
    decklist, the winner must be a real seat, and every seat gets a report.
    A hallucinated card, a play from another deck's list, or a phantom seat
    fails here and routes through the one-shot repair pass."""
    issues: list[str] = []
    allowed: dict[int, set[str]] = {}
    for player in grounding["players"]:
        cards = player.get("cards") or {}
        names = list(cards.get("opening_hand") or []) + list(cards.get("library") or [])
        names.append(player.get("commander", ""))
        seat_allowed = set()
        for name in names:
            clean = _clean(name)
            if clean:
                seat_allowed.add(clean)
                seat_allowed.add(clean.split(" // ")[0])  # MDFCs may be played by their front face
        allowed[player["seat"]] = seat_allowed

    turns = result.get("turns") or []
    if not turns:
        issues.append("no turns were played")
    for turn in turns:
        seat = turn.get("seat")
        if seat not in allowed:
            issues.append(f"turn {turn.get('turn', '?')} names unknown seat {seat!r}")
            continue
        for name in turn.get("cards_played") or []:
            clean = _clean(name)
            if clean and clean not in allowed[seat] and clean.split(" // ")[0] not in allowed[seat]:
                issues.append(f"turn {turn.get('turn', '?')}: seat {seat} played {name!r}, which is not in that deck")

    winner = result.get("winner") or {}
    if winner.get("seat") not in allowed:
        issues.append(f"winner names unknown seat {winner.get('seat')!r}")
    reported = {r.get("seat") for r in result.get("deck_reports") or []}
    for seat in allowed:
        if seat not in reported:
            issues.append(f"deck_reports is missing seat {seat}")
    return issues


class SimulationManager:
    def __init__(self) -> None:
        self._lock, self._sessions = threading.Lock(), {}

    def _save(self, session: dict) -> None:
        (SIMULATION_DIR / f"{session['id']}.json").write_text(json.dumps(session, indent=2))

    def start(self, deck_ids: list[str], seed: int | None = None) -> dict:
        session_id = uuid.uuid4().hex[:12]
        # Engine choice (2026-07-10): Forge — a real deterministic rules engine
        # with its own game AI — whenever its binaries are installed under
        # third_party/; the LLM referee stays as the fallback. Forge's sim mode
        # has no RNG seed parameter, so the seed only applies in LLM mode.
        if forge_engine.is_available():
            if not 2 <= len(deck_ids) <= 4:
                raise ValueError("Choose between two and four saved decks.")
            if len(set(deck_ids)) != len(deck_ids):
                raise ValueError("Choose each deck only once.")
            players = []
            for position, deck_id in enumerate(deck_ids, start=1):
                deck = archive.get_deck(deck_id)
                if deck is None:
                    raise ValueError("One of the selected decks could not be found.")
                players.append({"seat": position, "deck_id": deck_id, "commander": deck.get("commander", ""),
                                "archetype": deck.get("archetype", ""), "cards": {}})
            grounding = {"engine": "forge", "players": players}
            session_grounding = {"engine": "forge", "seed": None, "players": players}
        else:
            selected_seed = int(seed) if seed is not None else random.SystemRandom().randint(1, 2_147_483_647)
            grounding = build_grounding(deck_ids, selected_seed)
            grounding["engine"] = "llm"
            session_grounding = {"engine": "llm", "seed": selected_seed, "guidebook": grounding["guidebook"]["path"],
                                 "cache_refreshed_at": grounding["cache_meta"].get("refreshed_at"),
                                 "rules_effective_date": grounding["rules"].get("effective_date"),
                                 "rules_url": grounding["rules"].get("rules_url"),
                                 "oracle_cards": len(grounding["oracle_text"]), "players": grounding["players"]}
        session = {
            "id": session_id, "status": "running", "created_utc": datetime.now(timezone.utc).isoformat(),
            "grounding": session_grounding,
        }
        with self._lock:
            self._sessions[session_id] = session
        self._save(session)
        threading.Thread(target=self._run, args=(session_id, grounding), daemon=True, name=f"atelier-sim-{session_id}").start()
        return session

    def _run_forge(self, session_id: str, grounding: dict) -> dict:
        """Play the game on the Forge rules engine, then have the LLM write the
        deck reports FROM the engine's log (grounded; non-fatal if it fails).
        Returns the session update dict."""
        match = forge_engine.run_match([p["deck_id"] for p in grounding["players"]])
        parsed = match["result"]
        # Receipts: the engine's full typed log, plus the structured per-turn
        # detail export (every parsed engine event — casts with targets,
        # triggers, resolutions, combat assignments, damage by kind, life
        # deltas, zone changes), kept next to the session file and served at
        # /api/simulations/<id>/details and /forge-log.
        try:
            (SIMULATION_DIR / f"{session_id}.forge.log").write_text(match["raw_log"])
            (SIMULATION_DIR / f"{session_id}.forge.details.json").write_text(json.dumps({
                "session_id": session_id,
                "players": [{"seat": p["seat"], "commander": p["commander"], "archetype": p["archetype"]}
                            for p in grounding["players"]],
                "winner": parsed["winner"], "mulligans": parsed["mulligans"],
                "kept_hands": parsed.get("kept_hands", {}),
                "commander_damage": parsed.get("commander_damage", {}),
                "casts_by_seat": parsed.get("casts_by_seat", {}),
                "unsupported_cards": parsed["unsupported_cards"],
                **parsed.get("detail", {}),
            }, indent=2))
        except OSError:
            pass
        for player in grounding["players"]:
            player["cards"] = {"mulligans": parsed["mulligans"].get(player["seat"], 0),
                               "kept_hand": parsed.get("kept_hands", {}).get(player["seat"])}

        notes = []
        if parsed["unsupported_cards"]:
            notes.append("Forge's card database doesn't know these cards, so they were dropped for this game: "
                         + ", ".join(parsed["unsupported_cards"]) + ".")
        if parsed["is_draw"]:
            notes.append("No winner — the game hit Forge's clock and was called a draw.")

        opening_note, deck_reports = self._narrate_forge(session_id, grounding, parsed)
        return {"status": "complete", "result": {
            "opening_note": opening_note, "turns": parsed["turns"], "winner": parsed["winner"],
            "deck_reports": deck_reports, "unresolved_questions": notes,
        }}

    def _narrate_forge(self, session_id: str, grounding: dict, parsed: dict) -> tuple[str, list[dict]]:
        """One cheap LLM call to write the per-deck performance verdicts from
        the REAL game log — the one job the model keeps in Forge mode. Grounded
        by construction (it only sees engine facts) and non-fatal: a completed
        real game always ships, with plain fallback reports if this call fails."""
        seats = {p["seat"]: p for p in grounding["players"]}
        # The parser's per-seat cast/land record — unlike the turn entries'
        # cards_played it includes instants cast on other players' turns, so
        # legitimate interaction pieces survive the key_cards filter below.
        played_by_seat = {s: set(parsed.get("casts_by_seat", {}).get(s, [])) for s in seats}
        for t in parsed["turns"]:
            played_by_seat.setdefault(t["seat"], set()).update(t.get("cards_played") or [])
        fallback = (
            "",
            [{"seat": s, "verdict": "Game played to completion on the Forge rules engine — see the turn log above.",
              "key_cards": []} for s in sorted(seats)],
        )
        try:
            log_lines = "\n".join(
                f"R{t['turn']} Player {t['seat']} ({seats.get(t['seat'], {}).get('commander', '?')}): {t['play']} life={t['life']}"
                for t in parsed["turns"])
            schema = {
                "type": "object", "required": ["opening_note", "deck_reports"],
                "properties": {
                    "opening_note": {"type": "string", "description": "1-2 sentences framing the matchup."},
                    "deck_reports": {"type": "array", "items": {"type": "object", "required": ["seat", "verdict"], "properties": {
                        "seat": {"type": "integer"},
                        "verdict": {"type": "string", "description": "2-3 honest sentences on how this deck performed."},
                        "key_cards": {"type": "array", "items": {"type": "string"}},
                    }}},
                },
            }
            prompt = (
                "A Commander game was just played on the Forge rules engine (a deterministic MTG engine — "
                "every line below is a real engine fact, not narration). Write a 1-2 sentence opening_note and "
                "an honest 2-3 sentence deck_report per seat evaluating how each deck actually performed: what "
                "worked, what stumbled, which cards did the work. The reports grade decks a nightly pipeline "
                "builds, so honesty about weaknesses matters more than flattery. key_cards must only name cards "
                "that seat actually played in this log.\n\nPlayers:\n"
                + "\n".join(f"Player {s}: {p['commander']} — {p['archetype']}" for s, p in sorted(seats.items()))
                + f"\n\nOutcome: {parsed['winner']['method']} (turn {parsed['winner']['turn']})"
                + ("".join(f"\nCommander damage: {cmd} dealt {total} total to Player {t}."
                           for cmd, by in (parsed.get("commander_damage") or {}).items() for t, total in by.items()))
                + f"\n\nGame log:\n{log_lines}"
            )
            response = claude_cli.run(
                prompt, run_id=f"simulation-{session_id}", stage="simulation/narrate",
                model_tier_key="simulate", json_schema=schema, cwd=config.REPO_ROOT,
                timeout_s=300, disallowed_tools=_DISALLOWED_TOOLS,
            ).parsed_json()
            reports = []
            reported = {}
            for rep in response.get("deck_reports") or []:
                if rep.get("seat") in seats and rep.get("verdict"):
                    rep["key_cards"] = [c for c in rep.get("key_cards") or [] if c in played_by_seat.get(rep["seat"], set())]
                    reported[rep["seat"]] = rep
            for s in sorted(seats):
                reports.append(reported.get(s) or fallback[1][sorted(seats).index(s)])
            return str(response.get("opening_note") or ""), reports
        except Exception:  # noqa: BLE001 — narration is decoration; the engine's game always ships
            return fallback

    def _run(self, session_id: str, grounding: dict) -> None:
        if grounding.get("engine") == "forge":
            try:
                update = self._run_forge(session_id, grounding)
            except Exception as exc:  # noqa: BLE001 — surfaced as a normal failed session, never a stuck spinner
                update = {"status": "failed", "error": str(exc)}
            with self._lock:
                session = self._sessions.get(session_id)
                if session is not None:
                    session.update(update)
                    self._save(session)
            return
        try:
            result = claude_cli.run(
                _prompt(grounding), run_id=f"simulation-{session_id}", stage="simulation", model_tier_key="simulate",
                json_schema=_SCHEMA, cwd=config.REPO_ROOT, timeout_s=900,
                disallowed_tools=_DISALLOWED_TOOLS,
            )
            response = result.parsed_json()
            issues = validate_result(response, grounding)
            if issues:
                # One cheap repair before discarding a fully-paid rehearsal: the
                # deck pipeline never throws away a call on first offense, and
                # neither should this. Resume the same session (all grounding
                # already in context) and ask for the corrected full result.
                repair = claude_cli.run(
                    "Your game log failed validation. Fix ONLY these issues and return the complete "
                    "corrected result via the JSON schema again — the same game, corrected entries. "
                    "Every name in cards_played must be the exact printed name of a card from that "
                    "seat's supplied decklist; a play that can't be fixed that way must be replaced "
                    "with a legal line using cards the seat actually has.\n\n"
                    "Issues:\n" + "\n".join(f"- {issue}" for issue in issues),
                    run_id=f"simulation-{session_id}", stage="simulation/citation-repair",
                    model_tier_key="simulate", json_schema=_SCHEMA, cwd=config.REPO_ROOT,
                    timeout_s=900, disallowed_tools=_DISALLOWED_TOOLS,
                    resume_session_id=result.session_id,
                )
                response = repair.parsed_json()
                issues = validate_result(response, grounding)
            if issues:
                raise ValueError("Validation stopped this game: " + "; ".join(issues))
            update = {"status": "complete", "result": response}
        except Exception as exc:  # noqa: BLE001 — shown as a normal rehearsal failure, never a stuck spinner
            update = {"status": "failed", "error": str(exc)}
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.update(update)
                self._save(session)

    def get(self, session_id: str) -> dict | None:
        with self._lock:
            if session := self._sessions.get(session_id):
                return session
        try:
            return json.loads((SIMULATION_DIR / f"{session_id}.json").read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def list_sessions(self) -> list[dict]:
        """Newest-first summaries of every rehearsal on record — the disk files
        (past runs, they survive server restarts) merged with the in-memory
        sessions (a rehearsal running right now hasn't been saved as complete
        yet). Summaries only; the full result comes from get()."""
        sessions: dict[str, dict] = {}
        for path in SIMULATION_DIR.glob("*.json"):
            try:
                loaded = json.loads(path.read_text())
                sessions[str(loaded.get("id") or path.stem)] = loaded
            except (OSError, json.JSONDecodeError):
                continue  # one corrupt session file shouldn't hide the rest
        with self._lock:
            sessions.update(self._sessions)
        out = []
        for session in sessions.values():
            grounding = session.get("grounding") or {}
            out.append({
                "id": session.get("id"),
                "status": session.get("status"),
                "created_utc": session.get("created_utc"),
                "seed": grounding.get("seed"),
                "commanders": [p.get("commander", "?") for p in (grounding.get("players") or [])],
                "error": session.get("error"),
            })
        out.sort(key=lambda s: s.get("created_utc") or "", reverse=True)
        return out


MANAGER = SimulationManager()
