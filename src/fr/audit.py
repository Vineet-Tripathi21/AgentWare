"""Append-only, hash-chained audit trail plus a live event bus for the dashboard.

Every event in a run is written as one JSON line to traces/<run_id>.jsonl.
Each line carries `prev` (hash of the previous line) and `hash` (sha256 of this
line's canonical content + prev), so any edit or deletion breaks the chain.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import deque
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _canonical(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(event: dict[str, Any]) -> str:
    body = {k: v for k, v in event.items() if k != "hash"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


class EventBus:
    """In-process pub/sub used by the dashboard's SSE stream."""

    def __init__(self, history: int = 2000) -> None:
        self.history: deque[dict[str, Any]] = deque(maxlen=history)
        self._subs: set[asyncio.Queue] = set()

    def publish(self, event: dict[str, Any]) -> None:
        self.history.append(event)
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # slow consumer: drop, never block the agent
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)


BUS = EventBus()


class AuditLog:
    def __init__(self, run_id: str, traces_dir: Path, bus: EventBus | None = BUS) -> None:
        self.run_id = run_id
        self.path = traces_dir / f"{run_id}.jsonl"
        traces_dir.mkdir(parents=True, exist_ok=True)
        self.prev = GENESIS
        self.seq = 0
        self.bus = bus

    def log(self, type_: str, phase: str = "", **data: Any) -> dict[str, Any]:
        self.seq += 1
        event = {
            "run_id": self.run_id,
            "seq": self.seq,
            "ts": round(time.time(), 3),
            "phase": phase,
            "type": type_,
            "data": _scrub(data),  # traces get committed: never let a secret into one
            "prev": self.prev,
        }
        event["hash"] = compute_hash(event)
        self.prev = event["hash"]
        with self.path.open("a", encoding="utf-8") as f:
            f.write(_canonical(event) + "\n")
        if self.bus is not None:
            self.bus.publish(event)
        return event


def _scrub(obj: Any) -> Any:
    from .policy import redact  # local import: policy must not depend on audit
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_scrub(v) for v in obj]
    return obj


def read_trace(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def verify_trace(path: Path) -> tuple[bool, str]:
    """Return (ok, message). Detects edited, reordered or removed lines."""
    prev = GENESIS
    events = read_trace(path)
    for i, ev in enumerate(events, start=1):
        if ev.get("prev") != prev:
            return False, f"line {i}: chain broken (prev hash mismatch)"
        if compute_hash(ev) != ev.get("hash"):
            return False, f"line {i}: content was modified (hash mismatch)"
        if ev.get("seq") != i:
            return False, f"line {i}: sequence gap"
        prev = ev["hash"]
    return True, f"{len(events)} events, chain intact"
