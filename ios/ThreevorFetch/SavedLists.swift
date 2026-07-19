import Foundation
import Observation

/// Saved buy lists, kept in UserDefaults. First launch seeds from
/// SeedLists.json — a snapshot of the Mac app's state/buy_lists.json — so the
/// phone starts with the same lists as the web UI.
@MainActor
@Observable
final class SavedLists {
    struct SavedList: Codable, Identifiable, Hashable {
        var name: String
        var cards: [String]
        var id: String { name }
    }

    private static let storageKey = "savedBuyLists.v1"
    private(set) var lists: [SavedList] = []

    init() {
        load()
    }

    private func load() {
        if let data = UserDefaults.standard.data(forKey: Self.storageKey),
           let decoded = try? JSONDecoder().decode([SavedList].self, from: data) {
            lists = decoded
            return
        }
        lists = Self.seedLists()
        persist()
    }

    private static func seedLists() -> [SavedList] {
        guard let url = Bundle.main.url(forResource: "SeedLists", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let raw = try? JSONSerialization.jsonObject(with: data) as? [String: [String: Any]]
        else { return [] }
        return raw.compactMap { name, entry in
            guard let cards = entry["cards"] as? [String], !cards.isEmpty else { return nil }
            return SavedList(name: name, cards: cards)
        }
        .sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
    }

    private func persist() {
        if let data = try? JSONEncoder().encode(lists) {
            UserDefaults.standard.set(data, forKey: Self.storageKey)
        }
    }

    func save(name: String, cards: [String]) {
        let trimmed = name.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty, !cards.isEmpty else { return }
        if let idx = lists.firstIndex(where: { $0.name == trimmed }) {
            lists[idx].cards = cards
        } else {
            lists.append(SavedList(name: trimmed, cards: cards))
            lists.sort { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
        }
        persist()
    }

    func delete(name: String) {
        lists.removeAll { $0.name == name }
        persist()
    }
}
