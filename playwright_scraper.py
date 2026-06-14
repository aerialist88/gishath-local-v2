"""
playwright_scraper.py — Playwright-based scraper for BinderPOS (Shopify) stores.

Why this exists:
    The Go engine's HTTP client is blocked by Cloudflare TLS fingerprinting on
    all BinderPOS-backed stores.  A real Chromium browser presents the correct
    TLS fingerprint, so we use Playwright to bypass this.

Architecture:
    • One persistent headless Chromium browser shared across all searches.
    • A background thread runs a dedicated asyncio event loop.
    • Flask calls run_async() which submits coroutines to that loop via
      asyncio.run_coroutine_threadsafe — safe to call from a sync context.
    • A semaphore limits concurrent page loads to avoid overwhelming the browser.

Public API:
    start_browser()                      → call once at app startup
    stop_browser()                       → call at app shutdown
    run_async(coro)                      → run a coroutine from sync code, blocking
    search_many_playwright(card_names)   → {card_name: {"cards": [...], "errors": [...]}}

Card dict shape (matches engine_client):
    {name, url, img, price, inStock, isFoil, src, quality, extraInfo}

Scraping variants (three patterns used by BinderPOS stores):
    1 — Cards Citadel: custom HTML (div.Norm rows)
    2 — Shopify data-product-variants JSON attribute
    3 — productCard__card divs with chip data attributes
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

log = logging.getLogger(__name__)

# ── Shared matching/filtering/normalisation helpers ───────────────────────────
# Moved to filters.py so the engine-result path (presentation.py) and this
# Playwright path can't drift apart on what counts as a match, an accessory,
# a non-MTG result, or a quality/foil label.
from filters import (  # noqa: E402
    FOIL_KEYWORDS,
    QUALITY_MAP,
    _ACCESSORY_KEYWORDS,
    _MTG_NON_SINGLE_KEYWORDS,
    _NON_MTG_NAME_KEYWORDS,
    _NON_MTG_SET_KEYWORDS,
    _is_foil,
    _is_non_mtg,
    _name_matches,
    _normalise_quality,
)


# ── Store configuration ───────────────────────────────────────────────────────

@dataclass
class StoreConfig:
    name: str
    base_url: str
    search_path: str          # f-string template; {q} = URL-encoded card name
    variant: int              # 1, 2, or 3
    extra_headers: dict = field(default_factory=dict)

    def search_url(self, card_name: str) -> str:
        q = urllib.parse.quote(card_name)
        path = self.search_path.format(q=q)
        return self.base_url.rstrip("/") + path


BINDERPOS_STORES: list[StoreConfig] = [
    # ── Variant 1 (Cards Citadel custom HTML) ─────────────────────────────
    StoreConfig(
        name="Cards Citadel",
        base_url="https://cardscitadel.com",
        search_path="/search?q={q}",
        variant=1,
    ),
    # ── Variant 2 (data-product-variants JSON attr) ────────────────────────
    StoreConfig(
        name="Card Affinity",
        base_url="https://card-affinity.com",   # domain changed from www.cardaffinity.com
        search_path="/search?q={q}",
        variant=2,
    ),
    StoreConfig(
        name="Cardboard Crack Games",
        base_url="https://www.cardboardcrackgames.com",
        search_path="/search?q={q}",
        variant=2,
    ),
    StoreConfig(
        name="Flagship Games",
        base_url="https://flagshipgames.sg",
        search_path="/search?q={q}",
        variant=2,
    ),
    StoreConfig(
        name="Mana Pro",
        base_url="https://sg-manapro.com",      # manapro.sg is their info site; singles are on sg-manapro.com
        search_path="/search?type=product&q={q}",
        variant=2,
    ),
    StoreConfig(
        name="MTG Asia",
        base_url="https://www.mtg-asia.com",
        search_path="/search?q={q}",
        variant=2,
    ),
    StoreConfig(
        name="One MTG",
        base_url="https://www.onemtg.com.sg",
        search_path="/search?q={q}",
        variant=2,
    ),
    # ── Variant 3 (productCard__card with chip data attrs) ─────────────────
    StoreConfig(
        name="Games Haven",
        base_url="https://www.gameshaventcg.com",
        search_path="/search?q={q}",
        variant=3,
    ),
    StoreConfig(
        name="Grey Ogre Games",
        base_url="https://www.greyogregames.com",
        search_path="/search?q={q}",
        variant=3,
    ),
    StoreConfig(
        name="Hideout",
        base_url="https://hideoutcg.com",
        search_path="/search?q={q}",
        variant=3,
    ),
]

# Index by name for quick lookup.
STORE_BY_NAME: dict[str, StoreConfig] = {s.name: s for s in BINDERPOS_STORES}

# ── Browser lifecycle ─────────────────────────────────────────────────────────

_browser = None          # playwright Browser object
_playwright_ctx = None   # playwright Playwright context manager result
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None
_MAX_CONCURRENT_PAGES = 10
_semaphore: Optional[asyncio.Semaphore] = None

PAGE_TIMEOUT = 20_000   # ms — per-page navigation timeout
WAIT_SELECTOR_TIMEOUT = 5_000  # ms — wait for results selector (shorter = faster failure on no-results pages)


def start_browser() -> None:
    """Start background event loop + persistent Chromium browser.  Call once at app startup."""
    global _loop, _loop_thread

    _loop = asyncio.new_event_loop()
    _loop_thread = threading.Thread(target=_loop.run_forever, daemon=True, name="playwright-loop")
    _loop_thread.start()

    future = asyncio.run_coroutine_threadsafe(_init_browser(), _loop)
    future.result(timeout=60)   # wait up to 60 s for browser init
    log.info("Playwright browser ready.")


def stop_browser() -> None:
    """Shut down the browser and event loop.  Call at app shutdown."""
    global _browser, _playwright_ctx, _loop

    if _loop is None:
        return

    future = asyncio.run_coroutine_threadsafe(_close_browser(), _loop)
    try:
        future.result(timeout=15)
    except Exception as exc:
        log.warning("Error closing Playwright browser: %s", exc)

    _loop.call_soon_threadsafe(_loop.stop)
    log.info("Playwright browser stopped.")


def run_async(coro):
    """Submit a coroutine to the Playwright event loop and block until done.

    Safe to call from any sync context (Flask route handlers, atexit, etc.).
    """
    if _loop is None:
        raise RuntimeError("Playwright browser not started — call start_browser() first.")
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result()


async def _init_browser() -> None:
    global _browser, _playwright_ctx, _semaphore
    from playwright.async_api import async_playwright

    _playwright_ctx = async_playwright()
    pw = await _playwright_ctx.__aenter__()
    _browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    _semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PAGES)
    log.info("Chromium launched (headless).")


async def _close_browser() -> None:
    global _browser, _playwright_ctx
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright_ctx:
        try:
            await _playwright_ctx.__aexit__(None, None, None)
        except Exception:
            pass
        _playwright_ctx = None


# ── Stealth page factory ──────────────────────────────────────────────────────
# Cloudflare Bot Management fingerprints headless Chromium by checking:
#   • navigator.webdriver  (set to true by Playwright by default)
#   • navigator.plugins    (empty array in headless mode)
#   • navigator.languages  (often wrong/missing in headless)
#   • window.chrome        (absent in headless)
# Injecting the init script below before any page load masks these signals.
# This is sufficient for CF Bot Management tiers used by SG LGS; it does not
# defeat CF Turnstile or Enterprise Bot Management (those require a proxy).

_STEALTH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_STEALTH_SCRIPT = """
    // Mask webdriver flag — primary CF automation signal
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    // Fake a populated plugins array (headless = 0 plugins, dead giveaway)
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    // Set realistic language list
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    // Add chrome runtime stub (absent in headless, present in real Chrome)
    window.chrome = {runtime: {}};
