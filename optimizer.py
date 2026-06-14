"""
optimizer.py — shopping plan optimizer for Gishath Fetch v2.

Given the raw results_by_card dict (same structure used by presentation.py),
computes three shopping strategies and returns a ShoppingPlan.

Strategies
----------
  A — Cheapest Each:
      For every card, pick the absolute cheapest listing regardless of store.
      Maximum savings; potentially one store trip per card.

  B — Best Shop (greedy set cover):
      Greedy consolidation: pick the store that covers the most remaining
      cards, assign those, repeat for the remainder.  Minimises store trips
      at the cost of paying the non-cheapest price at the consolidating store.

  C — Best Shop with tolerance:
      Same greedy approach, but a card is only consolidated to a non-cheapest
      store if that store's price is within tolerance of the global cheapest:

          tolerance = min(cheapest_price * TOLERANCE_PCT, TOLERANCE_ABS_MAX)

      Examples (with defaults 20% / SGD 2.00):
          $2.50 card  → tol = min($0.50, $2.00) = $0.50  (accept up to $3.00)
          $10.00 card → tol = min($2.00, $2.00) = $2.00  (accept up to $12.00)
          $85.00 card → tol = min($17.00, $2.00) = $2.00 (accept up to $87.00)

      Cards whose cheapest store falls outside tolerance are not consolidated;
      they are assigned to their individual cheapest source instead.

Data flow
---------
  /search  → format_results() → frontend stores display rows
  /download → frontend sends rows back → rows_to_results() reconstructs
              a results_by_card-compatible dict → compute_plan() runs
              the three strategies → write_excel() receives ShoppingPlan
"""
from __future__ import annotations

from dataclasses import dataclass, field

from filters import _is_accessory

# ── Tolerance constants for Strategy C ───────────────────────────────────────
TOLERANCE_PCT     = 0.20   # max 20% above cheapest
TOLERANCE_ABS_MAX = 2.00   # … but never more than SGD 2.00 in absolute terms


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class CardAssignment:
    """A single card assigned to a store within a strategy."""
    card:           str
    store:          str
    price:          float
    cheapest_price: float   # global cheapest for this card (across all stores)
    url:            str
    name:           str
    quality:        str
    foil:           bool
    extra_info:     str

    @property
    def premium(self) -> float:
        """How much more than the global cheapest we're paying (>= 0)."""
        return max(0.0, round(self.price - self.cheapest_price, 2))

    @property
    def is_cheapest(self) -> bool:
        """True if this is (tied for) the global cheapest listing."""
        return self.premium < 0.005   # float tolerance


@dataclass
class StoreGroup:
    """All cards assigned to one store within a strategy."""
    store:       str
    assignments: list[CardAssignment] = field(default_factory=list)

    @property
    def total(self) -> float:
        return round(sum(a.price for a in self.assignments), 2)


@dataclass
class Strategy:
    """One shopping strategy — a label, grouped buy list, and summary stats."""
    label:       str
    description: str
    groups:      list[StoreGroup] = field(default_factory=list)
    not_found:   list[str]        = field(default_factory=list)

    @property
    def total_cost(self) -> float:
        return round(sum(g.total for g in self.groups), 2)

    @property
    def store_count(self) -> int:
        return len(self.groups)

    @property
    def cards_found(self) -> int:
        return sum(len(g.assignments) for g in self.groups)

    @property
    def all_assignments(self) -> list[CardAssignment]:
        return [a for g in self.groups for a in g.assignments]


@dataclass
class ShoppingPlan:
    """Container for all three strategies."""
    strategy_a:           Strategy
    strategy_b:           Strategy
    strategy_c:           Strategy
    total_cards_searched: int

    @property
    def min_possible_cost(self) -> float:
        """Theoretical minimum — sum of cheapest listing per found card."""
        return self.strategy_a.total_cost


# ── Public helpers ─────────────────────────────────────────────────────────────

