import Foundation

/// One in-stock listing from one store. Mirrors the card dict shape used by
/// app.py / engine_client / playwright_scraper: {name, url, img, price,
/// inStock, isFoil, src, quality, extraInfo}.
struct Listing: Identifiable, Hashable {
    let id = UUID()
    var name: String
    var url: String
    var img: String
    var price: Double
    var isFoil: Bool
    var src: String
    var quality: String
    var extraInfo: String
}

struct StoreError: Identifiable, Hashable {
    let id = UUID()
    var store: String
    var error: String
}

/// All listings for one searched card, sorted by price ascending.
struct CardResult: Identifiable {
    let id = UUID()
    var card: String
    var listings: [Listing]
}

enum Pricing {
    /// TCG Marketplace listings are re-priced to landed cost at merge time —
    /// same as app.py's TCG_MARKETPLACE_DELIVERY_FEE_SGD.
    static let tcgMarketplaceDeliveryFeeSGD = 0.40
    static let tcgMarketplaceName = "The TCG Marketplace"
    static let deliveryNote = "incl. SGD 0.40 delivery"

    static func sgd(_ value: Double) -> String {
        String(format: "SGD %.2f", value)
    }
}
