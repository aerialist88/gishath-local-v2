"""
deck_engine/live_view.py — terminal live view of the pipeline.

One panel per active `claude -p` call: stage name, model, the free-text
response streaming in token by token (confirmed real via a live capture on
2026-07-01 — see claude_cli.py's docstring for the exact event shapes this
reads), then a visible transition once the model packages its answer via the
internal `StructuredOutput` tool call, then a final done/cost/turns/duration
line.

Entirely optional and additive: claude_cli.run() only reports to a LiveView
if one has been registered via set_live_view() (deck_engine/run.py does this
at the top of main()). Every existing caller/test that never touches
live_view continues to behave exactly as before — this module has zero
effect unless explicitly turned on.

HISTORY COLLAPSE (added 2026-07-01, after a real run on Trevor's Mac): a full
pipeline run is easily 8-20+ `claude -p` calls once ideate's 3 parallel
angles, synthesize, build, N validate/repair attempts, and optimize are all
counted — rendering every one as a full 12-line panel forever made the
terminal view grow taller than the window, which breaks rich.Live's redraw
(it moves the cursor up over its own last frame; once the frame is taller
than the terminal, updates below the fold become invisible) and, even where
it fit, made consecutive panels look identical enough that real progress
was mistaken for a hang. Fix: only calls still in flight get a full panel;
finished calls collapse to a single one-line summary in a running list, so
total height stays roughly proportional to concurrency (usually 1-3 active
panels) rather than to total calls made.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

_MAX_VISIBLE_CHARS = 900  # keep panels a readable size; show the tail of the response, not the whole history
_MAX_HISTORY_LINES = 20   # oldest collapse further into a "+N earlier" note rather than growing unbounded


@dataclass
class _CallPane:
    stage: str
    model: str
    text: str = ""
    status: str = "thinking..."
    done: bool = False
    cost_usd: float = 0.0
    num_turns: int = 0
    duration_s: float = 0.0
    is_error: bool = False

    def render(self) -> Panel:
        body = Text(self.text[-_MAX_VISIBLE_CHARS:] if self.text else "(waiting for first token...)")
        body.append("\n\n")
        if self.done:
            style = "bold red" if self.is_error else "bold green"
            label = "FAILED" if self.is_error else "done"
            body.append(f"{label} — {self.duration_s:.0f}s · ${self.cost_usd:.4f} · {self.num_turns} turns", style=style)
        else:
            body.append(self.status, style="dim yellow")

        border_style = "red" if (self.done and self.is_error) else ("green" if self.done else "cyan")
        return Panel(body, title=f"[bold]{self.stage}[/] [dim]({self.model})[/]",
                     border_style=border_style, height=12)

    def render_summary(self) -> Text:
        """One-line form used once a call is done — see HISTORY COLLAPSE in the module docstring."""
        icon, style = ("✗", "bold red") if self.is_error else ("✓", "green")
        line = Text(f"{icon} ", style=style)
        line.append(f"{self.stage} ", style="bold" if not self.is_error else style)
        line.append(f"({self.model}) ", style="dim")
        line.append(f"— {self.duration_s:.0f}s · ${self.cost_usd:.4f} · {self.num_turns} turns", style=style)
        if self.is_error and self.text:
            line.append(f"\n    {self.text[-200:]}", style="dim red")
        return line


class LiveView:
    """Context manager wrapping one nightly run. Thread-safe — ideate's 3
    parallel subprocess calls all update this concurrently."""

    def __init__(self) -> None:
        self._panes: dict[str, _CallPane] = {}
        self._lock = threading.Lock()
        self._live = Live(self._render(), refresh_per_second=8, transient=False)

    @property
    def console(self):
        """Use view.console.print(...) instead of bare print() for any output alongside
        the live region — rich's Live manages a redrawing area, and un-routed print()
        calls while it's active can visually corrupt the display."""
        return self._live.console

    def __enter__(self) -> "LiveView":
        self._live.__enter__()
        return self

    def __exit__(self, *exc_info) -> None:
        self._live.__exit__(*exc_info)

    def _render(self) -> Group:
        with self._lock:
            panes = list(self._panes.values())  # insertion order == call order (dict preserves it)
        if not panes:
            return Group(Panel("waiting for the first call...", title="deck_engine"))

        finished = [p for p in panes if p.done]
        active = [p for p in panes if not p.done]

        parts: list = []
        if finished:
            lines = finished[-_MAX_HISTORY_LINES:]
            history_body = Group(*(p.render_summary() for p in lines))
            hidden = len(finished) - len(lines)
            title = f"[dim]completed ({len(finished)} call{'s' if len(finished) != 1 else ''}"
            title += f", {hidden} earlier not shown)[/]" if hidden else ")[/]"
            parts.append(Panel(history_body, title=title, border_style="dim"))
        parts.extend(p.render() for p in active)

        if not parts:  # unreachable given the guard above, but never render truly empty
            parts = [Panel("waiting for the first call...", title="deck_engine")]
        return Group(*parts)

    def _refresh(self) -> None:
        self._live.update(self._render())

    def start_call(self, stage: str, model: str) -> "_CallHandle":
        with self._lock:
            self._panes[stage] = _CallPane(stage=stage, model=model)
        self._refresh()
        return _CallHandle(self, stage)

    def _append_text(self, stage: str, chunk: str) -> None:
        with self._lock:
            pane = self._panes.get(stage)
            if pane is not None:
                pane.text += chunk
        self._refresh()

    def _set_status(self, stage: str, status: str) -> None:
        with self._lock:
            pane = self._panes.get(stage)
            if pane is not None:
                pane.status = status
        self._refresh()

    def _finish(self, stage: str, *, cost_usd: float, num_turns: int, duration_s: float, is_error: bool) -> None:
        with self._lock:
            pane = self._panes.get(stage)
            if pane is not None:
                pane.done = True
                pane.cost_usd = cost_usd
                pane.num_turns = num_turns
                pane.duration_s = duration_s
                pane.is_error = is_error
        self._refresh()


@dataclass
class _CallHandle:
    """Returned by LiveView.start_call() — the only thing claude_cli.run() touches per call."""
    _view: LiveView
    _stage: str

    def append_text(self, chunk: str) -> None:
        self._view._append_text(self._stage, chunk)

    def set_status(self, status: str) -> None:
        self._view._set_status(self._stage, status)

    def finish(self, *, cost_usd: float, num_turns: int, duration_s: float, is_error: bool) -> None:
        self._view._finish(self._stage, cost_usd=cost_usd, num_turns=num_turns,
                            duration_s=duration_s, is_error=is_error)
