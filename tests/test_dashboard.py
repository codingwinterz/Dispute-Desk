import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from disputedesk.decision import DecisionOutput, DecisionResult, decide
from disputedesk.decision_store import DecisionStore
from disputedesk.dispute_store import DisputeStore
from disputedesk.graph import GraphStore, context_to_facts
from disputedesk.webhooks import EventStore, create_app
from tests.fakes import FakeGraph, context_row, sample_dispute

WEB = Path(__file__).resolve().parent.parent / "disputedesk" / "web"
TUNNEL = {"X-Forwarded-For": "203.0.113.7"}

OUT = DecisionOutput.model_validate({
    "decision": "DEFEND", "risk_score": 22, "evidence_summary": "Signed delivery on file.",
    "reasoning": [{"point": "Delivered.", "cites": ["F5"]}, {"point": "Signed.", "cites": ["F5", "P1"]},
                  {"point": "No links.", "cites": ["F7"]}],
})


@pytest.fixture
def stores(tmp_path):
    d, dec = DisputeStore(tmp_path / "x.db"), DecisionStore(tmp_path / "x.db")
    yield d, dec
    d.close()
    dec.close()


@pytest.fixture
def client(stores, tmp_path):
    d, dec = stores
    app = create_app(verifier=object(), store=EventStore(tmp_path / "logs"), dispute_store=d, decision_store=dec)
    return TestClient(app)


def decided(stores, dispute_id="PP-R-TST-1"):
    d, dec = stores
    d.claim(dispute_id, "poll", sample_dispute(dispute_id), full=True)
    ctx = GraphStore(FakeGraph([context_row()])).dispute_context(dispute_id)
    dec.save(dispute_id, DecisionResult("ok", "gemini/test", 1, output=OUT, facts=context_to_facts(ctx)), ctx)


# --- API -------------------------------------------------------------------------------------

def test_queue_rows_carry_decision_summary(client, stores):
    decided(stores)
    stores[0].claim("PP-R-TST-2", "poll", sample_dispute("PP-R-TST-2"), full=True)
    rows = {r["dispute_id"]: r for r in client.get("/api/disputes").json()["disputes"]}
    assert rows["PP-R-TST-1"]["decision"] == {"status": "ok", "decision": "DEFEND", "risk_score": 22,
                                              "created_at": rows["PP-R-TST-1"]["decision"]["created_at"]}
    assert rows["PP-R-TST-2"]["decision"] is None
    assert all(r["source"] == "paypal" for r in rows.values())


def test_decision_endpoint_returns_dispute_decision_and_graph(client, stores):
    decided(stores)
    body = client.get("/api/decisions/PP-R-TST-1").json()
    assert body["dispute"]["dispute_id"] == "PP-R-TST-1" and body["dispute"]["source"] == "paypal"
    rec = body["decision"]
    assert rec["decision"] == "DEFEND" and rec["reasoning"][0]["cites"] == ["F5"]
    assert rec["graph_context"]["shipment"]["status"] == "DELIVERED"
    assert {f["source"] for f in rec["facts"]} <= {"paypal", "seeded"}
    assert all(f["ref"] for f in rec["facts"])


def test_decision_endpoint_pending_and_unknown(client, stores):
    stores[0].claim("PP-R-TST-2", "poll", sample_dispute("PP-R-TST-2"), full=True)
    assert client.get("/api/decisions/PP-R-TST-2").json()["decision"] is None
    resp = client.get("/api/decisions/PP-R-NOPE")
    assert resp.status_code == 404 and resp.json() == {"error": "unknown dispute"}


# --- local-only guard ----------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/case/PP-R-TST-1", "/static/app.css", "/static/queue.js",
                                  "/api/disputes", "/api/decisions/PP-R-TST-1"])
def test_dashboard_and_api_refuse_tunnelled_requests(client, stores, path):
    decided(stores)
    resp = client.get(path, headers=TUNNEL)
    assert resp.status_code == 403
    assert "PP-R-TST-1" not in resp.text


def test_webhook_stays_public_through_the_tunnel(client):
    # Reaches the handler (400 for missing signature headers), not the local-only guard.
    resp = client.post("/webhooks/paypal", content=b"{}", headers=TUNNEL)
    assert resp.status_code == 400 and resp.json() == {"error": "missing signature headers"}


# --- pages -------------------------------------------------------------------------------------

def test_pages_and_assets_are_served_locally(client):
    for path, marker in [("/", 'id="grid"'), ("/case/PP-R-ANY", 'id="case"'), ("/static/app.css", "--accent"),
                         ("/static/common.js", "renderNav"), ("/static/queue.js", "createGrid"),
                         ("/static/case.js", "graphPanel")]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert marker in resp.text, path


def test_case_page_never_embeds_the_requested_id(client):
    assert "<script>alert(1)</script>" not in client.get("/case/<script>alert(1)</script>").text


def test_ag_grid_is_community_pinned_and_shared_assets_used():
    html = (WEB / "queue.html").read_text(encoding="utf-8")
    urls = re.findall(r'(?:src|href)="(https://cdn\.jsdelivr\.net/npm/[^"]+)"', html)
    assert urls and all("ag-grid-community@32.3.3/" in u for u in urls)
    js = "\n".join(p.read_text(encoding="utf-8") for p in (WEB / "static").glob("*.js"))
    assert "enterprise" not in (html + js).lower() and "masterDetail" not in js
    for page in ("queue.html", "case.html"):
        text = (WEB / page).read_text(encoding="utf-8")
        assert "/static/app.css" in text and "/static/common.js" in text  # one stylesheet, one nav
    assert ".innerHTML" not in js and "insertAdjacentHTML" not in js  # data only ever goes in via textContent


def test_facts_carry_refs_but_llm_never_sees_them():
    ctx = GraphStore(FakeGraph([context_row([{"id": "SEED-D-1", "reason": "R", "status": "RESOLVED",
                                               "source": "seeded", "transaction_id": "T", "via": "device_hash"}])])
                     ).dispute_context("PP-R-TST-1")
    facts = context_to_facts(ctx)
    assert {f["ref"] for f in facts} >= {"dispute", "customer", "transaction", "shipment", "telemetry", "linked:SEED-D-1"}
    seen = {}

    def llm(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop after capturing the request")

    decide("PP-R-TST-1", facts, [], "m", completion=llm)
    sent = json.loads(seen["messages"][1]["content"])["facts"]
    assert all(set(f) == {"id", "text", "source"} for f in sent)


def test_old_decisions_get_fact_refs_from_their_graph_snapshot(client, stores):
    decided(stores)
    d, dec = stores
    rec = dec.get("PP-R-TST-1")
    old_facts = [{k: v for k, v in f.items() if k != "ref"} for f in rec["facts"]]
    old_facts[1]["text"] = "edited text that no longer matches"
    with dec._lock, dec._db:
        dec._db.execute("UPDATE decisions SET facts = ? WHERE dispute_id = ?", (json.dumps(old_facts), "PP-R-TST-1"))
    facts = client.get("/api/decisions/PP-R-TST-1").json()["decision"]["facts"]
    assert facts[0]["ref"] == "dispute"
    assert "ref" not in facts[1]  # text mismatch: no guess
    assert facts[4]["ref"] == "shipment"
