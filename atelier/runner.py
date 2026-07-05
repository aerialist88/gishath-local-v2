"""atelier/runner.py — runs the deck_engine pipeline on a background thread and
turns its progress into a UI-consumable event stream.

AtelierView implements the same surface deck_engine/live_view.LiveView exposes
(claude_cli.run() only ever touches start_call()'s handle, run.py additionally
uses `with view:` and view.console.print), plus the optional hooks run.main()
looks up with getattr (set_stage / concept_chosen / run_delivered /
run_failed). Where the terminal view renders rich panels, this view appends
plain dict events to a RunEventLog that the Flask server snapshots (for page
loads / reconnects) and tails (for SSE).

One run at a time — same constraint as the nightly job itself (the pipeline
mutates shared state: spend log, run log, scryfall cache refresh).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

from deck_engine import claude_cli, config

RUNS_DIR = config.STATE_DIR / "atelier_runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

_TEXT_TAIL_CHARS = 4000   # per-bench stream tail kept in the snapshot; SSE clients get full deltas live


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunEventLog:
    """Materialized run state + incremental event feed, thread-safe.

    Snapshot (state()) answers "what does the screen look like right now" for
    fresh page loads; events (wait_events()) answer "what changed since seq N"
    for SSE. Keeping both means reconnecting mid-run never replays thousands
    of token deltas."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._lock = threading.Condition()
        self._events: list[dict] = []
        self._state: dict = {
            "run_id": run_id,
            "status": "running",          # running | delivered | failed | cancelled
            "started_at": _now_iso(),
            "started_ts": time.time(),
            "forced_commander": None,
            "stage": "startup",
            "concept": None,
            "announces": [],
            "calls": {},                  # label -> call dict
            "call_order": [],
            "delivered": None,
            "failed": None,
            "demo": False,
        }

    # ── write side ───────────────────────────────────────────────────────────

    def emit(self, etype: str, **data) -> None:
        with self._lock:
            event = {"seq": len(self._events), "t": round(time.time(), 3), "type": etype, **data}
            self._events.append(event)
            self._apply(event)
            self._lock.notify_all()

    def _apply(self, e: dict) -> None:
        s = self._state
        etype = e["type"]
        if etype == "run_started":
            s["forced_commander"] = e.get("forced_commander")
            s["demo"] = bool(e.get("demo"))
        elif etype == "stage":
            s["stage"] = e["stage"]
        elif etype == "concept":
            s["concept"] = {k: e.get(k) for k in ("commander", "archetype", "rationale", "colors")}
        elif etype == "announce":
            s["announces"].append(e["text"])
        elif etype == "call_started":
            call = {"label": e["label"], "model": e["model"], "text_tail": "", "status": "thinking...",
                    "done": False, "is_error": False, "cost_usd": 0.0, "num_turns": 0,
                    "duration_s": 0.0, "started_at": e["t"]}
            s["calls"][e["label"]] = call
            s["call_order"].append(e["label"])
        elif etype == "call_text":
            call = s["calls"].get(e["label"])
            if call is not None:
                call["text_tail"] = (call["text_tail"] + e["chunk"])[-_TEXT_TAIL_CHARS:]
        elif etype == "call_status":
            call = s["calls"].get(e["label"])
            if call is not None:
                call["status"] = e["status"]
        elif etype == "call_finished":
            call = s["calls"].get(e["label"])
            if call is not None:
                call.update(done=True, is_error=e["is_error"], cost_usd=e["cost_usd"],
                            num_turns=e["num_turns"], duration_s=e["duration_s"])
        elif etype == "delivered":
            s["status"] = "delivered"
            s["delivered"] = {k: v for k, v in e.items() if k not in ("seq", "t", "type")}
        elif etype == "failed":
            s["status"] = "cancelled" if "cancelled by user" in (e.get("error") or "") else "failed"
            s["failed"] = {k: v for k, v in e.items() if k not in ("seq", "t", "type")}

    # ── read side ────────────────────────────────────────────────────────────

    def state(self) -> dict:
        with self._lock:
            s = json.loads(json.dumps(self._state))  # deep copy — callers serialize outside the lock
            s["next_seq"] = len(self._events)
            s["totals"] = self._totals()
            return s

    def _totals(self) -> dict:
        calls = self._state["calls"].values()
        return {
            "cost_usd": round(sum(c["cost_usd"] for c in calls), 4),
            "calls": len(self._state["call_order"]),
            "done_calls": sum(1 for c in calls if c["done"]),
            "elapsed_s": round(time.time() - self._state["started_ts"], 1),
        }

    def wait_events(self, since: int, timeout: float = 25.0) -> list[dict]:
        """Events with seq >= since; blocks up to `timeout` if there are none yet."""
        deadline = time.time() + timeout
        with self._lock:
            while len(self._events) <= since:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self._lock.wait(remaining)
            return self._events[since:]

    def finished(self) -> bool:
        with self._lock:
            return self._state["status"] != "running"

    def save(self) -> None:
        """Persist the final state for post-mortems (failure screen after an
        app restart, spend-by-stage view of the last halt)."""
        try:
            path = RUNS_DIR / f"{self.run_id[:8]}.json"
            path.write_text(json.dumps(self.state(), indent=1))
        except Exception:  # noqa: BLE001 — bookkeeping must never mask the run outcome
            pass


class _Console:
    def __init__(self, log: RunEventLog) -> None:
        self._log = log

    def print(self, msg) -> None:  # noqa: A003 — mirrors rich Console.print
        self._log.emit("announce", text=str(msg))


