import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from disputedesk.auth import TokenProvider
from disputedesk.autopilot import AutopilotStore, DAILY_AUTO_ACCEPT_LIMIT, p2_eligibility
from disputedesk.config import SANDBOX_BASE_URL
from disputedesk.decision import DecisionOutput, DecisionResult
from disputedesk.decision_store import DecisionStore
from disputedesk.demo import DEMO_MESSAGE, export_snapshot, scan
from disputedesk.dispute_store import DisputeStore
from disputedesk.disputes import ACCEPT_NOTE, DisputesClient
from disputedesk.drafts import DraftStore
from disputedesk.http_log import make_client
from disputedesk.policies import PolicySettings, PolicyStore
from disputedesk.submissions import SubmissionStore, Submitter
from disputedesk.webhooks import EventStore, create_app
from tests.conftest import TOKEN_URL
from tests.fakes import sample_dispute

ROOT = Path(__file__).resolve().parent.parent


def _p2_decision(decision="ACCEPT", cites=None, linked=None):
    cites = cites if cites is not None else ["F1", "P2"]
    return {
        "status": "ok",
        "decision": decision,
        "risk_score": 10,
        "reasoning": [
            {"point": "Small amount under P2.", "cites": cites},
            {"point": "No links.", "cites": ["F2"]},
            {"point": "Cost rule.", "cites": ["F1"]},
        ],
        "graph_context": {"linked_disputes": linked or []},
    }


def _p2_dispute(dispute_id="PP-R-P2-1", value="12.00", currency="USD", messages=None, evidences=None):
    d = sample_dispute(dispute_id, value=value)
    d["dispute_amount"]["currency_code"] = currency
    if messages is not None:
        d["messages"] = messages
    if evidences is not None:
        d["evidences"] = evidences
    return d


def test_p2_eligibility_enforces_amount_links_and_buyer_claims():
    policy = PolicySettings(accept_under_enabled=True, accept_under_amount=50.0)
    assert p2_eligibility(_p2_dispute(value="14.99"), _p2_decision(), policy, None) == (True, None)

    assert p2_eligibility(_p2_dispute(value="15.00"), _p2_decision(), policy, None) == (False, "amount_not_under_limit")
    assert p2_eligibility(_p2_dispute(value="12.00", currency="EUR"), _p2_decision(), policy, None) == (False, "invalid_amount")
    assert p2_eligibility(_p2_dispute(value="12.00"), _p2_decision(decision="DEFEND"), policy, None) == (False, "not_accept_decision")
    assert p2_eligibility(_p2_dispute(value="12.00"), _p2_decision(cites=["F1", "P4"]), policy, None) == (False, "not_p2_decision")
    assert p2_eligibility(_p2_dispute(value="12.00"), _p2_decision(linked=[{"id": "SEED-1"}]), policy, None) == (False, "linked_disputes_present")

    with_url = _p2_dispute(messages=[{"posted_by": "BUYER", "content": "See https://evil.example/proof"}])
    assert p2_eligibility(with_url, _p2_decision(), policy, None) == (False, "external_link_in_claim")

    with_www = _p2_dispute(evidences=[{"source": "REQUESTED_FROM_BUYER", "notes": "check www.example.com"}])
    assert p2_eligibility(with_www, _p2_decision(), policy, None) == (False, "external_link_in_claim")

    with_doc = _p2_dispute(evidences=[{"source": "REQUESTED_FROM_BUYER",
                                      "documents": [{"name": "img.png", "url": "https://api-m.sandbox.paypal.com/x"}]}])
    assert p2_eligibility(with_doc, _p2_decision(), policy, None) == (False, "external_link_in_claim")


@pytest.fixture
def env(tmp_path, settings):
    clock = [datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)]
    db = tmp_path / "x.db"
    d, dec, dr, sub = DisputeStore(db), DecisionStore(db), DraftStore(db), SubmissionStore(db)
    pol = PolicyStore(db)
    ap = AutopilotStore(db, now_fn=lambda: clock[0])
    http = make_client(tmp_path / "submit_http.jsonl", (settings.client_id, settings.client_secret))
    client = DisputesClient(settings, TokenProvider(settings, http), http)
    submitter = Submitter(d, dec, dr, sub, client)
    app = create_app(
        verifier=object(),
        store=EventStore(tmp_path / "logs"),
        dispute_store=d,
        decision_store=dec,
        policy_store=pol,
        draft_store=dr,
        submitter=submitter,
        autopilot_store=ap,
    )
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}))
        yield {"api": TestClient(app), "d": d, "dec": dec, "ap": ap, "clock": clock, "paypal": router}
    http.close()
    for s in (d, dec, dr, sub, pol, ap):
        s.close()


