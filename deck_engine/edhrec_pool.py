"""
deck_engine/edhrec_pool.py — S1 (PRD v4 amendment §3.2): EDHREC synergy candidate pool.

Fetches https://json.edhrec.com/pages/commanders/<slug>.json for the chosen
commander and extracts a candidate pool of up to ~150 synergy cards. This is
a CANDIDATE LIST for the build prompt, never a whitelist — off-pool
inclusions are explicitly allowed with a one-line justification, preserving
the "favour unorthodox" house rule. A pool built entirely from what other
pilots already play would otherwise bias every deck toward the same
consensus shell; the chosen build angle decides which slice of the pool to
lean into, not the other way around.

If the pool comes back under EDHREC_MIN_POOL_SIZE cards (brand-new/obscure
commander, or thin EDHREC data), callers fall back to pre-v4 behaviour (no
pool) — surfaced to the caller via pool_block()'s second return value so the
email/log can note why a given night's deck skipped this.

EDHREC's JSON endpoint is UNOFFICIAL (not a documented public API) — cache
aggressively (>=7-day TTL per commander), use a polite descriptive User-
Agent, and degrade gracefully on any failure. This must never sit on the
pipeline's critical path the way the Scryfall cache does (PRD §4 constraint):
every failure mode here returns an empty pool rather than raising, so a
dead/blocked/changed EDHREC endpoint degrades one dimension of deck quality,
never crashes a run.

NOT exercised against the live endpoint in this sandbox — json.edhrec.com is
unofficial and unlikely to be reachable from here, same class of restriction
already documented elsewhere in this project for Scryfall/TCG Marketplace.
Verify the real response shape against a live commander on Trevor's Mac
before trusting this beyond the mocked tests; _extract_card_names() below is
a best-effort guess at the schema and may need adjusting.
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import config

try:
    import certifi
    _SSL_CONTEXT: ssl.SSLContext | None = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None

EDHREC_CACHE_DIR: Path = config.STATE_DIR / "edhrec_cache"
EDHREC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
EDHREC_CACHE_TTL_DAYS: int = 7
EDHREC_POOL_TARGET: int = 150
EDHREC_MIN_POOL_SIZE: int = 50   # below this, treat as "no usable pool" — caller falls back to v3 behaviour
EDHREC_USER_AGENT: str = "GishathDeckEngine/1.0 (personal project; contact: trevorjow@hotmail.com)"


def _slugify(commander: str) -> str:
    """Best-effort EDHREC commander slug: lowercase, spaces -> hyphens, apostrophes/
    commas/periods stripped. e.g. "Zinnia, Valley's Voice" -> "zinnia-valleys-voice".
    If EDHREC's actual slug convention differs for a given card, that commander's
    fetch just 404s and this degrades to an empty pool (see fetch_pool()) — not a crash."""
    # EDHREC slugs a multi-face commander by its FRONT face only — slugging the
    # combined "A // B" name 404s and silently costs the run its whole pool.
    slug = commander.split(" // ")[0].lower().strip()
    slug = re.sub(r"[',\.]", "", slug)
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug


def _cache_path(slug: str) -> Path:
    return EDHREC_CACHE_DIR / f"{slug}.json"


def _cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text())
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
        age_days = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 86400
        return age_days <= EDHREC_CACHE_TTL_DAYS
    except (json.JSONDecodeError, OSError, KeyError, ValueError):
        return False


def _http_get_json(url: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": EDHREC_USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_card_names(payload: dict) -> list[str]:
    """EDHREC's commander-page JSON nests synergy cards under
    container.json_dict.cardlists[*].cardviews[*].name (as last publicly documented) —
    tries that shape, then a flatter fallback. Returns [] rather than raising if
    neither shape matches (see module docstring on schema drift)."""
    try:
        cardlists = (
            payload.get("container", {}).get("json_dict", {}).get("cardlists", [])
            or payload.get("cardlists", [])
        )
        names: list[str] = []
        for group in cardlists:
            for view in group.get("cardviews", []):
                name = view.get("name")
                if name:
                    names.append(name)
        return names
    except (AttributeError, TypeError):
        return []


def fetch_pool(commander: str, *, force: bool = False) -> list[str]:
    """Returns up to EDHREC_POOL_TARGET candidate card names for `commander`, or
    [] on any failure/thin-data condition. Never raises — see module docstring."""
    slug = _slugify(commander)
    path = _cache_path(slug)

    if not force and _cache_fresh(path):
        try:
            return json.loads(path.read_text()).get("cards", [])
        except (json.JSONDecodeError, OSError):
            pass  # corrupt cache file — fall through to a live fetch

    url = f"https://json.edhrec.com/pages/commanders/{slug}.json"
    try:
        payload = _http_get_json(url)
        time.sleep(0.1)  # polite pause — unofficial endpoint, be a good citizen
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        # Network/parse failure — prefer a stale cache over nothing, otherwise empty.
        if path.exists():
            try:
                return json.loads(path.read_text()).get("cards", [])
            except (json.JSONDecodeError, OSError):
                pass
        return []

    names = _extract_card_names(payload)
    seen: set[str] = set()
    deduped: list[str] = []
    for name in names:  # preserve EDHREC's own ranking order (more-played first)
        key = name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(name)
    pool = deduped[:EDHREC_POOL_TARGET]

    try:
        path.write_text(json.dumps({
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "commander": commander,
            "cards": pool,
        }))
    except OSError:
        pass  # caching is best-effort; a write failure shouldn't break the pipeline

    return pool


def pool_block(commander: str, cache: dict) -> tuple[str, bool]:
    """Returns (formatted prompt block, pool_usable). pool_usable=False (with a
    placeholder block) if the pool came back under EDHREC_MIN_POOL_SIZE — callers
    should note that fallback for the email/log (PRD v4 amendment §3.2 S1)."""
    pool = fetch_pool(commander)
    if len(pool) < EDHREC_MIN_POOL_SIZE:
        return (
            "(no usable EDHREC synergy pool for this commander tonight — too new/obscure, or the "
            "endpoint was unreachable; building without a candidate pool, same as before this feature.)",
            False,
        )

    from . import scryfall_cache  # local import — keeps this module usable standalone

    lines = []
    for name in pool:
        card = cache.get(name.strip().lower())
        oracle = scryfall_cache.oracle_text_of(card) if card else ""
        summary = (oracle.split(".")[0].strip() + ".") if oracle else "(no oracle text on file)"
        lines.append(f"- {name}: {summary}")
    return "\n".join(lines), True
