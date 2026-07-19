"""ck_price.py — Card Kingdom (CK) reference-price lookup for 3vor Fetch.

Per PRD_card_kingdom_price.md: replicates the reference-price banner from
upstream xenodus/gishathfetch (their CardKingdomPrice.jsx / cardKingdomPrice
API field), but sourced from MTGJSON only — no AWS, no DynamoDB, no live
Card Kingdom API call. Card Kingdom's own pricelist endpoint
(api.cardkingdom.com/api/v2/pricelist) is confirmed unreachable from this
project: it sits behind Cloudflare Enterprise Bot Management, which blocked
both a plain HTTP client and a full stealth headless-Chromium attempt
(see PRD §11). MTGJSON's bulk files are a plain, unprotected static host —
confirmed reachable and correctly shaped (PRD §12).

Three-phase design, mirroring upstream's Go gateway/cardkingdom package:

  1. refresh_cache() — the SLOW path. Downloads MTGJSON's AllPricesToday.json.bz2
     (small, ~50 MB decompressed — loaded fully in memory) and
     AllPrintings.json.bz2 (large — streamed via ijson, NEVER materialized whole
     in memory; that file is what resolves UUID -> card name/set/CK purchase
     URL). Builds a "cheapest CK listing per normalized card name" index and
     writes it to state/ck_prices.json.

  2. start_background_refresher() — the SCHEDULING path, called once from
     app.py at startup. NOT a fixed-time cron/launchd job on purpose: a laptop
     that's routinely off overnight would just silently miss a clock-based
     schedule, and since prices go stale after MAX_AGE_SECONDS the whole
     feature would then blink out with no warning. Instead, a daemon thread
     wakes every BACKGROUND_CHECK_INTERVAL_SECONDS and calls refresh_cache()
     whenever the cache is missing or older than STALE_REFRESH_THRESHOLD_SECONDS
     (comfortably inside MAX_AGE_SECONDS, so a slow/failed refresh still has
     margin before /search would start omitting prices). Self-healing:
     refreshes whenever the app happens to be running, regardless of time of
     day — including bootstrapping the cache from nothing on first run.
     refresh_ck_prices.py / `make ck-refresh` still exist for a manual,
     immediate, on-demand refresh (e.g. before a specific search session) —
     that path is no longer the primary mechanism, just a convenience.

  3. get_prices_for_buy_list() — the FAST path, called from app.py's /search.
     Pure local file read (mtime-checked, cached in-process), zero network
     calls, sub-millisecond per card. Returns None for any card whose cache
     entry is missing or older than MAX_AGE_SECONDS (mirrors upstream's
     CKPriceMaxAge = 48h) — stale data is omitted, never shown as if fresh.

Per Trevor's decisions on the PRD open questions:
  - Single cheapest price only (foil or non-foil, whichever's lower) — this
    falls out naturally from the cheapest-by-name-key cache construction
    (_consider_cheapest), same as upstream; no separate logic needed.
  - Raw USD, no SGD conversion.
  - Refresh cadence: originally "nightly cron", revised to staleness-triggered
    background refresh (see phase 2 above) once Trevor pointed out the laptop
    isn't reliably on overnight — a fixed-time job would just miss most nights.
  - Flat JSON cache (state/ck_prices.json), consistent with card_index.py's
    INDEX_PATH pattern.

Known trade-off vs upstream (accepted per PRD §11): no live CK stock quantity,
since that only came from the CK pricelist supplement, which is unreachable.
`quantity` is always 0. This is a reference/benchmark price, not an
inventory check, so the loss is acceptable.
"""
from __future__ import annotations

import bz2
import calendar
import json
import logging
import os
import shutil
import tempfile
import threading
import time

log = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(_BASE_DIR, "state")
CACHE_PATH = os.path.join(STATE_DIR, "ck_prices.json")

