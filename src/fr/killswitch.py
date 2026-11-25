"""One kill switch for the whole agent.

Three ways to pull it, all ending in the same place:
  * dashboard button  -> POST /api/kill
  * CLI               -> `fr kill`  (writes .fr/KILL, picked up within ~0.5s)
  * file              -> create .fr/KILL by hand

On kill, every registered run task is cancelled (not awaited to completion).
Cancellation propagates into the sandbox (container/process killed) and into
any in-flight HTTP call. While the switch is engaged, new runs are refused.
"""

from __future__ import annotations

import asyncio
from pathlib import Path


class Killed(Exception):
    """Raised when work is attempted while the kill switch is engaged."""


class KillSwitch:
    def __init__(self, kill_file: Path) -> None:
        self.kill_file = kill_file
        self._tasks: set[asyncio.Task] = set()
        self._watcher: asyncio.Task | None = None
        self.reason = ""

    @property
    def engaged(self) -> bool:
        return self.kill_file.exists()

    def check(self) -> None:
        if self.engaged:
            raise Killed(self.reason or "kill switch engaged")

    def register(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def engage(self, reason: str = "manual") -> int:
        self.reason = reason
        self.kill_file.parent.mkdir(parents=True, exist_ok=True)
        self.kill_file.write_text(reason, encoding="utf-8")
        return self._cancel_all()

    def release(self) -> None:
        self.reason = ""
        self.kill_file.unlink(missing_ok=True)

    def _cancel_all(self) -> int:
        n = 0
        for t in list(self._tasks):
            if not t.done():
                t.cancel(msg="KILL")
                n += 1
        return n

    async def watch(self, interval: float = 0.25) -> None:
        """Poll the kill file so `fr kill` from another terminal works."""
        while True:
            if self.engaged and any(not t.done() for t in self._tasks):
                if not self.reason:
                    self.reason = self.kill_file.read_text(encoding="utf-8") or "file"
                self._cancel_all()
            await asyncio.sleep(interval)

    def start_watcher(self) -> None:
        if self._watcher is None or self._watcher.done():
            self._watcher = asyncio.get_running_loop().create_task(self.watch())
