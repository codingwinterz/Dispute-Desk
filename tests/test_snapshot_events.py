"""Snapshot with decision history and merchant events; demo mode replays them. LLM and FalkorDB mocked."""

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from disputedesk import webhooks
from disputedesk.decision import DecisionOutput, DecisionResult
from disputedesk.decision_store import DecisionStore
from disputedesk.demo import export_snapshot, scan
from disputedesk.dispute_store import DisputeStore
from disputedesk.merchant_events import MerchantEventStore, parse_event_file
from tests.fakes import sample_dispute
from tests.test_demo import no_outside_access  # noqa: F401  (fixture: any PayPal/LLM/graph/.env access fails)

ROOT = Path(__file__).resolve().parent.parent
DID = "PP-R-YOV-10196864"
TXN = "5GU29781PX414531M"
EVENT_FILE = ROOT / "seed" / "events" / "yov_delivery_scan.json"
BUYER_PAYER = "QW7ZBUYER0001"         # on the dispute's buyer object
FACT_ONLY_PAYER = "ZZ9FACTONLY0002"   # appears only inside stored decisions (fact text and graph snapshot)
SANDBOX_EMAIL = "sb-47abc123xyz@personal.example.com"


def decision(decision, risk, payer):
    facts = [{"id": "F1", "text": f"Buyer PayPal payer_id {payer}.", "source": "paypal", "ref": "customer"},
             {"id": "F2", "text": f"Contact {SANDBOX_EMAIL} (account sb-47abc123xyz).", "source": "paypal"},
             {"id": "F3", "text": "Linked past dispute by SEED-BUYER-A.", "source": "seeded"}]
    out = DecisionOutput.model_validate({"decision": decision, "risk_score": risk, "evidence_summary": "Delivered.",
        "reasoning": [{"point": f"payer_id {payer} matches; sb-47abc123xyz noted.", "cites": ["F1"]}] * 3})
    ctx = {"customer": {"payer_id": payer, "source": "paypal"}, "events": []}
    return DecisionResult("ok", "gemini/test", 1, output=out, facts=facts), ctx


@pytest.fixture
def source_db(tmp_path):
    """A scratch-like store after the YOV event: an earlier decision archived, the event, the new decision."""
    db = tmp_path / "scratch.db"
    data = sample_dispute(DID, txn=TXN)
    tx = data["disputed_transactions"][0]
    tx["buyer"] = {"payer_id": BUYER_PAYER, "name": "Pat Buyer", "email": SANDBOX_EMAIL}
    tx["seller"] = {"merchant_id": "MERCHX99", "email": "sb-seller9@business.example.com", "name": "Shop"}
    data["messages"] = [{"posted_by": "BUYER", "content": f"write to {SANDBOX_EMAIL}"}]
    d, dec, ev = DisputeStore(db), DecisionStore(db), MerchantEventStore(db)
    d.claim(DID, "poll", data, full=True)
    first, ctx1 = decision("ACCEPT", 80, FACT_ONLY_PAYER)
    dec.save(DID, first, ctx1)
    event = parse_event_file(EVENT_FILE)
    ev.add(event, DID)
    second, ctx2 = decision("ACCEPT", 65, BUYER_PAYER)
    dec.save(DID, second, ctx2, trigger=f"merchant_event:{event.event_id}")
    for s in (d, dec, ev):
        s.close()
    return db


def written_snapshot(source_db, tmp_path) -> tuple[dict, str]:
    snap, mapping = export_snapshot(source_db)
    assert scan(snap, list(mapping)) == []
    path = tmp_path / "demo_snapshot.json"
    path.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
    return snap, path.read_text(encoding="utf-8")


def test_exported_file_has_no_payer_id_at_sign_or_sandbox_account(source_db, tmp_path):
    _, text = written_snapshot(source_db, tmp_path)
    for leaked in (BUYER_PAYER, FACT_ONLY_PAYER, "@", "sb-", "MERCHX99", "Pat Buyer"):
        assert leaked not in text, leaked
    assert "Buyer PayPal payer_id sandbox-buyer-" in text  # the fact survives, pseudonymised
    assert "SEED-BUYER-A" in text  # seeded test IDs are not PayPal identifiers and stay labelled


