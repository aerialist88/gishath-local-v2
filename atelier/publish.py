"""atelier/publish.py — bake the gallery into a self-contained static site.

The live Atelier (atelier/server.py) binds 127.0.0.1 and carries write controls
(commission, guild rules). To share "just the decks" with friends we don't
expose that app — we render a read-only snapshot of the gallery to a folder of
plain files that any static host (Cloudflare Pages, GitHub Pages, Netlify, or
even `python -m http.server`) can serve. No backend, no machine uptime.

    python -m atelier.publish                 # -> gallery_site/
    python -m atelier.publish --out /tmp/g    # somewhere else

What lands in the folder:
    index.html                standalone gallery + deck viewer (hash-routed)
    gallery.css / gallery.js   the Atelier's look, adapted to read bundled data
    px/*.png                   the mascot sprites the css references
    data/decks.json            every deck record, cost/diagnostics stripped
    data/art.json              card-name -> Scryfall image URIs (hotlinked)
    files/<id8>.txt|.xlsx      the downloadable Moxfield list + workbook

Diagnostics are stripped on purpose. The Atelier's courier principle —
"friends receive the deck and the tale, never the cost sheet" — applies here
too: the per-run API cost/turns/token `spend` block never leaves the machine.
Card prices (SGD, CK reference) stay; those are the deck's value, not ours.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from deck_engine import config

from . import archive, art

TEMPLATE_DIR = Path(__file__).resolve().parent / "gallery_static"
CSS_SRC = Path(__file__).resolve().parent / "static" / "atelier.css"
PX_SRC = Path(__file__).resolve().parent / "static" / "px"
DEFAULT_OUT = config.REPO_ROOT / "gallery_site"


def _card_names(deck: dict) -> list[str]:
    return [c.get("name", "") for c in deck.get("cards", []) if c.get("name")]


def _public_deck(deck: dict) -> dict:
    """A copy safe for a public page: drop the engine's cost/diagnostics."""
    out = dict(deck)
    out.pop("spend", None)  # API cost / turns / tokens — never leaves the machine
    return out


def build_site(dest: Path) -> dict:
    """Render the whole gallery into `dest`. Returns a small summary dict."""
    dest = dest.resolve()
    data_dir = dest / "data"
    files_dir = dest / "files"
    for d in (dest, data_dir, files_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Force the art index to build synchronously (the server does it lazily on
    # a thread; here we need it right now to bake the subset we reference).
    art._build_index()  # noqa: SLF001 — intentional synchronous warm-up

    decks: list[dict] = []
    art_index: dict[str, dict] = {}
    for summary in archive.list_decks():
        if summary.get("owner_deck"):
            continue  # 3vor's own uploads stay off the public site — it shows what the guild built
        deck = archive.get_deck(summary["id"])
        if deck is None:
            continue
        deck = _public_deck(deck)
        id8 = deck.get("run_id8") or summary["id"]

        # Copy the downloadable artifacts under clean, id-based names and point
        # the record at those relative paths (the live app served them via API).
        files_out: dict[str, str] = {}
        txt = archive.file_path(id8, "txt")
        if txt and txt.exists():
            shutil.copyfile(txt, files_dir / f"{id8}.txt")
            files_out["moxfield_txt"] = f"files/{id8}.txt"
        xlsx = archive.file_path(id8, "xlsx")
        if xlsx and xlsx.exists():
            shutil.copyfile(xlsx, files_dir / f"{id8}.xlsx")
            files_out["xlsx"] = f"files/{id8}.xlsx"
        deck["files"] = files_out

        for name in [deck.get("commander", "")] + _card_names(deck):
            key = name.strip().lower()
            if key and key not in art_index:
                entry = art.lookup(name)
                if entry:
                    art_index[key] = entry

        decks.append(deck)

    (data_dir / "decks.json").write_text(json.dumps(decks, ensure_ascii=False))
    (data_dir / "art.json").write_text(json.dumps(art_index, ensure_ascii=False))

    # Static shell: templates verbatim, css with its /static/px/ refs made
    # relative, and the sprite folder alongside.
    shutil.copyfile(TEMPLATE_DIR / "index.html", dest / "index.html")
    shutil.copyfile(TEMPLATE_DIR / "gallery.js", dest / "gallery.js")
    css = CSS_SRC.read_text().replace("/static/px/", "px/")
    (dest / "gallery.css").write_text(css)
    px_out = dest / "px"
    if px_out.exists():
        shutil.rmtree(px_out)
    shutil.copytree(PX_SRC, px_out)

    return {"decks": len(decks), "art": len(art_index), "out": str(dest)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Bake the Atelier gallery into a static site.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output folder (default: {DEFAULT_OUT})")
    args = ap.parse_args()
    summary = build_site(args.out)
    print(f"Gallery published: {summary['decks']} decks, {summary['art']} card images -> {summary['out']}")


if __name__ == "__main__":
    main()
