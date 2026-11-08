"""Load merchant-supplied (seeded) context into the graph around the real PayPal disputes.

The real disputes' transaction IDs come from their stored PayPal JSON, never from the seed file.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from disputedesk.config import PROJECT_ROOT
from disputedesk.graph import GraphStore

SEED_FILE = PROJECT_ROOT / "seed" / "merchant_data.json"


def load_seed(path: Path = SEED_FILE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _when(value: str) -> tuple[datetime | None, str]:
    """(full timestamp or None, the date part). Date-only values ("2026-11-03") have no time of day."""
    text = str(value)
    if len(text) > 10:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")), text[:10]
        except ValueError:
            pass
    return None, text[:10]


def shipment_timing_problems(dispute_id: str, transaction_created: str | None, dispute_created: str | None,
                             shipment: dict[str, Any] | None) -> list[str]:
    """A seeded shipment must be dated after the real transaction and no later than the moment the
    dispute was opened. Full timestamps are compared exactly; a date-only value is compared by day."""
    problems: list[str] = []
    for key in ("shipped_at", "delivered_at"):
        if not shipment or not shipment.get(key):
            continue
        at, day = _when(shipment[key])
        for bound, label, bad in ((transaction_created, "before the transaction", lambda x, y: x < y),
                                  (dispute_created, "after the dispute was opened", lambda x, y: x > y)):
            if not bound:
                continue
            b_at, b_day = _when(bound)
            if at and b_at:
                wrong = bad(at, b_at)
            else:
                wrong = bad(day, b_day)
            if wrong:
                problems.append(f"{dispute_id}: seeded {key} {shipment[key]} is {label} ({bound})")
    return problems


def _date_warnings(dispute_id: str, dispute: dict[str, Any], shipment: dict[str, Any] | None) -> list[str]:
    """Warnings for seeded shipment dates outside the transaction-to-dispute window (reported, not refused)."""
    created = min((t.get("create_time") or "" for t in dispute.get("disputed_transactions") or []), default="") or None
    return [f"warning {p}" for p in shipment_timing_problems(dispute_id, created, dispute.get("create_time"), shipment)]


def seed_graph(graph: GraphStore, disputes: dict[str, dict[str, Any]], data: dict[str, Any]) -> list[str]:
    """`disputes` maps dispute_id -> full stored GET response. Returns report lines."""
    report: list[str] = []
    for dispute_id, ctx in data["disputes"].items():
        dispute = disputes.get(dispute_id)
        if dispute is None:
            report.append(f"skipped {dispute_id}: not in the local store with full details (run the poller first)")
            continue
        txn_ids = graph.upsert_paypal_dispute(dispute)
        if not txn_ids:
            report.append(f"skipped {dispute_id}: stored JSON has no seller_transaction_id")
            continue
        report += _date_warnings(dispute_id, dispute, ctx.get("shipment"))
        for txn_id in txn_ids:
            if ctx.get("shipment"):
                graph.attach_shipment(txn_id, ctx["shipment"])
            if ctx.get("telemetry"):
                graph.attach_telemetry(txn_id, ctx["telemetry"])
        report.append(f"seeded {dispute_id}: transaction(s) {', '.join(txn_ids)}")
    for past in data.get("past_disputes", []):
        graph.add_seeded_dispute(past)
    report.append(f"seeded {len(data.get('past_disputes', []))} past dispute(s)")
    return report
