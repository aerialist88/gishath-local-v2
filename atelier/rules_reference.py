"""A compact, refreshable index over the official Magic rules text.

The full Comprehensive Rules are downloaded only into ignored local state.
Rehearsals receive small, named rule sections instead of a copy in a prompt or
the editable match guidebook. This keeps both version control and prompt
context focused while preserving exact, checkable citations.
"""
from __future__ import annotations

import html
import json
import re
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from deck_engine import config

RULES_PAGE_URL = "https://magic.wizards.com/en/rules"
RULES_PATH = config.STATE_DIR / "mtg_comprehensive_rules.txt"
META_PATH = config.STATE_DIR / "mtg_comprehensive_rules_meta.json"
# Wizards currently uses spaces in the document filename, so this deliberately
# stops at an HTML delimiter rather than whitespace.
_TXT_LINK_RE = re.compile(r"https?[^\"'<>]+MagicCompRules[^\"'<>]+\.txt", re.IGNORECASE)
_EFFECTIVE_RE = re.compile(r"effective as of ([A-Za-z]+ \d{1,2}, \d{4})", re.IGNORECASE)

# Kept deliberately small: enough to ground the bounded opening circuit and
# Commander-specific decisions. Complex interactions remain unresolved until a
# later, targeted rule slice is added. 603.3c/608.2 added 2026-07-10 after the
# first real rehearsal (c7edf53782de) had to punt a Selesnya Sanctuary bounce
# trigger as unresolvable — triggered-ability handling comes up on any
# ETB-trigger land, which is nearly every deck's turn 1.
_SECTIONS = (
    "103.1", "103.4", "103.5", "103.8", "104.2", "104.3", "117.1", "305.2",
    "601.2", "603.1", "603.2", "603.3", "608.2", "704.5",
    "903.1", "903.3", "903.4", "903.5", "903.6", "903.8", "903.9", "903.10",
)


def _ssl_context() -> ssl.SSLContext | None:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return None


def _get(url: str, timeout: float = 30) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": config.SCRYFALL_USER_AGENT, "Accept": "text/plain,text/html"})
    with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
        return response.read().decode("utf-8-sig", errors="replace")


def status() -> dict:
    meta: dict = {}
    try:
        meta = json.loads(META_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    return {"available": RULES_PATH.exists(), "rules_url": meta.get("rules_url", RULES_PAGE_URL),
            "effective_date": meta.get("effective_date"), "downloaded_utc": meta.get("downloaded_utc")}


def refresh() -> dict:
    """Download the official TXT reference and retain only a local cache."""
    page = html.unescape(_get(RULES_PAGE_URL))
    match = _TXT_LINK_RE.search(page)
    if not match:
        raise ValueError("Wizards' rules page did not expose a Comprehensive Rules TXT download.")
    rules_url = urllib.parse.quote(match.group(0), safe=":/?&=%")
    text = _get(rules_url, timeout=90)
    if "Magic: The Gathering Comprehensive Rules" not in text:
        raise ValueError("The downloaded Rules document was not recognized.")
    RULES_PATH.write_text(text)
    effective = _EFFECTIVE_RE.search(text)
    meta = {"rules_url": rules_url, "effective_date": effective.group(1) if effective else None,
            "downloaded_utc": datetime.now(timezone.utc).isoformat()}
    META_PATH.write_text(json.dumps(meta, indent=2))
    return status()


def _section(text: str, number: str) -> str:
    """Extract one numbered rule and its lettered subrules from the TXT file."""
    start = re.search(rf"(?<![\d.]){re.escape(number)}\.\s", text)
    if not start:
        return ""
    # A chapter heading (for example, "104. Ending the Game") can appear
    # before its first numbered rule, so stop at either form.
    next_rule = re.search(r"(?<![\d.])(?:\d{3}\.\d+\.\s|\d{3}\.\s+[A-Z])", text[start.end():])
    end = start.end() + next_rule.start() if next_rule else len(text)
    return " ".join(text[start.start():end].split())


def bundle() -> dict:
    """Return exact, low-volume citations required by the opening rehearsal."""
    if not RULES_PATH.exists():
        try:
            refresh()
        except Exception as exc:  # noqa: BLE001 — network/cache problem should be actionable in the UI
            raise ValueError("The official Magic rules cache is unavailable. Use Refresh official rules, then try again.") from exc
    text = RULES_PATH.read_text(errors="replace")
    sections = {f"CR {number}": value for number in _SECTIONS if (value := _section(text, number))}
    required = {"CR 103.8", "CR 903.6", "CR 903.10"}
    if not required.issubset(sections):
        raise ValueError("The local Comprehensive Rules cache is incomplete. Refresh official rules, then try again.")
    return {**status(), "sections": sections}
