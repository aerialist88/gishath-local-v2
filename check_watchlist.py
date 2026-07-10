"""check_watchlist.py — nightly price-watch alerts.

Runs the watchlist (state/watchlist.json, managed from the 3vor Fetch UI)
through the app's own /search endpoint and emails when a card's best price
dips to/under its target. Wired into run_nightly.sh right after the app is
confirmed up, so it rides the same nightly trigger as the deck engine — no
extra scheduling, no separate scraper.

Email goes through deck_engine's Gmail SMTP setup (deck_engine/.env must be
sourced, which run_nightly.sh already does). If email credentials are
missing, hits are still logged and the alert state is NOT updated, so the
alert fires again once email works.

Alert hygiene lives in watchlist.should_alert(): one email per dip — a card
sitting at SGD 8 under a SGD 10 target alerts once, then stays quiet unless
it drops further or bounces back above target first (which re-arms it).

Usage:
    source venv/bin/activate && python check_watchlist.py
Exit codes: 0 = ran fine (alerts or not), 1 = could not run (app down etc.).
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from email.message import EmailMessage

import httpx

import watchlist

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("check_watchlist")

APP_BASE = "http://127.0.0.1:5003"
HEALTH_TIMEOUT_S = 60.0    # app may still be booting (engine + Playwright)
SEARCH_TIMEOUT_S = 120.0   # /search's own budget is 60s; allow slack


def _wait_for_app() -> bool:
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{APP_BASE}/api/health", timeout=2.0).status_code == 200:
                return True
        except Exception:  # noqa: BLE001 — not up yet
            pass
        time.sleep(1.0)
    return False


def _best_listings(rows: list[dict]) -> dict[str, dict]:
    """card -> cheapest non-error row across all stores/listings."""
    best: dict[str, dict] = {}
    for r in rows:
        if r.get("is_error") or float(r.get("price_val") or 0) <= 0:
            continue
        card = r["card"]
        if card not in best or r["price_val"] < best[card]["price_val"]:
            best[card] = r
    return best


def _send_alert_email(hits: list[dict]) -> None:
    from deck_engine import config, emailer

    lines_txt = []
    lines_html = []
    for h in hits:
        line = (f"{h['card']} — SGD {h['price']:.2f} at {h['store']} "
                f"(target SGD {h['target']:.2f})")
        lines_txt.append(f"  - {line}" + (f"\n    {h['url']}" if h.get("url") else ""))
        link = f' · <a href="{h["url"]}">view listing</a>' if h.get("url") else ""
        lines_html.append(
            f'<li style="margin:6px 0;"><strong>{h["card"]}</strong> — '
            f'SGD {h["price"]:.2f} at {h["store"]} '
            f'<span style="color:#6B7280;">(target SGD {h["target"]:.2f})</span>{link}</li>'
        )

    msg = EmailMessage()
    plural = "s" if len(hits) > 1 else ""
    msg["Subject"] = f"3vor Fetch price alert — {len(hits)} card{plural} under target"
    msg["From"] = config.EMAIL_FROM
    msg["To"] = config.EMAIL_TO
    msg.set_content(
        f"Price watch hits ({datetime.now():%Y-%m-%d %H:%M}):\n\n"
        + "\n".join(lines_txt)
        + "\n\nManage the watchlist in 3vor Fetch (http://127.0.0.1:5003).\n"
    )
    msg.add_alternative(
        f"""<html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;">
<h3 style="color:#6D28D9;">3vor Fetch price alert</h3>
<ul style="padding-left:18px;">{''.join(lines_html)}</ul>
<p style="color:#6B7280;font-size:12px;">Prices are tonight's cheapest in-stock listing across the
scraped stores. Manage the watchlist in 3vor Fetch (http://127.0.0.1:5003).</p>
</body></html>""",
        subtype="html",
    )
    emailer._send(msg)  # noqa: SLF001 — same repo; reuses its local-fallback-on-failure behaviour


def main() -> int:
    entries = watchlist.list_entries()
    if not entries:
        log.info("Watchlist is empty — nothing to check.")
        return 0

    if not _wait_for_app():
        log.error("3vor Fetch app not reachable on %s — watchlist not checked.", APP_BASE)
        return 1

    cards = [e["card"] for e in entries]
    log.info("Checking %d watched card(s): %s", len(cards), ", ".join(cards))
    try:
        resp = httpx.post(f"{APP_BASE}/search", json={"buy_list": cards}, timeout=SEARCH_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.error("Watchlist search failed: %s", exc)
        return 1

    best = _best_listings(data.get("results", []))
    hits: list[dict] = []
    for entry in entries:
        row = best.get(entry["card"])
        if row is None:
            log.info("  %s: no in-stock listings tonight.", entry["card"])
            continue
        price = float(row["price_val"])
        if price > entry["target_sgd"]:
            log.info("  %s: SGD %.2f (above target %.2f).", entry["card"], price, entry["target_sgd"])
            watchlist.reset_alert(entry["card"])  # re-arm for the next dip
        elif watchlist.should_alert(entry, price):
            log.info("  %s: SGD %.2f <= target %.2f — ALERT.", entry["card"], price, entry["target_sgd"])
            hits.append({
                "card": entry["card"], "price": price, "target": entry["target_sgd"],
                "store": row.get("src", "?"), "url": row.get("url", ""),
            })
        else:
            log.info("  %s: SGD %.2f under target but already alerted at this level.",
                     entry["card"], price)

    if not hits:
        log.info("No new alerts.")
        return 0

    try:
        _send_alert_email(hits)
    except Exception as exc:  # noqa: BLE001 — emailer already wrote a local fallback file
        log.error("Alert email failed (alert state left armed so it retries tomorrow): %s", exc)
        return 1

    for h in hits:
        watchlist.mark_alerted(h["card"], h["price"])
    log.info("Alert email sent for %d card(s).", len(hits))
    return 0


if __name__ == "__main__":
    sys.exit(main())
