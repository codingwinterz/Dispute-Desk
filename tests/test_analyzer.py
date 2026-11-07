import time

import pytest

from disputedesk.analyzer import Analyzer
from disputedesk.decision import DecisionOutput, DecisionResult
from disputedesk.decision_store import DecisionStore
from disputedesk.dispute_store import DisputeStore
from disputedesk.events import DisputeDetected, DisputeUpdated, EventBus
from disputedesk.graph import GraphStore
from tests.fakes import FakeGraph, context_row, sample_dispute

OUT = DecisionOutput.model_validate({
    "decision": "ACCEPT", "risk_score": 70, "evidence_summary": "No carrier scan.",
    "reasoning": [{"point": "x", "cites": ["F1"]}, {"point": "y", "cites": ["P4"]}, {"point": "z", "cites": ["F2"]}],
})


class FakeDecide:
    def __init__(self, result="ok"):
        self.calls = []
        self.result = result

    def __call__(self, dispute_id, facts, rules, model):
        self.calls.append(dispute_id)
        if self.result == "raise":
            raise AssertionError("decide must not raise")
        if self.result == "ok":
            return DecisionResult("ok", model, 1, output=OUT, facts=facts)
        return DecisionResult("error", model, 2, error="invalid_output:schema", facts=facts)


@pytest.fixture
def stores(tmp_path):
    d, dec = DisputeStore(tmp_path / "x.db"), DecisionStore(tmp_path / "x.db")
    yield d, dec
    d.close()
    dec.close()


def make(stores, decide=None, graph=None):
    graph = graph if graph is not None else FakeGraph([context_row()])
    decide = decide or FakeDecide()
    return Analyzer(stores[0], stores[1], lambda: GraphStore(graph), "gemini/test", decide_fn=decide), decide, graph


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_detected_event_is_analyzed_once_on_worker(stores):
    disputes, decisions = stores
    disputes.claim("PP-R-TST-1", "poll", sample_dispute(), full=True)
    analyzer, decide, _ = make(stores)
    bus = EventBus()
    bus.subscribe(analyzer.on_event)
    analyzer.start()
    try:
        bus.emit(DisputeDetected("PP-R-TST-1", "poll"))
        assert wait_for(lambda: decisions.has_ok("PP-R-TST-1"))
        bus.emit(DisputeUpdated("PP-R-TST-1", "poll"))  # already decided: not re-run
        time.sleep(0.2)
    finally:
        analyzer.stop()
    assert decide.calls == ["PP-R-TST-1"]
    rec = decisions.get("PP-R-TST-1")
    assert rec["decision"] == "ACCEPT" and rec["risk_score"] == 70 and rec["facts"][0]["id"] == "F1"
    assert rec["graph_context"]["dispute"]["id"] == "PP-R-TST-1"


def test_webhook_snapshot_waits_for_full_details(stores):
    disputes, decisions = stores
    disputes.claim("PP-R-TST-1", "webhook", {"dispute_id": "PP-R-TST-1"}, full=False)
    analyzer, decide, _ = make(stores)
    assert analyzer.analyze("PP-R-TST-1") == "waiting_for_full_details"
    assert decide.calls == [] and decisions.get("PP-R-TST-1") is None
    disputes.update_full("PP-R-TST-1", sample_dispute())  # what the poller's backfill does
    assert analyzer.analyze("PP-R-TST-1") == "ok"


def test_sweep_queues_only_undecided_full_disputes(stores):
    disputes, decisions = stores
    disputes.claim("A", "poll", sample_dispute("A"), full=True)
    disputes.claim("B", "poll", sample_dispute("B"), full=True)
    disputes.claim("C", "webhook", {"dispute_id": "C"}, full=False)
    analyzer, _, _ = make(stores)
    analyzer.analyze("A")
    assert analyzer.sweep() == 1


def test_llm_error_is_stored_and_retried_later(stores):
    disputes, decisions = stores
    disputes.claim("PP-R-TST-1", "poll", sample_dispute(), full=True)
    analyzer, _, _ = make(stores, decide=FakeDecide("error"))
    assert analyzer.analyze("PP-R-TST-1") == "invalid_output:schema"
    assert decisions.get("PP-R-TST-1")["status"] == "error"
    assert analyzer.sweep() == 1  # an error is not a final decision


def test_graph_down_never_raises(stores):
    disputes, decisions = stores
    disputes.claim("PP-R-TST-1", "poll", sample_dispute(), full=True)

    def broken():
        raise ConnectionError("down")

    analyzer = Analyzer(disputes, decisions, broken, "m", decide_fn=FakeDecide("raise"))
    assert analyzer.analyze("PP-R-TST-1") == "graph_unavailable:ConnectionError"
    assert decisions.get("PP-R-TST-1")["error"] == "graph_unavailable:ConnectionError"


def test_worker_survives_unexpected_errors(stores):
    disputes, decisions = stores
    disputes.claim("A", "poll", sample_dispute("A"), full=True)
    disputes.claim("B", "poll", sample_dispute("B"), full=True)
    calls = []

    def flaky(dispute_id, facts, rules, model):
        calls.append(dispute_id)
        if dispute_id == "A":
            raise RuntimeError("unexpected")
        return DecisionResult("ok", model, 1, output=OUT, facts=facts)

    analyzer = Analyzer(disputes, decisions, lambda: GraphStore(FakeGraph([context_row()])), "m", decide_fn=flaky)
    analyzer.start()
    try:
        analyzer.enqueue("A")
        analyzer.enqueue("B")
        assert wait_for(lambda: decisions.has_ok("B"))
    finally:
        analyzer.stop()
    assert calls == ["A", "B"]


def test_app_lifespan_starts_sweeps_and_stops_analyzer(stores, tmp_path):
    from fastapi.testclient import TestClient

    from disputedesk.webhooks import EventStore, create_app

    disputes, decisions = stores
    disputes.claim("PP-R-TST-1", "poll", sample_dispute(), full=True)
    analyzer, decide, _ = make(stores)
    bus = EventBus()
    app = create_app(verifier=object(), store=EventStore(tmp_path / "logs"), dispute_store=disputes,
                     bus=bus, analyzer=analyzer)
    with TestClient(app):
        assert wait_for(lambda: decisions.has_ok("PP-R-TST-1"))  # startup sweep
        assert analyzer.on_event in bus._subscribers
    assert not analyzer._thread.is_alive()
    assert decide.calls == ["PP-R-TST-1"]
