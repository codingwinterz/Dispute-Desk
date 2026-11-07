import json

import pytest

from disputedesk.dispute_store import DisputeStore
from disputedesk.graph import GraphStore
from disputedesk.seed import load_seed, seed_graph
from tests.fakes import FakeGraph, sample_dispute

REAL = {
    "PP-R-JTQ-10195324": ("MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "25.00"),
    "PP-R-YOV-10196864": ("MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "50.00"),
    "PP-R-QQW-10196399": ("MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "40.00"),
    "PP-R-IQA-10196863": ("MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "10.00"),
}


@pytest.fixture
def data():
    return load_seed()


def stored():
    return {i: sample_dispute(i, reason, value, txn=f"TXN-FROM-JSON-{i[-3:]}") for i, (reason, value) in REAL.items()}


def linked_count(data, dispute_id):
    """Disputes sharing a device hash or IP with this dispute's checkout (same rule as CONTEXT_QUERY)."""
    tel = data["disputes"][dispute_id]["telemetry"]
    others = [d["telemetry"] for k, d in data["disputes"].items() if k != dispute_id]
    others += [p["telemetry"] for p in data["past_disputes"]]
    return sum(o["device_hash"] == tel["device_hash"] or o["ip"] == tel["ip"] for o in others)


def test_seed_covers_the_four_real_disputes(data):
    assert set(data["disputes"]) == set(REAL)


def test_scenarios_differ(data):
    d = data["disputes"]
    assert d["PP-R-JTQ-10195324"]["shipment"]["status"] == "DELIVERED"
    assert d["PP-R-JTQ-10195324"]["shipment"]["signed_by_buyer"] is True
    assert d["PP-R-YOV-10196864"]["shipment"]["status"] == "LABEL_CREATED"
    assert "delivered_at" not in d["PP-R-YOV-10196864"]["shipment"]
    assert linked_count(data, "PP-R-QQW-10196399") >= 2
    for other in ("PP-R-JTQ-10195324", "PP-R-YOV-10196864", "PP-R-IQA-10196863"):
        assert linked_count(data, other) == 0
    assert float(REAL["PP-R-IQA-10196863"][1]) < 15


def test_seed_file_has_no_real_transaction_ids(data):
    text = json.dumps(data)
    assert "seller_transaction_id" not in text
    assert all(p["dispute_id"].startswith("SEED-") for p in data["past_disputes"])


def test_seeding_uses_transaction_ids_from_dispute_json(data):
    g = FakeGraph()
    report = seed_graph(GraphStore(g), stored(), data)
    tids = {p.get("tid") for _, p in g.calls if p.get("tid")}
    assert {f"TXN-FROM-JSON-{i[-3:]}" for i in REAL} <= tids
    assert any("seeded 3 past dispute(s)" in line for line in report)


def test_every_seeded_node_is_labelled_seeded(data):
    g = FakeGraph()
    seed_graph(GraphStore(g), stored(), data)
    for q, p in g.calls:
        if "Shipment" in q or "Telemetry" in q:
            assert p["props"]["source"] == "seeded"
        if "SEED-" in json.dumps(p) and "tprops" in p:
            assert p["tprops"]["source"] == "seeded" and p["dprops"]["source"] == "seeded"


def test_missing_dispute_is_reported_not_invented(data):
    disputes = stored()
    disputes.pop("PP-R-YOV-10196864")
    report = seed_graph(GraphStore(FakeGraph()), disputes, data)
    assert any(line.startswith("skipped PP-R-YOV-10196864") for line in report)


def test_store_get_and_full_disputes(tmp_path):
    store = DisputeStore(tmp_path / "d.db")
    store.claim("A", "poll", {"dispute_id": "A"}, full=True)
    store.claim("B", "webhook", {"dispute_id": "B"}, full=False)
    assert store.get("A") == {"dispute_id": "A"} and store.get("nope") is None
    assert set(store.full_disputes()) == {"A"}
    store.close()


def test_shipment_dated_before_transaction_is_flagged(data):
    disputes = stored()
    disputes["PP-R-JTQ-10195324"]["disputed_transactions"][0]["create_time"] = "2026-12-01T00:00:00.000Z"
    report = seed_graph(GraphStore(FakeGraph()), disputes, data)
    assert any(line.startswith("warning PP-R-JTQ-10195324: seeded shipped_at") for line in report)
