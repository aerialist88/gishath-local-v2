#!/usr/bin/env python3
"""Fake `claude` binary for testing claude_cli.run()'s subprocess/streaming handling
against a REAL captured transcript, without needing a live authenticated session.
Ignores all arguments; just replays real_stream_capture.jsonl to stdout."""
import sys
import time
from pathlib import Path

FIXTURE = Path(__file__).parent / "real_stream_capture.jsonl"

for line in FIXTURE.read_text().splitlines():
    print(line, flush=True)
    time.sleep(0.01)  # tiny delay so this genuinely exercises line-by-line streaming, not one big read
sys.exit(0)
