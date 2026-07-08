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
import threading
import time
from datetime import datetime

import httpx
from flask import Flask, jsonify, render_template, request, send_file

import card_index
import ck_price
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
ENGINE_LOG_PATH = os.path.join(_log_dir, "engine.log")
_engine_proc: subprocess.Popen | None = None
_engine_log_fh = None                  # open file handle for engine output (kept to avoid GC)
_engine_lock = threading.Lock()        # serialise spawn/relaunch across request threads

# Overall wall-clock budget for a single /search request. The engine and the
# Playwright path share this deadline; whichever is still running when it
# elapses is abandoned so a stalled BinderPOS scrape can't hang the response.
SEARCH_BUDGET_SECONDS: float = 60.0

# Stores handled by the Go engine.
#
# Non-BinderPOS stores (direct HTTP, no Cloudflare issue):
#   Cards & Collections, Dueller's Point, 5 Mana, Mox & Lotus
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
#
# Agora Hobby moved to the Playwright path (2026-07-06): Cloudflare turned on
# an interactive Turnstile challenge site-wide, confirmed blocking the Go
# engine's plain HTTP client, curl, and even curl_cffi's Chrome-TLS
# impersonation alike — only a real/headless browser can solve it. See
# playwright_scraper.py's BINDERPOS_STORES (variant 4).
ENGINE_STORES = [
    "Cards & Collections",
    "Dueller's Point",
    "5 Mana",
    "Mox & Lotus",
    # Flagship Games and One MTG are handled by the Playwright/curl_cffi path
    # (playwright_scraper.py) — Go engine's BinderPOS API requires a residential
    # proxy (DYNAMIC_PROXY) which is not configured in the local setup.
    "The TCG Marketplace",
]


