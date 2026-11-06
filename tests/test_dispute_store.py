import pytest

from disputedesk.dispute_store import DisputeStore
from disputedesk.events import DisputeDetected, EventBus


@pytest.fixture
def store(tmp_path):
    s = DisputeStore(tmp_path / "disputes.sqlite3")
    yield s
    s.close()


def test_claim_once_across_sources(store):
    assert store.claim("PP-D-1", "webhook") is True
    assert store.claim("PP-D-1", "poll") is False
    assert store.source_of("PP-D-1") == "webhook"


def test_store_survives_restart(tmp_path):
    first = DisputeStore(tmp_path / "d.sqlite3")
    first.claim("PP-D-9", "poll")
    first.close()

    second = DisputeStore(tmp_path / "d.sqlite3")
    assert second.seen("PP-D-9")
    second.close()


def test_failing_subscriber_does_not_stop_others():
    bus = EventBus()
    got = []

    def broken(_):
        raise RuntimeError("boom")

    bus.subscribe(broken)
    bus.subscribe(got.append)
    bus.emit(DisputeDetected("PP-D-1", "poll"))

    assert got == [DisputeDetected("PP-D-1", "poll")]
