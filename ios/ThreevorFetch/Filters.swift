import Foundation

/// Port of filters.py — the single source of truth for what counts as a real
/// MTG single, a name match, and how foil/quality labels are normalised.
/// Keep the keyword sets in sync with filters.py when stores change.
enum Filters {

    // MARK: - Name matching (filters.py _name_matches)

    /// True if resultName is a plausible match for cardName.
    /// 1. Fast path: cardName appears as a whole-word-bounded substring.
    /// 2. Slow path: every word-token of cardName appears as a whole word.
    static func nameMatches(cardName: String, resultName: String) -> Bool {
        let cn = cardName.lowercased()
        let rn = resultName.lowercased()

        if wholeWordContains(haystack: rn, needle: cn) { return true }

        let tokens = tokenize(cn)
        if tokens.isEmpty { return false }
        return tokens.allSatisfy { wholeWordContains(haystack: rn, needle: $0) }
    }

    private static func tokenize(_ s: String) -> [String] {
        // filters.py: re.findall(r"[a-z0-9']+", cn)
        var tokens: [String] = []
        var current = ""
        for ch in s {
            if ch.isLetter || ch.isNumber || ch == "'" {
                current.append(ch)
            } else if !current.isEmpty {
                tokens.append(current); current = ""
            }
        }
        if !current.isEmpty { tokens.append(current) }
        return tokens
    }

    private static func wholeWordContains(haystack: String, needle: String) -> Bool {
        let pattern = "\\b" + NSRegularExpression.escapedPattern(for: needle) + "\\b"
        guard let re = try? NSRegularExpression(pattern: pattern) else { return false }
        let range = NSRange(haystack.startIndex..., in: haystack)
        return re.firstMatch(in: haystack, range: range) != nil
    }

    // MARK: - Accessory / non-MTG keyword sets (filters.py)

    static let accessoryKeywords: Set<String> = [
        "sleeve", "deck box", "deckbox", "playmat", "play mat", "binder",
        "booster box", "booster pack", "life counter", "life pad",
        "dice tower", "card storage", "storage box",
        "prerelease kit", "prerelease pack",
    ]

    static let nonMTGNameKeywords: Set<String> = [
        "pokémon", "pokemon", "yu-gi-oh", "yugioh", "ygo",
        "one piece", "digimon", "dragon ball", "cardfight",
        "flesh and blood", "shadowverse", "weiß schwarz", "weiss schwarz",
        // "force of will" is intentionally NOT here — it's a real MTG card name.
        "grand archive", "lorcana", "union arena",
        "battle spirits", "my hero academia", "gundam",
    ]

    static let nonMTGSetKeywords: Set<String> = [
        // Pokémon
        "scarlet & violet", "sword & shield", "sun & moon", "black & white",
        "x & y", "diamond & pearl", "heartgold", "soulsilver",
        // Yu-Gi-Oh
        "phantom nightmare", "maze of memories", "battles of legend",
        "legacy of destruction", "age of overlord", "infinite forbidden",
        "terminal world", "duel overload",
        // One Piece
        "romance dawn", "paramount war", "pillars of strength",
        "kingdom of intrigue", "awakening of the new era", "wings of captain",
        // Digimon
        "digimon card", "release special",
        // Dragon Ball Super
        "galactic battle", "union force", "cross worlds",
        // Flesh and Blood
        "welcome to rathe", "arcane rising", "crucible of war",
        // Lorcana
        "the first chapter", "rise of the floodborn", "into the inklands",
        // Force of Will TCG (game name appears in the set field, not card names)
        "force of will:",
    ]

    static let mtgNonSingleKeywords: Set<String> = [
        "art card", "art series", "oversized", "double-faced token", "checklist card",
    ]

    static func isNonMTG(name: String, extraInfo: String) -> Bool {
        let n = name.lowercased()
        let e = extraInfo.lowercased()
        if nonMTGNameKeywords.contains(where: { n.contains($0) }) { return true }
        if nonMTGNameKeywords.contains(where: { e.contains($0) }) { return true }
        if nonMTGSetKeywords.contains(where: { e.contains($0) }) { return true }
        if mtgNonSingleKeywords.contains(where: { n.contains($0) }) { return true }
        if accessoryKeywords.contains(where: { n.contains($0) }) { return true }
        return false
    }

    static func isAccessory(name: String, extraInfo: String) -> Bool {
        let n = name.lowercased()
        let e = extraInfo.lowercased()
        return accessoryKeywords.contains(where: { n.contains($0) || e.contains($0) })
    }

    // MARK: - Quality / foil normalisation

    static let qualityMap: [String: String] = [
        "NM": "Near Mint", "NM/M": "Near Mint", "M": "Near Mint",
        "LP": "Lightly Played", "EX": "Lightly Played", "EX+": "Lightly Played",
        "EX/EX+": "Lightly Played",
        "MP": "Moderately Played", "VG": "Moderately Played",
        "HP": "Heavily Played", "PL": "Heavily Played",
        "DM": "Damaged", "D": "Damaged",
        // Spelled-out conditions arrive uppercased from the variant-1 JS
        // (Cards Citadel writes "Near Mint" in its add-to-cart buttons).
        "NEAR MINT": "Near Mint", "LIGHTLY PLAYED": "Lightly Played",
        "MODERATELY PLAYED": "Moderately Played",
        "HEAVILY PLAYED": "Heavily Played", "DAMAGED": "Damaged",
    ]

    static let foilKeywords = ["foil", "etched", "galaxy", "surge", "halo", "gilded"]

    static func normaliseQuality(_ raw: String) -> String {
        let key = raw.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        return qualityMap[key] ?? raw.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func isFoilText(_ title: String) -> Bool {
        let t = title.lowercased()
        return foilKeywords.contains(where: { t.contains($0) })
    }
}