def _seed_p2(env, dispute_id="PP-R-P2-1", value="12.00"):
    env["d"].claim(dispute_id, "poll", _p2_dispute(dispute_id, value=value), full=True)
    out = DecisionOutput.model_validate({
        "decision": "ACCEPT",
        "risk_score": 10,
        "evidence_summary": ACCEPT_NOTE,
        "reasoning": [
            {"point": "Under P2 threshold.", "cites": ["F1", "P2"]},
            {"point": "No links.", "cites": ["F2"]},
            {"point": "Small dispute.", "cites": ["F1"]},
        ],
    })
    env["dec"].save(
        dispute_id,
        DecisionResult("ok", "m", 1, output=out, facts=[{"id": "F1", "text": "x", "source": "paypal"}]),
        {"linked_disputes": []},
        [{"id": "P2", "text": "Accept under 15.00 USD"}],
    )


def test_autopilot_off_by_default_and_30s_cancel_window(env):
    _seed_p2(env, "PP-R-P2-1", value="11.50")
    api, paypal, clock = env["api"], env["paypal"], env["clock"]

    state = api.get("/api/autopilot").json()
    assert state["enabled"] is False and state["pending"] == [] and state["daily_limit"] == 5

    # Enable opt-in Autopilot -> arms 30-second countdown
    armed = api.put("/api/autopilot/settings", json={"enabled": True}).json()
    assert armed["enabled"] is True and len(armed["pending"]) == 1
    assert armed["pending"][0]["dispute_id"] == "PP-R-P2-1"
    assert armed["pending"][0]["remaining_seconds"] == pytest.approx(30.0)

    # Cannot execute before 30s elapses
    clock[0] += timedelta(seconds=15)
    early = api.post("/api/submissions/PP-R-P2-1",
                     json={"confirm": True, "action": "accept_claim", "text": ACCEPT_NOTE, "autopilot": True})
    assert early.status_code == 409 and early.json()["error"] == "countdown_active"
    assert paypal.calls.call_count == 0

    # After 30s elapses -> executes accept_claim and logs to Activity
    clock[0] += timedelta(seconds=15)
    url = f"{SANDBOX_BASE_URL}/v1/customer/disputes/PP-R-P2-1"
    paypal.get(url).mock(return_value=httpx.Response(200, json=_p2_dispute("PP-R-P2-1", value="11.50")))
    accept_call = paypal.post(f"{url}/accept-claim").mock(return_value=httpx.Response(200, json={"links": []}))

    done = api.post("/api/submissions/PP-R-P2-1",
                    json={"confirm": True, "action": "accept_claim", "text": ACCEPT_NOTE, "autopilot": True})
    assert done.status_code == 200 and accept_call.call_count == 1

    after = api.get("/api/autopilot").json()
    assert after["daily_used"] == 1 and after["daily_remaining"] == 4 and after["pending"] == []
    activity = api.get("/api/autopilot/activity").json()["events"]
    assert any(e["kind"] == "autopilot_executed" and "1/5 today" in e["message"] for e in activity)
    case_body = api.get("/api/decisions/PP-R-P2-1").json()
    assert any(e["kind"] == "autopilot_executed" for e in case_body["autopilot_events"])


def test_autopilot_cancel_aborts_before_paypal_fires(env):
    _seed_p2(env, "PP-R-P2-CAN")
    api, paypal, clock = env["api"], env["paypal"], env["clock"]

    api.put("/api/autopilot/settings", json={"enabled": True})
    cancelled = api.post("/api/autopilot/PP-R-P2-CAN/cancel", json={})
    assert cancelled.status_code == 200 and cancelled.json()["job"]["state"] == "cancelled"

    clock[0] += timedelta(seconds=45)
    blocked = api.post("/api/submissions/PP-R-P2-CAN",
                       json={"confirm": True, "action": "accept_claim", "text": ACCEPT_NOTE, "autopilot": True})
    assert blocked.status_code == 409 and blocked.json()["error"] == "autopilot_cancelled"
    assert paypal.calls.call_count == 0
    assert any(e["kind"] == "autopilot_cancelled" for e in api.get("/api/autopilot/activity").json()["events"])


