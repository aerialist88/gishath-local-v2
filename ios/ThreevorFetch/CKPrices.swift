import Foundation

/// Card Kingdom (US) reference prices — port of ck_price.py's fast path.
///
/// The cache itself (CKPrices.json, ~6 MB) is bundled at build time: the Mac
/// builds it nightly from MTGJSON bulk files (far too heavy to do on-device),
/// publishes a copy outside the TCC-protected project folder, and the daily
/// 05:30 re-sign job bundles whatever is current. So the phone's CK prices
/// are at most ~a day old — and, mirroring ck_price.py's MAX_AGE_SECONDS
/// policy, anything older than 48h is omitted entirely rather than shown
/// as if fresh (e.g. if the Mac hasn't refreshed in days).
struct CKReference {
    let cardName: String
    let edition: String
    let priceUsd: Double
    let url: String
    let isFoil: Bool
    let asOf: String
}

enum CKPrices {
    private static let maxAgeSeconds: TimeInterval = 48 * 3600
    private static let dfcSeparator = " // "

    private struct Cache {
        let syncedAt: Date?
        let priceDate: String
        let entries: [String: [String: Any]]
    }

    private static let cache: Cache? = load()

    private static func load() -> Cache? {
        guard let url = Bundle.main.url(forResource: "CKPrices", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let entries = obj["entries"] as? [String: [String: Any]] else { return nil }
        let synced = (obj["syncedAt"] as? String)
            .flatMap { ISO8601DateFormatter().date(from: $0) }
        return Cache(syncedAt: synced,
                     priceDate: obj["priceDate"] as? String ?? "",
                     entries: entries)
    }

    /// Cheapest fresh CK listing for a card name, or nil (no cache, stale
    /// cache, or no listing) — callers just omit the banner, same as /search.
    static func lookup(_ cardName: String) -> CKReference? {
        guard let cache,
              let synced = cache.syncedAt,
              Date().timeIntervalSince(synced) <= maxAgeSeconds else { return nil }

        var best: [String: Any]?
        for key in lookupKeys(cardName) {
            guard let entry = cache.entries[key] else { continue }
            if best == nil || anyToDouble(entry["priceUsd"]) < anyToDouble(best!["priceUsd"]) {
                best = entry
            }
        }
        guard let best, anyToDouble(best["priceUsd"]) > 0 else { return nil }
        return CKReference(
            cardName: anyToString(best["cardName"]),
            edition: anyToString(best["edition"]),
            priceUsd: anyToDouble(best["priceUsd"]),
            url: anyToString(best["url"]),
            isFoil: best["isFoil"] as? Bool ?? false,
            asOf: cache.priceDate)
    }

    /// ck_price.py price_lookup_keys: combined name, then front/back faces
    /// for double-faced cards; cheapest across all keys wins.
    private static func lookupKeys(_ cardName: String) -> [String] {
        let trimmed = cardName.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return [] }
        let combined = trimmed.lowercased()
        guard let range = trimmed.range(of: dfcSeparator) else { return [combined] }
        let front = trimmed[..<range.lowerBound]
            .trimmingCharacters(in: .whitespaces).lowercased()
        let back = trimmed[range.upperBound...]
            .trimmingCharacters(in: .whitespaces).lowercased()
        guard !front.isEmpty, !back.isEmpty else { return [combined] }
        var keys: [String] = []
        for key in [combined, front, back] where !keys.contains(key) {
            keys.append(key)
        }
        return keys
    }
}
