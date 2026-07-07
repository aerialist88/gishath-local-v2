"""Ad-hoc check (not wired into any CI) that LiveView survives real concurrent
updates the way _draft()'s ThreadPoolExecutor(max_workers=3) actually drives
claude_cli.run() — 3 threads all calling start_call()/append_text()/finish()
on the same LiveView at once. Uses fake_claude.py to replay the real captured
transcript 3x in parallel rather than guessing at timing. Run manually:

    cd gishath-local-v2 && python3 -m deck_engine.tests.test_live_view_concurrency
"""
from __future__ import annotations

import concurrent.futures
import sys
import uuid
from pathlib import Path

from .. import claude_cli, config, live_view

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"


def main() -> int:
    # fake_claude.py is chmod +x with a #!/usr/bin/env python3 shebang and ignores
    # all argv, so pointing CLAUDE_BIN straight at it is enough.
    config.CLAUDE_BIN = str(FAKE_CLAUDE)

    view = live_view.LiveView()
    claude_cli.set_live_view(view)

    # A fresh id per run, not a fixed literal — spend_log.jsonl is never
    # truncated between runs (see deck_engine/README's pre-flight checklist),
    # so a hardcoded run_id would accumulate cost across every invocation of
    # this test ever made and eventually trip a real crucible cap (confirmed
    # 2026-07-05: exactly this happened once a non-zero cap was configured).
    run_id = str(uuid.uuid4())
    errors: list[str] = []

    def _one(i: int) -> None:
        try:
            result = claude_cli.run(
                f"fake draft prompt {i}", run_id=run_id, stage=f"draft/attempt1/{i + 1}",
                model_tier_key="draft", json_schema={"type": "object"},
            )
            assert result.text or result.raw.get("structured_output") is not None, (
                f"pane {i}: got no text and no structured_output"
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pane {i}: {exc}")

    try:
        with view:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(_one, range(3)))
    finally:
        claude_cli.set_live_view(None)

    if errors:
        print("FAILED:", errors, file=sys.stderr)
        return 1

    with view._lock:  # noqa: SLF001 — test-only introspection
        pane_count = len(view._panes)
        all_done = all(p.done for p in view._panes.values())
        any_error = any(p.is_error for p in view._panes.values())

    print(f"panes={pane_count} all_done={all_done} any_error={any_error}")
    if pane_count != 3 or not all_done or any_error:
        print("FAILED: unexpected pane state", file=sys.stderr)
        return 1

    print("OK: 3 concurrent panes started, streamed, and finished cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
