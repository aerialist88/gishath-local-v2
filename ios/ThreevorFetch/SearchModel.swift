import Foundation
import Observation
import WebKit
import os

/// Drives a search: web stores are scraped by a fixed crew of workers that
/// each own one WKWebView and pull (card × store) units off a queue — so a
/// unit's clock only ticks while it's actually being scraped, never while
/// queued. This is what fixes the mass timeouts big buy lists used to hit:
/// previously every unit raced a shared 75s timer that included queue wait.
///
/// The merge pipeline mirrors app.py (+ presentation.py): delivery fee →
/// accessory filter → name-match filter → sort by price. Results render
/// progressively as units complete; when the wall-clock budget runs out the
/// remaining queue is dropped (finished stores keep their results — same
/// partial-results philosophy as app.py's SEARCH_BUDGET_SECONDS handling).
@MainActor
@Observable
final class SearchModel {
    var buyListText = ""
    var isSearching = false
    var results: [CardResult] = []
    var storeErrors: [StoreError] = []
    var skipped: [String] = []
    var elapsed: Double = 0
    var completedUnits = 0
    var totalUnits = 0
    var hasSearched = false

    static let topN = 5
    static let webWorkerCount = 5   // == WebViewPool size

    private static let log = Logger(subsystem: "com.trevorjow.threevorfetch", category: "search")

    /// Wall-clock budget scaled to the list: ~25s/card, floor 90s, cap 5 min.
    static func budget(forCards n: Int) -> TimeInterval {
        min(300, max(90, Double(n) * 25))
    }

    // Worker-queue state (MainActor-only).
    private var pendingWebUnits: [(card: String, store: WebStore)] = []
    private var collected: [String: [Listing]] = [:]
    private var errorsByStore: [String: String] = [:]
    private var currentBuyList: [String] = []

    var totalListings: Int { results.reduce(0) { $0 + $1.listings.count } }

    func parsedBuyList() -> (valid: [String], skipped: [String]) {
        var valid: [String] = []
        var short: [String] = []
        for line in buyListText.split(separator: "\n") {
            let name = line.trimmingCharacters(in: .whitespaces)
            if name.count >= 3 { valid.append(name) }
            else if !name.isEmpty { short.append(name) }
        }
        return (valid, short)
    }

    func search() {
        guard !isSearching else { return }
        let (buyList, short) = parsedBuyList()
        guard !buyList.isEmpty else { return }

        isSearching = true
        hasSearched = true
        skipped = short
        results = []
        storeErrors = []
        elapsed = 0
        completedUnits = 0
        totalUnits = buyList.count * (WebStore.all.count + APIStores.names.count)
        currentBuyList = buyList
        collected = Dictionary(uniqueKeysWithValues: buyList.map { ($0, []) })
        errorsByStore = [:]

        // Card-major order spaces out hits to the same store (BinderPOS
        // rate-limits); Agora (variant 4) goes at the very back of the queue
        // so the chronically slow store can never starve fast ones.
        let fast = WebStore.all.filter { $0.variant != 4 }
        let slow = WebStore.all.filter { $0.variant == 4 }
        pendingWebUnits = buyList.flatMap { card in fast.map { (card, $0) } }
            + buyList.flatMap { card in slow.map { (card, $0) } }

        Task { await run(buyList: buyList) }
    }

