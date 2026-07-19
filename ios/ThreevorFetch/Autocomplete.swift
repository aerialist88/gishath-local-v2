import Foundation
import Observation

/// Card-name suggestions for the buy-list editor. The Mac app serves these
/// from a local Scryfall index; on the phone we hit Scryfall's public
/// autocomplete API directly (free, no key, ~50ms). Debounced, and stale
/// responses are discarded. Suggestions are a nicety — any failure just
/// means an empty list, never an error.
@MainActor
@Observable
final class Autocomplete {
    private(set) var suggestions: [String] = []
    private var task: Task<Void, Never>?

    /// TextEditor doesn't expose the cursor, so we complete the LAST line —
    /// the one being typed in the usual add-cards-at-the-end flow.
    func update(for text: String) {
        task?.cancel()
        let line = text.components(separatedBy: "\n").last?
            .trimmingCharacters(in: .whitespaces) ?? ""
        guard line.count >= 3 else {
            suggestions = []
            return
        }
        task = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 250_000_000)
            guard !Task.isCancelled else { return }

            var comps = URLComponents(string: "https://api.scryfall.com/cards/autocomplete")!
            comps.queryItems = [URLQueryItem(name: "q", value: line)]
            var req = URLRequest(url: comps.url!)
            req.setValue("3vorFetch/1.0", forHTTPHeaderField: "User-Agent")
            req.setValue("application/json", forHTTPHeaderField: "Accept")

            guard let (data, _) = try? await URLSession.shared.data(for: req),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let names = obj["data"] as? [String],
                  !Task.isCancelled else { return }

            // Already an exact, fully-typed name → nothing useful to suggest.
            if names.count == 1, names[0].caseInsensitiveCompare(line) == .orderedSame {
                self?.suggestions = []
            } else {
                self?.suggestions = Array(names.prefix(8))
            }
        }
    }

    func clear() {
        task?.cancel()
        suggestions = []
    }
}
