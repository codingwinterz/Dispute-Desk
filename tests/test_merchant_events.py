"""Merchant events: import, graph write, re-decision with history, API and case page. LLM and FalkorDB mocked."""

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from disputedesk.decision import DecisionOutput, DecisionResult
from disputedesk.decision_store import DecisionStore
from disputedesk.dispute_store import DisputeStore
from disputedesk.graph import EVENT_LABEL, GraphStore, context_to_facts, parse_context_row
from disputedesk.merchant_events import EventError, MerchantEventStore, find_dispute, parse_event
from tests.fakes import FakeGraph, context_row, sample_dispute

ROOT = Path(__file__).resolve().parent.parent
TXN = "TXN-SELLER-1"
EVENT = {"event_id": "EVT-1", "transaction_id": TXN, "kind": "carrier_scan", "carrier": "USPS", "status": "delivered",
         "location": "Denver, CO, US", "time": "2026-11-07T19:42:00Z", "signed_by_buyer": True}


def load_script():
    spec = importlib.util.spec_from_file_location("merchant_event_script", ROOT / "scripts" / "merchant_event.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def result(decision="ACCEPT", risk=80):
    out = DecisionOutput.model_validate({"decision": decision, "risk_score": risk, "evidence_summary": "summary",
                                         "reasoning": [{"point": f"{decision} because F1.", "cites": ["F1"]}] * 3})
    return DecisionResult("ok", "test-model", 1, output=out, facts=[{"id": "F1", "text": "x", "source": "paypal"}])


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "dd.db"
    disputes = DisputeStore(path)
    disputes.claim("PP-R-TST-1", "poll", sample_dispute(txn=TXN), True)
    disputes.close()
    return path


# --- parsing and resolution ----------------------------------------------------------------------

def test_event_is_validated_strictly():
    ev = parse_event(EVENT)
    assert ev.status == "DELIVERED" and ev.kind == "carrier_scan"
    for bad, why in ((EVENT | {"kind": "refund"}, "kind"), (EVENT | {"time": "yesterday"}, "time"),
                     (EVENT | {"extra": 1}, "extra"), ({k: v for k, v in EVENT.items() if k != "status"}, "status"),
                     (EVENT | {"event_id": "bad id!"}, "event_id")):
        with pytest.raises(EventError) as exc:
            parse_event(bad)
        assert any(why in p for p in exc.value.problems), (why, exc.value.problems)


def test_transaction_ids_come_from_stored_disputes_and_unknown_ones_are_rejected():
    stored = {"PP-R-TST-1": sample_dispute(txn=TXN)}
    assert find_dispute(TXN, stored) == "PP-R-TST-1"
    with pytest.raises(EventError) as exc:
        find_dispute("TXN-INVENTED", stored)
    assert "not on any stored PayPal dispute" in exc.value.problems[0] and "nothing was written" in exc.value.problems[0]


# --- graph ---------------------------------------------------------------------------------------

def test_event_is_written_as_seeded_simulated_merchant_event_with_parameters():
    g = FakeGraph()
    GraphStore(g).add_merchant_event(TXN, {"id": "EVT-1", "kind": "carrier_scan", "status": "DELIVERED", "location": "x",
                                           "time": "2026-11-07T19:42:00Z", "note": None})
    query, params = g.calls[-1]
    assert query.startswith("MATCH (t:Transaction {id: $tid})")  # never creates a transaction
    assert "MERGE (t)-[:HAS_EVENT]->(e)" in query and "DELIVERED" not in query  # values only as parameters
    assert params["tid"] == TXN and params["eid"] == "EVT-1"
    assert params["props"]["source"] == "seeded" and params["props"]["label"] == EVENT_LABEL == "simulated merchant event"
    assert "note" not in params["props"]  # empty fields are left out


def test_context_parses_events_and_turns_each_into_a_seeded_fact():
    row = context_row() + [[{"id": "EVT-1", "kind": "carrier_scan", "status": "DELIVERED", "location": "Denver, CO, US",
                             "time": "2026-11-07T19:42:00Z", "carrier": "USPS", "signed_by_buyer": True, "note": None,
                             "label": EVENT_LABEL, "source": "seeded"}]]
    ctx = parse_context_row(row)
    facts = context_to_facts(ctx)
    event_facts = [f for f in facts if "merchant event" in f["text"]]
    assert len(event_facts) == 1 and event_facts[0]["source"] == "seeded"
    assert "DELIVERED" in event_facts[0]["text"] and "signed by buyer: yes" in event_facts[0]["text"]
    # older rows and snapshots without the column still parse
    assert parse_context_row(context_row())["events"] == []
    assert not any("merchant event" in f["text"] for f in context_to_facts(parse_context_row(context_row())))


# --- decision history ------------------------------------------------------------------------------

def test_redeciding_keeps_the_old_decision_in_history_with_its_trigger(tmp_path):
    store = DecisionStore(tmp_path / "d.db")
    store.save("D1", result("ACCEPT", 80), {"ctx": 1})
    assert store.history("D1") == []
    store.save("D1", result("DEFEND", 20), {"ctx": 2}, trigger="merchant_event:EVT-1")
    now, past = store.get("D1"), store.history("D1")
    assert now["decision"] == "DEFEND" and now["trigger"] == "merchant_event:EVT-1"
    assert len(past) == 1 and past[0]["decision"] == "ACCEPT" and past[0]["risk_score"] == 80
    assert past[0]["replaced_by"] == "merchant_event:EVT-1" and past[0]["trigger"] is None
    store.close()


# --- the script, end to end (graph and LLM mocked) --------------------------------------------------

def run_script(db, tmp_path, event, decide):
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    graph = FakeGraph([context_row()])
    code = load_script().main(["--file", str(path)], graph_factory=lambda: GraphStore(graph), decide_fn=decide,
                              db_path=db, model="test-model")
    return code, graph


def test_import_writes_the_event_and_redecides_keeping_history(db, tmp_path, capsys):
    decisions = DecisionStore(db)
    decisions.save("PP-R-TST-1", result("ACCEPT", 80), {"before": True})
    decisions.close()
    calls = []

    def decide(dispute_id, facts, rules, model):
        calls.append(dispute_id)
        return result("DEFEND", 25)

    code, graph = run_script(db, tmp_path, EVENT, decide)
    assert code == 0 and calls == ["PP-R-TST-1"]
    assert any("HAS_EVENT" in q for q, _ in graph.calls)
    decisions, events = DecisionStore(db), MerchantEventStore(db)
    assert decisions.get("PP-R-TST-1")["decision"] == "DEFEND"
    assert decisions.get("PP-R-TST-1")["trigger"] == "merchant_event:EVT-1"
    assert [h["decision"] for h in decisions.history("PP-R-TST-1")] == ["ACCEPT"]
    stored = events.for_dispute("PP-R-TST-1")
    assert stored[0]["event_id"] == "EVT-1" and stored[0]["source"] == "seeded" and stored[0]["label"] == EVENT_LABEL
    assert stored[0]["note_production"] == "In production your store supplies this; in the sandbox it is simulated."
    out = capsys.readouterr().out
    assert "decision before: ACCEPT (risk 80)" in out and "decision after:  DEFEND (risk 25)" in out
    assert "the recommendation changed" in out
    decisions.close(); events.close()


def test_unknown_transaction_writes_nothing(db, tmp_path, capsys):
    def decide(*_):
        raise AssertionError("the LLM must not be called")

    code, graph = run_script(db, tmp_path, EVENT | {"transaction_id": "TXN-INVENTED"}, decide)
    assert code == 1 and graph.calls == []
    assert MerchantEventStore(db).for_dispute("PP-R-TST-1") == []
    assert "not on any stored PayPal dispute" in capsys.readouterr().out


def test_the_same_event_cannot_be_imported_twice(db, tmp_path):
    code, _ = run_script(db, tmp_path, EVENT, lambda *a: result("ACCEPT", 70))
    assert code == 0
    code, graph = run_script(db, tmp_path, EVENT, lambda *a: result("DEFEND", 10))
    assert code == 1 and graph.calls == []
    assert len(MerchantEventStore(db).for_dispute("PP-R-TST-1")) == 1


def test_unchanged_recommendation_is_reported_as_unchanged(db, tmp_path, capsys):
    decisions = DecisionStore(db)
    decisions.save("PP-R-TST-1", result("ACCEPT", 80), None)
    decisions.close()
    code, _ = run_script(db, tmp_path, EVENT, lambda *a: result("ACCEPT", 75))
    assert code == 0 and "the recommendation did not change; risk 80 -> 75" in capsys.readouterr().out


def test_sample_events_are_valid_and_name_stored_transactions():
    files = sorted((ROOT / "seed" / "events").glob("*.json"))
    assert len(files) >= 2
    snapshot = json.loads((ROOT / "data" / "demo_snapshot.json").read_text(encoding="utf-8"))
    stored = {d["dispute_id"]: d["data"] for d in snapshot["disputes"]}
    for f in files:
        ev = parse_event(json.loads(f.read_text(encoding="utf-8")))
        assert find_dispute(ev.transaction_id, stored)  # a real sandbox transaction, not an invented one


# --- API and case page (read-only display) ---------------------------------------------------------

def test_decision_route_returns_history_and_events_read_only(db, tmp_path):
    from disputedesk.webhooks import EventStore, create_app

    decisions = DecisionStore(db)
    decisions.save("PP-R-TST-1", result("ACCEPT", 80), None)
    decisions.close()
    run_script(db, tmp_path, EVENT, lambda *a: result("DEFEND", 25))
    app = create_app(verifier=object(), store=EventStore(tmp_path / "logs"), dispute_store=DisputeStore(db))
    with TestClient(app) as client:
        body = client.get("/api/decisions/PP-R-TST-1").json()
    assert body["events"][0]["event_id"] == "EVT-1" and body["events"][0]["source"] == "seeded"
    assert body["history"][0]["decision"] == "ACCEPT" and body["history"][0]["replaced_by"] == "merchant_event:EVT-1"
    assert body["decision"]["trigger"] == "merchant_event:EVT-1"


def test_case_page_shows_the_event_and_a_before_after_verdict():
    case = (ROOT / "disputedesk" / "web" / "static" / "case.js").read_text(encoding="utf-8")
    assert "Merchant event received:" in case and "(simulated)" in case
    assert "Re-analyzed after the merchant event:" in case
    change = case.split("function verdictChange(rec)", 1)[1].split("function disposeGraph()", 1)[0]
    assert 'h.replaced_by === trigger' in change and '"The recommendation changed after a merchant event"' in change
    assert "the recommendation did not change" in change
    assert "DD.sourceTag(ev.source)" in change and "ev.note_production" in change  # seeded tag and the note stay
    assert "fetch(" not in change  # display only
