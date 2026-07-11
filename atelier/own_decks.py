"""atelier/own_decks.py — 3vor's real decks, pasted in by hand.

The gallery is otherwise populated only by the nightly pipeline's deliveries;
this module lets Trevor upload the decklists of the paper decks he actually
owns so they sit in their own section of the gallery and — the real point —
take seats in the Forge match simulator against the AI-forged ones.

A pasted list is resolved card-by-card against the local Scryfall cache (the
same source of truth the pipeline validates with), then saved to
config.OUTPUT_DIR as a normal `*_deck.json` record with `owner_deck: true`,
so archive/simulator/forge_engine all consume it with zero special-casing.
The record keeps the pipeline's card-row shape with pricing fields left null
(a paper deck the master already owns has no buy price to mind).

Accepted line shapes (Moxfield / Archidekt / MTGO-ish exports, plain lists):

    1 Sol Ring
    1x Sol Ring [Ramp]
    Sol Ring
    1 Sol Ring (C21) 250 *F*

Section headers (Deck:, Commander:, Sideboard:, Maybeboard:, About, ...) are
recognised; cards under Sideboard/Maybeboard/Tokens are ignored, and a card
under a Commander section (or tagged *CMDR*) overrides the form's commander
field. Basics with a quantity expand into one row per copy — that keeps both
Forge's .dck writer and the LLM referee's card-by-card dealing honest.

Owner decks are deliberately excluded from the home screen's "fresh from the
forge" shelf (server.py) and from the public static gallery bake (publish.py):
those two surfaces are about what the guild built, not what 3vor owns.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

from deck_engine import config, scryfall_cache

from . import archive

OWNER_LABEL = "3vor"

_HEADER_WORDS = {
    "deck", "decklist", "main", "mainboard", "commander", "commanders",
    "sideboard", "maybeboard", "considering", "tokens", "about", "companion",
}
_SKIP_SECTIONS = {"sideboard", "maybeboard", "considering", "tokens", "about"}
_QTY_RE = re.compile(r"^(\d+)\s*[xX]?\s+(\S.*)$")
_FLAG_RE = re.compile(r"\s*\*[^*]*\*\s*")                     # *F*, *CMDR*, *E*
_CATEGORY_RE = re.compile(r"\s*\[[^\]]*\]\s*$")               # Archidekt trailing [Category]
_SET_TAIL_RE = re.compile(r"\s*\([A-Za-z0-9]{2,6}\)(\s+[\w★†-]+)?\s*$")  # (C21) 250
_MANA_SYM_RE = re.compile(r"\{([^}]+)\}")


def _parse(text: str) -> list[tuple[str, int, bool]]:
    """Pasted decklist -> [(name, qty, is_commander_marked)], mainboard only."""
    section = "main"
    entries: list[tuple[str, int, bool]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "//")) or line.upper().startswith("SB:"):
            continue
        head = line.rstrip(":").strip().lower()
        if head in _HEADER_WORDS and not _QTY_RE.match(line):
            section = "commander" if head.startswith("commander") else head
            continue
        qty, name = 1, line
        if m := _QTY_RE.match(line):
            qty, name = int(m.group(1)), m.group(2)
        marked = bool(re.search(r"\*CMDR\*", name, re.I)) or bool(re.search(r"\[commander[^\]]*\]", name, re.I))
        name = _CATEGORY_RE.sub("", _FLAG_RE.sub(" ", name))
        name = _SET_TAIL_RE.sub("", name)
        name = re.sub(r"\s+", " ", name).strip()
        if not name or section in _SKIP_SECTIONS:
            continue
        entries.append((name, max(1, min(qty, 60)), marked or section == "commander"))
    return entries


def _lookup(cache: dict[str, dict], name: str) -> dict | None:
    """Case-insensitive cache lookup; the cache already indexes DFC front faces."""
    key = re.sub(r"\s+", " ", name).strip().lower().replace("’", "'")
    card = cache.get(key)
    if card is None and " // " in key:
        card = cache.get(key.split(" // ")[0].strip())
    return card


_ROLE_ORDER = [
    ("land", "Lands"), ("creature", "Creatures"), ("planeswalker", "Planeswalkers"),
    ("battle", "Battles"), ("instant", "Instants"), ("sorcery", "Sorceries"),
    ("artifact", "Artifacts"), ("enchantment", "Enchantments"),
]


def _role(type_line: str) -> str:
    tl = type_line.lower()
    for needle, label in _ROLE_ORDER:
        if needle in tl:
            return label
    return "Other"


def _row(card: dict, is_commander: bool) -> dict:
    type_line = str(card.get("type_line") or "")
    return {
        "name": card["name"], "is_commander": is_commander,
        "role": _role(type_line), "phase": "",
        "price_sgd": None, "store": None, "over_cap": False,
        "ck_price_usd": None, "ck_url": None,
        "cmc": card.get("cmc"), "type_line": type_line,
        "rarity": str(card.get("rarity") or ""),
    }


def _mana_cost(card: dict) -> str:
    if mc := str(card.get("mana_cost") or ""):
        return mc
    return "".join(str(f.get("mana_cost") or "") for f in card.get("card_faces") or [])


def _stats(rows: list[dict], cards: list[dict]) -> dict:
    curve: dict[str, int] = {}
    pips: dict[str, int] = {}
    roles: dict[str, int] = {}
    for row, card in zip(rows, cards):
        if "land" not in row["type_line"].lower() and row["cmc"] is not None:
            bucket = "6+" if row["cmc"] >= 6 else str(int(row["cmc"]))
            curve[bucket] = curve.get(bucket, 0) + 1
        roles[row["role"]] = roles.get(row["role"], 0) + 1
        for sym in _MANA_SYM_RE.findall(_mana_cost(card)):
            if sym == "C":
                pips["C"] = pips.get("C", 0) + 1
            else:
                for ch in sym:
                    if ch in "WUBRG":
                        pips[ch] = pips.get(ch, 0) + 1
    return {"curve": curve, "pips": pips, "role_counts": roles}


def save_deck(text: str, commander: str = "", label: str = "") -> dict:
    """Parse, resolve, and shelve one of 3vor's decks. Raises ValueError with a
    user-facing message on anything the form should show inline."""
    try:
        cache = scryfall_cache.load_cache()
    except FileNotFoundError:
        raise ValueError("The local Scryfall cache is missing — run "
                         "`python -m deck_engine.scryfall_cache --refresh` first.") from None

    entries = _parse(text)
    if not entries:
        raise ValueError("No cards found in the pasted list.")

    cmdr_name = next((n for n, _, marked in entries if marked), "") or commander.strip()
    if not cmdr_name:
        raise ValueError("Name the deck's commander — type it in the field, or mark it in "
                         "the paste (a 'Commander:' section or a *CMDR* tag).")
    cmdr_card = _lookup(cache, cmdr_name)
    if cmdr_card is None:
        raise ValueError(f"'{cmdr_name}' isn't in the local Scryfall cache — check the spelling.")
    canon_commander = cmdr_card["name"]

    rows = [_row(cmdr_card, True)]
    cards = [cmdr_card]
    unknown: list[str] = []
    for name, qty, _marked in entries:
        card = _lookup(cache, name)
        if card is None:
            unknown.append(name)
            continue
        if card["name"] == canon_commander:
            continue  # the commander already sits at the top of the list
        for _ in range(qty):  # basics expand to one row per copy — see module docstring
            rows.append(_row(card, False))
            cards.append(card)
    if unknown:
        raise ValueError("These cards aren't in the local Scryfall cache (check the spelling, "
                         "or refresh the cache): " + ", ".join(sorted(set(unknown))))
    if len(rows) < 20:
        raise ValueError(f"Only {len(rows)} cards parsed — that doesn't look like a full "
                         "Commander deck. Paste the whole list.")

    id8 = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%d_%H-%M")
    safe_commander = "".join(c if c.isalnum() or c in " -_" else "" for c in canon_commander).strip() or "Deck"
    stem = f"{timestamp}_{safe_commander}_{id8}"
    txt_path = config.OUTPUT_DIR / f"{stem}_moxfield.txt"
    json_path = config.OUTPUT_DIR / f"{stem}_deck.json"

    payload = {
        "schema": 1,
        "run_id": id8, "run_id8": id8,
        "generated_utc": now.isoformat(),
        "commander": canon_commander,
        "archetype": label.strip() or f"{OWNER_LABEL}'s own deck",
        "summary": f"One of {OWNER_LABEL}'s real decks, uploaded by hand — not a guild commission.",
        "colors": [c for c in "WUBRG" if c in (cmdr_card.get("color_identity") or [])],
        "bracket": "",
        "legal": True,   # a real deck 3vor plays — the pipeline's legality gate never saw it
        "synergy_gate_fired": False,
        "edhrec_pool_used": False,
        "retried": False, "retry_reason": "",
        "cards": rows,
        "price": {
            "available": False, "total_sgd": None,
            "priced_count": 0, "unpriced_count": len(rows),
            "top_expensive": [], "per_card_cap_sgd": config.MAX_CARD_PRICE_SGD,
            "over_budget": [], "swaps_made": 0,
        },
        "gameplan": {},
        "stats": _stats(rows, cards),
        "spend": {},
        "files": {"xlsx": None, "moxfield_txt": txt_path.name},
        "owner_deck": True,
        "owner": OWNER_LABEL,
        "source": "import",
    }
    txt_path.write_text("\n".join(f"1 {r['name']}" for r in rows) + "\n")
    json_path.write_text(json.dumps(payload, indent=1))
    return {"id": id8, "commander": canon_commander, "count": len(rows),
            "archetype": payload["archetype"]}


def delete_deck(id8: str) -> dict:
    """Remove one of 3vor's uploaded decks. Guild commissions are untouchable
    from this path — only records marked owner_deck may be deleted."""
    rec = archive._scan().get(id8)  # noqa: SLF001 — same-package helper
    if not rec or not rec.get("deck_json"):
        raise LookupError("No deck with that id.")
    try:
        deck = json.loads(rec["deck_json"].read_text())
    except (OSError, json.JSONDecodeError):
        raise LookupError("That deck's record is unreadable.") from None
    if not deck.get("owner_deck"):
        raise PermissionError("Only 3vor's own uploaded decks can be removed — "
                              "the guild's commissions stay.")
    for key in ("deck_json", "txt", "xlsx"):
        path = rec.get(key)
        if path:
            path.unlink(missing_ok=True)
    return {"ok": True, "id": id8}