class _CallHandle:
    def __init__(self, log: RunEventLog, label: str) -> None:
        self._log = log
        self._label = label

    def append_text(self, chunk: str) -> None:
        self._log.emit("call_text", label=self._label, chunk=chunk)

    def set_status(self, status: str) -> None:
        self._log.emit("call_status", label=self._label, status=status)

    def finish(self, *, cost_usd: float, num_turns: int, duration_s: float, is_error: bool) -> None:
        self._log.emit("call_finished", label=self._label, cost_usd=cost_usd,
                       num_turns=num_turns, duration_s=duration_s, is_error=is_error)


class AtelierView:
    """Drop-in for live_view.LiveView that reports to a RunEventLog instead of
    the terminal. Also implements run.main()'s optional getattr hooks."""

    def __init__(self, log: RunEventLog) -> None:
        self._log = log
        self.console = _Console(log)

    def __enter__(self) -> "AtelierView":
        return self

    def __exit__(self, *exc_info) -> None:
        pass

    def start_call(self, stage: str, model: str) -> _CallHandle:
        self._log.emit("call_started", label=stage, model=model)
        return _CallHandle(self._log, stage)

    # optional run.py hooks
    def run_started(self, *, run_id: str, forced_commander=None, **_kw) -> None:
        self._log.emit("run_started", run_id=run_id, forced_commander=forced_commander)

    def set_stage(self, *, stage: str, **_kw) -> None:
        self._log.emit("stage", stage=stage)

    def concept_chosen(self, *, commander: str, archetype: str, rationale: str = "",
                       colors=None, **_kw) -> None:
        self._log.emit("concept", commander=commander, archetype=archetype,
                       rationale=rationale, colors=colors or [])

    def run_delivered(self, *, run_id: str, deck_json: str = "", xlsx: str = "",
                      moxfield_txt: str = "", spend_summary=None, **_kw) -> None:
        spend = spend_summary or {}
        self._log.emit("delivered", run_id=run_id, deck_id=run_id[:8],
                       deck_json=deck_json, xlsx=xlsx, moxfield_txt=moxfield_txt,
                       cost_usd=spend.get("total_cost_usd", 0.0),
                       turns=spend.get("total_turns", 0))

    def run_failed(self, *, run_id: str, stage: str, error: str, spend_summary=None, **_kw) -> None:
        spend = spend_summary or {}
        self._log.emit("failed", run_id=run_id, stage=stage, error=error,
                       cost_usd=spend.get("total_cost_usd", 0.0),
                       spend_stages=[
                           {"stage": st.get("stage"), "cost_usd": st.get("cost_usd", 0.0)}
                           for st in spend.get("stages", [])
                       ])


class RunManager:
    """One pipeline run at a time, on a daemon thread. The Flask server holds a
    single module-level instance."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._log: RunEventLog | None = None
        self._thread: threading.Thread | None = None

    def current(self) -> RunEventLog | None:
        return self._log

    def is_running(self) -> bool:
        log = self._log
        return log is not None and not log.finished()

    def start(self, forced_commander: str | None = None) -> dict:
        """Kick off a real pipeline run. Returns {run_id} or {error}."""
        with self._lock:
            if self.is_running():
                return {"error": "a commission is already on the bench", "run_id": self._log.run_id}
            import uuid
            run_id = str(uuid.uuid4())
            log = RunEventLog(run_id)
            self._log = log

            def _target() -> None:
                from deck_engine import run as engine_run
                view = AtelierView(log)
                try:
                    engine_run.main(view=view, forced_commander=forced_commander, run_id=run_id)
                except Exception as exc:  # noqa: BLE001 — belt over run.py's own braces
                    if not log.finished():
                        log.emit("failed", run_id=run_id, stage="unknown", error=str(exc), cost_usd=0.0)
                finally:
                    if not log.finished():
                        # run.main returned without delivering or failing through
                        # the hooks (shouldn't happen) — close the stream anyway.
                        log.emit("failed", run_id=run_id, stage="unknown",
                                 error="run ended without reporting an outcome", cost_usd=0.0)
                    log.save()

            self._thread = threading.Thread(target=_target, daemon=True, name="atelier-run")
            self._thread.start()
            return {"run_id": run_id}

    def start_demo(self) -> dict:
        """Scripted rehearsal run — full live-view theatre, no API spend."""
        with self._lock:
            if self.is_running():
                return {"error": "a commission is already on the bench", "run_id": self._log.run_id}
            import uuid
            run_id = f"demo-{uuid.uuid4()}"
            log = RunEventLog(run_id)
            self._log = log

            def _target() -> None:
                from . import demo
                try:
                    demo.play(log)
                except Exception as exc:  # noqa: BLE001
                    if not log.finished():
                        log.emit("failed", run_id=run_id, stage="demo", error=str(exc), cost_usd=0.0)
                finally:
                    log.save()

            self._thread = threading.Thread(target=_target, daemon=True, name="atelier-demo")
            self._thread.start()
            return {"run_id": run_id}

    def cancel(self) -> dict:
        """Abandon the active commission: the in-flight `claude -p` call is
        allowed to finish (killing it mid-stream risks corrupt state), then the
        next call raises and the run lands in the normal failure path."""
        log = self._log
        if log is None or log.finished():
            return {"error": "no commission is running"}
        if log.run_id.startswith("demo-"):
            log.emit("failed", run_id=log.run_id, stage="demo", error="cancelled by user", cost_usd=0.0)
            return {"ok": True}
        claude_cli.request_cancel(log.run_id)
        log.emit("announce", text="abandon requested — the current bench will finish, then the run halts")
        return {"ok": True}


MANAGER = RunManager()
