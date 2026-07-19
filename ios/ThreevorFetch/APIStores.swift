import Foundation
import Security

/// The three stores with JSON APIs, ported from engine-src/api/gateway/*.
/// These don't sit behind Cloudflare TLS fingerprinting (the Go engine's
/// plain HTTP client worked), so URLSession is enough — no WKWebView needed.
enum APIStores {
    static let names = ["Cards & Collections", "Mox & Lotus", Pricing.tcgMarketplaceName]

    static func search(store: String, cardName: String) async throws -> [Listing] {
        let listings: [Listing]
        switch store {
        case "Cards & Collections":         listings = try await cardsAndCollections(cardName)
        case "Mox & Lotus":                 listings = try await moxAndLotus(cardName)
        case Pricing.tcgMarketplaceName:    listings = try await tcgMarketplace(cardName)
        default:                            listings = []
        }
        return listings.filter { Filters.nameMatches(cardName: cardName, resultName: $0.name) }
    }

    private static let session: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 20
        cfg.httpAdditionalHeaders = ["Accept-Language": "en-US,en;q=0.9"]
        return URLSession(configuration: cfg)
    }()

    private static func fetchJSON(_ request: URLRequest) async throws -> Any {
        let (data, response) = try await session.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw NSError(domain: "APIStores", code: http.statusCode,
                          userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode)"])
        }
        return try JSONSerialization.jsonObject(with: data)
    }

    // MARK: - Cards & Collections (engine-src cardsandcollection/search.go)

    static func cardsAndCollections(_ cardName: String) async throws -> [Listing] {
        let base = "https://cardsandcollections.com"
        let query: [String: Any] = [
            "query": ["bool": ["should": [
                ["simple_query_string": ["query": cardName,
                                         "fields": ["name", "setCode", "setName"],
                                         "default_operator": "AND"]],
                ["multi_match": ["query": cardName, "type": "phrase_prefix",
                                 "fields": ["name", "setCode", "setName"]]],
            ]]],
            "post_filter": ["bool": ["must": ["terms": ["collectableContext.raw": ["MTG", "ACCESSORY"]]]]],
            "size": 20,
            "sort": [["quantityOnSale": "desc"]],
        ]

        var req = URLRequest(url: URL(string: base + "/api/catalog/")!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: query)

        let root = try await fetchJSON(req) as? [String: Any] ?? [:]
        let hitsWrap = root["hits"] as? [String: Any] ?? [:]
        let hits = hitsWrap["hits"] as? [[String: Any]] ?? []

        var listings: [Listing] = []
        for hit in hits {
            guard let source = hit["_source"] as? [String: Any] else { continue }
            let quantity = anyToInt(source["quantityOnSale"])
            let minPrice = anyToDouble(source["minPriceSale"])
            guard quantity > 0, minPrice > 0 else { continue }
            let id = anyToString(hit["_id"])
            let name = anyToString(source["name"]).trimmingCharacters(in: .whitespaces)
            let setName = anyToString(source["setName"])
            listings.append(Listing(
                name: name,
                url: base + "/product/\(id)?utm_source=gishath",
                img: anyToString(source["img"]),
                price: minPrice,
                isFoil: Filters.isFoilText(name),
                src: "Cards & Collections",
                quality: "",
                extraInfo: setName.isEmpty ? "" : "[\(setName)]"))
        }
        return listings
    }

    // MARK: - Mox & Lotus (engine-src moxandlotus/search.go)

    static func moxAndLotus(_ cardName: String) async throws -> [Listing] {
        let base = "https://moxandlotus.sg"
        var comps = URLComponents(string: base + "/api/products")!
        comps.queryItems = [
            .init(name: "limit", value: "48"),
            .init(name: "full_search", value: "true"),
            .init(name: "showStatus", value: "false"),
            .init(name: "is_paginated", value: "true"),
            .init(name: "in_stock", value: "true"),
            .init(name: "sortVariation", value: "true"),
            .init(name: "category_id", value: "1"),
            .init(name: "variation_code", value: "all"),
            .init(name: "order_by", value: "Price Low to High"),
            .init(name: "search", value: cardName),
        ]

        let root = try await fetchJSON(URLRequest(url: comps.url!)) as? [String: Any] ?? [:]
        let products = root["data"] as? [[String: Any]] ?? []

        var listings: [Listing] = []
        for product in products {
            let conditions = product["conditions"] as? [[String: Any]] ?? []
            let title = anyToString(product["title"]).trimmingCharacters(in: .whitespaces)
            let expansion = anyToString(product["expansion"])
            let expansionCode = anyToString(product["expansion_code"])
            let isFoil = anyToString(product["variation_code"]) == "foil"
            let productID = anyToInt(product["id"])

            guard let img = moxImageURL(
                expansionCode: expansionCode,
                cardNumber: anyToString(product["card_number"]),
                imagePath: product["image_path"]) else { continue }

            for condition in conditions where anyToInt(condition["stocks"]) > 0 {
                let price = anyToDouble(condition["price"])
                guard price > 0 else { continue }
                listings.append(Listing(
                    name: title,
                    url: base + "/view/\(expansionCode.lowercased())/\(productID)?utm_source=gishath",
                    img: img,
                    price: price,
                    isFoil: isFoil || Filters.isFoilText(title),
                    src: "Mox & Lotus",
                    quality: Filters.normaliseQuality(anyToString(condition["code"])),
                    extraInfo: expansion.isEmpty ? "" : "[\(expansion)]"))
            }
        }
        return listings
    }

    private static func moxImageURL(expansionCode: String, cardNumber: String, imagePath: Any?) -> String? {
        if let path = imagePath as? String,
           !path.trimmingCharacters(in: .whitespaces).isEmpty {
            return path.trimmingCharacters(in: .whitespaces)
        }
        guard let n = Int(cardNumber.trimmingCharacters(in: .whitespaces)) else { return nil }
        return "https://d3nmvyqkci0c2u.cloudfront.net/\(expansionCode.trimmingCharacters(in: .whitespaces))/"
            + String(format: "%03d", n) + ".png"
    }

    // MARK: - The TCG Marketplace (engine-src tcgmarketplace/search.go)

    static func tcgMarketplace(_ cardName: String) async throws -> [Listing] {
        var req = URLRequest(url: URL(string: "https://thetcgmarketplace.com:3501/product/advancedfilter")!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: [
            "category_id": 3,
            "name_exact_match": false,
            "available_only": true,
            "name": cardName,
            "order": "name_asc",
        ] as [String: Any])

        let root = try await fetchJSON(req) as? [String: Any] ?? [:]
        let outer = root["data"] as? [String: Any] ?? [:]
        let items = outer["data"] as? [[String: Any]] ?? []

        let searchLink = TCGSearchLink.buildSearchURL(cardName)

        var listings: [Listing] = []
        for item in items {
            let stock = anyToInt(item["available"])
            guard stock > 0 else { continue }
            let price = anyToDouble(item["from"])
            guard price > 0 else { continue }

            // Strip "[SET] " prefix from the listing name.
            var name = anyToString(item["name"]).trimmingCharacters(in: .whitespaces)
            if let idx = name.firstIndex(of: "]"), name.distance(from: name.startIndex, to: idx) > 1 {
                name = String(name[name.index(after: idx)...]).trimmingCharacters(in: .whitespaces)
            }

            let setName = anyToString(item["setname"])
            let extra = setName.isEmpty ? "" : "[\(setName)]"
            let img = anyToString(item["image"]).split(separator: " ").first.map(String.init) ?? ""

            listings.append(Listing(
                name: name,
                url: searchLink,
                img: img,
                price: price,
                isFoil: name.contains("Surge Foil") || extra.contains("Surge Foil") || Filters.isFoilText(name),
                src: Pricing.tcgMarketplaceName,
                quality: "",
                extraInfo: extra))
        }
        return listings
    }
}

