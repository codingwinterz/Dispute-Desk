import threading

import pytest

from disputedesk.dispute_store import DisputeStore
from disputedesk.events import DisputeDetected, EventBus

DISPUTE = {
    "dispute_id": "PP-R-TEST-1",
    "status": "WAITING_FOR_SELLER_RESPONSE",
    "dispute_life_cycle_stage": "CHARGEBACK",
    "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
    "dispute_amount": {"currency_code": "USD", "value": "25.00"},
    "seller_response_due_date": "2026-11-20T00:00:00.000Z",
    "links": [
        {"rel": "self", "method": "GET", "href": "https://example.invalid/self"},
        {"rel": "provide_evidence", "method": "POST", "href": "https://example.invalid/pe"},
        {"rel": "accept_claim", "method": "POST", "href": "https://example.invalid/ac"},
    ],
}


@pytest.fixture
def store(tmp_path):
    s = DisputeStore(tmp_path / "disputedesk.db")
    yield s
    s.close()


def test_claim_once_across_sources(store):
    assert store.claim("PP-R-TEST-1", "webhook", {}, full=False) is True
    assert store.claim("PP-R-TEST-1", "poll", DISPUTE, full=True) is False
    assert store.source_of("PP-R-TEST-1") == "webhook"
    assert store.needs_full("PP-R-TEST-1")


def test_update_full_keeps_source(store):
    store.claim("PP-R-TEST-1", "webhook", {"dispute_id": "PP-R-TEST-1"}, full=False)
    store.update_full("PP-R-TEST-1", DISPUTE)

    assert not store.needs_full("PP-R-TEST-1")
    [row] = store.summaries()
    assert row["source"] == "paypal" and row["detected_via"] == "webhook"
    assert row["status"] == "WAITING_FOR_SELLER_RESPONSE"


def test_summary_fields(store):
    store.claim("PP-R-TEST-1", "poll", DISPUTE, full=True)

    [row] = store.summaries()

    assert row["dispute_id"] == "PP-R-TEST-1"
    assert row["stage"] == "CHARGEBACK"
    assert row["amount"] == {"currency_code": "USD", "value": "25.00"}
    assert row["seller_response_due_date"] == "2026-11-20T00:00:00.000Z"
    assert row["source"] == "paypal" and row["detected_via"] == "poll"
    assert row["detected_at"]
    assert row["actions"] == ["accept_claim", "provide_evidence"]  # "self" is a GET, not an action


def test_concurrent_claims_have_one_winner(store):
    barrier = threading.Barrier(8)
    results = []

    def worker(source):
        barrier.wait()
        results.append(store.claim("PP-R-TEST-1", source, DISPUTE, full=True))

    threads = [threading.Thread(target=worker, args=(("poll", "webhook")[i % 2],)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1


def test_store_survives_restart(tmp_path):
    first = DisputeStore(tmp_path / "d.db")
    first.claim("PP-R-TEST-9", "poll", DISPUTE, full=True)
    first.close()

    second = DisputeStore(tmp_path / "d.db")
    assert second.seen("PP-R-TEST-9")
    second.close()


def test_failing_subscriber_does_not_stop_others():
    bus = EventBus()
    got = []

    def broken(_):
        raise RuntimeError("boom")

    bus.subscribe(broken)
    bus.subscribe(got.append)
    bus.emit(DisputeDetected("PP-R-TEST-1", "poll"))

    assert got == [DisputeDetected("PP-R-TEST-1", "poll")]
