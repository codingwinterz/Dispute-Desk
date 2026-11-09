"""SQLite store of disputes already processed, shared by the poller and the webhook path.

Default file: data/disputedesk.db (git-ignored). claim() is a single INSERT OR IGNORE inside a
transaction, under a lock, so when the poller and a webhook see the same dispute at the same
time exactly one of them wins and emits dispute_detected.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOURCES = ("poll", "webhook")


def safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


def summarize(dispute_id: str, source: str, detected_at: str, data: dict[str, Any]) -> dict[str, Any]:
    """Dashboard view of one stored dispute. Field names follow GET /v1/customer/disputes/{id}:
    https://developer.paypal.com/docs/api/customer-disputes/v1/#disputes_get"""
    links = data.get("links") or []
    # Action links are the non-GET HATEOAS links (e.g. provide_evidence, accept_claim); "self" is a GET.
    actions = sorted({
        str(link["rel"]) for link in links
        if isinstance(link, dict) and link.get("rel") and str(link.get("method", "")).upper() != "GET"
    })
    return {
        "dispute_id": dispute_id,
        "status": data.get("status"),
        "stage": data.get("dispute_life_cycle_stage"),
        "reason": data.get("reason"),
        "amount": data.get("dispute_amount"),
        "seller_response_due_date": data.get("seller_response_due_date"),
        # When PayPal created the dispute; the case timeline compares it with detected_at.
        "create_time": data.get("create_time"),
        # Every dispute record comes from PayPal; detected_via says which path saw it first.
        "source": "paypal",
        "detected_via": source,
        "detected_at": detected_at,
        "actions": actions,
    }


class DisputeStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        # Used from the poller thread and the webhook threadpool; every access holds the lock.
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS disputes ("
                " dispute_id TEXT PRIMARY KEY,"
                " source TEXT NOT NULL CHECK (source IN ('poll', 'webhook')),"
                " detected_at TEXT NOT NULL,"
                # 1 once data holds the full GET /v1/customer/disputes/{id} response;
                # 0 while it only holds a webhook's resource.
                " full_fetched INTEGER NOT NULL DEFAULT 0,"
                " data TEXT NOT NULL)"
            )

    def seen(self, dispute_id: str) -> bool:
        return self._row(dispute_id, "1") is not None

    def source_of(self, dispute_id: str) -> str | None:
        row = self._row(dispute_id, "source")
        return row[0] if row else None

    def needs_full(self, dispute_id: str) -> bool:
        row = self._row(dispute_id, "full_fetched")
        return row is not None and not row[0]

    def claim(self, dispute_id: str, source: str, data: dict[str, Any], full: bool) -> bool:
        """Record a dispute as processed. Returns False if it was already claimed (by either path)."""
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._db:  # one transaction
            cur = self._db.execute(
                "INSERT OR IGNORE INTO disputes (dispute_id, source, detected_at, full_fetched, data)"
                " VALUES (?, ?, ?, ?, ?)",
                (dispute_id, source, now, int(full), json.dumps(data)),
            )
        return cur.rowcount == 1

    def update_full(self, dispute_id: str, data: dict[str, Any]) -> None:
        """Replace the stored data with a full GET response. Never changes source or detected_at."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE disputes SET data = ?, full_fetched = 1 WHERE dispute_id = ?",
                (json.dumps(data), dispute_id),
            )

    def get(self, dispute_id: str) -> dict[str, Any] | None:
        """Stored dispute JSON (full GET response, or the webhook snapshot until backfilled)."""
        row = self._row(dispute_id, "data")
        return json.loads(row[0]) if row else None

    def full_disputes(self) -> dict[str, dict[str, Any]]:
        """dispute_id -> data for every dispute whose full GET response is stored."""
        with self._lock:
            rows = self._db.execute("SELECT dispute_id, data FROM disputes WHERE full_fetched = 1").fetchall()
        return {i: json.loads(raw) for i, raw in rows}

    def summary(self, dispute_id: str) -> dict[str, Any] | None:
        row = self._row(dispute_id, "dispute_id, source, detected_at, data")
        return summarize(row[0], row[1], row[2], json.loads(row[3])) if row else None

    def summaries(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT dispute_id, source, detected_at, data FROM disputes ORDER BY detected_at, dispute_id"
            ).fetchall()
        return [summarize(i, s, d, json.loads(raw)) for i, s, d, raw in rows]

    def _row(self, dispute_id: str, columns: str) -> tuple[Any, ...] | None:
        with self._lock:
            return self._db.execute(
                f"SELECT {columns} FROM disputes WHERE dispute_id = ?", (dispute_id,)
            ).fetchone()

    def close(self) -> None:
        with self._lock:
            self._db.close()
