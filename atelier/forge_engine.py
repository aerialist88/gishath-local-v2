"""atelier/forge_engine.py — real-rules Commander matches via Forge.

Trevor forked Card-Forge/forge (github.com/aerialist88/forge, 2026-07-10) and
asked for its engine to power the Atelier's match simulator. The one-call LLM
referee measurably hallucinated hidden state — the Ms. Bumbleflower audit
found phantom token swarms, cards cast that were never drawn, and a singleton
cast twice — because a language model narrates state instead of tracking it.
Forge is a deterministic MTG rules engine with a competent game AI: hands,
libraries, the battlefield, priority, the stack, and combat are all real data
structures, so requirements like "zero hallucination", "correct interactions",
and "aim to win" hold by construction rather than by prompt.

This module runs Forge headlessly as a subprocess and parses its typed game
log (Turn/Land/Add To Stack/Combat/Damage/Life/Mulligan/Game Outcome lines)
into the session shape the Atelier's match UI already renders. The LLM keeps
exactly one job — writing the per-deck performance reports FROM the engine's
log (see simulator.py) — the part it's actually good at.

Launch path: forge_bridge/AtelierSim.java (compiled by forge_bridge/build.sh
into third_party/forge/atelier-classes/) replicates SimulateMatch's Commander
flow but exposes the AI knobs Forge's stock `sim` mode hardcodes: `-p` picks
the AI personality profile per run, `-sim` enables AIOption.USE_SIMULATION.
When the compiled class is missing, the stock `sim -d <decks...> -f Commander`
mode is used instead — identical log output either way, since AtelierSim
reuses SimulateMatch.simulateSingleMatch for the game loop and logging.
NOTE: the 2026-07-11 phase-0 benchmark found USE_SIMULATION crashes Forge
2.0.13 in 4-player Commander (RuntimeException in SpellAbilityChoicesIterator,
3 of 3 games) — the engine then force-ends the game and stamps an arbitrary
winner, so leave it off until the fork fixes it upstream.

Layout (all gitignored, ~800MB of downloaded binaries):
    third_party/jdk-21*/                       portable Temurin JDK (this Mac has no system Java)
    third_party/forge/                         Forge 2.0.13 release (jar-with-dependencies)
    third_party/forge/forge.profile.properties sandboxes Forge's user data into...
    third_party/forge-data/                    decks/commander/*.dck, forge.log, etc.

Known trade-offs, accepted deliberately:
  - No replay seed: SimulateMatch exposes no RNG seed, so Forge games are not
    reproducible run-to-run (the UI's seed field is ignored in this mode).
  - Forge's AI mulligans by its own judgement, not the house ~2-3-lands rule;
    its decisions are at least real decisions against real hands.
  - Cards Forge's database doesn't know are dropped from the deck with an
    "unsupported card" warning, which we surface in the session verbatim.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from deck_engine import config

from . import archive

THIRD_PARTY = config.REPO_ROOT / "third_party"
FORGE_DIR = THIRD_PARTY / "forge"
FORGE_JAR = FORGE_DIR / "forge-gui-desktop-2.0.13-jar-with-dependencies.jar"
ATELIER_SIM_CLASSES = FORGE_DIR / "atelier-classes"  # forge_bridge/build.sh output
# Trevor's fork (third_party/forge-src, built by scratch fork_setup / mvn):
# the jar the LLM-coach work will land in. Off until it has parity + the coach.
USE_FORK_JAR = False
FORK_JAR_GLOB = "forge-src/forge-gui-desktop/target/forge-gui-desktop-*-jar-with-dependencies.jar"
DECKS_DIR = THIRD_PARTY / "forge-data" / "decks" / "commander"
# Forge's own per-game clock (-c): past this it calls a draw. The phase-0
# benchmark (2026-07-11) saw healthy 4-player games need 300+s of game time,
# so the old 300s clock was silently drawing real games.
GAME_TIMEOUT_S = 600
SUBPROCESS_TIMEOUT_S = 900  # hard kill: JVM boot (~15s) + card DB load + the game itself
AI_PROFILE = ""  # Forge AI personality for every seat: Default/Cautious/Reckless/Experimental ("" = Forge's preference file, i.e. Default)

_PLAYER_RE = re.compile(r"Ai\((\d+)\)-(.+)")
_CARD_ID_RE = re.compile(r"\s*\(\d+\)")  # Forge suffixes battlefield object ids: "Werebear (28)"


def java_path() -> Path | None:
    for candidate in sorted(THIRD_PARTY.glob("jdk-*/Contents/Home/bin/java")):
        if candidate.exists():
            return candidate
    return None


def active_jar() -> Path:
    """The Forge jar matches run on: the release jar, or the fork build when
    USE_FORK_JAR is flipped and a built jar exists under third_party/forge-src."""
    if USE_FORK_JAR:
        forks = sorted(THIRD_PARTY.glob(FORK_JAR_GLOB))
        if forks:
            return forks[-1]
    return FORGE_JAR


def is_available() -> bool:
    """True when the Forge jar and the portable JDK are both in place."""
    return FORGE_JAR.exists() and java_path() is not None


_QTY_SUFFIX_RE = re.compile(r"^(.*?)\s*[×x]\s*(\d+)$")  # backfilled decks store basics as one row: "Forest ×10"


def _forge_name(name: str) -> tuple[str, int]:
    """Atelier card-row name -> (Forge-recognized name, quantity).

    Two record quirks Forge rejects otherwise (found in the first real 4-player
    run, where Bello's deck silently lost its 18 basics and had a non-game):
      - pre-Atelier backfilled decks fold basics into one row: 'Forest ×10'
      - double-faced cards are stored full-name ('A // B'); Forge's database
        wants the front face only."""
    name = name.strip()
    qty = 1
    m = _QTY_SUFFIX_RE.match(name)
    if m:
        name, qty = m.group(1).strip(), int(m.group(2))
    if " // " in name:
        name = name.split(" // ")[0].strip()
    return name, qty


def deck_land_names(deck: dict) -> set[str]:
    """Lowercased Forge names of every land in a deck — lets the board tracker
    classify a mana source as a land rather than a rock. Uses the card row's
    Scryfall type_line."""
    out: set[str] = set()
    for row in deck.get("cards") or []:
        if "land" in str(row.get("type_line") or "").lower():
            name, _ = _forge_name(str(row.get("name") or ""))
            if name:
                out.add(name.lower())
    return out


def _write_dck(deck: dict, seat: int) -> str:
    """Serialize one Atelier deck record into Forge's .dck format. Returns the
    filename (relative, as SimulateMatch wants it). Deck Name is the commander,
    so Forge's player names — 'Ai(<seat>)-<name>' — read naturally in the log."""
    commander = str(deck.get("commander") or "").strip()
    commander_card, _ = _forge_name(commander)  # MDFC commanders: Forge wants the front face
    lines = ["[metadata]", f"Name={commander}", "[Commander]", f"1 {commander_card}", "[Main]"]
    for row in deck.get("cards") or []:
        raw = str(row.get("name") or "").strip()
        if not raw or row.get("is_commander") or raw == commander:
            continue
        name, qty = _forge_name(raw)
        if name:
            lines.append(f"{qty} {name}")
    DECKS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"atelier_seat{seat}.dck"
    (DECKS_DIR / filename).write_text("\n".join(lines) + "\n")
    return filename


def _seat_of(player: str, names_by_seat: dict[int, str]) -> int | None:
    m = _PLAYER_RE.match(player.strip())
    if not m:
        return None
    seat = int(m.group(1))
    return seat if seat in names_by_seat else None


def _strip_ids(text: str) -> str:
    return _CARD_ID_RE.sub("", text)


def _split_targets(blob: str, names_by_seat: dict[int, str]) -> list[str]:
    """'[Ai(2)-Kudo...] [Ms. Bumbleflower (100)]' -> ['Player 2', 'Ms. Bumbleflower'].
    Targets can also be raw spell text (Swan Song targeting a stack object)."""
    out = []
    for part in re.split(r"\]\s*\[", blob.strip("[]")):
        part = part.strip()
        seat = _seat_of(part, names_by_seat)
        out.append(f"Player {seat}" if seat is not None else _strip_ids(part))
    return out


def _short(target: str, limit: int = 48) -> str:
    return target if len(target) <= limit else target[: limit - 1] + "…"


_ADD_RE = re.compile(r"Add (.+?)\.")
_PIP_RE = re.compile(r"\{([^}]+)\}")
_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}