def _spawn_engine() -> subprocess.Popen:
    """Launch the engine binary, redirecting its output to ENGINE_LOG_PATH.

    CRITICAL (root cause of the engine going dead after some use): the engine
    logs several lines per shop on every search. If its stdout is a PIPE that
    nobody drains, the OS pipe buffer (~64 KB) fills and the engine BLOCKS on
    its next write — going unresponsive after a handful of searches, which
    silently drops all engine stores until a manual restart. Writing to a real
    file removes that failure mode entirely while preserving the engine's logs.
    """
    global _engine_log_fh

    if not os.path.isfile(ENGINE_BIN):
        log.error(
            "\n"
            "  Engine binary not found: %s\n"
            "  Build it first:\n"
            "      make engine-build\n",
            ENGINE_BIN,
        )
        sys.exit(1)

    # Append so engine history survives relaunches; line-buffered.
    _engine_log_fh = open(ENGINE_LOG_PATH, "a", buffering=1, encoding="utf-8")
    _engine_log_fh.write(f"\n===== engine launch {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
    _engine_log_fh.flush()

    env = {**os.environ, "GISHATH_ENGINE_PORT": ENGINE_PORT}
    proc = subprocess.Popen(
        [ENGINE_BIN],
        env=env,
        stdout=_engine_log_fh,
        stderr=subprocess.STDOUT,
    )
    log.info(
        "gishath-engine started  PID=%d  port=%s  (output → %s)",
        proc.pid, ENGINE_PORT, ENGINE_LOG_PATH,
    )
    return proc


def _engine_is_healthy() -> bool:
    """Fast liveness probe — one cheap /healthz call."""
    try:
        return httpx.get(f"{ENGINE_BASE}/healthz", timeout=1.0).status_code == 200
    except Exception:
        return False


def _wait_healthy(proc: subprocess.Popen, timeout: float) -> bool:
    """Poll /healthz until the engine is ready, the process exits, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False  # process exited during startup
        if _engine_is_healthy():
            return True
        time.sleep(0.4)
    return False


def _engine_tail(n: int = 20) -> str:
    """Last n lines of the engine log — for diagnosing a failed (re)launch."""
    try:
        with open(ENGINE_LOG_PATH, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-n:]) or "(no output)"
    except Exception:
        return "(engine log unavailable)"


def _start_engine() -> None:
    """Spawn the engine at boot and wait until healthy, or abort."""
    global _engine_proc
    with _engine_lock:
        _engine_proc = _spawn_engine()
        if _wait_healthy(_engine_proc, 20.0):
            log.info("gishath-engine healthy on %s", ENGINE_BASE)
            return
        log.error("Engine did not become healthy within 20 s:\n%s", _engine_tail())
    _stop_engine()
    sys.exit(1)


def _ensure_engine_healthy() -> bool:
    """If the engine is down, relaunch it once. Returns True if healthy.

    Mirrors the Playwright path's _ensure_browser_healthy(): a crash no longer
    silently drops every engine store until a manual restart — the next search
    revives it. Costs only one cheap /healthz call on the common (healthy) path.
    """
    global _engine_proc
    if _engine_is_healthy():
        return True
    with _engine_lock:
        # Re-check inside the lock — another request thread may have just revived it.
        if _engine_is_healthy():
            return True
        log.warning("Engine unresponsive — relaunching.")
        _stop_engine()
        _engine_proc = _spawn_engine()
        if _wait_healthy(_engine_proc, 20.0):
            log.info("gishath-engine relaunched and healthy.")
            return True
        log.error("Engine relaunch failed:\n%s", _engine_tail())
        return False


def _stop_engine() -> None:
    """Gracefully terminate the engine subprocess and close its log handle."""
    global _engine_proc, _engine_log_fh
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
        if _engine_log_fh is not None:
            try:
                _engine_log_fh.close()
            except Exception:
                pass
            _engine_log_fh = None


def _shutdown() -> None:
    """Ordered shutdown: CK price refresher first (daemon thread, cheapest to
    stop), then Playwright, then engine."""
    ck_price.stop_background_refresher()
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


@app.route("/api/autocomplete")
def autocomplete():
    """Card-name suggestions for the buy-list box (local Scryfall index).

    Suggestions are a nicety: an empty list (too-short query, index still
    warming up, or no match) is a normal 200, never an error.
    """
    query = request.args.get("q", "")
    return jsonify(card_index.search(query))


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

    # Card Kingdom reference price — pure local file read (state/ck_prices.json,
    # refreshed nightly by refresh_ck_prices.py), no network call, negligible
    # latency. None per-card if the cache is missing, stale (>48h), or has no
    # listing for that name — /search never blocks on or fails because of this.
    ck_prices = ck_price.get_prices_for_buy_list(buy_list)

    # Run Go engine (non-BinderPOS) and Playwright (BinderPOS) concurrently.
    # The engine runs its own asyncio.run(); Playwright submits to its
    # persistent background loop via run_async(). Both are submitted to a
    # ThreadPoolExecutor so they overlap in wall-clock time.
    #
    # The two paths are collected INDEPENDENTLY. A timeout or crash in one
    # must NOT discard the other's results: engine results (fast, reliable)
    # should still render when the Playwright/BinderPOS path stalls on
    # Cloudflare challenges or 429 rate-limiting (and vice-versa). A shared
    # wall-clock deadline bounds the total request time; whichever future is
    # still running when the deadline passes is abandoned (its background
    # thread finishes harmlessly and its result is discarded).
    def _run_engine():
        # Revive the engine if it died since the last search (see _ensure_engine_healthy).
        _ensure_engine_healthy()
        return asyncio.run(search_many(buy_list, stores=ENGINE_STORES))

    # Playwright publishes completed per-store results into pw_partial as it
    # goes, so if it blows the wall-clock budget below we can still keep the
    # stores/cards that finished instead of discarding the whole path. pw_lock
    # guards the cross-thread read (Flask thread) vs writes (Playwright loop).
    pw_partial: dict[str, dict] = {}
    pw_lock = threading.Lock()

    def _run_playwright():
        return run_async(search_many_playwright(buy_list, sink=pw_partial, sink_lock=pw_lock))

    engine_results: dict[str, dict] = {}
    playwright_results: dict[str, dict] = {}
    soft_errors: list[dict] = []
    deadline = t0 + SEARCH_BUDGET_SECONDS

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    try:
        eng_future = pool.submit(_run_engine)
        pw_future  = pool.submit(_run_playwright)

        try:
            engine_results = eng_future.result(timeout=max(0.1, deadline - time.monotonic()))
        except concurrent.futures.TimeoutError:
            log.error("Engine search exceeded %.0fs budget — continuing without engine results", SEARCH_BUDGET_SECONDS)
            soft_errors.append({"store": "engine", "error": f"timed out (>{SEARCH_BUDGET_SECONDS:.0f}s)"})
        except Exception as exc:
            log.exception("Engine search failed: %s", exc)
            soft_errors.append({"store": "engine", "error": str(exc)})

        try:
            playwright_results = pw_future.result(timeout=max(0.1, deadline - time.monotonic()))
        except concurrent.futures.TimeoutError:
            # Don't discard the whole path — keep the stores/cards that finished.
            # The coroutine keeps running on its loop; snapshot pw_partial (deep
            # enough that later appends can't mutate what we return) under the lock.
            with pw_lock:
                playwright_results = {
                    name: {"cards": list(v["cards"]), "errors": list(v["errors"])}
                    for name, v in pw_partial.items()
                }
            got = sum(1 for v in playwright_results.values() if v["cards"] or v["errors"])
            log.error(
                "Playwright search exceeded %.0fs budget — keeping partial results (%d/%d cards had data)",
                SEARCH_BUDGET_SECONDS, got, len(buy_list),
            )
            soft_errors.append({
                "store": "Playwright (BinderPOS)",
                "error": f"timed out (>{SEARCH_BUDGET_SECONDS:.0f}s); kept partial results for {got}/{len(buy_list)} cards",
            })
        except Exception as exc:
            log.exception("Playwright search failed: %s", exc)
            soft_errors.append({"store": "Playwright (BinderPOS)", "error": str(exc)})
    finally:
        # wait=False so we never block the response on a future that's still
        # stuck (e.g. Playwright on a Cloudflare interstitial).
        pool.shutdown(wait=False)

    # Hard-fail only if BOTH paths produced nothing at all — otherwise return
    # whatever succeeded and surface the failure as a store error chip.
    if not engine_results and not playwright_results:
        log.error("Search failed: both engine and Playwright returned no data")
        return jsonify({"error": "Search failed — no data from engine or Playwright. Check server logs."}), 500

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

    # Surface path-level failures (engine/Playwright timeout or crash) as chips too.
    for serr in soft_errors:
        store = serr.get("store", "?")
        if store not in seen_stores:
            seen_stores.add(store)
            store_errors.append(serr)

    return jsonify({
        "results":      rows,
        "elapsed":      elapsed,
        "store_errors": store_errors,
        "skipped":      skipped,
        "ck_prices":    ck_prices,
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

    # Recompute CK reference prices from the row's own card names rather than
    # relying on the client to echo back what /search returned — same local
    # file read, no network call, and works even if a client ever posts rows
    # without ck_prices attached.
    ck_prices: dict = {}
    try:
        card_names = list({r.get("card", "") for r in rows if r.get("card")})
        ck_prices = ck_price.get_prices_for_buy_list(card_names)
    except Exception as exc:
        log.warning("CK price lookup failed (export will still work): %s", exc)

    xlsx_bytes = write_excel(rows, plan=plan, ck_prices=ck_prices)
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
    ck_price.start_background_refresher()  # self-healing CK price cache (see ck_price.py)

    port = int(os.environ.get("PORT", 5003))
    log.info("Flask app starting on http://127.0.0.1:%d", port)

    # IMPORTANT: debug=False and use_reloader=False are both required.
    # Flask's reloader spawns a child process which would try to start a
    # second engine subprocess and the health-check would fail (port in use).
    app.run(debug=False, host="127.0.0.1", port=port, use_reloader=False)
