import SwiftUI

struct ContentView: View {
    @State private var model = SearchModel()
    @State private var savedLists = SavedLists()
    @State private var autocomplete = Autocomplete()
    @State private var showSaveDialog = false
    @State private var newListName = ""
    @State private var expandedCards: Set<String> = []
    @FocusState private var editorFocused: Bool
    @Environment(\.openURL) private var openURL

    var body: some View {
        NavigationStack {
            List {
                inputSection
                if model.isSearching { progressSection }
                if model.hasSearched && !model.isSearching { statsSection }
                if !model.storeErrors.isEmpty && !model.isSearching { errorSection }
                if !model.results.isEmpty { resultSections }
            }
            .listStyle(.insetGrouped)
            .navigationTitle("🦕 3vor Fetch")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if !model.results.isEmpty, let csv = csvFileURL() {
                    ShareLink(item: csv) { Image(systemName: "square.and.arrow.up") }
                }
            }
            .alert("Save buy list", isPresented: $showSaveDialog) {
                TextField("List name", text: $newListName)
                Button("Save") {
                    savedLists.save(name: newListName, cards: model.parsedBuyList().valid)
                    newListName = ""
                }
                Button("Cancel", role: .cancel) { newListName = "" }
            } message: {
                Text("Saves the current buy list on this phone.")
            }
        }
    }

    // MARK: - Input

    private var inputSection: some View {
        Section {
            VStack(alignment: .leading, spacing: 8) {
                Text("CARD BUY LIST")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                + Text("  (one name per line, min 3 characters)")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)

                ZStack(alignment: .topLeading) {
                    if model.buyListText.isEmpty {
                        Text("Ancient Copper Dragon\nGishath, Sun's Avatar\nThe One Ring")
                            .foregroundStyle(.tertiary)
                            .padding(.top, 8)
                            .padding(.leading, 5)
                    }
                    TextEditor(text: $model.buyListText)
                        .frame(minHeight: 120)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .font(.body.monospaced())
                        .focused($editorFocused)
                        .onChange(of: model.buyListText) {
                            autocomplete.update(for: model.buyListText)
                        }
                }

                if editorFocused && !autocomplete.suggestions.isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 6) {
                            ForEach(autocomplete.suggestions, id: \.self) { name in
                                Button(name) { applySuggestion(name) }
                                    .font(.caption)
                                    .buttonStyle(.bordered)
                                    .buttonBorderShape(.capsule)
                            }
                        }
                    }
                }

                HStack {
                    Button {
                        editorFocused = false   // drop the keyboard before results arrive
                        autocomplete.clear()
                        model.search()
                    } label: {
                        Label(model.isSearching ? "Searching…" : "Search",
                              systemImage: "magnifyingglass")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.isSearching || model.parsedBuyList().valid.isEmpty)

                    Button {
                        model.buyListText = ""
                        autocomplete.clear()
                    } label: {
                        Label("Clear", systemImage: "xmark")
                    }
                    .buttonStyle(.bordered)
                    .disabled(model.isSearching)
                }

                HStack {
                    Menu {
                        ForEach(savedLists.lists) { list in
                            Button("\(list.name) (\(list.cards.count))") {
                                model.buyListText = list.cards.joined(separator: "\n")
                            }
                        }
                        if !savedLists.lists.isEmpty {
                            Divider()
                            Menu("Delete a list…") {
                                ForEach(savedLists.lists) { list in
                                    Button(list.name, role: .destructive) {
                                        savedLists.delete(name: list.name)
                                    }
                                }
                            }
                        }
                    } label: {
                        Label("Saved lists", systemImage: "list.star")
                    }
                    Spacer()
                    Button {
                        showSaveDialog = true
                    } label: {
                        Label("Save", systemImage: "square.and.arrow.down")
                    }
                    .disabled(model.parsedBuyList().valid.isEmpty)
                }
                .font(.subheadline)

                if !model.skipped.isEmpty {
                    Text("Skipped (too short): \(model.skipped.joined(separator: ", "))")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
            }
            .padding(.vertical, 4)
        }
    }

    /// Card Kingdom (US) reference banner — mirrors the web UI's CK chip.
    private func ckReferenceRow(_ ck: CKReference) -> some View {
        HStack(spacing: 6) {
            Text("CK (US)")
                .font(.caption2.weight(.bold))
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(Color.brown.opacity(0.25), in: Capsule())
                .foregroundStyle(.brown)
            Text("from $\(String(format: "%.2f", ck.priceUsd))")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.orange)
            Text("· \(ck.edition)\(ck.isFoil ? " · Foil" : "")")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer()
            Image(systemName: "arrow.up.right.square")
                .font(.caption)
                .foregroundStyle(.tertiary)
        }
        .contentShape(Rectangle())
        .onTapGesture {
            if let url = URL(string: ck.url), !ck.url.isEmpty { openURL(url) }
        }
    }

    /// Replace the line being typed (the last one) with the tapped suggestion.
    private func applySuggestion(_ name: String) {
        var lines = model.buyListText.components(separatedBy: "\n")
        if lines.isEmpty {
            lines = [name]
        } else {
            lines[lines.count - 1] = name
        }
        model.buyListText = lines.joined(separator: "\n") + "\n"
        autocomplete.clear()
    }

    // MARK: - Progress / stats / errors

    private var progressSection: some View {
        Section {
            VStack(alignment: .leading, spacing: 6) {
                ProgressView(value: Double(model.completedUnits),
                             total: Double(max(model.totalUnits, 1)))
                Text("Querying all stores — \(model.completedUnits)/\(model.totalUnits) lookups done")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(.vertical, 4)
        }
    }

    private var statsSection: some View {
        Section {
            HStack(spacing: 12) {
                statChip("⏱", String(format: "%.1fs", model.elapsed))
                statChip("🃏", "\(model.results.count) cards")
                statChip("📋", "\(model.totalListings) listings")
            }
        }
    }

    private func statChip(_ emoji: String, _ text: String) -> some View {
        HStack(spacing: 4) {
            Text(emoji)
            Text(text).font(.caption.weight(.medium))
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(.quaternary, in: Capsule())
    }

    private var errorSection: some View {
        Section("Store errors") {
            ForEach(model.storeErrors) { err in
                HStack {
                    Image(systemName: "exclamationmark.triangle")
                        .foregroundStyle(.red)
                    Text("\(err.store): \(err.error)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    // MARK: - Results

    private var resultSections: some View {
        ForEach(model.results) { result in
            Section {
                if let ck = CKPrices.lookup(result.card) {
                    ckReferenceRow(ck)
                }
                if result.listings.isEmpty {
                    Text("No listings found")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    // Display cap: top 5 always, next 5 behind "Show more"
                    // (10 max on screen). Everything is still searched and
                    // kept — the CSV export contains the full list; a phone
                    // screen just never benefits from listing #40 of a
                    // price-sorted result.
                    let expanded = expandedCards.contains(result.card)
                    let cap = expanded ? SearchModel.topN * 2 : SearchModel.topN
                    let visible = Array(result.listings.prefix(cap))

                    ForEach(Array(visible.enumerated()), id: \.element.id) { index, listing in
                        ListingRow(rank: index + 1, listing: listing)
                            .contentShape(Rectangle())
                            .onTapGesture {
                                if let url = URL(string: listing.url), !listing.url.isEmpty {
                                    openURL(url)
                                }
                            }
                    }

                    if result.listings.count > SearchModel.topN {
                        Button {
                            if expanded { expandedCards.remove(result.card) }
                            else { expandedCards.insert(result.card) }
                        } label: {
                            Text(expanded
                                 ? "Show top \(SearchModel.topN) only"
                                 : "Show next \(min(SearchModel.topN, result.listings.count - SearchModel.topN))")
                                .font(.caption)
                        }
                    }
                }
            } header: {
                Text(result.card)
            }
        }
    }

    // MARK: - CSV export (replaces the web UI's Excel download)

    private func csvFileURL() -> URL? {
        var csv = "Card,Rank,Store,Listing Name,Set / Printing,Foil,Quality,Price (SGD)\n"
        for result in model.results {
            for (index, l) in result.listings.enumerated() {
                let fields = [result.card, "#\(index + 1)", l.src, l.name, l.extraInfo,
                              l.isFoil ? "Foil" : "", l.quality, String(format: "%.2f", l.price)]
                csv += fields.map { "\"" + $0.replacingOccurrences(of: "\"", with: "\"\"") + "\"" }
                    .joined(separator: ",") + "\n"
            }
        }
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("3vor-fetch-results.csv")
        do {
            try csv.write(to: url, atomically: true, encoding: .utf8)
            return url
        } catch {
            return nil
        }
    }
}

struct ListingRow: View {
    let rank: Int
    let listing: Listing

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Text("#\(rank)")
                .font(.caption.weight(.bold))
                .foregroundStyle(rank == 1 ? Color.yellow : (rank <= SearchModel.topN ? Color.orange : Color.secondary))
                .frame(width: 28, alignment: .leading)

            VStack(alignment: .leading, spacing: 2) {
                Text(listing.name)
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(2)
                HStack(spacing: 6) {
                    Text(listing.src)
                        .font(.caption)
                        .foregroundStyle(.indigo)
                    if listing.isFoil {
                        Text("✨ Foil").font(.caption2)
                    }
                    if !listing.quality.isEmpty {
                        Text(listing.quality)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
                if !listing.extraInfo.isEmpty {
                    Text(listing.extraInfo)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
            }

            Spacer()

            Text(Pricing.sgd(listing.price))
                .font(.subheadline.weight(.bold))
                .foregroundStyle(rank <= SearchModel.topN ? Color.green : Color.primary)
        }
        .padding(.vertical, 2)
    }
}

#Preview {
    ContentView()
}
