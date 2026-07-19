import Foundation
import WebKit

/// Scrapes WebStore search pages in hidden WKWebViews — the iOS equivalent of
/// playwright_scraper.py's headless Chromium. Timings mirror the Playwright
/// path: 20s navigation timeout, 5s wait-for-selector, 2 goto attempts, and a
/// per-store circuit breaker so one store having a bad day can't burn the
/// whole search budget.
enum ScrapeError: Error, LocalizedError {
    case navigationTimeout
    case circuitOpen
    case cloudflareChallenge
    case badURL

    var errorDescription: String? {
        switch self {
        case .navigationTimeout:   return "page load timed out"
        case .circuitOpen:         return "skipped (store repeatedly timing out)"
        case .cloudflareChallenge: return "Cloudflare challenge not cleared"
        case .badURL:              return "could not build search URL"
        }
    }
}

// MARK: - Circuit breaker (playwright_scraper.py _breaker_*)

actor CircuitBreaker {
    static let shared = CircuitBreaker()
    private var fails: [String: Int] = [:]
    private var openUntil: [String: Date] = [:]
    private let threshold = 3
    private let cooldown: TimeInterval = 600

    func check(_ store: String) throws {
        if let until = openUntil[store], until > Date() { throw ScrapeError.circuitOpen }
    }
    func recordTimeout(_ store: String) {
        let n = (fails[store] ?? 0) + 1
        fails[store] = n
        if n >= threshold { openUntil[store] = Date().addingTimeInterval(cooldown) }
    }
    func recordSuccess(_ store: String) {
        fails[store] = 0
        openUntil[store] = nil
    }
}

// MARK: - WebView pool

/// A small pool of reusable off-screen WKWebViews. All WKWebView work must
/// happen on the main actor; the pool suspends callers when every page slot
/// is busy (Playwright used a semaphore of 10 pages — on a phone 4 is plenty).
@MainActor
final class WebViewPool {
    static let shared = WebViewPool(size: 5)

    private let size: Int
    private var idle: [WKWebView] = []
    private var created = 0
    private var waiters: [CheckedContinuation<WKWebView, Never>] = []
    private static var blockRules: WKContentRuleList?

    init(size: Int) { self.size = size }

    /// Compile the asset-block rule list once (images/fonts/media are dead
    /// weight — extraction only reads attributes and text).
    static func prepare() async {
        guard blockRules == nil else { return }
        let rules = """
        [{"trigger":{"url-filter":".*","resource-type":["image","font","media"]},
          "action":{"type":"block"}}]
        """
        blockRules = try? await WKContentRuleListStore.default().compileContentRuleList(
            forIdentifier: "block-heavy-assets", encodedContentRuleList: rules)
    }

    func acquire() async -> WKWebView {
        if let wv = idle.popLast() { return wv }
        if created < size {
            created += 1
            return makeWebView()
        }
        return await withCheckedContinuation { waiters.append($0) }
    }

    func release(_ wv: WKWebView) {
        wv.stopLoading()
        if let waiter = waiters.first {
            waiters.removeFirst()
            waiter.resume(returning: wv)
        } else {
            idle.append(wv)
        }
    }

    private func makeWebView() -> WKWebView {
        let cfg = WKWebViewConfiguration()
        if let rules = Self.blockRules {
            cfg.userContentController.add(rules)
        }
        let wv = WKWebView(frame: CGRect(x: 0, y: 0, width: 390, height: 844), configuration: cfg)
        // Real mobile Safari UA — WKWebView's default omits the Safari token,
        // which some Cloudflare configs treat as suspicious.
        wv.customUserAgent = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
            + "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
        return wv
    }
}

// MARK: - Navigation delegate bridging to async/await

@MainActor
private final class NavigationWaiter: NSObject, WKNavigationDelegate {
    private var continuation: CheckedContinuation<Void, Error>?
    private var navigation: WKNavigation?

