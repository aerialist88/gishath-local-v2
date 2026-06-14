"""
presentation.py — formats raw engine results into display rows for the UI and Excel export.

Design (per PRD v2 defaults):
  - All in-stock results returned by the engine are shown. The engine has already:
      * Filtered to inStock=true, price > 0, excluded art/Japanese cards.
      * Ranked by match quality (exact > prefix > partial), then price ASC.
  - The first TOP_N results per card receive a rank badge (#1–#5); the rest
    are flagged `hidden=True` for the frontend to collapse by default.
  - Foil badge and Quality column are both included in every row.
  - Cards with no results get a single placeholder row.
  - Store errors are returned separately (not mixed into result rows) so the
    UI can display them as summary chips.
"""
from __future__ import annotations

from filters import _is_accessory, _name_matches

TOP_N: int = 5  # rows that receive an explicit rank badge


def format_results(
    results_by_card: dict[str, dict],
    buy_list: list[str],
) -> list[dict]:
    """Flatten per-card engine results into an ordered list of display rows.

    Args:
        results_by_card: {card_name: {"cards": [...Card], "errors": [...StoreError]}}
        buy_list:        ordered list of input card names (preserves search order)

    Returns:
        List of row dicts with keys:
            card        str   — input card name (repeated per result row)
            rank        str   — "#1"–"#5", or "" for results beyond TOP_N
            rank_n      int   — numeric rank (0 for placeholder rows)
            src         str   — store name
            url         str   — direct listing URL (may be empty)
            name        str   — cleaned listing name from the engine
            extra_info  str   — set / printing / variant text
            foil        bool
            quality     str   — NM, LP, MP, HP, etc.
            price       str   — "SGD X.XX"
            price_val   float — raw price for sorting in Excel
            is_error    bool  — True for no-results placeholder rows
            hidden      bool  — True for results ranked > TOP_N (collapsed in UI)
    """
    rows: list[dict] = []

    for card_name in buy_list:
        entry = results_by_card.get(card_name, {"cards": [], "errors": []})
        cards: list[dict] = entry.get("cards", [])

        if not cards:
            rows.append(_no_results_row(card_name))
            continue

        # Drop accessories (sleeves, deck boxes, playmats, etc.) that slip through
        # when the card name appears in a product line name.
        cards = [c for c in cards if not _is_accessory(c)]
        if not cards:
            rows.append(_no_results_row(card_name))
            continue

        # Drop results whose name doesn't actually match the searched card.
        # The Go engine returns partial-match hits (e.g. "One Ring to Rule Them All"
        # for a "The One Ring" query). Whole-word matching filters these out.
        cards = [c for c in cards if _name_matches(card_name, c.get("name", ""))]
        if not cards:
            rows.append(_no_results_row(card_name))
            continue

        # Sort all results by price ascending before ranking.
        # The Go engine pre-sorts its own results, but Playwright results are
        # appended after, so we must re-sort the merged list here.
        cards = sorted(cards, key=lambda c: float(c.get("price", 0) or 0))

        for i, card in enumerate(cards):
            rank_n = i + 1
            rows.append({
                "card":       card_name,
                "rank":       f"#{rank_n}" if rank_n <= TOP_N else "",
                "rank_n":     rank_n,
                "src":        card.get("src", ""),
                "url":        card.get("url", ""),
                "name":       card.get("name", ""),
                "extra_info": card.get("extraInfo", ""),
                "foil":       bool(card.get("isFoil", False)),
                "quality":    card.get("quality", ""),
                "price":      _fmt_price(card.get("price", 0)),
                "price_val":  float(card.get("price", 0)),
                "is_error":   False,
                "hidden":     rank_n > TOP_N,
            })

    return rows


def _fmt_price(price: float | int) -> str:
    return f"SGD {float(price):.2f}"


def _no_results_row(card_name: str) -> dict:
    return {
        "card":       card_name,
        "rank":       "—",
        "rank_n":     0,
        "src":        "",
        "url":        "",
        "name":       "No results found",
        "extra_info": "",
        "foil":       False,
        "quality":    "",
        "price":      "—",
        "price_val":  0.0,
        "is_error":   True,
        "hidden":     False,
    }
