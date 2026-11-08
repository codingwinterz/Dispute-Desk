"""Merchant records: shipments, checkout devices and earlier disputes that the merchant's store supplies.

In the sandbox these are simulated (source "seeded") and come from an input JSON file such as
seed/merchant_data.json. import_records() validates the file, resolves each real dispute's
transaction ID from its stored PayPal JSON (never from the file), and stores the records in SQLite
for the Merchant records page. Seeding the FalkorDB graph from the same file is a separate, optional
step (disputedesk.seed). Nothing here calls PayPal.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

NOTE = "In production your store supplies this; in the sandbox it is simulated."


class Shipment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    carrier: str = Field(min_length=1)
    tracking_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    shipped_at: str | None = None
    delivered_at: str | None = None
    last_scan: str | None = None
    signed_by_buyer: bool | None = None


class Telemetry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    ip: str = Field(min_length=1)
    device_hash: str = Field(min_length=1)
    geo: str = Field(min_length=1)


class DisputeContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: str | None = None
    shipment: Shipment | None = None
    telemetry: Telemetry | None = None


class PastDispute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dispute_id: str = Field(min_length=1)
    transaction_id: str = Field(min_length=1)
    payer_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    status: str = Field(min_length=1)
    amount: float | None = None
    currency: str | None = None
    create_time: str | None = None
    telemetry: Telemetry | None = None


class MerchantFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    about: str | None = Field(default=None, alias="_about")
    disputes: dict[str, DisputeContext] = Field(default_factory=dict)
    past_disputes: list[PastDispute] = Field(default_factory=list)


class RecordsError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def parse_file(path: Path) -> MerchantFile:
    """Validate an input file. Raises RecordsError with one readable line per problem."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RecordsError([f"{path.name} was not found."]) from None
    except ValueError as exc:
        raise RecordsError([f"{path.name} is not valid JSON ({exc.msg} at line {exc.lineno})."]) from None
    try:
        return MerchantFile.model_validate(raw)
    except ValidationError as exc:
        raise RecordsError([f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:20]]) from None


class MerchantRecordStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS merchant_records ("
                " record_id TEXT PRIMARY KEY,"
                " kind TEXT NOT NULL CHECK (kind IN ('shipment', 'telemetry', 'past_dispute')),"
                " dispute_id TEXT,"
                " transaction_id TEXT,"
                " data TEXT NOT NULL,"
                " source TEXT NOT NULL CHECK (source IN ('paypal', 'seeded')),"
                " imported_at TEXT NOT NULL,"
                " input_file TEXT NOT NULL)"
            )

    def replace_all(self, rows: list[dict[str, Any]]) -> None:
        """An import replaces the previous one, so the page always matches the last input file."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM merchant_records")
            self._db.executemany(
                "INSERT INTO merchant_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(r["record_id"], r["kind"], r["dispute_id"], r["transaction_id"], json.dumps(r["data"]),
                  r["source"], r["imported_at"], r["input_file"]) for r in rows])

    def rows(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._db.execute("SELECT * FROM merchant_records ORDER BY kind, dispute_id, record_id")
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) | {"data": json.loads(r[cols.index("data")])} for r in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._db.close()


def import_records(parsed: MerchantFile, disputes: dict[str, dict[str, Any]], input_name: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Rows to store and report lines. `disputes` maps dispute_id -> stored full GET response."""
    now = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []
    report: list[str] = []
    for dispute_id, ctx in parsed.disputes.items():
        dispute = disputes.get(dispute_id)
        txns = [t.get("seller_transaction_id") for t in (dispute or {}).get("disputed_transactions") or []
                if isinstance(t, dict) and t.get("seller_transaction_id")]
        if not txns:
            report.append(f"skipped {dispute_id}: not in the local store with full details yet (the poller adds it)")
            continue
        for kind, record in (("shipment", ctx.shipment), ("telemetry", ctx.telemetry)):
            if record is not None:
                rows.append({"record_id": record.id, "kind": kind, "dispute_id": dispute_id, "transaction_id": txns[0],
                             "data": record.model_dump(exclude_none=True), "source": "seeded",
                             "imported_at": now, "input_file": input_name})
        report.append(f"imported {dispute_id}: transaction {txns[0]}")
    for past in parsed.past_disputes:
        rows.append({"record_id": past.dispute_id, "kind": "past_dispute", "dispute_id": past.dispute_id,
                     "transaction_id": past.transaction_id, "data": past.model_dump(exclude_none=True),
                     "source": "seeded", "imported_at": now, "input_file": input_name})
    report.append(f"imported {len(parsed.past_disputes)} earlier dispute(s)")
    return rows, report


def records_view(rows: list[dict[str, Any]], disputes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Grouped for the page: one entry per transaction of a stored PayPal dispute, plus earlier disputes."""
    by_txn: dict[str, dict[str, Any]] = {}
    past = []
    for r in rows:
        if r["kind"] == "past_dispute":
            past.append(r["data"] | {"source": r["source"]})
            continue
        entry = by_txn.setdefault(r["transaction_id"], {
            "transaction_id": r["transaction_id"], "dispute_id": r["dispute_id"], "transaction_source": "paypal",
            "dispute": _dispute_brief(disputes.get(r["dispute_id"])), "shipment": None, "telemetry": None})
        entry[r["kind"]] = r["data"] | {"source": r["source"]}
    meta = rows[0] if rows else None
    return {
        "note": NOTE,
        "imported_at": meta["imported_at"] if meta else None,
        "input_file": meta["input_file"] if meta else None,
        "transactions": sorted(by_txn.values(), key=lambda e: e["dispute_id"] or ""),
        "past_disputes": sorted(past, key=lambda p: p.get("create_time") or "", reverse=True),
    }


def _dispute_brief(d: dict[str, Any] | None) -> dict[str, Any] | None:
    if not d:
        return None
    return {"status": d.get("status"), "reason": d.get("reason"), "amount": d.get("dispute_amount"), "source": "paypal"}
