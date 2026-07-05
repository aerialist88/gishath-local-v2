"""
deck_engine/pricing.py — stage 6: cheapest-SG pricing (PRD §4b, revised v3).

v2 draft (WRONG, corrected before build started): call `engine_client.
search_many()` directly. That only reaches the 6 Go-engine stores — the 9
BinderPOS/Playwright stores are only reachable through gishath-local-v2's own
`/search` Flask route (engine + Playwright fan-out, merge, health-check/
relaunch all live in app.py). Locked v3 approach: this module is an HTTP
CLIENT of the already-running gishath-local-v2 app. `make run` must be up
before the nightly job calls this — run_nightly.sh checks/starts it. No new
scraping code; this file only talks HTTP to the existing app.
"""
from __future__ import annotations

import json as _json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import config

if str(config.REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(config.REPO_ROOT))

import optimizer  # noqa: E402 — gishath-local-v2/optimizer.py: rows_to_results(), compute_plan()


@dataclass
class PricingOutcome:
    plan: object | None          # optimizer.ShoppingPlan, or None if pricing failed
    rows: list[dict] = field(default_factory=list)
    available: bool = True       # False if gishath-local-v2 couldn't be reached at all
    error: str = ""
    # PRD v4 amendment §3.4: prices for cards swapped IN by the budget pass —
    # (lowercased_name, price, store) tuples appended by budget_pass.py after
    # re-pricing only the substitutes (never a full-deck re-scrape). Overlaid on
    # top of the plan's own assignments in cheapest_by_card() below, so every
    # downstream consumer (export breakdown, email headline/top-5) prices the
    # FINAL post-swap deck without knowing the budget pass exists.
    extra_assignments: list[tuple[str, float, str]] = field(default_factory=list)


def cheapest_by_card(pricing: "PricingOutcome") -> dict[str, tuple[float, str]]:
    """Cheapest-per-card SGD price/store lookup from Strategy A (compute_plan()'s
    "absolute cheapest listing for every card" strategy) — shared by export.py's
    Breakdown sheet and emailer.py's SGD headline/top-5 (PRD v4 amendment §3.3) so
    both compute the same numbers off the same source, never two independently
    drifting versions."""
    result: dict[str, tuple[float, str]] = {}
    if pricing.available and pricing.plan is not None:
        for assignment in pricing.plan.strategy_a.all_assignments:
            result[assignment.card.strip().lower()] = (assignment.price, assignment.store)
    # Budget-pass re-prices overlay the original plan (§3.4) — applied last so a
    # swapped-in card's fresh price wins over any stale hit from the original scrape.
    for name_key, price, store in pricing.extra_assignments:
        result[name_key] = (price, store)
    return result


def deck_price_summary(pricing: "PricingOutcome", all_cards: list[str], top_n: int = 5) -> dict:
    """Returns {'total': float, 'priced_count': int, 'unpriced_count': int,
    'top_expensive': [(card, price), ...]} — the email's SGD headline + top-5 most
    expensive cards (PRD v4 amendment §3.3, "the most decision-relevant number the
    v3 email was missing entirely"). `all_cards` should include the commander."""
    priced = cheapest_by_card(pricing)
    total = 0.0
    priced_count = 0
    unpriced_count = 0
    entries: list[tuple[str, float]] = []
    for card in all_cards:
        key = card.strip().lower()
        info = priced.get(key)
        if info is None:
            unpriced_count += 1
            continue
        price, _store = info
        total += price
        priced_count += 1
        entries.append((card, price))
    entries.sort(key=lambda e: e[1], reverse=True)
    return {
        "total": round(total, 2),
        "priced_count": priced_count,
        "unpriced_count": unpriced_count,
        "top_expensive": entries[:top_n],
    }


def wait_for_gishath_app(
    timeout_s: float = config.GISHATH_STARTUP_TIMEOUT_S,
    poll_interval_s: float = 1.0,
) -> bool:
    """Poll /api/health until it responds 200 or timeout_s elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(config.GISHATH_HEALTH_URL, timeout=3.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(poll_interval_s)
    return False


def fetch_prices(card_names: list[str]) -> PricingOutcome:
    """Call gishath-local-v2's /search endpoint and build a shopping plan.

    Fails SOFT by design (PRD §2.4 / §6): if the app is unreachable or
    /search errors, returns available=False with an error string rather than
    raising — the deck still ships, with prices flagged unavailable, instead
    of the whole run blocking on a pricing hiccup.
    """
    if not wait_for_gishath_app():
        return PricingOutcome(
            plan=None, available=False,
            error=(
                f"gishath-local-v2 not reachable at {config.GISHATH_APP_BASE} after "
                f"{config.GISHATH_STARTUP_TIMEOUT_S:.0f}s. Is `make run` running? "
                "run_nightly.sh should have started it — check server.log / logs/engine.log."
            ),
        )

    body = _json.dumps({"buy_list": card_names}).encode("utf-8")
    req = urllib.request.Request(
        config.GISHATH_SEARCH_URL, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90.0) as resp:
            payload = _json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, _json.JSONDecodeError) as exc:
        return PricingOutcome(plan=None, available=False, error=f"/search request failed: {exc}")

    if "error" in payload:
        return PricingOutcome(plan=None, available=False, error=f"/search returned an error: {payload['error']}")

    # Mirror app.py's own /download behaviour: drop rank>5 "hidden" rows
    # before building the shopping plan — these are the same top-5-per-card
    # display rows the web UI exports today.
    rows = [r for r in payload.get("results", []) if not r.get("hidden", False)]

    try:
        results_by_card = optimizer.rows_to_results(rows)
        seen: set[str] = set()
        buy_list: list[str] = []
        for r in rows:
            name = r.get("card", "")
            if name and name not in seen:
                seen.add(name)
                buy_list.append(name)
        plan = optimizer.compute_plan(results_by_card, buy_list)
    except Exception as exc:  # noqa: BLE001 — pricing must never take down the whole run
        return PricingOutcome(plan=None, rows=rows, available=True, error=f"compute_plan() failed: {exc}")

    return PricingOutcome(plan=plan, rows=rows, available=True)