# The iPhone app bundles CK prices at build time, and its nightly re-sign job
# (launchd) cannot read this Desktop project folder (TCC). Publish a copy
# where it can — only if the directory already exists (i.e. the iOS toolchain
# has been set up on this Mac); otherwise this is a silent no-op.
IOS_SIDE_COPY_PATH = os.path.expanduser(
    "~/Library/Application Support/ThreevorFetch/ck_prices.json")


def _publish_ios_side_copy() -> None:
    try:
        if os.path.isdir(os.path.dirname(IOS_SIDE_COPY_PATH)):
            shutil.copyfile(CACHE_PATH, IOS_SIDE_COPY_PATH)
            log.info("ck price refresh: published iOS side copy to %s", IOS_SIDE_COPY_PATH)
    except Exception:
        # A failed side copy must never fail the real refresh.
        log.exception("ck_price: failed to publish iOS side copy")

ALL_PRICES_TODAY_URL = "https://mtgjson.com/api/v5/AllPricesToday.json.bz2"
ALL_PRINTINGS_URL = "https://mtgjson.com/api/v5/AllPrintings.json.bz2"

ALL_PRICES_TODAY_TIMEOUT_S = 120
ALL_PRINTINGS_HTTP_TIMEOUT_S = 600  # AllPrintings is large; this is a nightly job, not request-time

# How old a cache entry may be before /search omits it rather than show stale
# data. Mirrors upstream's config.CKPriceMaxAge (48h).
MAX_AGE_SECONDS = 48 * 3600

# How old the cache may get before the background refresher proactively
# refreshes it. Deliberately well inside MAX_AGE_SECONDS (28h of margin) so a
# slow refresh, a failed attempt, or the laptop being closed mid-refresh still
# leaves room to retry before /search would actually start omitting prices.
STALE_REFRESH_THRESHOLD_SECONDS = 20 * 3600

# How often the background thread wakes up to check staleness. Cheap check
# (one os.path.getmtime call) — no reason to check more often than this.
BACKGROUND_CHECK_INTERVAL_SECONDS = 60 * 60

_DFC_SEPARATOR = " // "

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


# ── Name normalization (port of gateway/cardkingdom/names.go) ─────────────────

def normalize_name_key(name: str) -> str:
    """Lowercases and trims a card name for cache lookup."""
    return name.strip().lower()


