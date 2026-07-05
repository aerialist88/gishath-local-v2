"""
deck_engine/run_log.py — persistent record of past nightly runs.

Backs the dedupe rule in PRD §4c: don't repeat a commander within
DEDUPE_COMMANDER_DAYS, soft-avoid repeating an archetype within
DEDUPE_ARCHETYPE_SOFT_DAYS. Plain JSON, not SQLite — a nightly personal
project doesn't need concurrent-writer safety, and JSON is trivially
diffable/greppable if Trevor wants to inspect history by hand.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config


@dataclass
class RunRecord:
    """One completed (or attempted) nightly run."""
    timestamp: str                 # ISO 8601, UTC
    commander: str
    archetype: str
    status: str                    # "success" | "error"
    colors: list[str] = field(default_factory=list)
    error_summary: str = ""        # populated only when status == "error"

    @staticmethod
    def now(commander: str, archetype: str, status: str,
            colors: list[str] | None = None, error_summary: str = "") -> "RunRecord":
        return RunRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            commander=commander,
            archetype=archetype,
            status=status,
            colors=colors or [],
            error_summary=error_summary,
        )


def _load_raw(path: Path = config.RUN_LOG_PATH) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        # Corrupt/partial log must never block a run — start fresh but don't
        # silently destroy the old file, rename it aside for inspection.
        if path.exists():
            path.rename(path.with_suffix(".json.corrupt"))
        return []


def load_records(path: Path = config.RUN_LOG_PATH) -> list[RunRecord]:
    return [RunRecord(**r) for r in _load_raw(path)]


def append_record(record: RunRecord, path: Path = config.RUN_LOG_PATH) -> None:
    records = _load_raw(path)
    records.append(asdict(record))
    path.write_text(json.dumps(records, indent=2))


def _within_days(timestamp_iso: str, days: int) -> bool:
    ts = datetime.fromisoformat(timestamp_iso)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts) <= timedelta(days=days)


def is_commander_available(commander: str, path: Path = config.RUN_LOG_PATH,
                            window_days: int = config.DEDUPE_COMMANDER_DAYS) -> bool:
    """Hard rule: False if this commander appeared in a *successful* run within window_days."""
    name = commander.strip().lower()
    for r in load_records(path):
        if r.status == "success" and r.commander.strip().lower() == name and _within_days(r.timestamp, window_days):
            return False
    return True


def recent_archetypes(path: Path = config.RUN_LOG_PATH,
                       window_days: int = config.DEDUPE_ARCHETYPE_SOFT_DAYS) -> set[str]:
    """Soft rule: archetypes seen in successful runs within window_days — bias away from, don't hard-block."""
    return {
        r.archetype.strip().lower()
        for r in load_records(path)
        if r.status == "success" and r.archetype and _within_days(r.timestamp, window_days)
    }


def recent_commanders(path: Path = config.RUN_LOG_PATH,
                       window_days: int = config.DEDUPE_COMMANDER_DAYS) -> set[str]:
    """All commanders currently blocked by the hard dedupe window — useful for prompting the selector."""
    return {
        r.commander.strip().lower()
        for r in load_records(path)
        if r.status == "success" and _within_days(r.timestamp, window_days)
    }


def successful_run_count(path: Path = config.RUN_LOG_PATH) -> int:
    """Total successful runs ever logged — used as the newsletter issue number
    (PRD v4 amendment §3.3: subject 'EDH Nightly #N — ...')."""
    return sum(1 for r in load_records(path) if r.status == "success")


def recent_deck_lines(n: int = 7, path: Path = config.RUN_LOG_PATH) -> list[str]:
    """Most recent n successful decks as 'Commander — Archetype' strings, newest
    first — for the newsletter's "last 7 decks" one-liner list (PRD v4 amendment
    §3.3). Returns fewer than n if run history is shorter than that."""
    successes = [r for r in load_records(path) if r.status == "success" and r.commander]
    successes.sort(key=lambda r: r.timestamp, reverse=True)
    return [f"{r.commander} — {r.archetype}" if r.archetype else r.commander for r in successes[:n]]
