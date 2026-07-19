import Foundation

/// Stores scraped by loading their search page in a hidden WKWebView and
/// running an extraction script — the same JS playwright_scraper.py evaluates
/// in headless Chromium. WKWebView is a real WebKit browser with a genuine
/// Apple TLS fingerprint, so it passes the Cloudflare checks that block plain
/// HTTP clients.
///
/// Variants 1–4 are copied verbatim from playwright_scraper.py.
/// Variants 5–6 replace the Go engine's goquery parsers for the two stores
/// that serve plain server-rendered HTML (Dueller's Point, 5 Mana).
struct WebStore: Sendable {
    let name: String
    let baseURL: String
    let searchPath: String   // contains {q}
    let variant: Int
    /// Selector that signals results have rendered (playwright wait_for_selector).
    var waitSelector: String {
        switch variant {
        case 1: return "div.Norm"
        case 2: return "[data-product-variants]"
        case 3: return "div.productCard__card"
        case 4: return "div#store_listingcontainer div.store-item"
        case 5: return "div.container table"
        case 6: return "ul.product-grid li"
        default: return "body"
        }
    }

    func searchURL(for cardName: String) -> URL? {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~ ")
        let q = cardName
            .addingPercentEncoding(withAllowedCharacters: allowed)?
            .replacingOccurrences(of: " ", with: "%20") ?? cardName
        return URL(string: baseURL + searchPath.replacingOccurrences(of: "{q}", with: q))
    }

    static let all: [WebStore] = [
        // ── Variant 1 (Cards Citadel custom HTML) ──
        WebStore(name: "Cards Citadel", baseURL: "https://cardscitadel.com",
                 searchPath: "/search?q={q}", variant: 1),
        // ── Variant 2 (Shopify data-product-variants JSON attr) ──
        WebStore(name: "Card Affinity", baseURL: "https://card-affinity.com",
                 searchPath: "/search?q={q}", variant: 2),
        WebStore(name: "Flagship Games", baseURL: "https://flagshipgames.sg",
                 searchPath: "/search?q={q}", variant: 2),
        WebStore(name: "Mana Pro", baseURL: "https://sg-manapro.com",
                 searchPath: "/search?type=product&q={q}", variant: 2),
        WebStore(name: "MTG Asia", baseURL: "https://www.mtg-asia.com",
                 searchPath: "/search?q={q}", variant: 2),
        WebStore(name: "One MTG", baseURL: "https://www.onemtg.com.sg",
                 searchPath: "/search?q={q}", variant: 2),
        // ── Variant 3 (productCard__card with chip data attrs) ──
        WebStore(name: "Games Haven", baseURL: "https://www.gameshaventcg.com",
                 searchPath: "/search?q={q}", variant: 3),
        WebStore(name: "Grey Ogre Games", baseURL: "https://www.greyogregames.com",
                 searchPath: "/search?q={q}", variant: 3),
        WebStore(name: "Hideout", baseURL: "https://hideoutcg.com",
                 searchPath: "/search?q={q}", variant: 3),
        // ── Variant 5 (Dueller's Point — port of engine-src goquery parser) ──
        WebStore(name: "Dueller's Point", baseURL: "https://www.duellerspoint.com",
                 searchPath: "/products/search?search_text={q}", variant: 5),
        // ── Variant 6 (5 Mana — port of engine-src goquery parser) ──
        WebStore(name: "5 Mana", baseURL: "https://5-mana.sg",
                 searchPath: "/search?q={q}&filter.v.availability=1", variant: 6),
        // ── Variant 4 (Agora Hobby) — deliberately LAST so its notorious
        // slowness (7–30s per request; see playwright_scraper.py's dedicated
        // semaphore) queues behind every faster store instead of starving
        // them of webview slots. It also gets a single goto attempt.
        WebStore(name: "Agora Hobby", baseURL: "https://agorahobby.com",
                 searchPath: "/store/search?category=mtg&searchfield={q}", variant: 4),
    ]

    /// Agora holds a page slot for up to pageTimeout per attempt; one attempt
    /// keeps its worst case at 20s instead of 40s (other stores keep 2).
    var gotoAttempts: Int { variant == 4 ? 1 : 2 }
}