def _unique(keys: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def name_lookup_keys(card_name: str) -> list[str]:
    """Normalized lookup keys for a card name, including each face of a
    double-faced card split on ' // '. Mirrors names.go's NameLookupKeys."""
    trimmed = card_name.strip()
    if not trimmed:
        return []
    keys = [normalize_name_key(trimmed)]
    if _DFC_SEPARATOR in trimmed:
        before, after = trimmed.split(_DFC_SEPARATOR, 1)
        front = normalize_name_key(before)
        back = normalize_name_key(after)
        if front:
            keys.append(front)
        if back:
            keys.append(back)
    return _unique(keys)


def price_lookup_keys(card_name: str) -> list[str]:
    """Lookup keys checked when resolving a search-time CK price. For
    double-faced cards, the combined name, front face, and back face are all
    checked together; the cheapest fresh listing across all three wins.
    Mirrors names.go's PriceLookupKeys."""
    trimmed = card_name.strip()
    if not trimmed:
        return []
    combined = normalize_name_key(trimmed)
    if _DFC_SEPARATOR not in trimmed:
        return [combined]
    before, after = trimmed.split(_DFC_SEPARATOR, 1)
    front = normalize_name_key(before)
    back = normalize_name_key(after)
    if not front or not back:
        return [combined]
    return _unique([combined, front, back])


def _listing_name_keys(card_name: str, is_foil: bool) -> list[str]:
    """Keys that should receive a listing when indexing it. Foil DFC listings
    are stored only under the combined name so a foil variant doesn't
    overwrite a cheaper face-only name. Mirrors names.go's ListingNameKeys."""
    keys = name_lookup_keys(card_name)
    if not is_foil or len(keys) <= 1:
        return keys
    return keys[:1]


# ── Refresh path (slow — MTGJSON download + parse) ─────────────────────────────

def _latest_retail_price(by_date: dict | None) -> float:
    """Mirrors mtgjson_fetch.go's latestRetailPrice: despite the name, this
    takes the MAX value across all dates in the map, not the calendar-latest
    one. AllPricesToday is a single-day snapshot so this is usually a no-op
    (one date key), but replicated faithfully for parity with upstream."""
    if not by_date:
        return 0.0
    best = 0.0
    for price in by_date.values():
        if isinstance(price, (int, float)) and price > best:
            best = float(price)
    return best


def _fetch_ck_prices_by_uuid(client) -> tuple[dict[str, dict], str]:
    """Downloads AllPricesToday.json.bz2 and returns
    ({uuid: {"normal": float, "foil": float}}, meta_date). Small file (~50MB
    decompressed) — loaded fully in memory, matching the confirmed-safe
    feasibility check."""
    resp = client.get(
        ALL_PRICES_TODAY_URL,
        headers={"Accept": "application/octet-stream"},
        timeout=ALL_PRICES_TODAY_TIMEOUT_S,
    )
    resp.raise_for_status()
    raw = bz2.decompress(resp.content)
    payload = json.loads(raw)

    meta_date = (payload.get("meta") or {}).get("date", "")
    prices_by_uuid: dict[str, dict] = {}
    for uuid, entry in (payload.get("data") or {}).items():
        paper = (entry or {}).get("paper") or {}
        ck = paper.get("cardkingdom") or {}
        retail = ck.get("retail") or {}
        normal = _latest_retail_price(retail.get("normal"))
        foil = _latest_retail_price(retail.get("foil"))
        if normal > 0 or foil > 0:
            prices_by_uuid[uuid] = {"normal": normal, "foil": foil}

    return prices_by_uuid, meta_date


def _consider_cheapest(cheapest: dict[str, dict], card_name: str, edition: str,
                        price_usd: float, url: str, is_foil: bool) -> None:
    """Inserts a candidate listing into the cheapest-by-name-key map, keeping
    whichever is cheaper for each key. This is the single piece of logic that
    makes 'show one cheapest price regardless of foil status' true — a normal
    and foil listing for the same card compete on the same name key, and only
    the lower price survives. Mirrors index.go / mtgjson_fetch.go's
    considerCheapestListing."""
    if price_usd <= 0 or not url:
        return
    listing = {
        "cardName": card_name,
        "edition": edition,
        "priceUsd": round(price_usd, 2),
        "url": url,
        "quantity": 0,  # no live stock data available — see module docstring
        "isFoil": is_foil,
    }
    for key in _listing_name_keys(card_name, is_foil):
        existing = cheapest.get(key)
        if existing is None or listing["priceUsd"] < existing["priceUsd"]:
            cheapest[key] = listing


def _printing_key(card: dict) -> tuple:
    """Groups card faces of the same physical printing together, so a modal
    double-faced card's two faces aggregate into one printing rather than two.
    Mirrors mtgjson_fetch.go's printingKeyFor."""
    number = (card.get("number") or "").strip()
    side = (card.get("side") or "").strip()
    if side in ("a", "b"):
        return (number, True, "")
    return (number, False, (card.get("name") or "").strip())


def _prefer_name(current: str, candidate: str) -> str:
    """Prefers the combined 'Front // Back' name over a single-face name when
    both are seen for the same printing. Mirrors preferCardName."""
    candidate = candidate.strip()
    current = current.strip()
    if not candidate:
        return current
    if not current:
        return candidate
    if _DFC_SEPARATOR in candidate:
        return candidate
    if _DFC_SEPARATOR in current:
        return current
    return candidate


def _aggregate_set_cards(cards: list[dict], prices_by_uuid: dict[str, dict]) -> dict[tuple, dict]:
    """One pass over a set's card list, grouping by printing and picking the
    cheapest normal/foil UUID price plus a CK purchase URL per printing.
    Mirrors mergePrintingAggregate."""
    aggregates: dict[tuple, dict] = {}
    for card in cards:
        name = (card.get("name") or "").strip()
        if not name:
            continue

        key = _printing_key(card)
        agg = aggregates.setdefault(key, {
            "card_name": "", "card_kingdom": "", "card_kingdom_foil": "",
            "price_normal": 0.0, "price_foil": 0.0,
        })
        agg["card_name"] = _prefer_name(agg["card_name"], name)

        purchase_urls = card.get("purchaseUrls") or {}
        ck_url = purchase_urls.get("cardKingdom") or ""
        ck_foil_url = purchase_urls.get("cardKingdomFoil") or ""
        if ck_url:
            agg["card_kingdom"] = ck_url
        if ck_foil_url:
            agg["card_kingdom_foil"] = ck_foil_url

        price_entry = prices_by_uuid.get(card.get("uuid"))
        if price_entry:
            normal = price_entry.get("normal", 0.0)
            foil = price_entry.get("foil", 0.0)
            if normal > 0 and (agg["price_normal"] <= 0 or normal < agg["price_normal"]):
                agg["price_normal"] = normal
            if foil > 0 and (agg["price_foil"] <= 0 or foil < agg["price_foil"]):
                agg["price_foil"] = foil

    return aggregates


def _apply_set_aggregates(cheapest: dict[str, dict], set_name: str, aggregates: dict[tuple, dict]) -> None:
    for agg in aggregates.values():
        if agg["price_normal"] > 0 and agg["card_kingdom"]:
            _consider_cheapest(cheapest, agg["card_name"], set_name, agg["price_normal"], agg["card_kingdom"], False)
        if agg["price_foil"] > 0:
            foil_url = agg["card_kingdom_foil"] or agg["card_kingdom"]
            if foil_url:
                _consider_cheapest(cheapest, agg["card_name"], set_name, agg["price_foil"], foil_url, True)


def _download_to_temp_file(client, url: str) -> str:
    """Streams a large download to a temp file on disk (never held fully in
    memory). Disk is not the scarce resource here (256GB SSD); RAM is (8GB
    M2 Air) — this is why AllPrintings goes to disk first, then gets streamed
    back out via bz2.BZ2File + ijson, rather than ever being fully decompressed
    or fully JSON-parsed in memory at once."""
    fd, path = tempfile.mkstemp(suffix=".json.bz2", prefix="mtgjson_")
    os.close(fd)
    try:
        with client.stream(
            "GET", url,
            headers={"Accept": "application/octet-stream"},
            timeout=ALL_PRINTINGS_HTTP_TIMEOUT_S,
        ) as resp:
            resp.raise_for_status()
            with open(path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return path


def _stream_all_printings_listings(bz2_path: str, prices_by_uuid: dict[str, dict]) -> dict[str, dict]:
    """Streams AllPrintings.json.bz2 one set at a time via ijson — memory
    footprint is bounded by the largest single set's card list (a few hundred
    cards), never the whole ~700-set catalog. Mirrors mtgjson_fetch.go's
    decodeAllPrintingsSets / decodeSetCatalog.

    The Collection section's print-identity index (print_index.py) rides this
    same pass as a free rider — same download, same stream, one extra dict per
    set. Its hooks swallow their own errors so it can never break CK prices.
    """
    import ijson

    import print_index

    cheapest: dict[str, dict] = {}
    print_index.collect_begin(prices_by_uuid)
    with bz2.BZ2File(bz2_path, "rb") as f:
        for set_code, set_obj in ijson.kvitems(f, "data"):
            set_name = set_obj.get("name") or set_code
            cards = set_obj.get("cards") or []
            aggregates = _aggregate_set_cards(cards, prices_by_uuid)
            _apply_set_aggregates(cheapest, set_name, aggregates)
            print_index.collect_set(set_code, set_obj)
    print_index.collect_finish()
    return cheapest


def _now_iso() -> str:
    return time.strftime(_ISO_FORMAT, time.gmtime())


def _write_cache(cheapest: dict[str, dict], price_date: str) -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    synced_at = _now_iso()
    payload = {
        "syncedAt": synced_at,
        "priceDate": price_date,
        "entries": cheapest,
    }
    # PID-suffixed temp filename: if the manual refresh_ck_prices.py script and
    # the background refresher ever fire at the same time (unlikely — the
    # background loop only triggers when the cache is >20h stale, and a manual
    # run would itself refresh it — but not impossible), each writer gets its
    # own temp file rather than two processes racing on the same path. Only
    # the write that finishes last actually lands, via os.replace — the other
    # simply becomes a no-op rename target, not a corrupted cache.
    tmp_path = f"{CACHE_PATH}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp_path, CACHE_PATH)  # atomic — a crash mid-write never corrupts the live cache
    _publish_ios_side_copy()
    return synced_at


def refresh_cache(client=None) -> dict:
    """The slow path: fetch fresh CK prices from MTGJSON and overwrite the
    local cache. Call this from refresh_ck_prices.py (nightly), not from
    request handlers. Returns a summary dict: {entries, priceDate, syncedAt}."""
    import httpx

    own_client = client is None
    if own_client:
        client = httpx.Client()

    temp_path = None
    try:
        log.info("ck price refresh: fetching AllPricesToday")
        prices_by_uuid, meta_date = _fetch_ck_prices_by_uuid(client)
        log.info("ck price refresh: %d uuids carry a CK price (price date %s)", len(prices_by_uuid), meta_date)

        log.info("ck price refresh: downloading AllPrintings (streaming to disk)")
        temp_path = _download_to_temp_file(client, ALL_PRINTINGS_URL)

        log.info("ck price refresh: streaming AllPrintings and building cheapest-by-name index")
        cheapest = _stream_all_printings_listings(temp_path, prices_by_uuid)
        log.info("ck price refresh: %d name-key entries", len(cheapest))

        synced_at = _write_cache(cheapest, meta_date)
        log.info("ck price refresh: wrote %s (synced_at=%s)", CACHE_PATH, synced_at)

        return {"entries": len(cheapest), "priceDate": meta_date, "syncedAt": synced_at}
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        if own_client:
            client.close()


# ── Lookup path (fast — local file read, no network) ───────────────────────────

_cache: dict | None = None
_cache_mtime: float | None = None
_cache_lock = threading.Lock()


def _load_cache() -> dict | None:
    """Loads state/ck_prices.json, re-reading only when the file's mtime has
    changed since the last load (so a background or manual refresh is picked
    up without restarting the Flask app, but a hot /search loop doesn't
    re-parse the file on every request)."""
    global _cache, _cache_mtime
    with _cache_lock:
        try:
            mtime = os.path.getmtime(CACHE_PATH)
        except OSError:
            return None
        if _cache is not None and _cache_mtime == mtime:
            return _cache
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception:
            log.exception("ck_price: failed to load cache at %s", CACHE_PATH)
            return None
        _cache = loaded
        _cache_mtime = mtime
        return _cache


def _cache_age_seconds(cache: dict) -> float | None:
    synced_at = cache.get("syncedAt")
    if not synced_at:
        return None
    try:
        synced_struct = time.strptime(synced_at, _ISO_FORMAT)
    except ValueError:
        return None
    return time.time() - calendar.timegm(synced_struct)


def is_stale(threshold_seconds: float = STALE_REFRESH_THRESHOLD_SECONDS) -> bool:
    """True if the cache doesn't exist yet, or is older than threshold_seconds."""
    cache = _load_cache()
    if not cache:
        return True
    age = _cache_age_seconds(cache)
    return age is None or age > threshold_seconds


# ── Background self-healing refresher ───────────────────────────────────────
# Started once from app.py at startup. Replaces a fixed-time cron/launchd job
# (see module docstring, phase 2, for why) — checks staleness periodically and
# refreshes whenever the app happens to be running and the cache is due,
# rather than at a specific clock time the laptop might be off for.

_refresh_lock = threading.Lock()
_refreshing = False
_background_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _refresh_once_guarded() -> None:
    """Runs refresh_cache(), but only if no other refresh is already in
    flight (guards against the periodic check firing again mid-refresh, or
    overlapping a concurrent trigger)."""
    global _refreshing
    with _refresh_lock:
        if _refreshing:
            return
        _refreshing = True
    try:
        log.info("ck_price: cache is stale — starting background refresh")
        started = time.monotonic()
        summary = refresh_cache()
        log.info(
            "ck_price: background refresh done in %.0fs — %d cards indexed, price date %s",
            time.monotonic() - started, summary["entries"], summary["priceDate"],
        )
    except Exception:
        # A failed refresh (network blip, MTGJSON hiccup, etc.) leaves the
        # existing cache untouched — refresh_cache()/_write_cache() only
        # replace it on full success. The next periodic check retries.
        log.exception("ck_price: background refresh failed — will retry at the next staleness check")
    finally:
        with _refresh_lock:
            _refreshing = False


def _background_loop() -> None:
    log.info(
        "ck_price: background refresher started (checks every %.0fh, refreshes past %.0fh old)",
        BACKGROUND_CHECK_INTERVAL_SECONDS / 3600, STALE_REFRESH_THRESHOLD_SECONDS / 3600,
    )
    while not _stop_event.is_set():
        try:
            if is_stale():
                _refresh_once_guarded()
        except Exception:
            log.exception("ck_price: background staleness check failed")
        # Sleep in one wait() call that returns early if stop_background_refresher()
        # is called mid-sleep, rather than polling in a tight loop.
        _stop_event.wait(BACKGROUND_CHECK_INTERVAL_SECONDS)


def start_background_refresher() -> None:
    """Call once at app startup (see app.py). Spawns a daemon thread — dies
    automatically with the process, no explicit stop required, but
    stop_background_refresher() is provided for symmetry with start_browser()/
    stop_browser() and to allow a clean stop during tests."""
    global _background_thread
    if _background_thread is not None and _background_thread.is_alive():
        return
    _stop_event.clear()
    _background_thread = threading.Thread(target=_background_loop, name="ck-price-refresher", daemon=True)
    _background_thread.start()


def stop_background_refresher() -> None:
    _stop_event.set()


def get_prices_for_buy_list(card_names: list[str]) -> dict[str, dict | None]:
    """The fast path called from /search. Returns {card_name: listing | None}.
    A listing dict has cardName/edition/priceUsd/url/quantity/isFoil/asOf.
    None means: no cache yet, cache is stale (> MAX_AGE_SECONDS old), or no CK
    listing found for that name — all three are treated the same way by
    /search: omit the banner rather than risk showing wrong data."""
    cache = _load_cache()
    if not cache:
        return {name: None for name in card_names}

    age = _cache_age_seconds(cache)
    if age is None or age > MAX_AGE_SECONDS:
        log.warning("ck_price: cache is stale (age=%s) — omitting CK prices this search", age)
        return {name: None for name in card_names}

    entries = cache.get("entries", {})
    price_date = cache.get("priceDate", "")

    result: dict[str, dict | None] = {}
    for name in card_names:
        best = None
        for key in price_lookup_keys(name):
            entry = entries.get(key)
            if entry is None:
                continue
            if best is None or entry["priceUsd"] < best["priceUsd"]:
                best = entry
        if best is not None:
            best = dict(best)
            best["asOf"] = price_date
        result[name] = best
    return result
