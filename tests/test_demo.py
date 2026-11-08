"""Demo mode: served from the snapshot with no PayPal, Gemini or FalkorDB access, and read-only."""

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from disputedesk import decision as decision_mod
from disputedesk import graph as graph_mod
from disputedesk import webhooks
from disputedesk.decision import SANDBOX_NOTE, DecisionOutput, DecisionResult
from disputedesk.decision_store import DecisionStore
from disputedesk.demo import DEMO_MESSAGE, export_snapshot, scan
from disputedesk.dispute_store import DisputeStore
from disputedesk.graph import GraphStore, context_to_facts
from disputedesk.submissions import SubmissionStore
from tests.fakes import FakeGraph, context_row, sample_dispute

ROOT = Path(__file__).resolve().parent.parent
DID = "PP-R-TST-1"
TEXT = f"{SANDBOX_NOTE} Delivered and signed."


def make_store(tmp_path) -> Path:
    """A real-shaped local store with a buyer email/name/payer ID and a seller merchant ID."""
    db = tmp_path / "src.db"
    d, dec, sub = DisputeStore(db), DecisionStore(db), SubmissionStore(db)
    data = sample_dispute(DID)
    data["disputed_transactions"][0]["seller"] = {"merchant_id": "MERCH123XYZ", "email": "shop@example.com", "name": "Shop Owner"}
    data["links"][1]["href"] = "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-R-TST-1/accept-claim"
    data["messages"] = [{"posted_by": "BUYER", "content": "Contact me at buyer@example.com"}]
    d.claim(DID, "poll", data, full=True)
    ctx = GraphStore(FakeGraph([context_row()])).dispute_context(DID)
    facts = context_to_facts(ctx)
    out = DecisionOutput.model_validate({"decision": "DEFEND", "risk_score": 10, "evidence_summary": TEXT,
        "reasoning": [{"point": "Buyer BUYER1 got it.", "cites": ["F2"]}, {"point": "b", "cites": ["F5"]}, {"point": "c", "cites": ["F7"]}]})
    dec.save(DID, DecisionResult("ok", "gemini/test", 1, output=out, facts=facts), ctx, [{"id": "P1", "text": "rule"}])
    sub.claim(DID, "provide_evidence", TEXT)
    sub.finish(DID, "succeeded", 200, None)
    sub.mark_verified("provide_evidence_notes_only", DID)
    for s in (d, dec, sub):
        s.close()
    return db


@pytest.fixture
def snapshot_file(tmp_path):
    snap, mapping = export_snapshot(make_store(tmp_path))
    assert scan(snap, list(mapping)) == []
    path = tmp_path / "demo_snapshot.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    return path


@pytest.fixture
def no_outside_access(monkeypatch):
    """Any PayPal/HTTP request, FalkorDB connection, LLM call or .env load fails the test."""
    def boom(*_a, **_k):
        raise AssertionError("demo mode must not touch this")
    monkeypatch.setattr(webhooks, "load_settings", boom)
    monkeypatch.setattr(webhooks, "load_agent_settings", boom)
    monkeypatch.setattr(webhooks, "connect", boom)
    monkeypatch.setattr(graph_mod, "connect", boom)
    monkeypatch.setattr(decision_mod, "_litellm_completion", boom)
    for var in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_WEBHOOK_ID", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:  # no routes: every request fails
        yield router