/// Port of engine-src tcgmarketplace/searchlink.go: the storefront's /search
/// route wants each path segment RSA-OAEP(SHA1)-encrypted with the public key
/// from its JS bundle, base64'd, "/" replaced with "_". Any failure falls back
/// to the bare storefront (that was the pre-deep-link behaviour, so it never
/// regresses). SecKey handles the 1023-bit key that Go's stdlib refuses.
enum TCGSearchLink {
    static let storeBase = "https://thetcgmarketplace.com"
    private static let publicKeyPEM = """
        MIGeMA0GCSqGSIb3DQEBAQUAA4GMADCBiAKBgGlempQY/LwZbvzeYl76yMaH/onD
        /olkEmMC5rbms3BSAA/TbzPMEVjjXcKjFHcBlKC5KOAyqNF5z7VZc6hyM6GL8l4o
        bNBp6LWUmeZUWFm7rsLNXIm+Sv7IOw2z/1frbyKgWagqRstIkEnmqqsgDrLJc9OS
        t5FfOO99tterVzVlAgMBAAE=
        """

    static func buildSearchURL(_ searchStr: String) -> String {
        guard let key = loadKey(),
              let filter = encryptSegment(key, searchStr),
              let catid = encryptSegment(key, "3") else { return storeBase }
        return storeBase + "/search/" + filter + "/" + catid
    }