/// Extraction scripts. Each is an arrow-function source; the scraper wraps it
/// as `(<js>)()` and expects an array of {title, href, img, price, quality,
/// extra, foil?} objects back.
enum ExtractionJS {
    static func script(forVariant variant: Int) -> String {
        switch variant {
        case 1: return v1
        case 2: return v2
        case 3: return v3
        case 4: return v4
        case 5: return v5
        case 6: return v6
        default: return "() => []"
        }
    }

    static let v1 = #"""
() => {
    const cards = [];
    document.querySelectorAll('div.Norm').forEach(row => {
        const titleEl = row.querySelector('p.productTitle a, p.productTitle');
        const linkEl   = row.querySelector('a[href]');
        if (!titleEl) return;
        const rawTitle = titleEl.innerText.trim();
        const href     = linkEl ? linkEl.getAttribute('href') : '';

        const imgEl = row.querySelector('img');
        const img   = imgEl ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src') || '') : '';

        const addBtn = row.querySelector('div.addNow, button.addNow, [class*="addNow"]');
        let price = 0, quality = '';
        if (addBtn) {
            const txt = addBtn.innerText || '';
            const priceM = txt.match(/\$\s*([\d,.]+)/);
            if (priceM) price = parseFloat(priceM[1].replace(',',''));
            // Diverges from playwright verbatim: Citadel spells conditions out
            // ("Add 1x Near Mint ($29.40) to Cart"), so match full names too.
            const qualM = txt.match(/\b(NM\/M|NM|LP|MP|HP|DM|EX[+]?|VG|PL|Near Mint|Lightly Played|Moderately Played|Heavily Played|Damaged)\b/i);
            if (qualM) quality = qualM[1].toUpperCase();
        }

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
"""#

    static let v2 = #"""
() => {
    const cards = [];
    document.querySelectorAll('[data-product-variants]').forEach(el => {
        let variants;
        try { variants = JSON.parse(el.getAttribute('data-product-variants')); }
        catch(e) { return; }
        if (!Array.isArray(variants)) return;

        const card = el.closest('[class*="product"], [class*="card"], article, li') || el.parentElement;
        const linkEl = card ? card.querySelector('a[href]') : null;
        const imgEl  = card ? card.querySelector('img') : null;
        const href   = linkEl ? linkEl.getAttribute('href') : '';
        const img    = imgEl  ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src') || '') : '';

        variants.forEach(v => {
            const available = v.Available || v.available;
            if (!available) return;

            // Integer price = cents (Shopify money); decimal = dollars.
            const rawPrice = v.Price ?? v.price ?? 0;
            const price    = typeof rawPrice === 'number'
                ? (Number.isInteger(rawPrice) ? rawPrice / 100 : rawPrice)
                : (/^\d+$/.test(String(rawPrice).trim()) ? parseInt(rawPrice, 10) / 100 : parseFloat(rawPrice) || 0);
            if (!price) return;

            const title   = v.Title || v.title || '';
            const name    = v.Name  || v.name  || '';

            cards.push({ title: name || title, href: href || '', img, price, quality: title });
        });
    });
    return cards;
}
"""#

    static let v3 = #"""
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

        card.querySelectorAll('ul.productChip__grid li, [class*="productChip"] li').forEach(chip => {
            const avail = chip.getAttribute('data-variantavailable');
            const qty   = parseInt(chip.getAttribute('data-variantqty') || '0', 10);
            if (avail === 'false' || qty <= 0) return;

            // Integer = cents regardless of magnitude; decimals are dollars.
            const rawStr   = (chip.getAttribute('data-variantprice') || '0').trim();
            const price    = /^\d+$/.test(rawStr) ? parseInt(rawStr, 10) / 100 : (parseFloat(rawStr) || 0);
            if (!price) return;

            const quality = (chip.getAttribute('data-varianttitle') || chip.innerText || '').trim();
            cards.push({ title: name, href, img, price, quality, extra });
        });
    });
    return cards;
}
"""#

    static let v4 = #"""
() => {
    const cards = [];
    document.querySelectorAll('div#store_listingcontainer div.store-item').forEach(item => {
        const stockEl = item.querySelector('div.store-item-stock');
        if (!stockEl || stockEl.innerText.trim() === 'Stock: 0') return;

        const priceEl = item.querySelector('div.store-item-price');
        const rawPrice = priceEl ? (priceEl.innerText || '').replace(/\$/g, '').replace(/,/g, '').trim() : '';
        const price = parseFloat(rawPrice) || 0;
        if (!price) return;

        const titleEl = item.querySelector('div.store-item-title');
        const name = titleEl ? titleEl.innerText.trim() : '';
        if (!name) return;

        const catEl = item.querySelector('div.store-item-cat');
        const catText = catEl ? catEl.innerText.trim() : '';
        let quality = '';
        const parts = catText.split(' - ');
        if (parts.length === 2) quality = parts[1].trim();
        let extra = '';
        const bracketIdx = catText.indexOf(']');
        if (bracketIdx > 1) extra = catText.slice(0, bracketIdx + 1);

        const imgEl = item.querySelector('div.store-item-img');
        const img = imgEl ? (imgEl.getAttribute('data-img') || '') : '';

        cards.push({ title: name, href: window.location.href, img, price, quality, extra });
    });
    return cards;
}
"""#

    // Dueller's Point — mirrors engine-src/api/gateway/duellerpoint/search.go:
    // rows in div.container table > tbody; td cells are [thumb, name, set,
    // condition, stock, price]; in stock iff td[4] contains "left".
    static let v5 = #"""
() => {
    const cards = [];
    document.querySelectorAll('div.container table > tbody tr').forEach(tr => {
        const tds = tr.querySelectorAll('td');
        if (tds.length < 6) return;
        const a   = tds[0].querySelector('a.product-list-thumb');
        const im  = tds[0].querySelector('a.product-list-thumb img');
        const name = (tds[1].innerText || '').trim();
        if (!name) return;
        const setTxt = (tds[2].innerText || '').trim();
        const extra  = setTxt ? '[' + setTxt + ']' : '';

        let quality = '';
        tds[3].querySelectorAll('p').forEach(p => {
            const span = p.querySelector('span');
            if (span && (span.innerText || '').includes('Condition')) {
                const st = p.querySelector('strong');
                if (st) quality = (st.innerText || '').trim();
            }
        });

        if (!((tds[4].innerText || '').includes('left'))) return;

        const priceM = (tds[5].innerText || '').match(/[\d,]+\.?\d*/);
        const price  = priceM ? parseFloat(priceM[0].replace(/,/g, '')) : 0;
        if (!price) return;

        cards.push({
            title: name,
            href: a ? (a.getAttribute('href') || '') : '',
            img:  im ? (im.getAttribute('src') || '') : '',
            price, quality, extra,
            foil: name.includes('Foil'),
        });
    });
    return cards;
}
"""#

    // 5 Mana — mirrors engine-src/api/gateway/fivemana/search.go:
    // Shopify grid; name in h3.card__heading a (with "[Foil]" marker), sale
    // price in span.price-item--sale.price-item--last.
    static let v6 = #"""
() => {
    const cards = [];
    document.querySelectorAll('ul.product-grid li').forEach(li => {
        const a = li.querySelector('h3.card__heading a');
        if (!a) return;
        const rawName = (a.innerText || '').trim();
        if (!rawName) return;
        const name = rawName.replace(/\[Foil\]/gi, '').trim();
        const href = a.getAttribute('href') || '';
        const im   = li.querySelector('div.card__media img');
        const priceEl = li.querySelector('span.price-item.price-item--sale.price-item--last');
        const priceM  = priceEl ? (priceEl.innerText || '').match(/[\d,]+\.?\d*/) : null;
        const price   = priceM ? parseFloat(priceM[0].replace(/,/g, '')) : 0;
        if (!price) return;

        cards.push({
            title: name,
            href,
            img: im ? (im.getAttribute('src') || '') : '',
            price, quality: '', extra: '',
            foil: rawName.toLowerCase().includes('[foil]'),
        });
    });
    return cards;
}
"""#
}