"""


async def _new_stealth_page():
    """Open a new browser page pre-configured with CF stealth properties."""
    if _browser is None:
        raise RuntimeError("Browser not initialised — call start_browser() first.")
    page = await _browser.new_page(user_agent=_STEALTH_UA)
    await page.add_init_script(_STEALTH_SCRIPT)
    return page


# ── JS extractors (run inside the browser page) ───────────────────────────────

# Variant 1: Cards Citadel — custom HTML product rows
_JS_VARIANT1 = r"""
() => {
    const cards = [];
    document.querySelectorAll('div.Norm').forEach(row => {
        // Title and URL
        const titleEl = row.querySelector('p.productTitle a, p.productTitle');
        const linkEl   = row.querySelector('a[href]');
        if (!titleEl) return;
        const rawTitle = titleEl.innerText.trim();
        const href     = linkEl ? linkEl.getAttribute('href') : '';

        // Image
        const imgEl = row.querySelector('img');
        const img   = imgEl ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src') || '') : '';

        // Price and quality from the "addNow" button text / surrounding text
        // Pattern: "Add to Cart - NM - $2.50" or similar
        const addBtn = row.querySelector('div.addNow, button.addNow, [class*="addNow"]');
        let price = 0, quality = '';
        if (addBtn) {
            const txt = addBtn.innerText || '';
            const priceM = txt.match(/\$\s*([\d,.]+)/);
            if (priceM) price = parseFloat(priceM[1].replace(',',''));
            const qualM = txt.match(/\b(NM|LP|MP|HP|DM|EX[+]?|VG|PL|NM\/M)\b/i);
            if (qualM) quality = qualM[1].toUpperCase();
        }

        // Fallback: look for price in any element with "price" class
        if (!price) {
            const priceEl = row.querySelector('[class*="price"]');
            if (priceEl) {
                const m = (priceEl.innerText || '').match(/[\d,.]+/);
                if (m) price = parseFloat(m[0].replace(',',''));
            }
        }

        if (price > 0) {
            cards.push({ title: rawTitle, href, img, price, quality });
        }
    });
    return cards;
}
"""

# Variant 2: Shopify data-product-variants JSON attribute
_JS_VARIANT2 = r"""
() => {
    const cards = [];
    document.querySelectorAll('[data-product-variants]').forEach(el => {
        let variants;
        try { variants = JSON.parse(el.getAttribute('data-product-variants')); }
        catch(e) { return; }
        if (!Array.isArray(variants)) return;

        // Find the product container to get URL and image
        const card = el.closest('[class*="product"], [class*="card"], article, li') || el.parentElement;
        const linkEl = card ? card.querySelector('a[href]') : null;
        const imgEl  = card ? card.querySelector('img') : null;
        const href   = linkEl ? linkEl.getAttribute('href') : '';
        const img    = imgEl  ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src') || '') : '';

        variants.forEach(v => {
            // CardInfo schema: ID, Title (variant name = quality), Name (card+set), Price (cents), Available (bool/int)
            const available = v.Available || v.available;
            if (!available) return;

            const rawPrice = v.Price ?? v.price ?? 0;
            const price    = typeof rawPrice === 'number'
                ? (rawPrice > 500 ? rawPrice / 100 : rawPrice)   // cents if >500, else assume dollars
                : parseFloat(rawPrice) || 0;
            if (!price) return;

            const title   = v.Title || v.title || '';
            const name    = v.Name  || v.name  || '';
            const variantHref = href || '';

            cards.push({ title: name || title, href: variantHref, img, price, quality: title });
        });
    });
    return cards;
}
"""

# Variant 3: productCard__card divs with chip data attributes
_JS_VARIANT3 = r"""
() => {
    const cards = [];
    document.querySelectorAll('div.productCard__card').forEach(card => {
        const titleEl = card.querySelector('p.productCard__title, [class*="productCard__title"]');
        const linkEl  = card.querySelector('a[href]');
        const imgEl   = card.querySelector('img');
        const setEl   = card.querySelector('p.productCard__setName, [class*="setName"]');

        if (!titleEl) return;
        const name  = titleEl.innerText.trim();
        const href  = linkEl ? linkEl.getAttribute('href') : '';
        const img   = imgEl  ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src') || '') : '';
        const extra = setEl  ? setEl.innerText.trim() : '';

        // Each chip li has data-variant* attributes
        card.querySelectorAll('ul.productChip__grid li, [class*="productChip"] li').forEach(chip => {
            const avail = chip.getAttribute('data-variantavailable');
            const qty   = parseInt(chip.getAttribute('data-variantqty') || '0', 10);
            if (avail === 'false' || qty <= 0) return;

            const rawPrice = parseFloat(chip.getAttribute('data-variantprice') || '0');
            const price    = rawPrice > 500 ? rawPrice / 100 : rawPrice;   // cents → dollars
            if (!price) return;

            const quality = (chip.getAttribute('data-varianttitle') || chip.innerText || '').trim();
            cards.push({ title: name, href, img, price, quality, extra });
        });
    });
    return cards;
}
"""


# ── curl_cffi HTTP client (primary — bypasses Cloudflare TLS fingerprinting) ──

_cffi_session: cffi_requests.Session | None = None
_cffi_session_lock = threading.Lock()


def _get_cffi_session() -> cffi_requests.Session:
    global _cffi_session
    if _cffi_session is None:
        with _cffi_session_lock:
            if _cffi_session is None:
                _cffi_session = cffi_requests.Session(impersonate="chrome120")
    return _cffi_session


def _is_cf_challenge(html: str) -> bool:
    """Return True if the response is a Cloudflare bot-challenge page (not real content)."""
    markers = (
        'id="challenge-form"',
        'cf-browser-verification',
        'checking your browser',
        'just a moment',
        'ddos protection by cloudflare',
        'cf-turnstile',
    )
    lower = html.lower()
    return any(m in lower for m in markers)


def _cffi_fetch_sync(url: str) -> str:
    """Synchronous fetch via curl_cffi (Chrome TLS impersonation). Returns HTML."""
    session = _get_cffi_session()
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    return resp.text


# ── BeautifulSoup parsers (mirror the JS extractor logic for each variant) ────

def _parse_bs_variant1(soup: BeautifulSoup, store: StoreConfig) -> list[dict]:
    """Variant 1: Cards Citadel custom HTML (div.Norm rows)."""
    cards = []
    for row in soup.select("div.Norm"):
        title_el = row.select_one("p.productTitle a, p.productTitle")
        link_el  = row.select_one("a[href]")
        img_el   = row.select_one("img")
        if not title_el:
            continue
        raw_title = title_el.get_text(strip=True)
        href = link_el["href"] if link_el else ""
        img  = (img_el.get("src") or img_el.get("data-src") or "") if img_el else ""

        price   = 0.0
        quality = ""
        add_btn = row.select_one('div.addNow, button.addNow, [class*="addNow"]')
        if add_btn:
            txt = add_btn.get_text()
            pm = re.search(r"\$\s*([\d,.]+)", txt)
            if pm:
                price = float(pm.group(1).replace(",", ""))
            qm = re.search(r"\b(NM|LP|MP|HP|DM|EX[+]?|VG|PL|NM\/M)\b", txt, re.IGNORECASE)
            if qm:
                quality = qm.group(1).upper()

        if not price:
            pe = row.select_one('[class*="price"]')
            if pe:
                m = re.search(r"[\d,.]+", pe.get_text())
                if m:
                    price = float(m.group(0).replace(",", ""))

        if price > 0:
            cards.append({"title": raw_title, "href": href, "img": img, "price": price, "quality": quality})
    return cards


def _parse_bs_variant2(soup: BeautifulSoup, store: StoreConfig) -> list[dict]:
    """Variant 2: Shopify data-product-variants JSON attribute."""
    cards = []
    for el in soup.select("[data-product-variants]"):
        try:
            variants = json.loads(el["data-product-variants"])
        except (ValueError, KeyError):
            continue
        if not isinstance(variants, list):
            continue

        # Walk up to find product container for URL + image
        card_el = el.find_parent(class_=re.compile(r"product|card")) or el.parent
        link_el = card_el.select_one("a[href]") if card_el else None
        img_el  = card_el.select_one("img")      if card_el else None
        href = link_el["href"]                                    if link_el else ""
        img  = (img_el.get("src") or img_el.get("data-src") or "") if img_el  else ""

        for v in variants:
            available = v.get("Available") or v.get("available")
            if not available:
                continue
            raw_price = v.get("Price") or v.get("price") or 0
            if isinstance(raw_price, (int, float)):
                price = raw_price / 100 if raw_price > 500 else float(raw_price)
            else:
                try:
                    price = float(raw_price)
                except (TypeError, ValueError):
                    price = 0.0
            if not price:
                continue
            title = v.get("Title") or v.get("title") or ""
            name  = v.get("Name")  or v.get("name")  or ""
            cards.append({"title": name or title, "href": href, "img": img, "price": price, "quality": title})
    return cards


def _parse_bs_variant3(soup: BeautifulSoup, store: StoreConfig) -> list[dict]:
    """Variant 3: productCard__card divs with chip data attributes."""
    cards = []
    for card_el in soup.select("div.productCard__card"):
        title_el = card_el.select_one('p.productCard__title, [class*="productCard__title"]')
        link_el  = card_el.select_one("a[href]")
        img_el   = card_el.select_one("img")
        set_el   = card_el.select_one('p.productCard__setName, [class*="setName"]')
        if not title_el:
            continue
        name  = title_el.get_text(strip=True)
        href  = link_el["href"]                                    if link_el else ""
        img   = (img_el.get("src") or img_el.get("data-src") or "") if img_el  else ""
        extra = set_el.get_text(strip=True)                         if set_el  else ""

        for chip in card_el.select('ul.productChip__grid li, [class*="productChip"] li'):
            if chip.get("data-variantavailable") == "false":
                continue
            qty = int(chip.get("data-variantqty") or "0")
            if qty <= 0:
                continue
            try:
                raw_price = float(chip.get("data-variantprice") or "0")
            except (TypeError, ValueError):
                continue
            price = raw_price / 100 if raw_price > 500 else raw_price
            if not price:
                continue
            quality = (chip.get("data-varianttitle") or chip.get_text(strip=True))
            cards.append({"title": name, "href": href, "img": img, "price": price, "quality": quality, "extra": extra})
    return cards


_BS_PARSERS = {
    1: _parse_bs_variant1,
    2: _parse_bs_variant2,
    3: _parse_bs_variant3,
}


async def _scrape_with_cffi(store: StoreConfig, card_name: str) -> list[dict] | None:
    """Fetch with curl_cffi + parse with BeautifulSoup.

    Returns list of cards (possibly empty) if the page loaded cleanly.
    Returns None if a Cloudflare challenge is detected or the fetch fails,
    signalling the caller to fall back to Playwright.
    """
    url = store.search_url(card_name)
    loop = asyncio.get_event_loop()
    try:
        html = await loop.run_in_executor(None, _cffi_fetch_sync, url)
    except Exception as exc:
        log.warning("[%s] curl_cffi fetch error for '%s': %s — will try Playwright", store.name, card_name, exc)
        return None

    if _is_cf_challenge(html):
        log.warning("[%s] CF challenge page for '%s' — falling back to Playwright", store.name, card_name)
        return None

    soup     = BeautifulSoup(html, "lxml")
    raw_list = _BS_PARSERS[store.variant](soup, store)
    log.debug("[%s] curl_cffi parsed %d raw items for '%s'", store.name, len(raw_list), card_name)

    cards = []
    for raw in raw_list:
        card = _make_card(raw, store)
        if card is None:
            continue
        if not _name_matches(card_name, card["name"]):
            log.debug("[%s] curl_cffi skipping '%s' — name mismatch for search '%s'", store.name, card["name"], card_name)
            continue
        cards.append(card)

    log.info("[%s] curl_cffi → %d card(s) for '%s'", store.name, len(cards), card_name)
    return cards


# ── Core scraping logic ───────────────────────────────────────────────────────

def _make_card(raw: dict, store: StoreConfig) -> dict | None:
    """Convert a raw JS-extracted dict into the standard card shape."""
    price = float(raw.get("price", 0) or 0)
    if price <= 0:
        return None

    title    = str(raw.get("title", "")).strip()
    quality  = _normalise_quality(str(raw.get("quality", "")).strip())
    href     = str(raw.get("href", "")).strip()
    img      = str(raw.get("img", "")).strip()
    extra    = str(raw.get("extra", "")).strip()

    # Resolve relative URLs
    if href and not href.startswith("http"):
        href = store.base_url.rstrip("/") + "/" + href.lstrip("/")
    if img and not img.startswith("http"):
        img = store.base_url.rstrip("/") + "/" + img.lstrip("/")

    # Strip leading "//" protocol-relative URLs
    if href.startswith("//"):
        href = "https:" + href
    if img.startswith("//"):
        img = "https:" + img

    # UTM tagging
    if href:
        sep = "&" if "?" in href else "?"
        href = href + sep + "utm_source=gishath"

    # Drop non-MTG results from multi-TCG stores (Pokémon, YGO, One Piece, etc.)
    if _is_non_mtg(title, extra):
        log.debug("[%s] Dropping non-MTG result: '%s' (set: '%s')", store.name, title, extra)
        return None

    return {
        "name":      title,
        "url":       href,
        "img":       img,
        "price":     price,
        "inStock":   True,
        "isFoil":    _is_foil(title) or _is_foil(quality),
        "src":       store.name,
        "quality":   quality,
        "extraInfo": extra,
    }


async def _scrape_with_playwright(store: StoreConfig, card_name: str) -> list[dict]:
    """Open one Playwright browser page, navigate, extract cards via JS evaluator.

    This is the fallback path used when curl_cffi detects a Cloudflare challenge
    or fails to fetch the page.  Playwright presents a real Chromium fingerprint
    and can solve JS challenges that curl_cffi cannot.
    """
    if _browser is None:
        raise RuntimeError("Browser not initialised.")

    url = store.search_url(card_name)
    js  = {1: _JS_VARIANT1, 2: _JS_VARIANT2, 3: _JS_VARIANT3}[store.variant]

    async with _semaphore:
        page = await _new_stealth_page()
        try:
            # Block heavy assets to speed up scraping
            await page.route(
                "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,eot,otf}",
                lambda route: route.abort(),
            )
            await page.goto(url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")

            # Wait for at least one relevant element to appear (or timeout silently)
            wait_sel = {
                1: "div.Norm",
                2: "[data-product-variants]",
                3: "div.productCard__card",
            }[store.variant]
            try:
                await page.wait_for_selector(wait_sel, timeout=WAIT_SELECTOR_TIMEOUT)
            except Exception:
                # Selector not found — could be: CF challenge, genuine no-results, or
                # page structure change.  Capture title + body snippet to tell them apart.
                try:
                    page_title   = await page.title()
                    page_snippet = (await page.inner_text("body"))[:400].replace("\n", " ").strip()
                except Exception:
                    page_title   = "<could not read title>"
                    page_snippet = "<could not read body>"
                log.warning(
                    "[%s] Playwright: selector '%s' not found for '%s'\n"
                    "  page title   : %s\n"
                    "  body snippet : %s",
                    store.name, wait_sel, card_name, page_title, page_snippet,
                )
                return []

            raw_list: list[dict] = await page.evaluate(js)
            log.debug("[%s] Playwright parsed %d raw items for '%s'", store.name, len(raw_list), card_name)

            cards = []
            for raw in raw_list:
                card = _make_card(raw, store)
                if card is None:
                    continue
                # Filter: only keep results that contain the search term
                if not _name_matches(card_name, card["name"]):
                    log.debug("[%s] Playwright skipping '%s' — name mismatch for search '%s'", store.name, card["name"], card_name)
                    continue
                cards.append(card)

            log.info("[%s] Playwright → %d card(s) for '%s'", store.name, len(cards), card_name)
            return cards

        except Exception as exc:
            log.warning("[%s] Playwright error scraping '%s': %s", store.name, card_name, exc)
            raise
        finally:
            try:
                await page.close()
            except Exception:
                pass


async def _scrape_store_for_card(store: StoreConfig, card_name: str) -> list[dict]:
    """Try curl_cffi (fast, no browser) first; fall back to Playwright if needed.

    curl_cffi impersonates Chrome's TLS fingerprint, bypassing most Cloudflare
    TLS-layer checks without spinning up a full browser.  If it gets a CF
    challenge page (JS-based bot check that requires a real browser), we fall
    through to Playwright automatically.
    """
    # Phase 1: curl_cffi + BeautifulSoup (no browser overhead)
    try:
        cffi_cards = await _scrape_with_cffi(store, card_name)
        if cffi_cards is not None:
            log.debug("[%s] curl_cffi returned %d cards for '%s'", store.name, len(cffi_cards), card_name)
            return cffi_cards
    except Exception as exc:
        log.warning("[%s] curl_cffi unexpected error for '%s': %s", store.name, card_name, exc)

    # Phase 2: Playwright fallback (real Chromium, handles JS challenges)
    log.info("[%s] Using Playwright fallback for '%s'", store.name, card_name)
    return await _scrape_with_playwright(store, card_name)


async def _search_one_store(store: StoreConfig, card_name: str) -> tuple[list[dict], dict | None]:
    """Returns (cards, store_error_or_None)."""
    try:
        cards = await _scrape_store_for_card(store, card_name)
        return cards, None
    except Exception as exc:
        err = {"store": store.name, "error": f"Playwright scrape failed: {exc}"}
        return [], err


async def _search_card_all_stores(card_name: str) -> dict:
    """Search all BinderPOS stores for a single card concurrently."""
    tasks = [_search_one_store(store, card_name) for store in BINDERPOS_STORES]
    results = await asyncio.gather(*tasks)

    all_cards: list[dict] = []
    errors: list[dict] = []
    for cards, err in results:
        all_cards.extend(cards)
        if err:
            errors.append(err)

    return {"cards": all_cards, "errors": errors}


async def debug_store(store: StoreConfig, card_name: str) -> dict:
    """Run full diagnostic for a single store + card.  Called by /debug/stores.

    Returns a dict with:
        store        str   — store name
        url          str   — search URL attempted
        cffi_ok      bool  — did curl_cffi fetch succeed without CF challenge
        cffi_raw     int   — raw item count from BeautifulSoup parser
        cffi_cards   int   — cards that passed the name filter
        playwright   bool  — did Playwright fallback run (cffi failed/CF'd)
        pw_raw       int   — raw item count from Playwright JS evaluator
        pw_cards     int   — cards that passed the name filter
        total_cards  int   — final card count returned
        error        str   — last exception message if any
    """
    url = store.search_url(card_name)
    result: dict = {
        "store":        store.name,
        "url":          url,
        "cffi_ok":      False,
        "cffi_raw":     0,
        "cffi_cards":   0,
        "playwright":   False,
        "pw_raw":       0,
        "pw_cards":     0,
        "total_cards":  0,
        "error":        "",
        "page_title":   "",   # set when Playwright selector fails — reveals CF challenge
        "page_snippet": "",   # first 400 chars of body text at that moment
    }

    # ── Phase 1: curl_cffi ────────────────────────────────────────────────────
    loop = asyncio.get_event_loop()
    try:
        html = await loop.run_in_executor(None, _cffi_fetch_sync, url)
        if _is_cf_challenge(html):
            result["error"] = "CF challenge detected — curl_cffi blocked"
        else:
            result["cffi_ok"] = True
            soup     = BeautifulSoup(html, "lxml")
            raw_list = _BS_PARSERS[store.variant](soup, store)
            result["cffi_raw"] = len(raw_list)
            cards = []
            for raw in raw_list:
                card = _make_card(raw, store)
                if card is None:
                    continue
                if not _name_matches(card_name, card["name"]):
                    continue
                cards.append(card)
            result["cffi_cards"]  = len(cards)
            result["total_cards"] = len(cards)
            return result   # curl_cffi succeeded — skip Playwright
    except Exception as exc:
        result["error"] = f"curl_cffi error: {exc}"

    # ── Phase 2: Playwright fallback ──────────────────────────────────────────
    result["playwright"] = True
    try:
        if _browser is None:
            result["error"] += " | Playwright browser not running"
            return result

        js    = {1: _JS_VARIANT1, 2: _JS_VARIANT2, 3: _JS_VARIANT3}[store.variant]
        async with _semaphore:
            page = await _new_stealth_page()
            try:
                await page.goto(url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
                wait_sel = {1: "div.Norm", 2: "[data-product-variants]", 3: "div.productCard__card"}[store.variant]
                try:
                    await page.wait_for_selector(wait_sel, timeout=WAIT_SELECTOR_TIMEOUT)
                except Exception:
                    # Capture page state so we can distinguish CF challenge vs empty vs changed layout
                    try:
                        page_title   = await page.title()
                        page_snippet = (await page.inner_text("body"))[:400].replace("\n", " ").strip()
                    except Exception:
                        page_title   = "<could not read title>"
                        page_snippet = "<could not read body>"
                    result["page_title"]   = page_title
                    result["page_snippet"] = page_snippet
                    result["error"] += (
                        f" | Playwright: selector '{wait_sel}' not found"
                        f" | page title: {page_title!r}"
                    )
                    return result
                raw_list = await page.evaluate(js)
                result["pw_raw"] = len(raw_list)
                cards = []
                for raw in raw_list:
                    card = _make_card(raw, store)
                    if card is None:
                        continue
                    if not _name_matches(card_name, card["name"]):
                        continue
                    cards.append(card)
                result["pw_cards"]    = len(cards)
                result["total_cards"] = len(cards)
            finally:
                await page.close()
    except Exception as exc:
        result["error"] += f" | Playwright error: {exc}"

    return result


async def debug_all_stores(card_name: str) -> list[dict]:
    """Run debug_store for every configured Playwright store concurrently."""
    tasks = [debug_store(store, card_name) for store in BINDERPOS_STORES]
    return list(await asyncio.gather(*tasks))


async def search_many_playwright(card_names: list[str]) -> dict[str, dict]:
    """Search all BinderPOS stores for multiple cards concurrently.

    Returns:
        {
            "Abrade": {"cards": [...], "errors": [...]},
            ...
        }

    Card shape matches engine_client output:
        {name, url, img, price, inStock, isFoil, src, quality, extraInfo}
    """
    tasks = {name: _search_card_all_stores(name) for name in card_names}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    out: dict[str, dict] = {}
    for card_name, result in zip(tasks.keys(), results):
        if isinstance(result, Exception):
            log.error("search_many_playwright: unhandled exception for '%s': %s", card_name, result)
            out[card_name] = {
                "cards": [],
                "errors": [{"store": "playwright", "error": str(result)}],
            }
        else:
            out[card_name] = result

    return out