@pytest.fixture
def demo_client(snapshot_file, no_outside_access, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("DEMO_SNAPSHOT", str(snapshot_file))
    with TestClient(webhooks.create_app()) as client:  # runs the lifespan: proves no poller/analyzer starts
        yield client, no_outside_access


# --- export ------------------------------------------------------------------------------------

def test_export_strips_identifiers_and_keeps_labels(tmp_path):
    snap, mapping = export_snapshot(make_store(tmp_path))
    text = json.dumps(snap)
    for original in ("buyer@example.com", "shop@example.com", "BUYER1", "MERCH123XYZ", "Test Buyer", "Shop Owner"):
        assert original not in text, original
    assert "sandbox-buyer-1" in text
    dispute = snap["disputes"][0]
    assert dispute["source"] == "paypal" and dispute["detected_via"] == "poll"
    assert "messages" not in dispute["data"] and "buyer" not in dispute["data"]["disputed_transactions"][0]
    assert all("href" not in link for link in dispute["data"]["links"])
    facts = snap["decisions"][0]["facts"]
    assert {f["source"] for f in facts} == {"paypal", "seeded"}
    assert any(f["text"] == "Buyer PayPal payer_id sandbox-buyer-1." for f in facts)
    assert snap["decisions"][0]["reasoning"][0]["point"] == "Buyer sandbox-buyer-1 got it."
    assert snap["submissions"][0]["state"] == "succeeded" and snap["verified_behaviours"]


@pytest.mark.parametrize("planted,check", [
    ("someone@example.org", "email_address"),
    ("A21AAFakeTokenValue1234567890abc", "paypal_access_token"),
    ("Bearer abcdefghijklmnop", "bearer_token"),
    ("AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKE12", "google_api_key"),
])
def test_scan_catches_planted_values(tmp_path, planted, check):
    snap, mapping = export_snapshot(make_store(tmp_path))
    snap["decisions"][0]["evidence_summary"] += " " + planted
    assert check in scan(snap, list(mapping))
    assert planted not in " ".join(scan(snap, list(mapping)))  # names of checks only, never values


def test_committed_snapshot_passes_the_scan():
    path = ROOT / "data" / "demo_snapshot.json"
    snap = json.loads(path.read_text(encoding="utf-8"))
    assert scan(snap, []) == []
    assert all(d["source"] == "paypal" for d in snap["disputes"])
    assert {f["source"] for dec in snap["decisions"] for f in dec["facts"]} <= {"paypal", "seeded"}


# --- serving ----------------------------------------------------------------------------------

def test_demo_serves_every_page_and_api_without_outside_access(demo_client):
    client, router = demo_client
    for path in ("/", "/overview", "/activity", "/insights", "/policies", f"/case/{DID}", "/static/common.js",
                 "/api/disputes", f"/api/decisions/{DID}", "/api/policies", f"/api/submissions/{DID}/dry-run"):
        assert client.get(path).status_code == 200, path
    mode = client.get("/api/mode").json()
    assert mode["demo"] is True and mode["recorded_at"] and mode["message"] == DEMO_MESSAGE
    rows = client.get("/api/disputes").json()["disputes"]
    assert rows[0]["dispute_id"] == DID and rows[0]["decision"]["decision"] == "DEFEND"
    case = client.get(f"/api/decisions/{DID}").json()
    assert case["decision"]["graph_context"]["customer"]["payer_id"] == "sandbox-buyer-1"
    assert case["submit"]["can_submit"] is False and case["submit"]["submission"]["state"] == "succeeded"
    assert client.get(f"/api/submissions/{DID}/dry-run").json()["sent"] is False
    assert router.calls.call_count == 0  # not a single HTTP request left the app


@pytest.mark.parametrize("method,path,body", [
    ("post", f"/api/submissions/{DID}", {"confirm": True, "action": "provide_evidence", "text": TEXT}),
    ("post", f"/api/submissions/{DID}", {"confirm": True, "action": "accept_claim", "text": "x"}),
    ("put", f"/api/drafts/{DID}", {"text": TEXT}),
    ("put", "/api/policies", {"accept_under_amount": 20}),
    ("post", "/webhooks/paypal", {"id": "WH-1"}),
])
def test_demo_refuses_every_write(demo_client, method, path, body):
    client, router = demo_client
    resp = getattr(client, method)(path, json=body)
    assert resp.status_code == 403 and resp.json() == {"error": "demo_mode", "message": DEMO_MESSAGE}
    assert router.calls.call_count == 0


def test_demo_policy_preview_still_computes_rule_text(demo_client):
    client, _ = demo_client
    resp = client.post("/api/policies/preview", json={"accept_under_amount": 30})
    assert resp.status_code == 200 and "30.00 USD" in resp.json()["rules"][1]["text"]


def test_demo_mode_needs_no_env_file_and_cleans_up(snapshot_file, no_outside_access, monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_SNAPSHOT", str(snapshot_file))
    app = webhooks.create_app(demo=True)
    db_path = Path(app.state.dispute_store.path)
    with TestClient(app) as client:
        assert client.get("/api/disputes").status_code == 200
        assert db_path.exists() and db_path != ROOT / "data" / "disputedesk.db"
    assert not db_path.parent.exists()  # temporary folder removed at shutdown


def test_real_mode_reports_not_demo(tmp_path):
    db = tmp_path / "x.db"
    app = webhooks.create_app(verifier=object(), store=webhooks.EventStore(tmp_path / "l"),
                              dispute_store=DisputeStore(db), demo=False)
    assert TestClient(app).get("/api/mode").json() == {"demo": False}


def test_demo_snapshot_is_committable():
    import subprocess
    out = subprocess.run(["git", "check-ignore", "-q", "data/demo_snapshot.json"], cwd=ROOT)
    assert out.returncode == 1  # not ignored
    assert subprocess.run(["git", "check-ignore", "-q", "data/disputedesk.db"], cwd=ROOT).returncode == 0


def test_ui_marks_demo_mode():
    web = ROOT / "disputedesk" / "web" / "static"
    common = (web / "common.js").read_text(encoding="utf-8")
    assert 'const DEMO_LABEL = "Demo mode: recorded sandbox data";' in common and '"Demo data"' in common
    assert 'fetch("/api/mode"' in common
    case_js = (web / "case.js").read_text(encoding="utf-8")
    assert "DD.DEMO_LABEL" in case_js and "await DD.modeReady" in case_js
    assert "DD.DEMO_LABEL" in (web / "policies.js").read_text(encoding="utf-8")