    private static func loadKey() -> SecKey? {
        guard let spki = Data(base64Encoded: publicKeyPEM.replacingOccurrences(of: "\n", with: "")),
              let pkcs1 = extractPKCS1(fromSPKI: spki) else { return nil }
        let attrs: [CFString: Any] = [
            kSecAttrKeyType: kSecAttrKeyTypeRSA,
            kSecAttrKeyClass: kSecAttrKeyClassPublic,
        ]
        return SecKeyCreateWithData(pkcs1 as CFData, attrs as CFDictionary, nil)
    }

    private static func encryptSegment(_ key: SecKey, _ plaintext: String) -> String? {
        guard let data = plaintext.data(using: .utf8),
              let encrypted = SecKeyCreateEncryptedData(
                key, .rsaEncryptionOAEPSHA1, data as CFData, nil) as Data? else { return nil }
        return encrypted.base64EncodedString().replacingOccurrences(of: "/", with: "_")
    }

    /// Minimal ASN.1 walk: SubjectPublicKeyInfo = SEQ { SEQ{alg}, BIT STRING { PKCS#1 } }.
    /// SecKeyCreateWithData wants the inner PKCS#1 RSAPublicKey blob.
    private static func extractPKCS1(fromSPKI der: Data) -> Data? {
        let bytes = [UInt8](der)
        var i = 0

        func readHeader() -> (tag: UInt8, length: Int)? {
            guard i + 2 <= bytes.count else { return nil }
            let tag = bytes[i]; i += 1
            var length = Int(bytes[i]); i += 1
            if length & 0x80 != 0 {
                let count = length & 0x7F
                guard count <= 4, i + count <= bytes.count else { return nil }
                length = 0
                for _ in 0..<count { length = (length << 8) | Int(bytes[i]); i += 1 }
            }
            return (tag, length)
        }

        guard let outer = readHeader(), outer.tag == 0x30 else { return nil }
        guard let alg = readHeader(), alg.tag == 0x30 else { return nil }
        i += alg.length // skip the AlgorithmIdentifier body
        guard let bits = readHeader(), bits.tag == 0x03, bits.length > 1 else { return nil }
        i += 1 // skip the BIT STRING's unused-bits byte
        guard i + bits.length - 1 <= bytes.count else { return nil }
        return Data(bytes[i..<(i + bits.length - 1)])
    }
}
