"""Idempotency store: the same PR head SHA is never processed twice (unless forced)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(state_dir / "runs.sqlite", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS runs (key TEXT PRIMARY KEY, run_id TEXT, "
                        "status TEXT, ts REAL)")
        self.db.commit()

    def seen(self, key: str) -> str | None:
        row = self.db.execute("SELECT status FROM runs WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def record(self, key: str, run_id: str, status: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?)",
                        (key, run_id, status, time.time()))
        self.db.commit()