def _mana_pips(line: str) -> int:
    """How much mana a 'Mana:' activation produced, from its 'Add …' clause.
    '{C}{C}' -> 2, '{2}' -> 2, '{R} or {W}' -> 1 (a choice), a worded 'Add one
    mana of any color' -> 1. Never returns 0 — every Mana line made mana."""
    m = _ADD_RE.search(line)
    clause = m.group(1) if m else ""
    if " or " in clause:  # a choice between colors is still one mana
        return 1
    total = sum(int(sym) if sym.isdigit() else 1 for sym in _PIP_RE.findall(clause))
    return total or 1


def parse_log(raw: str, names_by_seat: dict[int, str], land_names: set[str] | None = None) -> dict:
    """Forge's typed game log -> the Atelier session-result shape.

    Every fact here is transcribed from an engine event line — nothing is
    inferred. Turn entries are grouped per player-turn and summarized tersely
    (lands / casts with targets / responses by other seats / activations /
    combat / damage / eliminations); life totals come from the engine's own
    Life lines; the winner from its Game Outcome lines.

    Turn numbering: Forge numbers player-turns sequentially across the whole
    game and eliminated seats stop taking turns, so dividing by the seat count
    breaks as soon as someone dies. Each entry's `turn` is instead that seat's
    OWN turn count ("their turn 7") — robust to eliminations, extra turns, and
    a randomized starting player. The Forge-global number is kept as
    `game_turn` for cross-referencing the raw log.

    Alongside the summary, a full structured account is built under `detail`:
    every parsed engine event (casts, activations, triggers, resolutions,
    combat assignments, damage with combat/non-combat kind, life changes,
    zone changes, discards, draws, replacement effects), in order, per turn —
    the machine-readable export the raw log is not.

    Board tracker: a battlefield is reconstructed by object id (lands from
    `Land:` lines and ramp discovered via `Mana:` lines; creatures from
    `Resolve Stack: X (id) - Creature P/T`; removed on `Zone Change … from
    Battlefield`). Each turn entry gets a `board` snapshot per seat — lands and
    creatures in play, and mana produced that turn (summed from `Mana:` lines'
    Add clauses). `land_names` (lowercased) classifies mana sources as land vs
    rock; without it, only cards seen entering via `Land:` count as lands.
    Static noncreature permanents (anthems, mana-less artifacts) leave no id
    trail in Forge's log, so they are deliberately not counted — the tracker
    reports lands/creatures/mana, never a permanents total it can't stand
    behind."""
    seats = sorted(names_by_seat)
    commanders = {name: s for s, name in names_by_seat.items()}
    land_names = {n.lower() for n in (land_names or set())}
    life = {s: 40 for s in seats}
    mulligans = {s: 0 for s in seats}
    kept_hands: dict[int, int] = {}
    eliminated: set[int] = set()
    cmd_damage: dict[str, dict[int, int]] = {}   # commander name -> target seat -> total combat damage
    casts_by_seat: dict[int, set[str]] = {s: set() for s in seats}
    turn_counts = {s: 0 for s in seats}
    # Battlefield reconstructed by object id, for the per-turn board tracker.
    battlefield: dict[int, dict] = {}   # id -> {"seat", "kind": land|creature|other, "name"}
    last_cast: dict[str, int] = {}      # cleaned card name -> seat that most recently cast it
    pt_by_name: dict[str, str] = {}     # cleaned creature name -> "P/T", for richer cast prose
    turns: list[dict] = []
    detail_turns: list[dict] = []
    pregame: list[dict] = []
    current: dict | None = None   # the player-turn being accumulated
    unsupported: list[str] = []
    outcome_reasons: list[str] = []
    winner_seat = None

    def _enter(oid: int, seat: int | None, kind: str, name: str) -> None:
        if oid not in battlefield:
            battlefield[oid] = {"seat": seat, "kind": kind, "name": name}
        else:  # first sighting may be as an unknown mana source; upgrade kind/seat when learned
            slot = battlefield[oid]
            if kind != "other":
                slot["kind"] = kind
            if slot["seat"] is None and seat is not None:
                slot["seat"] = seat

    def _board_snapshot() -> dict:
        # Lands and mana-produced only: both are grounded in id-bearing engine
        # lines. Creatures and static permanents have no reliable id trail (see
        # the Resolve Stack note below), so no permanents total is claimed.
        snap = {s: {"lands": 0, "mana": current["mana"][s] if current else 0} for s in seats}
        for obj in battlefield.values():
            if obj["seat"] in snap and obj["kind"] == "land":
                snap[obj["seat"]]["lands"] += 1
        return {str(s): snap[s] for s in seats}

    def _event(event: dict) -> None:
        (current["events"] if current is not None else pregame).append(event)

    def _flush():
        nonlocal current
        if current is None:
            return
        active = current["entry"]["seat"]
        bits = []
        if current["lands"]:
            bits.append("Plays " + ", ".join(current["lands"]) + ".")
        if current["casts"]:  # append each creature's P/T, learned when its spell resolved
            labels = [c["name"] + (f" ({pt})" if (pt := pt_by_name.get(c['name'])) else "")
                      + (f" → {_short(c['target'])}" if c["target"] else "") for c in current["casts"]]
            bits.append("Casts " + ", ".join(labels) + ".")
        bits.extend(current["responses"])   # spells/abilities from the other seats, in order
        if current["triggers"]:
            # Deduped with ×N counts; a trigger that fired once with a target
            # keeps it ("Vendilion Clique → Player 1"). Longest turns get capped
            # rather than flooding the row — the detail export has the rest.
            shown = list(current["triggers"].items())
            labels = []
            for (t_seat, t_name), info in shown[:8]:
                label = t_name if t_seat == active else f"Player {t_seat}'s {t_name}"
                if info["count"] > 1:
                    label += f" ×{info['count']}"
                elif info["target"]:
                    label += f" → {_short(info['target'])}"
                labels.append(label)
            if len(shown) > 8:
                labels.append(f"+{len(shown) - 8} more")
            bits.append("Triggers: " + ", ".join(labels) + ".")
        for who, n in current["draws"]:
            bits.append((f"Player {who} draws" if who != active else "Draws") + (f" {n} cards." if n != 1 else " a card."))
        if current["attacks"]:
            for target, attackers in current["attacks"].items():
                bits.append(f"Attacks {target} with " + ", ".join(attackers) + ".")
        bits.extend(current["blocks"])
        for target, dmg in current["damage"].items():
            bits.append(f"Deals {dmg} to {target}.")
        for cmd, target, total in current["cmd_damage"]:
            bits.append(f"Commander damage: {cmd} has dealt {total} total to {target}.")
        if current["died"]:
            bits.append("To graveyard: " + ", ".join(current["died"]) + ".")
        if current["exiled"]:
            bits.append("Exiled: " + ", ".join(current["exiled"]) + ".")
        bits.extend(current["discards"])
        bits.extend(current["eliminations"])
        if not bits:
            bits.append("No plays — passes.")
        current["entry"]["play"] = " ".join(bits)
        current["entry"]["life"] = [life[s] for s in seats]
        current["entry"]["cards_played"] = current["cards_played"]
        # Distinct trigger sources this turn — the UI adds these to the
        # hover-preview name set, so "Triggers: Bitterblossom" shows its art.
        current["entry"]["triggered"] = sorted({name for _s, name in current["triggers"]})
        current["entry"]["board"] = _board_snapshot()
        turns.append(current["entry"])
        detail_turns.append({
            "game_turn": current["entry"]["game_turn"], "seat": active,
            "seat_turn": current["entry"]["turn"],
            "life_after": {str(s): life[s] for s in seats},
            "board": current["entry"]["board"],
            "events": current["events"],
        })
        current = None

    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("Turn: Turn "):
            _flush()
            m = re.match(r"Turn: Turn (\d+) \((.+)\)$", line)
            if not m:
                continue
            seat = _seat_of(m.group(2), names_by_seat)
            if seat is None:
                continue
            turn_counts[seat] += 1
            current = {
                "entry": {"turn": turn_counts[seat], "game_turn": int(m.group(1)),
                          "seat": seat, "play": "", "life": [], "cards_played": []},
                "lands": [], "casts": [], "responses": [], "draws": [], "attacks": {}, "damage": {},
                "cmd_damage": [], "blocks": [], "died": [], "exiled": [], "discards": [],
                "eliminations": [], "cards_played": [], "events": [], "mana": {s: 0 for s in seats},
                "triggers": {},   # (seat, source name) -> {count, target} — see the triggered branch
            }
        elif line.startswith("Land: ") and current is not None:
            m = re.match(r"Land: (.+) played (.+?)(?: \((\d+)\))?$", line)
            if m:
                name = _strip_ids(m.group(2))
                seat = _seat_of(m.group(1), names_by_seat)
                current["lands"].append(name)
                current["cards_played"].append(name)
                casts_by_seat[current["entry"]["seat"]].add(name)
                if m.group(3):
                    _enter(int(m.group(3)), seat, "land", name)
                _event({"type": "land", "seat": seat, "card": name})
        elif line.startswith("Add To Stack: "):
            m = re.match(r"Add To Stack: (.+?) (cast|activated|triggered) (.+?)(?: targeting \[(.+)\])?$", line)
            if m:
                seat = _seat_of(m.group(1), names_by_seat)
                verb, name = m.group(2), _strip_ids(m.group(3))
                targets = _split_targets(m.group(4), names_by_seat) if m.group(4) else []
                _event({"type": verb if verb != "cast" else "cast",
                        "seat": seat, "card": name, **({"targets": targets} if targets else {})})
                if seat is None or current is None:
                    continue
                if verb == "triggered":
                    # Triggered abilities (Pantlaza's discover, Edgar's eminence
                    # tokens, Blood Artist drains...) join the turn prose as ONE
                    # deduped "Triggers:" sentence per turn (see _flush) — real
                    # games log hundreds of trigger lines, so line-per-trigger
                    # prose would drown the actual plays. Every individual
                    # occurrence still lands in the detail export above.
                    slot = current["triggers"].setdefault((seat, name), {"count": 0, "target": ""})
                    slot["count"] += 1
                    if targets and not slot["target"]:
                        slot["target"] = targets[0]
                    continue
                if verb == "cast":
                    last_cast[name] = seat  # so the object's controller is known when it resolves with an id
                    casts_by_seat[seat].add(name)
                    if seat == current["entry"]["seat"]:
                        current["casts"].append({"name": name, "target": targets[0] if targets else ""})
                        current["cards_played"].append(name)
                    else:  # instants and flash from the other seats — the interaction that decides games
                        label = name + (f" → {_short(targets[0])}" if targets else "")
                        current["responses"].append(f"Player {seat} casts {label}.")
                elif verb == "activated":
                    label = name + (f" → {_short(targets[0])}" if targets else "")
                    prefix = "Activates" if seat == current["entry"]["seat"] else f"Player {seat} activates"
                    current["responses"].append(f"{prefix} {label}.")
        elif line.startswith("Resolve Stack: "):
            body = line[len("Resolve Stack: "):]
            # A creature resolving prints its base P/T (no id): "Werebear - Creature 2 / 2".
            # Used only to enrich cast prose — creatures can't be board-tracked
            # by id because neither the resolve nor any ETB line carries one.
            cm = re.match(r"(.+?) - Creature (\d+) / (\d+)$", body)
            if cm:
                pt_by_name[cm.group(1).strip()] = f"{cm.group(2)}/{cm.group(3)}"
            # Card draws name their player: "Growth Spiral - Ai(1)-... draws a card / three cards".
            for dm in re.finditer(r"Ai\((\d+)\)-.+? draws (a|an|\w+) cards?", body):
                dseat = int(dm.group(1))
                n = 1 if dm.group(2) in ("a", "an") else _WORD_NUM.get(dm.group(2).lower(), 1)
                if dseat in names_by_seat and current is not None:
                    current["draws"].append((dseat, n))
                    _event({"type": "draw", "seat": dseat, "cards": n})
            _event({"type": "resolve", "text": _strip_ids(body)})
        elif line.startswith("Replacement Effect: "):
            _event({"type": "replacement", "text": line[len("Replacement Effect: "):]})
        # Combat declarations are multi-line log entries: only the first line
        # carries the "Combat: " prefix; continuation lines start straight at
        # the player name ("Ai(N)-..."), so both shapes are matched here.
        elif current is not None and (m := re.match(r"(?:Combat: )?(Ai\(\d+\)-.+?) assigned (.+?) to attack (.+?)\.$", line)):
            target_seat = _seat_of(m.group(3), names_by_seat)
            target = f"Player {target_seat}" if target_seat else _strip_ids(m.group(3))
            attackers = [_strip_ids(a.strip()) for a in re.split(r",| and ", m.group(2)) if a.strip()]
            current["attacks"].setdefault(target, []).extend(attackers)
            _event({"type": "attack", "seat": _seat_of(m.group(1), names_by_seat), "target": target, "attackers": attackers})
        elif current is not None and (m := re.match(r"(?:Combat: )?(Ai\(\d+\)-.+?) assigned (.+?) to block (.+?)\.?$", line)):
            blocker_seat = _seat_of(m.group(1), names_by_seat)
            who = f"Player {blocker_seat}" if blocker_seat else _strip_ids(m.group(1))
            blockers = [_strip_ids(b.strip()) for b in re.split(r",| and ", m.group(2)) if b.strip()]
            attacker = _strip_ids(m.group(3))
            current["blocks"].append(f"{who} blocks {attacker} with " + ", ".join(blockers) + ".")
            _event({"type": "block", "seat": blocker_seat, "attacker": attacker, "blockers": blockers})
        elif current is not None and (m := re.match(r"(?:Combat: )?(Ai\(\d+\)-.+?) didn't block (.+?)\.?$", line)):
            _event({"type": "no_block", "seat": _seat_of(m.group(1), names_by_seat), "attacker": _strip_ids(m.group(2))})
        elif line.startswith("Zone Change: ") and current is not None:
            m = re.match(r"Zone Change: (.+?)(?: \((\d+)\))? was put into (Graveyard|Exile) from Battlefield\.?$", line)
            if m:
                name = _strip_ids(m.group(1))
                if m.group(2):
                    battlefield.pop(int(m.group(2)), None)  # left the battlefield — drop from the board tracker
                bucket = current["died"] if m.group(3) == "Graveyard" else current["exiled"]
                if name not in bucket:
                    bucket.append(name)
                _event({"type": "zone", "card": name, "to": m.group(3).lower()})
        elif line.startswith("Mana: ") and current is not None:
            m = re.match(r"Mana: (.+?) \((\d+)\) -", line)
            if m:
                oid, mname = int(m.group(2)), m.group(1).strip()
                # Attribute to the object's known controller; otherwise to whoever
                # cast it (mana rocks/dorks), else the active player (ramped lands).
                slot = battlefield.get(oid)
                seat = slot["seat"] if slot and slot["seat"] is not None else last_cast.get(mname, current["entry"]["seat"])
                if oid not in battlefield:  # first sighting: a mana source we hadn't placed yet
                    _enter(oid, seat, "land" if mname.lower() in land_names else "other", mname)
                if seat in current["mana"]:
                    current["mana"][seat] += _mana_pips(line)
        elif line.startswith("Discard: ") and current is not None:
            m = re.match(r"Discard: (.+?) discards (.+?)\.?$", line)
            if m:
                seat = _seat_of(m.group(1), names_by_seat)
                name = _strip_ids(m.group(2))
                who = "Discards" if seat == current["entry"]["seat"] else f"Player {seat} discards"
                current["discards"].append(f"{who} {name}.")
                _event({"type": "discard", "seat": seat, "card": name})
        elif line.startswith("Damage: ") and current is not None:
            m = re.match(r"Damage: (.+?) deals (\d+) ((?:non-)?combat )?damage to (.+)\.$", line)
            if m:
                source, amount = _strip_ids(m.group(1)), int(m.group(2))
                kind = (m.group(3) or "").strip() or "unspecified"
                target_seat = _seat_of(m.group(4), names_by_seat)
                target = f"Player {target_seat}" if target_seat is not None else _strip_ids(m.group(4))
                _event({"type": "damage", "source": source, "target": target, "amount": amount, "kind": kind})
                if target_seat is not None:
                    current["damage"][target] = current["damage"].get(target, 0) + amount
                    # Commander damage: combat damage to a player from a card named
                    # exactly like a seat's commander (rule 903.10a — 21 total loses).
                    if kind == "combat" and source in commanders:
                        by = cmd_damage.setdefault(source, {})
                        by[target_seat] = by.get(target_seat, 0) + amount
                        current["cmd_damage"].append((source, target, by[target_seat]))
        elif line.startswith("Life: "):
            m = re.match(r"Life: Life: (.+?) (-?\d+) > (-?\d+)$", line)
            if m:
                seat = _seat_of(m.group(1), names_by_seat)
                if seat is not None:
                    life[seat] = int(m.group(3))
                    _event({"type": "life", "seat": seat, "from": int(m.group(2)), "to": life[seat]})
                    if life[seat] <= 0 and seat not in eliminated and current is not None:
                        eliminated.add(seat)
                        current["eliminations"].append(f"Player {seat} is eliminated ({life[seat]} life).")
        elif line.startswith("Mulligan: "):
            if m := re.match(r"Mulligan: (.+?) has mulliganed down to", line):
                seat = _seat_of(m.group(1), names_by_seat)
                if seat is not None:
                    mulligans[seat] += 1  # counts the free Commander mulligan too — it's a real reshuffle
                    _event({"type": "mulligan", "seat": seat})
            elif m := re.match(r"Mulligan: (.+?) has kept a hand of (\d+) cards", line):
                seat = _seat_of(m.group(1), names_by_seat)
                if seat is not None:
                    kept_hands[seat] = int(m.group(2))
                    _event({"type": "kept_hand", "seat": seat, "cards": int(m.group(2))})
        elif "An unsupported card was requested:" in line:
            m = re.search(r'requested: "(.+?)"', line)
            if m and m.group(1) not in unsupported:
                unsupported.append(m.group(1))
        elif line.startswith("Game Outcome: "):
            body = line[len("Game Outcome: "):]
            # "Game Outcome: Turn N" uses yet another turn count (matches neither
            # rounds nor player-turns in observed logs) — ignored; the winner's
            # turn is taken from the parsed turn entries instead.
            if re.match(r"Turn \d+$", body):
                continue
            if " has won" in body or " has lost" in body:
                if " has won" in body:
                    seat = _seat_of(body.split(" has won")[0], names_by_seat)
                    if seat is not None:
                        winner_seat = seat
                # Replace each literal engine player name with a readable one.
                for s, name in names_by_seat.items():
                    body = body.replace(f"Ai({s})-{name}", name)
                outcome_reasons.append(body)
                _event({"type": "outcome", "text": body})

    _flush()

    is_draw = winner_seat is None
    # The winner's turn, in the same per-seat count the turn log shows.
    winner_turn = turn_counts.get(winner_seat, 0) if not is_draw else max(turn_counts.values(), default=0)
    method = "; ".join(outcome_reasons) if outcome_reasons else (
        "Draw — Forge's game clock expired." if is_draw else "See the engine log.")

    return {
        "turns": turns,
        "winner": {"seat": winner_seat if winner_seat is not None else 0,
                   "turn": winner_turn, "method": method},
        "mulligans": mulligans,
        "kept_hands": kept_hands,
        "unsupported_cards": unsupported,
        "is_draw": is_draw,
        "casts_by_seat": {s: sorted(names) for s, names in casts_by_seat.items()},
        "commander_damage": {cmd: {str(t): total for t, total in by.items()} for cmd, by in cmd_damage.items()},
        "detail": {"pregame": pregame, "turns": detail_turns},
    }