def test_snapshot_carries_history_events_and_the_trigger(source_db, tmp_path):
    snap, _ = written_snapshot(source_db, tmp_path)
    assert snap["decisions"][0]["trigger"] == "merchant_event:EVT-YOV-SCAN-1"
    [hist] = snap["decision_history"]
    assert hist["replaced_by"] == "merchant_event:EVT-YOV-SCAN-1" and hist["record"]["risk_score"] == 80
    assert hist["record"]["facts"][0]["text"].startswith("Buyer PayPal payer_id sandbox-buyer-")
    [event] = snap["merchant_events"]
    assert event["event_id"] == "EVT-YOV-SCAN-1" and event["label"] == "simulated merchant event"
    assert event["source"] == "seeded" and event["data"]["status"] == "DELIVERED"
    # Buyer and seller objects and messages are still never copied.
    data = snap["disputes"][0]["data"]
    assert "messages" not in data and {"buyer", "seller"}.isdisjoint(data["disputed_transactions"][0])


@pytest.mark.parametrize("planted,check", [("contact me at someone", None), ("x @ y", "at_sign"),
                                           ("account sb-zz11", "sandbox_account")])
def test_scan_fails_on_any_at_sign_or_sandbox_account(source_db, planted, check):
    snap, mapping = export_snapshot(source_db)
    snap["decisions"][0]["evidence_summary"] += " " + planted
    failed = scan(snap, list(mapping))
    assert (check in failed) if check else failed == []
    assert planted not in " ".join(failed)


@pytest.fixture
def demo_with_events(source_db, tmp_path, no_outside_access, monkeypatch):  # noqa: F811
    _, text = written_snapshot(source_db, tmp_path)
    path = tmp_path / "snap.json"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("DEMO_SNAPSHOT", str(path))
    with TestClient(webhooks.create_app()) as client:  # lifespan runs: no poller, analyzer, graph or LLM
        yield client, no_outside_access


def test_demo_mode_serves_the_recorded_history_and_event(demo_with_events):
    client, router = demo_with_events
    body = client.get(f"/api/decisions/{DID}").json()
    assert body["decision"]["trigger"] == "merchant_event:EVT-YOV-SCAN-1"
    assert [(h["decision"], h["risk_score"], h["replaced_by"]) for h in body["history"]] == \
        [("ACCEPT", 80, "merchant_event:EVT-YOV-SCAN-1")]
    [event] = body["events"]
    assert event["event_id"] == "EVT-YOV-SCAN-1" and event["location"] == "Denver, CO, US"
    assert client.get("/api/mode").json()["demo"] is True
    assert client.post(f"/api/submissions/{DID}", json={"confirm": True}).status_code == 403  # still read-only
    assert len(router.calls) == 0


def test_case_page_labels_replayed_events_as_recorded_in_demo_mode():
    case = (ROOT / "disputedesk" / "web" / "static" / "case.js").read_text(encoding="utf-8")
    assert 'const RECORDED_EVENT = "recorded, simulated merchant event";' in case
    timeline = case.split("function caseTimeline", 1)[1].split("function verdictChange", 1)[0]
    assert "DD.mode.demo ? `(${RECORDED_EVENT})` : \"(simulated)\"" in timeline
    change = case.split("function verdictChange", 1)[1].split("function disposeGraph", 1)[0]
    assert "Recorded, simulated merchant event (demo data)." in change
    # Countdowns in demo mode run on the snapshot's recorded time (shared clock in common.js).
    common = (ROOT / "disputedesk" / "web" / "static" / "common.js").read_text(encoding="utf-8")
    assert "return mode.demo && mode.recorded_at ? Date.parse(mode.recorded_at) : Date.now();" in common


def test_export_script_writes_history_and_events(source_db, tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("export_demo_script", ROOT / "scripts" / "export_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "DISPUTE_DB", source_db)
    out = tmp_path / "out.json"
    assert mod.main(["--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "1 earlier decisions, 1 merchant events" in printed and str(tmp_path) not in printed
    text = out.read_text(encoding="utf-8")
    assert BUYER_PAYER not in text and FACT_ONLY_PAYER not in text and "@" not in text and "sb-" not in text