    func waitForLoad(_ webView: WKWebView, url: URL) async throws {
        // Cancellation (e.g. the withTimeout race firing) must resume the
        // continuation, or the webview slot would stay wedged until the
        // URLRequest's own timeout fires.
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
                continuation = cont
                webView.navigationDelegate = self
                navigation = webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20))
            }
        } onCancel: {
            Task { @MainActor [weak self, weak webView] in
                webView?.stopLoading()
                self?.finishAnyway(.failure(CancellationError()))
            }
        }
    }

    /// Delegate callbacks only count if they belong to the navigation WE
    /// started. Reused webviews deliver stragglers: when a timed-out
    /// attempt's load is cancelled by the next attempt's load(), the OLD
    /// navigation's failure (NSURLError -999 "cancelled") arrives at the
    /// current delegate and must not poison the fresh attempt.
    private func finish(_ navigation: WKNavigation?, _ result: Result<Void, Error>) {
        guard navigation === self.navigation else { return }
        finishAnyway(result)
    }

    private func finishAnyway(_ result: Result<Void, Error>) {
        guard let cont = continuation else { return }
        continuation = nil
        cont.resume(with: result)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        finish(navigation, .success(()))
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        finish(navigation, .failure(error))
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        finish(navigation, .failure(error))
    }
}

// MARK: - Scraper

enum WebViewScraper {
    static let pageTimeout: TimeInterval = 20
    static let selectorTimeout: TimeInterval = 5
    static let challengeGrace: TimeInterval = 8   // extra wait for CF managed challenge to auto-clear
    static let gotoAttempts = 2

    /// Scrape one store for one card using a pooled webview. Convenience for
    /// one-off calls (and the macOS debug harness); the search fan-out uses
    /// the webView-owning overload below via SearchModel's worker queue.
    static func search(store: WebStore, cardName: String) async throws -> [Listing] {
        let webView = await WebViewPool.shared.acquire()
        defer { Task { @MainActor in WebViewPool.shared.release(webView) } }
        return try await search(store: store, cardName: cardName, webView: webView)
    }

    /// Scrape with a caller-owned webview. All waits are internally bounded
    /// (20s navigation × attempts, ≤13s selector), so callers do NOT need an
    /// outer timeout — crucially, time spent queued for a webview never
    /// counts against a store.
    static func search(store: WebStore, cardName: String, webView: WKWebView) async throws -> [Listing] {
        try await CircuitBreaker.shared.check(store.name)
        guard let url = store.searchURL(for: cardName) else { throw ScrapeError.badURL }

        var lastError: Error = ScrapeError.navigationTimeout
        let attempts = store.gotoAttempts
        for attempt in 1...attempts {
            do {
                let raw = try await loadAndExtract(webView: webView, url: url, store: store)
                await CircuitBreaker.shared.recordSuccess(store.name)
                return raw.compactMap { makeListing(raw: $0, store: store) }
                          .filter { Filters.nameMatches(cardName: cardName, resultName: $0.name) }
            } catch {
                lastError = error
                if attempt == attempts {
                    await CircuitBreaker.shared.recordTimeout(store.name)
                }
            }
        }
        throw lastError
    }

    @MainActor
    private static func loadAndExtract(webView: WKWebView, url: URL, store: WebStore) async throws -> [[String: Any]] {
        let waiter = NavigationWaiter()
        try await withTimeout(pageTimeout) {
            try await waiter.waitForLoad(webView, url: url)
        }

        // Wait for the results selector; if a Cloudflare interstitial is
        // showing, give it extra grace to auto-clear (real-browser challenges
        // usually resolve without interaction), then re-check.
        var found = await waitForSelector(webView, store.waitSelector, timeout: selectorTimeout)
        if !found {
            if await looksLikeChallenge(webView) {
                found = await waitForSelector(webView, store.waitSelector, timeout: challengeGrace)
                if !found { throw ScrapeError.cloudflareChallenge }
            } else {
                // Genuine no-results page — extraction below returns [].
            }
        }

        let js = "(" + ExtractionJS.script(forVariant: store.variant) + ")()"
        let result = try await evaluate(webView, js)
        return result as? [[String: Any]] ?? []
    }

    @MainActor
    private static func waitForSelector(_ webView: WKWebView, _ selector: String, timeout: TimeInterval) async -> Bool {
        let js = "!!document.querySelector(" + jsonString(selector) + ")"
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline, !Task.isCancelled {
            if let hit = try? await evaluate(webView, js) as? Bool, hit { return true }
            try? await Task.sleep(nanoseconds: 300_000_000)
        }
        return false
    }

    @MainActor
    private static func looksLikeChallenge(_ webView: WKWebView) async -> Bool {
        let js = "(document.title + ' ' + document.body.innerText.slice(0, 2000)).toLowerCase()"
        guard let text = try? await evaluate(webView, js) as? String else { return false }
        // Markers from playwright_scraper.py _is_cf_challenge.
        let markers = ["challenge-form", "cf-browser-verification", "checking your browser",
                       "just a moment", "ddos protection by cloudflare", "cf-turnstile"]
        return markers.contains(where: { text.contains($0) })
    }