def coach_available() -> bool:
    """Coached matches need the fork jar (AtelierCoach hooks) and the launcher."""
    forks = sorted(THIRD_PARTY.glob(FORK_JAR_GLOB))
    return bool(forks) and (ATELIER_SIM_CLASSES / "AtelierSim.class").exists()


# Coached games pay real wall time for LLM calls, and Forge's -c clock counts
# wall seconds — so a coached game gets a much longer clock or every match
# would end as a clock-draw mid-thought.
COACHED_GAME_TIMEOUT_S = 2400
COACHED_SUBPROCESS_TIMEOUT_S = 3000
COACH_JAVA_TIMEOUT_MS = 100_000  # Java-side per-fetch cap; > coach.TURN_CALL_TIMEOUT_S


def run_match(deck_ids: list[str], coach: bool = False) -> dict:
    """Play one real Commander game between the given Atelier decks.

    With coach=True (requires the fork build, see coach_available), every seat
    gets the LLM turn coach from atelier/coach.py: standing orders per deck +
    per-own-turn threat ranking / hold list, enforced inside Forge's AI by the
    forge.ai.AtelierCoach hooks. Costs ~40-60 haiku calls and adds minutes of
    wall time per game — a deliberately opt-in mode.

    Returns {result: <parsed session result>, raw_log: str, names_by_seat,
    coach: {calls, failures} when coached}. Raises ValueError on setup
    problems and subprocess errors — the caller (simulator.MANAGER) shows
    those as a normal failed session."""
    if not is_available():
        raise ValueError("Forge engine is not installed under third_party/ — see atelier/forge_engine.py.")
    if coach and not coach_available():
        raise ValueError("Coached matches need the fork build — run forge_bridge/build.sh and the forge-src Maven build.")

    names_by_seat: dict[int, str] = {}
    decks_by_seat: dict[int, dict] = {}
    land_names: set[str] = set()   # for the board tracker: classify mana sources as land vs rock
    filenames: list[str] = []
    for seat, deck_id in enumerate(deck_ids, start=1):
        deck = archive.get_deck(deck_id)
        if deck is None:
            raise ValueError("One of the selected decks could not be found.")
        names_by_seat[seat] = str(deck.get("commander") or f"Deck {seat}")
        # get_deck records don't carry their own id — reattach it here, the
        # coach keys its standing-orders cache on it (state/coach_orders/).
        decks_by_seat[seat] = {**deck, "id": deck_id}
        land_names |= deck_land_names(deck)
        filenames.append(_write_dck(deck, seat))

    coach_server = None
    coach_props: list[str] = []
    game_timeout, subprocess_timeout = GAME_TIMEOUT_S, SUBPROCESS_TIMEOUT_S
    if coach:
        from . import coach as coach_mod  # deferred: pulls in claude_cli
        coach_server = coach_mod.CoachServer(decks_by_seat)
        port = coach_server.start()   # generates/loads standing orders first
        coach_props = [
            f"-Datelier.coach.url=http://127.0.0.1:{port}/coach",
            f"-Datelier.coach.token={coach_server.token}",
            f"-Datelier.coach.timeout_ms={COACH_JAVA_TIMEOUT_MS}",
        ]
        game_timeout, subprocess_timeout = COACHED_GAME_TIMEOUT_S, COACHED_SUBPROCESS_TIMEOUT_S

    jar = sorted(THIRD_PARTY.glob(FORK_JAR_GLOB))[-1] if coach else active_jar()
    if (ATELIER_SIM_CLASSES / "AtelierSim.class").exists():
        # Classes dir deliberately FIRST on the classpath: with the fork jar's
        # absolute path ahead of it the JVM inexplicably fails to find
        # AtelierSim (observed 2026-07-11); classes-first works with both jars.
        cmd = [
            str(java_path()), "-Xmx4g", *coach_props,
            "-cp", f"{ATELIER_SIM_CLASSES}:{jar}", "AtelierSim",
            "-d", *[str(DECKS_DIR / f) for f in filenames], "-c", str(game_timeout),
            *(["-p", AI_PROFILE] if AI_PROFILE else []),
        ]
    else:  # launcher not built — Forge's stock sim mode (no AI knobs, same log)
        cmd = [
            str(java_path()), "-Xmx4g",
            "-jar", str(jar),
            "sim", "-d", *filenames, "-f", "Commander", "-n", "1", "-c", str(game_timeout),
        ]
    try:
        proc = subprocess.run(
            cmd, cwd=str(FORGE_DIR), capture_output=True, text=True, timeout=subprocess_timeout,
        )
    finally:
        if coach_server is not None:
            coach_server.stop()
    raw = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    if "Game Result:" not in raw:
        tail = raw.strip().splitlines()[-8:]
        raise ValueError("Forge did not finish the game. Log tail: " + " | ".join(tail))

    out = {"result": parse_log(raw, names_by_seat, land_names), "raw_log": raw, "names_by_seat": names_by_seat}
    if coach_server is not None:
        out["coach"] = {"calls": coach_server.calls, "failures": coach_server.failures,
                        "run_id": coach_server.run_id}
    return out
