"""atelier/server.py — Flask API + SSE + static frontend for the Atelier.

Local, single-user, same trust model as app.py (binds 127.0.0.1). Runs on its
own port (5077) so the pricing app (5003) stays untouched — the pipeline's
pricing stage talks to 5003 exactly as it does on a nightly run.

    python -m atelier.server            # then open http://127.0.0.1:5077
    python -m atelier.desktop           # same server inside a native window
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from deck_engine import config, run_log

from . import archive, art, commanders, forge_engine, own_decks, rules_reference, settings, simulator
from .runner import MANAGER, RUNS_DIR

STATIC_DIR = Path(__file__).resolve().parent / "static"
PORT = int(os.environ.get("ATELIER_PORT", 5077))

app = Flask(__name__, static_folder=None)


@app.after_request
def _cors(response):
    response.headers["Access-Control-Allow-Origin"] = "http://localhost:5010"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ── frontend ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:filename>")
def static_files(filename: str):
    return send_from_directory(STATIC_DIR, filename)


# ── status / commission ──────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"ok": True})


def _pricing_up() -> bool:
    """Is the gishath-local-v2 pricing app (port 5003) alive? The pipeline
    degrades to an unpriced deck without it, so the home screen warns."""
    import urllib.request
    try:
        with urllib.request.urlopen(config.GISHATH_HEALTH_URL, timeout=1.5):
            return True
    except Exception:  # noqa: BLE001
        return False


@app.route("/api/status")
def status():
    """Everything the home screen needs in one call."""
    log = MANAGER.current()
    # The home screen is the guild's shopfront — 3vor's own uploads live in the
    # gallery's dedicated section, never on the "fresh from the forge" shelf.
    decks = [d for d in archive.list_decks() if not d.get("owner_deck")]
    stored = settings.current()
    return jsonify({
        "pricing_up": _pricing_up(),
        "run": log.state() if log else None,
        "running": MANAGER.is_running(),
        "night_no": run_log.successful_run_count() + 1,
        "nightly_time": stored["nightly_time"],
        "nightly_enabled": stored["nightly_enabled"],
        "latest_deck": decks[0] if decks else None,
        "decks": decks[:10],
        "knobs": {
            "bracket": stored["bracket"],
            "deck_budget_sgd": stored["deck_budget_sgd"],
            "max_card_price_sgd": stored["max_card_price_sgd"],
            "max_run_spend_usd": stored["max_run_spend_usd"],
            "dedupe_days": stored["dedupe_commander_days"],
        },
    })


@app.route("/api/commission", methods=["POST"])
def commission():
    body = request.get_json(silent=True) or {}
    if body.get("demo"):
        result = MANAGER.start_demo()
    else:
        commander = (body.get("commander") or "").strip() or None
        result = MANAGER.start(forced_commander=commander)
    code = 409 if "error" in result else 200
    return jsonify(result), code


@app.route("/api/run/abandon", methods=["POST"])
def abandon():
    result = MANAGER.cancel()
    return jsonify(result), (400 if "error" in result else 200)


# ── live run ─────────────────────────────────────────────────────────────────

@app.route("/api/run/snapshot")
def run_snapshot():
    log = MANAGER.current()
    if log is None:
        return jsonify({"error": "no run"}), 404
    return jsonify(log.state())


@app.route("/api/run/events")
def run_events():
    """SSE: events since ?since=<seq>. Sends a heartbeat comment every idle
    wait so proxies/webviews never reap the connection; ends with event:done
    once the run finishes and everything has been flushed."""
    log = MANAGER.current()
    if log is None:
        return jsonify({"error": "no run"}), 404
    try:
        since = int(request.args.get("since", 0))
    except ValueError:
        since = 0

    def _gen(cursor: int):
        while True:
            events = log.wait_events(cursor, timeout=20.0)
            if events:
                cursor = events[-1]["seq"] + 1
                for e in events:
                    yield f"data: {json.dumps(e)}\n\n"
            else:
                yield ": heartbeat\n\n"
            if log.finished() and not log.wait_events(cursor, timeout=0.05):
                yield "event: done\ndata: {}\n\n"
                return

    return Response(_gen(since), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/run/last_failed")
def last_failed():
    """Most recent persisted run that halted — backs the failure screen after
    an app restart (the in-memory log is gone, the JSON post-mortem isn't)."""
    candidates = sorted(RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates[:5]:
        try:
            state = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            continue
        if state.get("status") in ("failed", "cancelled"):
            return jsonify(state)
    return jsonify({"error": "no failed runs"}), 404


# ── gallery ──────────────────────────────────────────────────────────────────

@app.route("/api/decks")
def decks():
    return jsonify(archive.list_decks())


@app.route("/api/decks/<id8>")
def deck_detail(id8: str):
    deck = archive.get_deck(id8)
    if deck is None:
        return jsonify({"error": "deck not found"}), 404
    return jsonify(deck)


@app.route("/api/decks/import", methods=["POST"])
def deck_import():
    """Shelve one of 3vor's own decks from a pasted decklist."""
    body = request.get_json(silent=True) or {}
    try:
        result = own_decks.save_deck(
            text=str(body.get("text") or ""),
            commander=str(body.get("commander") or ""),
            label=str(body.get("label") or ""),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result), 201


@app.route("/api/decks/<id8>", methods=["DELETE"])
def deck_delete(id8: str):
    """Remove an uploaded deck. Guild commissions refuse (403) by design."""
    try:
        return jsonify(own_decks.delete_deck(id8))
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403


@app.route("/api/decks/<id8>/file/<kind>")
def deck_file(id8: str, kind: str):
    path = archive.file_path(id8, kind)
    if path is None or not path.exists():
        return jsonify({"error": "file not found"}), 404
    return send_file(path, as_attachment=True, download_name=path.name)


@app.route("/api/art")
def card_art():
    name = request.args.get("name", "")
    if not name:
        return jsonify({"error": "name required"}), 400
    entry = art.lookup(name)
    if entry is None:
        return jsonify({"pending": True}), 202
    return jsonify(entry)


@app.route("/api/commanders")
def commander_search():
    query = request.args.get("q", "")
    return jsonify(commanders.search(query))


# ── grounded match rehearsal ────────────────────────────────────────────────

@app.route("/api/simulations", methods=["POST"])
def start_simulation():
    body = request.get_json(silent=True) or {}
    deck_ids = body.get("deck_ids") or []
    if not isinstance(deck_ids, list):
        return jsonify({"error": "deck_ids must be a list"}), 400
    try:
        seed = body.get("seed")
        session = simulator.MANAGER.start([str(deck_id) for deck_id in deck_ids], seed=seed)
        return jsonify(session), 202
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/simulations")
def list_simulations():
    return jsonify(simulator.MANAGER.list_sessions())


@app.route("/api/simulations/<session_id>")
def simulation_detail(session_id: str):
    session = simulator.MANAGER.get(session_id)
    if session is None:
        return jsonify({"error": "rehearsal not found"}), 404
    return jsonify(session)


def _simulation_receipt(session_id: str, suffix: str, mimetype: str):
    """Serve a per-game receipt file (Forge raw log / structured detail export).
    The id is reduced to hex so a crafted id can't path-escape SIMULATION_DIR."""
    clean = "".join(c for c in session_id if c in "0123456789abcdef")
    path = simulator.SIMULATION_DIR / f"{clean}{suffix}"
    if clean != session_id or not path.exists():
        return jsonify({"error": "no engine receipt for that game"}), 404
    return send_file(path, mimetype=mimetype, as_attachment=True, download_name=path.name)


@app.route("/api/simulations/<session_id>/details")
def simulation_forge_details(session_id: str):
    """The structured per-turn export: every parsed engine event, in order.
    Games played before this export existed only saved the raw log — for
    those, the export is rebuilt from that log on the fly."""
    clean = "".join(c for c in session_id if c in "0123456789abcdef")
    if clean == session_id and not (simulator.SIMULATION_DIR / f"{clean}.forge.details.json").exists():
        log_path = simulator.SIMULATION_DIR / f"{clean}.forge.log"
        session = simulator.MANAGER.get(clean)
        if log_path.exists() and session is not None:
            players = (session.get("grounding") or {}).get("players") or []
            names = {p["seat"]: p.get("commander", f"Deck {p['seat']}") for p in players}
            land_names: set[str] = set()
            for p in players:
                deck = archive.get_deck(p.get("deck_id"))
                if deck:
                    land_names |= forge_engine.deck_land_names(deck)
            parsed = forge_engine.parse_log(log_path.read_text(), names, land_names)
            return jsonify({"session_id": clean, "rebuilt_from_raw_log": True,
                            "players": players, "winner": parsed["winner"],
                            "mulligans": parsed["mulligans"], "kept_hands": parsed["kept_hands"],
                            "commander_damage": parsed["commander_damage"],
                            "casts_by_seat": parsed["casts_by_seat"],
                            "unsupported_cards": parsed["unsupported_cards"], **parsed["detail"]})
    return _simulation_receipt(session_id, ".forge.details.json", "application/json")


@app.route("/api/simulations/<session_id>/forge-log")
def simulation_forge_log(session_id: str):
    """Forge's complete raw typed game log, verbatim."""
    return _simulation_receipt(session_id, ".forge.log", "text/plain")


@app.route("/api/engine")
def engine_status():
    """Which simulation engine games will run on — the UI hides the LLM
    referee's grounding paraphernalia (source documents, replay seed) when
    the Forge rules engine is installed and will be used instead."""
    return jsonify({"forge": forge_engine.is_available()})


@app.route("/api/rules", methods=["GET", "POST"])
def comprehensive_rules():
    """Status/explicit refresh for the local official-rules cache."""
    try:
        return jsonify(rules_reference.refresh() if request.method == "POST" else rules_reference.status())
    except Exception as exc:  # noqa: BLE001 — download failures should be actionable, not an HTML 500 page
        return jsonify({"error": str(exc)}), 502


# ── guild rules ──────────────────────────────────────────────────────────────

@app.route("/api/settings", methods=["GET", "PUT"])
def guild_rules():
    if request.method == "GET":
        return jsonify(settings.current())
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(settings.save(body))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


def main() -> None:
    # threaded=True: SSE holds one connection open per live-view tab while the
    # pipeline thread and normal API calls keep flowing.
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
