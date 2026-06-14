"""
app.py — Gishath Fetch v2 Flask application.

Startup sequence:
    1. Verify the engine binary exists (bin/gishath-engine).
    2. Spawn it as a subprocess on 127.0.0.1:<GISHATH_ENGINE_PORT>.
    3. Health-check poll /healthz until ready (or abort after 20 s).
    4. Start a persistent Playwright Chromium browser for BinderPOS stores.
    5. Serve Flask on 0.0.0.0:5001 (or $PORT).

Shutdown (SIGTERM / Ctrl-C / atexit):
    Stop Playwright browser → SIGTERM engine → SIGKILL if it doesn't exit in 5 s.

Architecture:
    • Go engine handles all non-BinderPOS stores (fast, direct HTTP).
    • Playwright handles the 10 BinderPOS/Shopify stores (bypasses Cloudflare TLS blocking).
    • Both run concurrently per search and their results are merged before returning.

Routes:
    GET  /          — UI (templates/index.html)
    POST /search    — JSON buy list → JSON results + store error summary
    POST /download  — JSON rows → xlsx download
"""
from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import io
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

import httpx
from flask import Flask, jsonify, render_template, request, send_file

from engine_client import ENGINE_BASE, ENGINE_PORT, search_many
from export.excel import write_excel
from optimizer import compute_plan, rows_to_results
from playwright_scraper import (
    BINDERPOS_STORES,
    debug_all_stores,
    run_async,
    search_many_playwright,
    start_browser,
    stop_browser,
)
from presentation import format_results

# ── Logging ───────────────────────────────────────────────────────────────────
_LOG_FORMAT = "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s"
_LOG_DATEFMT = "%H:%M:%S"

# Console handler — INFO and above (keeps terminal readable)
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))

# File handler — DEBUG and above (full scraping trace for diagnostics)
_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_log_dir, exist_ok=True)
_file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(_log_dir, "gishath.log"),
    maxBytes=5 * 1024 * 1024,   # 5 MB per file
    backupCount=3,               # keep last 3 rotated files
    encoding="utf-8",
)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))

logging.basicConfig(level=logging.DEBUG, handlers=[_console_handler, _file_handler])
log = logging.getLogger(__name__)

# ── Flask ─────────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.after_request
def _cors(response):
    # Allow the local hub (http://localhost:5010) to read responses cross-origin.
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response

# ── Engine subprocess ─────────────────────────────────────────────────────────
_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
ENGINE_BIN  = os.path.join(_BASE_DIR, "bin", "gishath-engine")
_engine_proc: subprocess.Popen | None = None

# Stores handled by the Go engine.
#
# Non-BinderPOS stores (direct HTTP, no Cloudflare issue):
#   Agora Hobby, Cards & Collections, Dueller's Point, 5 Mana, Mox & Lotus
#
# BinderPOS stores routed through the engine (uses BinderPOS decklist API at
# portal.binderpos.com — bypasses per-store Cloudflare; more reliable than
# Playwright HTML scraping for these two stores):
#   Flagship Games, OneMtg
#
# TCG Marketplace: the store's old authenticated API
# (/encoder/advancedsearch, required TCG_MARKETPLACE_ACCESS_TOKEN — never
# issued despite a request sent 2026-05-24) is gone. The site was rebuilt
# and the engine now calls its new unauthenticated /product/advancedfilter
# endpoint directly — no token or env var needed.
ENGINE_STORES = [
    "Agora Hobby",
    "Cards & Collections",
    "Dueller's Point",
    "5 Mana",
    "Mox & Lotus",
    # Flagship Games and One MTG are handled by the Playwright/curl_cffi path
    # (playwright_scraper.py) — Go engine's BinderPOS API requires a residential
    # proxy (DYNAMIC_PROXY) which is not configured in the local setup.
    "The TCG Marketplace",
]


