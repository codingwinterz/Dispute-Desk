import json

from disputedesk.graph import CONTEXT_QUERY, GraphStore, context_to_facts
from tests.fakes import FakeGraph, context_row, sample_dispute


def test_upsert_uses_parameters_and_skips_buyer_pii():
    g = FakeGraph()
    txns = GraphStore(g).upsert_paypal_dispute(sample_dispute())
    assert txns == ["TXN-SELLER-1"]
    queries = " ".join(q for q, _ in g.calls)
    assert "MERGE (d:Dispute" in queries and "MERGE (t:Transaction" in queries and "MERGE (c:Customer" in queries
    # Values travel as parameters, never inside the query text.
    assert "PP-R-TST-1" not in queries and "TXN-SELLER-1" not in queries
    everything = json.dumps([p for _, p in g.calls])
    assert "buyer@example.com" not in everything and "Test Buyer" not in everything
    assert all(p.get("props", {}).get("source", "paypal") == "paypal" for _, p in g.calls)


def test_upsert_without_transactions_links_nothing():
    d = sample_dispute()
    d["disputed_transactions"] = []
    assert GraphStore(FakeGraph()).upsert_paypal_dispute(d) == []


def test_context_query_excludes_own_transaction():
    assert "other_t <> t" in CONTEXT_QUERY
    assert "c.email" not in CONTEXT_QUERY


def test_context_parses_row_and_builds_facts():
    linked = [{"id": "SEED-D-1", "reason": "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "status": "RESOLVED",
               "source": "seeded", "transaction_id": "SEED-T-1", "via": "device_hash"}]
    ctx = GraphStore(FakeGraph([context_row(linked)])).dispute_context("PP-R-TST-1")
    assert ctx["dispute"]["id"] == "PP-R-TST-1" and ctx["shipment"]["status"] == "DELIVERED"
    facts = context_to_facts(ctx)
    assert [f["id"] for f in facts] == [f"F{i}" for i in range(1, len(facts) + 1)]
    assert all(f["source"] in ("paypal", "seeded") for f in facts)
    text = " ".join(f["text"] for f in facts)
    assert "SEED-D-1" in text and "device hash" in text and "signed by buyer: yes" in text


def test_missing_optional_nodes_become_explicit_facts():
    ctx = GraphStore(FakeGraph([context_row(shipment=False, telemetry=False)])).dispute_context("PP-R-TST-1")
    assert ctx["shipment"] is None and ctx["telemetry"] is None
    text = " ".join(f["text"] for f in context_to_facts(ctx))
    assert "No shipment record" in text and "No checkout telemetry" in text
    assert "No other dispute shares" in text


def test_unknown_dispute_returns_none():
    assert GraphStore(FakeGraph([])).dispute_context("nope") is None
