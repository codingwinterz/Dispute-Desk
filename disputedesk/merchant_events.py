"""Merchant events: new evidence from the merchant's own systems, such as a carrier scan.

In production the store would send these; in the sandbox they are simulated: a small JSON file is
imported with scripts/merchant_event.py, written to the graph (source "seeded", labelled
"simulated merchant event") and kept here for the case page's timeline. The event names a
transaction; the dispute it belongs to is found in the stored PayPal disputes, and an unknown
transaction is rejected, never invented. Nothing here calls PayPal.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from disputedesk.graph import EVENT_LABEL, SEEDED
from disputedesk.merchant_records import NOTE


class EventError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class MerchantEvent(BaseModel):
    """One event. Only carrier scans for now: status, location and time, as a carrier reports them."""
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    transaction_id: str = Field(min_length=1, max_length=64)
    kind: Literal["carrier_scan"]
    status: str = Field(min_length=1, max_length=40)
    location: str = Field(min_length=1, max_length=120)
    time: str = Field(min_length=1)
    carrier: str | None = Field(default=None, max_length=40)
    signed_by_buyer: bool | None = None
    note: str | None = Field(default=None, max_length=240)

    @field_validator("time")
    @classmethod
    def _iso_time(cls, value: str) -> str:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("time must be an ISO 8601 date and time, for example 2026-11-07T18:22:00Z") from exc
        return value

    @field_validator("status")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper().replace(" ", "_")


def parse_event(data: Any) -> MerchantEvent:
    try:
        return MerchantEvent.model_validate(data)
    except ValidationError as exc:
        raise EventError([f"{'.'.join(str(p) for p in e['loc']) or 'event'}: {e['msg']}" for e in exc.errors()]) from exc


def parse_event_file(path: Path) -> MerchantEvent:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EventError([f"{path} does not exist"]) from exc
    except json.JSONDecodeError as exc:
        raise EventError([f"not valid JSON (line {exc.lineno}): {exc.msg}"]) from exc
    return parse_event(data)


def find_dispute(transaction_id: str, disputes: dict[str, dict[str, Any]]) -> str:
    """The stored dispute whose PayPal JSON lists this seller transaction ID. Raises for unknown IDs."""
    matches = [dispute_id for dispute_id, d in disputes.items()
               if any(isinstance(t, dict) and t.get("seller_transaction_id") == transaction_id
                      for t in d.get("disputed_transactions") or [])]
    if not matches:
        raise EventError([f"transaction {transaction_id} is not on any stored PayPal dispute. Events can only "
                          "attach to transactions the poller has already stored; nothing was written."])
    if len(matches) > 1:
        raise EventError([f"transaction {transaction_id} appears on several disputes ({', '.join(sorted(matches))}); "
                          "nothing was written."])
    return matches[0]


def graph_props(event: MerchantEvent) -> dict[str, Any]:
    """What goes on the graph's MerchantEvent node (GraphStore adds label and source)."""
    return {"id": event.event_id, "kind": event.kind, "status": event.status, "location": event.location,
            "time": event.time, "carrier": event.carrier, "signed_by_buyer": event.signed_by_buyer, "note": event.note}


class MerchantEventStore:
    """Imported events, for the case page timeline (same SQLite file as the disputes)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS merchant_events ("
                " event_id TEXT PRIMARY KEY,"
                " dispute_id TEXT NOT NULL,"
                " transaction_id TEXT NOT NULL,"
                " data TEXT NOT NULL,"          # JSON of the validated event
                " source TEXT NOT NULL,"
                " label TEXT NOT NULL,"
                " received_at TEXT NOT NULL)"
            )

    def exists(self, event_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM merchant_events WHERE event_id = ?", (event_id,)).fetchone() is not None

    def add(self, event: MerchantEvent, dispute_id: str) -> str:
        received_at = datetime.now(timezone.utc).isoformat()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO merchant_events (event_id, dispute_id, transaction_id, data, source, label, received_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (event.event_id, dispute_id, event.transaction_id, event.model_dump_json(), SEEDED, EVENT_LABEL, received_at))
        return received_at

    def for_dispute(self, dispute_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT event_id, transaction_id, data, source, label, received_at FROM merchant_events"
                " WHERE dispute_id = ? ORDER BY received_at", (dispute_id,)).fetchall()
        return [json.loads(data) | {"event_id": eid, "transaction_id": tid, "source": src, "label": label,
                                    "received_at": at, "note_production": NOTE}
                for eid, tid, data, src, label, at in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()