def _start_engine() -> None:
    """Spawn the engine binary and wait until it reports healthy."""
    global _engine_proc

    if not os.path.isfile(ENGINE_BIN):
        log.error(
            "\n"
            "  Engine binary not found: %s\n"
            "  Build it first:\n"
            "      make engine-build\n",
            ENGINE_BIN,
        )
        sys.exit(1)

    env = {**os.environ, "GISHATH_ENGINE_PORT": ENGINE_PORT}
    _engine_proc = subprocess.Popen(
        [ENGINE_BIN],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    log.info("gishath-engine started  PID=%d  port=%s", _engine_proc.pid, ENGINE_PORT)

    # Poll /healthz until ready or timeout
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if _engine_proc.poll() is not None:
            out, _ = _engine_proc.communicate()
            log.error("Engine exited unexpectedly:\n%s", out or "(no output)")
            sys.exit(1)
        try:
            r = httpx.get(f"{ENGINE_BASE}/healthz", timeout=1.0)
            if r.status_code == 200:
                log.info("gishath-engine healthy on %s", ENGINE_BASE)
                return
        except Exception:
            pass
        time.sleep(0.4)

    log.error("Engine did not become healthy within 20 s. Aborting.")
    _stop_engine()
    sys.exit(1)


def _stop_engine() -> None:
    """Gracefully terminate the engine subprocess."""
    global _engine_proc
    if _engine_proc is None:
        return
    pid = _engine_proc.pid
    log.info("Stopping gishath-engine (PID %d)…", pid)
    try:
        _engine_proc.terminate()
        _engine_proc.wait(timeout=5)
        log.info("Engine stopped.")
    except subprocess.TimeoutExpired:
        log.warning("Engine did not exit; sending SIGKILL.")
        _engine_proc.kill()
    except Exception as exc:
        log.warning("Error stopping engine: %s", exc)
    finally:
        _engine_proc = None


def _shutdown() -> None:
    """Ordered shutdown: Playwright first, then engine."""
    stop_browser()
    _stop_engine()


atexit.register(_shutdown)


def _handle_signal(sig: int, frame) -> None:  # noqa: ANN001
    log.info("Received signal %d — shutting down.", sig)
    _shutdown()
    raise SystemExit(0)


signal.signal(signal.SIGTERM, _handle_signal)
# SIGINT (Ctrl-C) triggers KeyboardInterrupt → atexit fires naturally.


# ── Search helpers ────────────────────────────────────────────────────────────

def _merge_results(
    engine_results: dict[str, dict],
    playwright_results: dict[str, dict],
    buy_list: list[str],
) -> dict[str, dict]:
    """Merge Go engine and Playwright results into a single dict per card."""
    merged: dict[str, dict] = {}
    for card_name in buy_list:
        eng = engine_results.get(card_name, {"cards": [], "errors": []})
        pw  = playwright_results.get(card_name, {"cards": [], "errors": []})
        merged[card_name] = {
            "cards":  eng["cards"] + pw["cards"],
            "errors": eng["errors"] + pw["errors"],
        }
    return merged


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    """Lightweight liveness check for the hub status bar."""
    return jsonify({"ok": True})


@app.route("/search", methods=["POST"])
def search():
    body = request.get_json(force=True, silent=True) or {}
    raw: list[str] = body.get("buy_list", [])

    # Engine rejects queries shorter than 3 chars — filter early.
    buy_list = [s.strip() for s in raw if len(s.strip()) >= 3]
    skipped  = [s.strip() for s in raw if 0 < len(s.strip()) < 3]

    if not buy_list:
        return jsonify({"error": "No valid card names supplied (minimum 3 characters each)."}), 400

    t0 = time.monotonic()
    try:
        # Run Go engine (non-BinderPOS) and Playwright (BinderPOS) concurrently.
        # The engine runs its own asyncio.run(); Playwright submits to its
        # persistent background loop via run_async(). Both are submitted to a
        # ThreadPoolExecutor so they overlap in wall-clock time.
        def _run_engine():
            return asyncio.run(search_many(buy_list, stores=ENGINE_STORES))

        def _run_playwright():
            return run_async(search_many_playwright(buy_list))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            eng_future = pool.submit(_run_engine)
            pw_future  = pool.submit(_run_playwright)
            engine_results     = eng_future.result(timeout=60)
            playwright_results = pw_future.result(timeout=60)

    except Exception as exc:
        log.exception("Search failed: %s", exc)
        return jsonify({"error": "Search failed. Check server logs."}), 500

    elapsed = round(time.monotonic() - t0, 1)
    results_by_card = _merge_results(engine_results, playwright_results, buy_list)
    rows = format_results(results_by_card, buy_list)

    # Collect store errors across all cards, deduplicated by store name.
    seen_stores: set[str] = set()
    store_errors: list[dict] = []
    for card_name in buy_list:
        for err in results_by_card.get(card_name, {}).get("errors", []):
            store = err.get("store", "?")
            if store not in seen_stores:
                seen_stores.add(store)
                store_errors.append({"store": store, "error": err.get("error", "")})

    return jsonify({
        "results":      rows,
        "elapsed":      elapsed,
        "store_errors": store_errors,
        "skipped":      skipped,
    })


@app.route("/debug/stores")
def debug_stores():
    """Diagnostic endpoint — tests every Playwright store with a single card.

    Usage:
        GET /debug/stores                  — tests with default card "Lightning Bolt"
        GET /debug/stores?card=Counterspell

    Returns JSON array, one object per store:
        store        — store name
        url          — search URL attempted
        cffi_ok      — curl_cffi fetched without CF block
        cffi_raw     — raw BeautifulSoup item count
        cffi_cards   — cards passing name filter (curl_cffi path)
        playwright   — true if Playwright fallback was used
        pw_raw       — raw JS evaluator item count (Playwright path)
        pw_cards     — cards passing name filter (Playwright path)
        total_cards  — final result count
        error        — error/warning string (empty if clean)

    Also tests all engine stores via a single engine search and appends their results.
    """
    card = request.args.get("card", "Lightning Bolt").strip()
    if len(card) < 3:
        return jsonify({"error": "card name must be at least 3 characters"}), 400

    log.info("/debug/stores — testing all stores with card='%s'", card)

    # ── Playwright stores ─────────────────────────────────────────────────────
    try:
        pw_results: list[dict] = run_async(debug_all_stores(card))
    except Exception as exc:
        pw_results = [{"store": "playwright", "error": str(exc)}]

    # ── Engine stores ─────────────────────────────────────────────────────────
    import asyncio as _asyncio
    try:
        engine_data = _asyncio.run(search_many([card], stores=ENGINE_STORES))
        engine_per_card = engine_data.get(card, {"cards": [], "errors": []})
        engine_results = []
        seen: set[str] = set()
        for c in engine_per_card["cards"]:
            src = c.get("src", "unknown")
            if src not in seen:
                seen.add(src)
            engine_results.append({"store": src, "card_name": c.get("name", ""), "price": c.get("price", 0)})
        # Summarise per store
        store_summary: dict[str, dict] = {}
        for c in engine_per_card["cards"]:
            src = c.get("src", "unknown")
            if src not in store_summary:
                store_summary[src] = {"store": src, "source": "engine", "total_cards": 0, "error": ""}
            store_summary[src]["total_cards"] += 1
        for err in engine_per_card["errors"]:
            src = err.get("store", "unknown")
            if src not in store_summary:
                store_summary[src] = {"store": src, "source": "engine", "total_cards": 0}
            store_summary[src]["error"] = err.get("error", "")
        engine_summary = list(store_summary.values())
        # Add any engine stores with zero results (shows they were queried)
        known = {s["store"] for s in engine_summary}
        for name in ENGINE_STORES:
            if name not in known:
                engine_summary.append({"store": name, "source": "engine", "total_cards": 0, "error": "no results"})
    except Exception as exc:
        engine_summary = [{"store": "engine", "source": "engine", "total_cards": 0, "error": str(exc)}]

    # Tag Playwright results with source
    for r in pw_results:
        r["source"] = "playwright"

    all_results = sorted(pw_results + engine_summary, key=lambda x: x["store"])
    return jsonify({"card": card, "stores": all_results})


@app.route("/download", methods=["POST"])
def download():
    body = request.get_json(force=True, silent=True) or {}
    rows: list[dict] = body.get("results", [])

    if not rows:
        return jsonify({"error": "No results to export."}), 400

    # Export top-5 per card only — exclude hidden (rank > 5) rows.
    rows = [r for r in rows if not r.get("hidden", False)]

    # Build shopping plan from the same display rows.
    # rows_to_results() reconstructs the raw card data; the ordered unique card
    # names from the rows serve as buy_list (preserves search order).
    plan = None
    try:
        results_by_card = rows_to_results(rows)
        # Preserve the order cards appeared in the results list.
        seen: set[str] = set()
        buy_list: list[str] = []
        for r in rows:
            name = r.get("card", "")
            if name and name not in seen:
                seen.add(name)
                buy_list.append(name)
        plan = compute_plan(results_by_card, buy_list)
    except Exception as exc:
        log.warning("Shopping plan computation failed (export will still work): %s", exc)

    xlsx_bytes = write_excel(rows, plan=plan)
    timestamp  = datetime.now().strftime("%Y-%m-%d_%H-%M")
    filename   = f"gishath_results_{timestamp}.xlsx"

    return send_file(
        io.BytesIO(xlsx_bytes),
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _start_engine()
    start_browser()   # Playwright Chromium (background thread + event loop)

    port = int(os.environ.get("PORT", 5003))
    log.info("Flask app starting on http://127.0.0.1:%d", port)

    # IMPORTANT: debug=False and use_reloader=False are both required.
    # Flask's reloader spawns a child process which would try to start a
    # second engine subprocess and the health-check would fail (port in use).
    app.run(debug=False, host="127.0.0.1", port=port, use_reloader=False)