    /// Callback-based evaluateJavaScript wrapped by hand — the async overload
    /// traps when the script legitimately returns null.
    @MainActor
    private static func evaluate(_ webView: WKWebView, _ js: String) async throws -> Any? {
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Any?, Error>) in
            webView.evaluateJavaScript(js) { value, error in
                if let error { cont.resume(throwing: error) }
                else { cont.resume(returning: value) }
            }
        }
    }

    private static func jsonString(_ s: String) -> String {
        let data = (try? JSONSerialization.data(withJSONObject: [s])) ?? Data()
        let arr = String(data: data, encoding: .utf8) ?? "[\"\"]"
        return String(arr.dropFirst().dropLast())
    }

    // MARK: makeListing (playwright_scraper.py _make_card)

    static func makeListing(raw: [String: Any], store: WebStore) -> Listing? {
        let price = anyToDouble(raw["price"])
        guard price > 0 else { return nil }

        // innerText can embed newlines (Cards Citadel puts the set name on a
        // second line inside the title element) — keep line 1 as the name and
        // fold the rest into extraInfo if it's otherwise empty.
        var title   = anyToString(raw["title"]).trimmingCharacters(in: .whitespacesAndNewlines)
        var titleRest = ""
        if let newline = title.firstIndex(of: "\n") {
            titleRest = String(title[title.index(after: newline)...])
                .replacingOccurrences(of: "\n", with: " ")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            title = String(title[..<newline]).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        let quality = Filters.normaliseQuality(anyToString(raw["quality"]))
        var href    = anyToString(raw["href"]).trimmingCharacters(in: .whitespacesAndNewlines)
        var img     = anyToString(raw["img"]).trimmingCharacters(in: .whitespacesAndNewlines)
        var extra   = anyToString(raw["extra"]).trimmingCharacters(in: .whitespacesAndNewlines)
        if extra.isEmpty { extra = titleRest }
        let explicitFoil = raw["foil"] as? Bool ?? false

        href = resolveURL(href, base: store.baseURL)
        img  = resolveURL(img, base: store.baseURL)
        if !href.isEmpty {
            href += (href.contains("?") ? "&" : "?") + "utm_source=gishath"
        }

        guard !title.isEmpty, !Filters.isNonMTG(name: title, extraInfo: extra) else { return nil }

        return Listing(
            name: title, url: href, img: img, price: price,
            isFoil: explicitFoil || Filters.isFoilText(title) || Filters.isFoilText(quality),
            src: store.name, quality: quality, extraInfo: extra)
    }

    private static func resolveURL(_ value: String, base: String) -> String {
        guard !value.isEmpty else { return value }
        if value.hasPrefix("//") { return "https:" + value }
        if value.hasPrefix("http") { return value }
        return base.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            .appending("/")
            .appending(value.hasPrefix("/") ? String(value.dropFirst()) : value)
    }
}

// MARK: - Small helpers shared with the API stores

func anyToString(_ v: Any?) -> String {
    if let s = v as? String { return s }
    if let n = v as? NSNumber { return n.stringValue }
    return ""
}

func anyToDouble(_ v: Any?) -> Double {
    if let d = v as? Double { return d }
    if let n = v as? NSNumber { return n.doubleValue }
    if let s = v as? String { return Double(s.trimmingCharacters(in: .whitespaces)) ?? 0 }
    return 0
}

func anyToInt(_ v: Any?) -> Int {
    if let i = v as? Int { return i }
    if let n = v as? NSNumber { return n.intValue }
    if let s = v as? String { return Int(s.trimmingCharacters(in: .whitespaces)) ?? 0 }
    return 0
}

struct TimeoutError: Error {}

/// Race an operation against a wall-clock deadline.
func withTimeout<T: Sendable>(_ seconds: TimeInterval, _ op: @escaping @Sendable () async throws -> T) async throws -> T {
    try await withThrowingTaskGroup(of: T.self) { group in
        group.addTask { try await op() }
        group.addTask {
            try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            throw TimeoutError()
        }
        guard let first = try await group.next() else { throw TimeoutError() }
        group.cancelAll()
        return first
    }
}
