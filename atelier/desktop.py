"""atelier/desktop.py — the Atelier as a native macOS window.

pywebview wraps the same local Flask server in a WKWebView window — no
Electron, no node toolchain, one extra pip dependency. The server thread is a
daemon: closing the window exits the process and takes the server with it.
A run in flight when the window closes dies with it (same as ctrl-C on the
nightly script) — the spend log and any exported files up to that point are
already on disk.

    python -m atelier.desktop
"""
from __future__ import annotations

import threading
import time
import urllib.request

from . import server


def _wait_for_server(timeout_s: float = 15.0) -> bool:
    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{server.PORT}/api/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return True
        except Exception:  # noqa: BLE001 — not up yet
            time.sleep(0.2)
    return False


def main() -> None:
    try:
        import webview
    except ImportError:
        raise SystemExit(
            "pywebview isn't installed. Run:  ./venv/bin/pip install pywebview\n"
            "(or use the browser instead:  python -m atelier.server)"
        )

    threading.Thread(target=server.main, daemon=True, name="atelier-server").start()
    if not _wait_for_server():
        raise SystemExit("The Atelier server didn't come up on "
                         f"port {server.PORT} — check for another process on that port.")

    webview.create_window(
        "The Foundry",
        f"http://127.0.0.1:{server.PORT}",
        width=1360, height=900, min_size=(920, 640),
    )
    webview.start()


if __name__ == "__main__":
    main()
