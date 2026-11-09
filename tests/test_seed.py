import json
from pathlib import Path

import pytest

from disputedesk.dispute_store import DisputeStore
from disputedesk.graph import GraphStore
from disputedesk.seed import load_seed, seed_graph, shipment_timing_problems
from tests.fakes import FakeGraph, sample_dispute

REAL = {
    "PP-R-JTQ-10195324": ("MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "25.00"),
    "PP-R-YOV-10196864": ("MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "50.00"),
    "PP-R-QQW-10196399": ("MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "40.00"),
    "PP-R-IQA-10196863": ("MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "10.00"),
    "PP-R-CTG-10197156": ("MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", "12.00"),
    "PP-R-WZR-10197157": ("MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "27.00"),
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


def test_seed_covers_the_real_disputes(data):
    assert set(data["disputes"]) == set(REAL)


def test_ctg_and_wzr_records_match_their_scenarios(data):
    d = data["disputes"]
    ctg, wzr = d["PP-R-CTG-10197156"], d["PP-R-WZR-10197157"]
    assert ctg["shipment"]["status"] == "DELIVERED" and ctg["shipment"]["signed_by_buyer"] is False
    assert wzr["shipment"]["status"] == "DELIVERED" and wzr["shipment"]["signed_by_buyer"] is True
    for dispute_id in ("PP-R-CTG-10197156", "PP-R-WZR-10197157"):
        assert d[dispute_id]["telemetry"] and linked_count(data, dispute_id) == 0  # a device record, no links


# --- seed validation: shipments sit between the transaction and the dispute being opened ----------

TIMES = json.loads((Path(__file__).resolve().parent / "fixtures" / "seed_dispute_times.json").read_text(encoding="utf-8"))["disputes"]
# Older sandbox records seeded before this rule existed: their shipments are dated after the dispute was
# opened. They are kept unchanged on purpose and named here, so any NEW seeded dispute is still checked.
EXEMPT_LEGACY = {"PP-R-JTQ-10195324", "PP-R-QQW-10196399", "PP-R-IQA-10196863"}


def test_every_seeded_shipment_is_dated_between_its_transaction_and_its_dispute(data):
    assert set(TIMES) == set(data["disputes"])  # every seeded dispute has its stored times on file
    problems = []
    for dispute_id, ctx in data["disputes"].items():
        if dispute_id in EXEMPT_LEGACY:
            continue
        t = TIMES[dispute_id]
        problems += shipment_timing_problems(dispute_id, t["transaction_created"], t["dispute_created"], ctx.get("shipment"))
    assert problems == []


def test_the_exemptions_are_only_the_three_legacy_records(data):
    # if one of them is ever corrected, drop it from EXEMPT_LEGACY so the rule covers it again
    for dispute_id in EXEMPT_LEGACY:
        t = TIMES[dispute_id]
        assert shipment_timing_problems(dispute_id, t["transaction_created"], t["dispute_created"], data["disputes"][dispute_id].get("shipment"))


def test_the_times_fixture_matches_the_recorded_disputes():
    snapshot = json.loads((Path(__file__).resolve().parent.parent / "data" / "demo_snapshot.json").read_text(encoding="utf-8"))
    for row in snapshot["disputes"]:
        d = row["data"]
        if row["dispute_id"] in TIMES:
            assert TIMES[row["dispute_id"]] == {"transaction_created": min(x["create_time"] for x in d["disputed_transactions"]),
                                                "dispute_created": d["create_time"]}, row["dispute_id"]


def test_timing_check_catches_each_kind_of_mistake():
    txn, opened = "2026-11-08T17:07:43.000Z", "2026-11-08T17:12:18.485Z"
    ok = {"shipped_at": "2026-11-08T17:09:14Z", "delivered_at": "2026-11-08T17:10:46Z"}
    assert shipment_timing_problems("D", txn, opened, ok) == []
    late = shipment_timing_problems("D", txn, opened, ok | {"delivered_at": "2026-11-08T17:16:00Z"})
    assert len(late) == 1 and "delivered_at" in late[0] and "after the dispute was opened" in late[0]
    early = shipment_timing_problems("D", txn, opened, ok | {"shipped_at": "2026-11-08T17:00:00Z"})
    assert len(early) == 1 and "before the transaction" in early[0]
    assert shipment_timing_problems("D", txn, opened, {"shipped_at": "2026-11-09"})  # a later day, date only
    assert shipment_timing_problems("D", txn, opened, None) == []


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
