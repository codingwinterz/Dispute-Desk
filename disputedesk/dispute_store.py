"""SQLite record of dispute IDs already processed, shared by the poller and the webhook path.

The file lives under logs/ (git-ignored). claim() is atomic, so a dispute seen by both the
poller and a webhook is processed exactly once, whichever gets there first.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SOURCES = ("poll", "webhook")


def safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


class DisputeStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        # Used from the poller thread and webhook threadpool; all access goes through the lock.
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS seen_disputes ("
                " dispute_id TEXT PRIMARY KEY,"
                " source TEXT NOT NULL,"
                " first_seen TEXT NOT NULL)"
            )

    def seen(self, dispute_id: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT 1 FROM seen_disputes WHERE dispute_id = ?", (dispute_id,)).fetchone()
        return row is not None

    def source_of(self, dispute_id: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT source FROM seen_disputes WHERE dispute_id = ?", (dispute_id,)).fetchone()
        return row[0] if row else None

    def claim(self, dispute_id: str, source: str) -> bool:
        """Record a dispute as processed. Returns False if it was already claimed."""
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO seen_disputes (dispute_id, source, first_seen) VALUES (?, ?, ?)",
                (dispute_id, source, now),
            )
        return cur.rowcount == 1

    def close(self) -> None:
        with self._lock:
            self._db.close()
