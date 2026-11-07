"""Load merchant-supplied (seeded) context into the graph around the real PayPal disputes.

The real disputes' transaction IDs come from their stored PayPal JSON, never from the seed file.
"""

import json
from pathlib import Path
from typing import Any

from disputedesk.config import PROJECT_ROOT
from disputedesk.graph import GraphStore

SEED_FILE = PROJECT_ROOT / "seed" / "merchant_data.json"


def load_seed(path: Path = SEED_FILE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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
