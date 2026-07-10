"""
deck_engine/scryfall_cache.py — local Scryfall bulk-data cache + deck validation.

PRD §4e: validation runs against a local cache of Scryfall's bulk
`default_cards` file, refreshed periodically (SCRYFALL_CACHE_MAX_AGE_DAYS),
NOT live per-card API calls. This is both faster and avoids hammering
Scryfall's live API during the agent pipeline's validate/repair loop, which
can issue many lookups per attempt.

NOTE ON SANDBOX TESTING: `api.scryfall.com` is blocked by this sandbox's
network allowlist (`X-Proxy-Error: blocked-by-allowlist`, confirmed
2026-07-01 — same class of issue already documented in
project-gishath for thetcgmarketplace.com:3501). This module could not be
exercised against the live API from here. Run `refresh_cache()` once on
Trevor's Mac (real internet) before the first dry run, and re-run
`python -m deck_engine.scryfall_cache --refresh` any time the cache looks
stale.

Usage:
    from deck_engine import scryfall_cache
    scryfall_cache.refresh_if_stale()          # no-op if cache is fresh
    cache = scryfall_cache.load_cache()
    result = scryfall_cache.validate_deck(commander="Gishath, Sun's Avatar",
                                           decklist=[...99 other names...],
                                           cache=cache)
    if not result.is_valid:
        ...feed result back into the repair prompt...
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config

# macOS python.org framework builds of Python don't wire the stdlib `ssl`
# module into the system trust store by default (unlike Homebrew Python or
# httpx, which bundles certifi) — bare urllib.request.urlopen() then fails
# with SSLCertVerificationError: unable to get local issuer certificate.
# Building an explicit SSLContext from certifi's bundle fixes this
# regardless of which Python distribution/venv this runs under, rather than
# depending on Trevor having run the one-off "Install Certificates.command"
# that ships with python.org installers.
try:
    import certifi
    _SSL_CONTEXT: ssl.SSLContext | None = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = None  # falls back to the interpreter's default trust store

# Fields kept from each Scryfall card object — trimmed aggressively, the full
# bulk file is large and we only need enough to validate legality/singleton/
# color-identity, not full oracle data.
#
# PRD v4 amendment additions (2026-07-03): cmc/mana_cost/rarity/image_uris/
# card_faces — needed for export.py's CMC/type/rarity columns + curve/pip
# stats block, and emailer.py's commander image (§3.3). card_faces covers
# double-faced cards, where Scryfall nests image_uris/mana_cost per-face
# instead of top-level.
_KEEP_FIELDS = (
    "name", "type_line", "oracle_text", "color_identity", "legalities",
    "layout", "oracle_id", "cmc", "mana_cost", "rarity", "image_uris", "card_faces",
)

# Basic lands (incl. Wastes and their Snow- variants) are singleton-exempt.
_BASIC_LAND_NAMES = {
    "plains", "island", "swamp", "mountain", "forest", "wastes",
    "snow-covered plains", "snow-covered island", "snow-covered swamp",
    "snow-covered mountain", "snow-covered forest", "snow-covered wastes",
}

# Cards whose oracle text grants an explicit "any number of cards with this
# name" singleton exemption (Relentless Rats, Persistent Petitioners, etc.)
# are detected dynamically from oracle_text below rather than hardcoded here,
# so new printings of the mechanic don't need a code change.
_SINGLETON_EXEMPT_PHRASE = "a deck can have any number of cards named"


def _user_agent_headers() -> dict:
    return {
        "User-Agent": config.SCRYFALL_USER_AGENT,
        "Accept": "application/json;q=0.9,*/*;q=0.8",
    }


def _http_get_json(url: str, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(url, headers=_user_agent_headers())
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _find_default_cards_uri(bulk_index: dict) -> str:
    for entry in bulk_index.get("data", []):
        if entry.get("type") == "default_cards":
            return entry["download_uri"]
    raise RuntimeError("Scryfall bulk-data index had no 'default_cards' entry — API contract may have changed.")


def refresh_cache(force: bool = False) -> int:
    """Download the latest Scryfall default_cards bulk file and rebuild the local cache.

    Returns the number of cards cached. Raises on network/parse failure —
    callers should catch and treat as a pipeline-blocking error (never ship
    a deck validated against a cache we know failed to refresh AND is stale;
    a stale-but-previously-good cache is fine, see refresh_if_stale()).
    """
    bulk_index = _http_get_json(config.SCRYFALL_BULK_INDEX_URL)
    download_uri = _find_default_cards_uri(bulk_index)

    # Rate-limit courtesy: Scryfall asks for a moment between requests; this
    # is our second and last request of the refresh, but pause anyway.
    time.sleep(0.1)

    raw_cards = _http_get_json(download_uri, timeout=180.0)

    trimmed: dict[str, dict] = {}
    for card in raw_cards:
        name = card.get("name", "").strip()
        if not name:
            continue
        key = name.lower()
        if key in trimmed:
            continue  # keep first printing seen; legality/color-identity don't vary by printing
        entry = {f: card.get(f) for f in _KEEP_FIELDS}
        trimmed[key] = entry

        # MDFCs (modal double-faced Sagas, transform creatures, etc.) get a
        # top-level `name` of "Front // Back" from Scryfall's bulk data — but
        # decklists (Moxfield, EDHREC, and every human deckbuilder) refer to
        # them by the front face's name alone. Without this, validate_deck()
        # spuriously flags real, legal cards as "not found on Scryfall (likely
        # hallucinated)" (confirmed real incident 2026-07-01: "The Fall of
        # Lord Konda" flagged unknown on the first repair pass; only passed
        # on the second because the repair agent happened to recall the exact
        # "// Fragment of Konda" suffix from training data — a lucky,
        # unverifiable guess in a headless run with no live Scryfall access,
        # not a real fix). setdefault() so a genuine distinct single-faced
        # card never gets silently shadowed by an MDFC's front-face alias.
        if " // " in name:
            front_key = name.split(" // ", 1)[0].strip().lower()
            trimmed.setdefault(front_key, entry)

    config.SCRYFALL_CACHE_PATH.write_text(json.dumps(trimmed))
    config.SCRYFALL_CACHE_META_PATH.write_text(json.dumps({
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "card_count": len(trimmed),
        "source_uri": download_uri,
        # Schema-version stamp (added 2026-07-03, after a real run shipped blank
        # CMC/Rarity/mana-curve/colour-pip data): a cache refreshed 2026-07-01
        # (before cmc/mana_cost/rarity/image_uris/card_faces were added to
        # _KEEP_FIELDS on 2026-07-03) was well within SCRYFALL_CACHE_MAX_AGE_DAYS,
        # so the age-only staleness check in refresh_if_stale() had no way to know
        # the trimmed entries were missing fields the rest of the pipeline now
        # expects — every card looked present and valid, just silently blank on
        # the new columns. Recording the exact field set here lets
        # refresh_if_stale() force a refresh whenever _KEEP_FIELDS changes,
        # regardless of age, so adding a field to _KEEP_FIELDS in the future
        # can't silently ship stale-shaped data again.
        "keep_fields": sorted(_KEEP_FIELDS),
    }, indent=2))
    return len(trimmed)


def _cache_meta() -> dict | None:
    if not config.SCRYFALL_CACHE_META_PATH.exists():
        return None
    try:
        return json.loads(config.SCRYFALL_CACHE_META_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _cache_age_days() -> float | None:
    meta = _cache_meta()
    if meta is None:
        return None
    try:
        refreshed_at = datetime.fromisoformat(meta["refreshed_at"])
        return (datetime.now(timezone.utc) - refreshed_at).total_seconds() / 86400
    except (KeyError, ValueError):
        return None


def _cache_schema_matches() -> bool:
    """False if the on-disk cache predates the current _KEEP_FIELDS (see the
    schema-version comment in refresh_cache()) — treated as stale regardless of
    age. Older cache files with no "keep_fields" entry at all are also treated as
    a mismatch (conservative default: refresh rather than risk silently serving
    fields that don't exist)."""
    meta = _cache_meta()
    if meta is None:
        return False
    return meta.get("keep_fields") == sorted(_KEEP_FIELDS)


def refresh_if_stale(max_age_days: int = config.SCRYFALL_CACHE_MAX_AGE_DAYS) -> bool:
    """Refresh the cache if missing, older than max_age_days, OR built from an
    older version of _KEEP_FIELDS (schema mismatch — see refresh_cache()'s
    "keep_fields" comment). Returns True if a refresh ran."""
    age = _cache_age_days()
    fresh_enough = age is not None and age <= max_age_days and config.SCRYFALL_CACHE_PATH.exists()
    if fresh_enough and _cache_schema_matches():
        return False
    refresh_cache()
    return True


def load_cache(path: Path = config.SCRYFALL_CACHE_PATH) -> dict[str, dict]:
    if not path.exists():
        raise FileNotFoundError(
            f"No Scryfall cache at {path} — run refresh_cache() at least once "
            "(requires real internet; blocked in this sandbox)."
        )
    return json.loads(path.read_text())


# ── Validation ────────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    commander: str
    card_count: int
    unknown_cards: list[str] = field(default_factory=list)       # not found at all — likely hallucinated
    banned_cards: list[str] = field(default_factory=list)
    color_identity_violations: list[str] = field(default_factory=list)
    singleton_violations: list[str] = field(default_factory=list)  # duplicate, non-exempt names
    wrong_card_count: bool = False
    land_count: int = 0            # lands actually found in the decklist (cache type_line)
    min_lands: int = 0             # floor requested by the caller; 0 = land check not requested
    too_few_lands: bool = False
    # Land count the repair MESSAGE aims at. The gate fires at min_lands (a
    # tripwire a little under quota), but the message must name the real quota
    # floor: run 9e430ab7 shipped 33 lands because the prompt said "at least 33"
    # (the tripwire) and the model treated that floor as the target. 0 = message
    # falls back to min_lands.
    land_target: int = 0

    @property
    def is_valid(self) -> bool:
        return not (
            self.unknown_cards
            or self.banned_cards
            or self.color_identity_violations
            or self.singleton_violations
            or self.wrong_card_count
            or self.too_few_lands
        )

    def as_repair_notes(self) -> str:
        """Plain-text summary to feed straight back into the repair prompt."""
        if self.is_valid:
            return "Deck is valid — no repairs needed."
        lines = []
        if self.wrong_card_count:
            lines.append(f"- Deck has {self.card_count} cards, expected {config.DECK_SIZE} (commander + {config.DECK_SIZE - 1}).")
        if self.unknown_cards:
            lines.append(f"- Not found on Scryfall (likely hallucinated — replace with a real card): {', '.join(self.unknown_cards)}")
        if self.banned_cards:
            lines.append(f"- Banned in Commander: {', '.join(self.banned_cards)}")
        if self.color_identity_violations:
            lines.append(f"- Outside {self.commander}'s color identity: {', '.join(self.color_identity_violations)}")
        if self.singleton_violations:
            lines.append(f"- Singleton violation (duplicate, not exempt): {', '.join(self.singleton_violations)}")
        if self.too_few_lands:
            target = max(self.land_target, self.min_lands)
            lines.append(
                f"- Only {self.land_count} lands — the mana base has been damaged. Add basic lands "
                f"in the commander's colors (cutting the weakest nonland cards) until the deck has "
                f"at least {target} lands."
            )
        return "\n".join(lines)


def _is_singleton_exempt(card: dict) -> bool:
    name_lower = (card.get("name") or "").lower()
    if name_lower in _BASIC_LAND_NAMES:
        return True
    oracle_text = (card.get("oracle_text") or "").lower()
    return _SINGLETON_EXEMPT_PHRASE in oracle_text


def validate_deck(commander: str, decklist: list[str], cache: dict[str, dict] | None = None,
                  min_lands: int = 0, land_target: int = 0) -> ValidationResult:
    """Validate a 99-card decklist against `commander`.

    Args:
        commander: commander card name (validated for existence/color-identity source, but not
                   itself checked for legality — assumed chosen legitimately by the selector).
        decklist:  the other cards in the deck (should be config.DECK_SIZE - 1 entries).
        cache:     pre-loaded cache dict; loads from disk if omitted.
        min_lands: if > 0, flag the deck when it has fewer lands than this. Guards against
                   pipeline stages mangling the mana base (run 81f2b542 shipped 23 lands after
                   repair regurgitations ate a third of it), so callers should pass a floor a
                   little under the draft quota, not the quota itself — this is a tripwire for
                   catastrophic loss, not quota enforcement.
        land_target: land count the repair message tells the model to reach (the real quota
                   floor), so the tripwire never doubles as the target. 0 = use min_lands.
    """
    cache = cache if cache is not None else load_cache()
    result = ValidationResult(commander=commander, card_count=len(decklist) + 1,
                              min_lands=min_lands, land_target=land_target)

    commander_card = cache.get(commander.strip().lower())
    commander_identity = set(commander_card.get("color_identity", [])) if commander_card else set()
    if commander_card is None:
        result.unknown_cards.append(commander)

    if len(decklist) != config.DECK_SIZE - 1:
        result.wrong_card_count = True

    seen_counts: dict[str, int] = {}
    for raw_name in decklist:
        name = raw_name.strip()
        key = name.lower()
        seen_counts[key] = seen_counts.get(key, 0) + 1
        card = cache.get(key)

        if card is None:
            result.unknown_cards.append(name)
            continue

        if "land" in (card.get("type_line") or "").lower():
            result.land_count += 1

        legality = (card.get("legalities") or {}).get("commander", "not_legal")
        if legality == "banned":
            result.banned_cards.append(name)

        card_identity = set(card.get("color_identity", []))
        if commander_card is not None and not card_identity.issubset(commander_identity):
            result.color_identity_violations.append(name)

    for key, count in seen_counts.items():
        if count <= 1:
            continue
        card = cache.get(key)
        if card is not None and _is_singleton_exempt(card):
            continue
        result.singleton_violations.append(key)

    if min_lands > 0 and result.land_count < min_lands:
        result.too_few_lands = True

    return result


if __name__ == "__main__":
    import sys
    if "--refresh" in sys.argv:
        n = refresh_cache()
        print(f"Refreshed Scryfall cache: {n} cards.")
    else:
        age = _cache_age_days()
        print(f"Cache age (days): {age if age is not None else 'no cache found'}")
        print(f"Schema matches current _KEEP_FIELDS: {_cache_schema_matches()}")
        print("Run with --refresh to force a rebuild.")