    private func run(buyList: [String]) async {
        let start = Date()
        let deadline = start.addingTimeInterval(Self.budget(forCards: buyList.count))
        Self.log.info("search start: \(buyList.count, privacy: .public) cards, \(self.totalUnits, privacy: .public) units, budget \(Int(deadline.timeIntervalSince(start)), privacy: .public)s")
        print("search start: \(buyList.count) cards, \(totalUnits) units, budget \(Int(deadline.timeIntervalSince(start)))s")
        await WebViewPool.prepare()

        await withTaskGroup(of: Void.self) { group in
            // Web-store workers: each owns one webview for the whole search.
            let workers = min(Self.webWorkerCount, pendingWebUnits.count)
            for _ in 0..<workers {
                group.addTask { @MainActor [weak self] in
                    guard let self else { return }
                    let webView = await WebViewPool.shared.acquire()
                    while let unit = self.claimNextWebUnit(deadline: deadline) {
                        var listings: [Listing] = []
                        var err: String?
                        do {
                            listings = try await WebViewScraper.search(
                                store: unit.store, cardName: unit.card, webView: webView)
                        } catch {
                            err = Self.describe(error)
                        }
                        self.record(card: unit.card, store: unit.store.name,
                                    listings: listings, error: err)
                    }
                    WebViewPool.shared.release(webView)
                }
            }

            // API stores: one worker per store, cards processed sequentially —
            // firing all cards at once (25 concurrent hits on one domain)
            // caused real timeouts, and the Go engine rate-limits per domain
            // for exactly this reason.
            for storeName in APIStores.names {
                group.addTask { [weak self] in
                    guard let self else { return }
                    for card in buyList {
                        var listings: [Listing] = []
                        var err: String?
                        do {
                            listings = try await withTimeout(30) {
                                try await APIStores.search(store: storeName, cardName: card)
                            }
                        } catch {
                            err = Self.describe(error)
                        }
                        await self.record(card: card, store: storeName,
                                          listings: listings, error: err)
                    }
                }
            }
        }

        elapsed = (Date().timeIntervalSince(start) * 10).rounded() / 10
        isSearching = false
        Self.log.info("search done in \(self.elapsed, privacy: .public)s — \(self.totalListings, privacy: .public) listings, \(self.errorsByStore.count, privacy: .public) stores with errors")
        print("search done in \(elapsed)s — \(totalListings) listings, errors: \(errorsByStore.map { "\($0.key): \($0.value)" }.joined(separator: " | "))")
    }

    /// Next unit off the queue — or nil when empty / past the deadline.
    /// Past-deadline drains the queue, marking untouched stores as skipped.
    private func claimNextWebUnit(deadline: Date) -> (card: String, store: WebStore)? {
        guard !pendingWebUnits.isEmpty else { return nil }
        guard Date() < deadline else {
            for unit in pendingWebUnits where errorsByStore[unit.store.name] == nil {
                errorsByStore[unit.store.name] = "search budget hit — remaining lookups skipped"
            }
            completedUnits += pendingWebUnits.count
            Self.log.warning("budget hit with \(self.pendingWebUnits.count, privacy: .public) units still queued")
            pendingWebUnits.removeAll()
            refreshDisplayedResults()
            return nil
        }
        return pendingWebUnits.removeFirst()
    }

    private func record(card: String, store: String, listings: [Listing], error: String?) {
        collected[card, default: []].append(contentsOf: listings)
        if let error {
            Self.log.error("\(store, privacy: .public) / \(card, privacy: .public): \(error, privacy: .public)")
            print("ERR \(store) / \(card): \(error)")
            if errorsByStore[store] == nil { errorsByStore[store] = error }
        }
        completedUnits += 1
        refreshDisplayedResults()
    }

    /// Progressive render — listings appear as stores finish.
    private func refreshDisplayedResults() {
        results = Self.buildResults(buyList: currentBuyList, collected: collected)
        storeErrors = errorsByStore.map { StoreError(store: $0.key, error: $0.value) }
            .sorted { $0.store < $1.store }
    }

    private nonisolated static func describe(_ error: Error) -> String {
        if error is TimeoutError { return "timed out" }
        return (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }

    /// app.py _merge_results + presentation.py format_results, minus the row
    /// flattening (SwiftUI renders sections per card directly).
    nonisolated static func buildResults(buyList: [String], collected: [String: [Listing]]) -> [CardResult] {
        buyList.map { card in
            var listings = collected[card] ?? []

            // TCG Marketplace landed cost (+ SGD 0.40 delivery to pickup point).
            listings = listings.map { listing in
                var l = listing
                if l.src == Pricing.tcgMarketplaceName {
                    l.price = ((l.price + Pricing.tcgMarketplaceDeliveryFeeSGD) * 100).rounded() / 100
                    l.extraInfo = l.extraInfo.isEmpty
                        ? Pricing.deliveryNote
                        : l.extraInfo + " · " + Pricing.deliveryNote
                }
                return l
            }

            listings.removeAll { Filters.isAccessory(name: $0.name, extraInfo: $0.extraInfo) }
            listings.removeAll { !Filters.nameMatches(cardName: card, resultName: $0.name) }
            listings.sort { $0.price < $1.price }

            return CardResult(card: card, listings: listings)
        }
    }
}