def rows_to_results(rows: list[dict]) -> dict[str, dict]:
    """Reconstruct a results_by_card dict from the display rows sent by the frontend.

    The /download endpoint receives the same pre-formatted display rows that
    were returned by /search (top-5 per card, hidden rows excluded by the
    caller before they are sent here).  This function inverts format_results()
    back into the raw dict that compute_plan() expects, so we don't need to
    re-run the scrapers.

    Fields reconstructed (keys match what the Go engine / Playwright return):
        src, url, name, extraInfo, isFoil, quality, price
    """
    results: dict[str, dict] = {}
    for row in rows:
        if row.get("is_error"):
            continue
        card = row.get("card", "")
        if not card:
            continue
        if card not in results:
            results[card] = {"cards": [], "errors": []}
        results[card]["cards"].append({
            "src":       row.get("src", ""),
            "url":       row.get("url", ""),
            "name":      row.get("name", ""),
            "extraInfo": row.get("extra_info", ""),
            "isFoil":    bool(row.get("foil", False)),
            "quality":   row.get("quality", ""),
            "price":     float(row.get("price_val", 0)),
        })
    return results


def compute_plan(results_by_card: dict[str, dict], buy_list: list[str]) -> ShoppingPlan:
    """Compute the three shopping strategies from raw results.

    Args:
        results_by_card: {card_name: {"cards": [...], "errors": [...]}}
                         Use rows_to_results() to build this from display rows.
        buy_list:        Ordered list of card names (preserves search order).

    Returns:
        ShoppingPlan with strategy_a, strategy_b, strategy_c populated.
    """
    # ── Step 1: Build per-card option lists (sorted by price ASC) ────────────
    options_by_card: dict[str, list[dict]] = {}
    not_found: list[str] = []

    for card_name in buy_list:
        entry = results_by_card.get(card_name, {"cards": [], "errors": []})
        cards = [c for c in entry.get("cards", []) if not _is_accessory(c)]
        if not cards:
            not_found.append(card_name)
            continue
        options_by_card[card_name] = sorted(
            cards, key=lambda c: float(c.get("price", 0) or 0)
        )

    # ── Step 2: Build cheapest-at-store index ─────────────────────────────────
    # cheapest_at_store[store][card] = cheapest listing for that card at that store
    cheapest_at_store: dict[str, dict[str, dict]] = {}
    for card_name, listings in options_by_card.items():
        for listing in listings:
            store = listing.get("src", "")
            if not store:
                continue
            if store not in cheapest_at_store:
                cheapest_at_store[store] = {}
            existing = cheapest_at_store[store].get(card_name)
            if existing is None or float(listing.get("price", 0)) < float(existing.get("price", 0)):
                cheapest_at_store[store][card_name] = listing

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _cheapest_price(card_name: str) -> float:
        listings = options_by_card.get(card_name, [])
        return float(listings[0].get("price", 0)) if listings else 0.0

    def _to_assignment(card_name: str, listing: dict) -> CardAssignment:
        return CardAssignment(
            card=card_name,
            store=listing.get("src", ""),
            price=float(listing.get("price", 0)),
            cheapest_price=_cheapest_price(card_name),
            url=listing.get("url", ""),
            name=listing.get("name", ""),
            quality=listing.get("quality", ""),
            foil=bool(listing.get("isFoil", False)),
            extra_info=listing.get("extraInfo", ""),
        )

    def _groups_from(assigned: dict[str, CardAssignment]) -> list[StoreGroup]:
        """Pack a {card → assignment} dict into sorted StoreGroups."""
        store_map: dict[str, StoreGroup] = {}
        for card_name, assignment in assigned.items():
            s = assignment.store
            if s not in store_map:
                store_map[s] = StoreGroup(store=s)
            store_map[s].assignments.append(assignment)
        groups = sorted(store_map.values(), key=lambda g: (-len(g.assignments), g.store))
        for g in groups:
            g.assignments.sort(key=lambda a: a.card)
        return groups

    # ── Strategy A: Cheapest Each ─────────────────────────────────────────────

    def _build_strategy_a() -> Strategy:
        assigned = {
            card_name: _to_assignment(card_name, listings[0])
            for card_name, listings in options_by_card.items()
        }
        return Strategy(
            label="A: Cheapest Each",
            description=(
                "Absolute cheapest listing for every card, regardless of store. "
                "Maximum savings — minimum total spend."
            ),
            groups=_groups_from(assigned),
            not_found=list(not_found),
        )

    # ── Greedy set cover (shared by B and C) ──────────────────────────────────

    def _greedy(tolerance_fn=None) -> list[StoreGroup]:
        """
        Greedy set cover over remaining cards.

        tolerance_fn(card_name, store_price) → bool
            Return True if we're willing to pay store_price for this card
            when consolidating to this store.  None means always accept.
        """
        remaining: set[str] = set(options_by_card.keys())
        assigned: dict[str, CardAssignment] = {}

        while remaining:
            best_store: str | None = None
            best_covered: list[str] = []
            best_total = float("inf")

            for store, store_listings in cheapest_at_store.items():
                covered = []
                for card_name in remaining:
                    if card_name not in store_listings:
                        continue
                    store_price = float(store_listings[card_name].get("price", 0))
                    if tolerance_fn is None or tolerance_fn(card_name, store_price):
                        covered.append(card_name)

                if not covered:
                    continue

                total = sum(
                    float(store_listings[c].get("price", 0)) for c in covered
                )
                # Primary sort: most coverage. Tie-break: lowest total cost.
                if len(covered) > len(best_covered) or (
                    len(covered) == len(best_covered) and total < best_total
                ):
                    best_store = store
                    best_covered = covered
                    best_total = total

            if best_store is None:
                # Tolerance is too tight for any remaining card → fall back to
                # cheapest-each for whatever is left.
                for card_name in remaining:
                    listings = options_by_card.get(card_name, [])
                    if listings:
                        assigned[card_name] = _to_assignment(card_name, listings[0])
                break

            for card_name in best_covered:
                listing = cheapest_at_store[best_store][card_name]
                assigned[card_name] = _to_assignment(card_name, listing)
            remaining -= set(best_covered)

        return _groups_from(assigned)

    # ── Strategy B: Best Shop (no tolerance) ─────────────────────────────────

    def _build_strategy_b() -> Strategy:
        return Strategy(
            label="B: Best Shop",
            description=(
                "Greedy store consolidation — buy as many cards as possible from "
                "one shop, supplement the rest from the next best, and so on. "
                "Fewest store trips; may pay above cheapest for some cards."
            ),
            groups=_greedy(tolerance_fn=None),
            not_found=list(not_found),
        )

    # ── Strategy C: Best Shop with price tolerance ────────────────────────────

    def _build_strategy_c() -> Strategy:
        def _within_tolerance(card_name: str, store_price: float) -> bool:
            cp = _cheapest_price(card_name)
            if cp == 0:
                return True
            tol = min(cp * TOLERANCE_PCT, TOLERANCE_ABS_MAX)
            return store_price <= cp + tol

        pct  = int(TOLERANCE_PCT * 100)
        desc = (
            f"Consolidate only when the store's price is within "
            f"{pct}% or SGD {TOLERANCE_ABS_MAX:.2f} of the cheapest listing "
            f"(whichever is smaller). Cards that exceed the tolerance are "
            f"purchased from their individual cheapest source."
        )
        return Strategy(
            label=f"C: Best Shop (≤{pct}% / SGD {TOLERANCE_ABS_MAX:.0f} tol.)",
            description=desc,
            groups=_greedy(tolerance_fn=_within_tolerance),
            not_found=list(not_found),
        )

    return ShoppingPlan(
        strategy_a=_build_strategy_a(),
        strategy_b=_build_strategy_b(),
        strategy_c=_build_strategy_c(),
        total_cards_searched=len(buy_list),
    )
