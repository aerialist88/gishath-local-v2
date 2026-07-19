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

Response shape verified against the live endpoint 2026-07-14 (bre-of-clan-
stoutarm): container.json_dict.cardlists holds ~a dozen sections (newcards,
highsynergycards, topcards, gamechangers, creatures, instants, sorceries,
utilityartifacts, enchantments, planeswalkers, utilitylands, manaartifacts,
lands), each ordered by play rate. The pool takes a proportional slice of
every section (_proportional_trim) rather than a flat first-N of the
concatenation — the flat slice always ran out before manaartifacts/lands,
which is why no deck's candidate pool ever contained Sol Ring.
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
# Bumped when the pool-building logic changes shape enough that old cached
# pools are wrong, not just stale. Format 2 = proportional per-section trim
# (2026-07-14): format-1 caches were a flat first-150 slice of EDHREC's
# section order, which always truncated before the manaartifacts/lands
# sections — Sol Ring sits at overall position ~200, so no pool ever offered
# it (or any mana rock or nonbasic land) to the drafters.
EDHREC_CACHE_FORMAT: int = 2
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
        if payload.get("format") != EDHREC_CACHE_FORMAT:
            return False  # older pool-building logic — refetch (stale-on-network-failure still uses it)
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
        age_days = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 86400
        return age_days <= EDHREC_CACHE_TTL_DAYS
    except (json.JSONDecodeError, OSError, KeyError, ValueError):
        return False


def _http_get_json(url: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": EDHREC_USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_card_sections(payload: dict) -> list[list[str]]:
    """EDHREC's commander-page JSON nests synergy cards under
    container.json_dict.cardlists[*].cardviews[*].name (as last publicly documented) —
    tries that shape, then a flatter fallback. Returns one name list per cardlist
    section (highsynergycards, topcards, creatures, ..., manaartifacts, lands),
    preserving both section order and EDHREC's play-rate order within each.
    Returns [] rather than raising if neither shape matches (see module
    docstring on schema drift)."""
    try:
        cardlists = (
            payload.get("container", {}).get("json_dict", {}).get("cardlists", [])
            or payload.get("cardlists", [])
        )
        sections: list[list[str]] = []
        for group in cardlists:
            names = [view.get("name") for view in group.get("cardviews", []) if view.get("name")]
            if names:
                sections.append(names)
        return sections
    except (AttributeError, TypeError):
        return []


SMALL_SECTION_MAX: int = 15  # sections this size or under are curated headline lists — never trim them


def _proportional_trim(sections: list[list[str]], target: int) -> list[str]:
    """Trim to `target` names, keeping small sections whole (EDHREC's curated
    headline lists — newcards/highsynergycards/topcards/gamechangers — plus
    naturally small ones like manaartifacts and planeswalkers) and slicing the
    large type sections proportionally (largest-remainder rounding), top-ranked
    cards first. A flat first-`target` slice of the concatenated sections
    always exhausted the budget in the early sections and silently dropped the
    manaartifacts and lands sections for EVERY commander — which is why no
    drafted pool ever contained Sol Ring (verified live 2026-07-14: overall
    position ~203 of ~253 on a typical page)."""
    total = sum(len(names) for names in sections)
    if total <= target:
        return [name for names in sections for name in names]

    small_total = sum(len(names) for names in sections if len(names) <= SMALL_SECTION_MAX)
    if small_total < target:
        keep_whole = lambda names: len(names) <= SMALL_SECTION_MAX  # noqa: E731
        budget = target - small_total
    else:
        keep_whole = lambda names: False  # noqa: E731 — degenerate page of tiny sections: trim everything
        budget = target

    large = [i for i, names in enumerate(sections) if not keep_whole(names)]
    large_total = sum(len(sections[i]) for i in large)
    quotas = {i: len(sections[i]) * budget / large_total for i in large}
    counts = {i: int(quotas[i]) for i in large}
    remainders = sorted(large, key=lambda i: quotas[i] - counts[i], reverse=True)
    for i in remainders[: budget - sum(counts.values())]:
        counts[i] = min(counts[i] + 1, len(sections[i]))

    return [
        name
        for i, names in enumerate(sections)
        for name in (names if keep_whole(names) else names[: counts[i]])
    ]


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

    # Dedupe across sections first (a topcards entry repeats in its type
    # section) so duplicates don't inflate a section's share of the trim;
    # first occurrence wins, preserving EDHREC's own ranking order.
    seen: set[str] = set()
    sections: list[list[str]] = []
    for names in _extract_card_sections(payload):
        unique = []
        for name in names:
            key = name.strip().lower()
            if key not in seen:
                seen.add(key)
                unique.append(name)
        if unique:
            sections.append(unique)
    pool = _proportional_trim(sections, EDHREC_POOL_TARGET)

    try:
        path.write_text(json.dumps({
            "format": EDHREC_CACHE_FORMAT,
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