def test_autopilot_enforces_daily_cap_of_5_in_sqlite(env):
    api, paypal, clock = env["api"], env["paypal"], env["clock"]
    for i in range(1, 7):
        _seed_p2(env, f"PP-R-CAP-{i}", value="9.00")

    armed = api.put("/api/autopilot/settings", json={"enabled": True}).json()
    assert len(armed["pending"]) == DAILY_AUTO_ACCEPT_LIMIT
    assert armed["jobs"]["PP-R-CAP-6"]["state"] == "capped"

    clock[0] += timedelta(seconds=31)
    for i in range(1, 6):
        url = f"{SANDBOX_BASE_URL}/v1/customer/disputes/PP-R-CAP-{i}"
        paypal.get(url).mock(return_value=httpx.Response(200, json=_p2_dispute(f"PP-R-CAP-{i}", value="9.00")))
        paypal.post(f"{url}/accept-claim").mock(return_value=httpx.Response(200, json={"links": []}))
        r = api.post(f"/api/submissions/PP-R-CAP-{i}",
                     json={"confirm": True, "action": "accept_claim", "text": ACCEPT_NOTE, "autopilot": True})
        assert r.status_code == 200

    # 6th dispute cannot execute on the same day
    capped = api.post("/api/submissions/PP-R-CAP-6",
                      json={"confirm": True, "action": "accept_claim", "text": ACCEPT_NOTE, "autopilot": True})
    assert capped.status_code in (409, 429)
    assert api.get("/api/autopilot").json()["daily_used"] == 5


def test_demo_mode_blocks_and_hides_autopilot(tmp_path, monkeypatch):
    src = tmp_path / "src.db"
    d, dec = DisputeStore(src), DecisionStore(src)
    d.claim("PP-R-TST-1", "poll", _p2_dispute("PP-R-TST-1", value="10.00"), full=True)
    out = DecisionOutput.model_validate({
        "decision": "ACCEPT", "risk_score": 10, "evidence_summary": ACCEPT_NOTE,
        "reasoning": [{"point": "a", "cites": ["F1", "P2"]}, {"point": "b", "cites": ["F1"]}, {"point": "c", "cites": ["F1"]}],
    })
    dec.save("PP-R-TST-1", DecisionResult("ok", "m", 1, output=out, facts=[{"id": "F1", "text": "x", "source": "paypal"}]),
             {"linked_disputes": []})
    d.close()
    dec.close()

    snap, mapping = export_snapshot(src)
    assert scan(snap, list(mapping)) == []
    snap_path = tmp_path / "demo_snapshot.json"
    snap_path.write_text(json.dumps(snap), encoding="utf-8")

    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("DEMO_SNAPSHOT", str(snap_path))
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        with TestClient(create_app()) as client:
            for method, path, body in (
                ("get", "/api/autopilot", None),
                ("get", "/api/autopilot/activity", None),
                ("put", "/api/autopilot/settings", {"enabled": True}),
                ("post", "/api/autopilot/PP-R-TST-1/cancel", {}),
                ("post", "/api/submissions/PP-R-TST-1",
                 {"confirm": True, "action": "accept_claim", "text": ACCEPT_NOTE, "autopilot": True}),
            ):
                resp = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
                assert resp.status_code == 403 and resp.json() == {"error": "demo_mode", "message": DEMO_MESSAGE}
            case_body = client.get("/api/decisions/PP-R-TST-1").json()
            assert case_body["autopilot"] is None and case_body["autopilot_events"] == []
        assert router.calls.call_count == 0

    static = ROOT / "disputedesk" / "web" / "static"
    common = (static / "common.js").read_text(encoding="utf-8")
    assert "if (disposed || m.demo)" in common and "if (disposed || mode.demo || !state)" in common
    policies = (static / "policies.js").read_text(encoding="utf-8")
    assert "if (DD.mode.demo)" in policies and "mountAutopilot" in policies.split("else {", 1)[1]
    activity = (static / "activity.js").read_text(encoding="utf-8")
    assert "!DD.mode.demo" in activity

