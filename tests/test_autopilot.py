import ast
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import respx
from fastapi.testclient import TestClient

from disputedesk import submissions as submissions_mod
from disputedesk.analyzer import Analyzer
from disputedesk.autopilot import (
    BLOCKING_SUBMISSION_STATES,
    DAILY_LIMIT,
    MODE,
    UNAVAILABLE_MESSAGE,
    Autopilot,
    AutopilotStore,
    p2_eligibility,
)
from disputedesk.decision import DecisionOutput, DecisionResult
from disputedesk.decision_store import DecisionStore
from disputedesk.demo import DEMO_MESSAGE, export_snapshot, scan
from disputedesk.dispute_store import DisputeStore
from disputedesk.disputes import ACCEPT_NOTE
from disputedesk.drafts import DraftStore
from disputedesk.events import DisputeUpdated, EventBus
from disputedesk.graph import GraphStore
from disputedesk.policies import PolicySettings, PolicyStore
from disputedesk.submissions import SubmissionStore, Submitter
from disputedesk.webhooks import EventStore, create_app
from tests.fakes import FakeDisputesClient, FakeGraph, context_row, sample_dispute

ROOT = Path(__file__).resolve().parent.parent


def _dispute(
    dispute_id="PP-R-P2-1",
    value="12.00",
    currency="USD",
    stage="CHARGEBACK",
    status="WAITING_FOR_SELLER_RESPONSE",
    messages=None,
    evidences=None,
    has_accept=True,
):
    d = sample_dispute(dispute_id, value=value, status=status)
    d["dispute_amount"]["currency_code"] = currency
    d["dispute_life_cycle_stage"] = stage
    if not has_accept:
        d["links"] = [link for link in d["links"] if link.get("rel") != "accept_claim"]
    if messages is not None:
        d["messages"] = messages
    if evidences is not None:
        d["evidences"] = evidences
    return d


def _decision(decision="ACCEPT", cites=None, linked=None, created_at="2026-11-11T12:00:00+00:00"):
    cites = cites if cites is not None else ["F1", "P2"]
    return {
        "status": "ok",
        "decision": decision,
        "risk_score": 10,
        "created_at": created_at,
        "reasoning": [
            {"point": "Small amount under P2.", "cites": cites},
            {"point": "No linked disputes.", "cites": ["F2"]},
            {"point": "Cost threshold.", "cites": ["F1"]},
        ],
        "graph_context": {"linked_disputes": linked or []},
    }


def _seed_case(stores, dispute_id="PP-R-P2-1", value="12.00", stage="CHARGEBACK", decision="ACCEPT", cites=None):
    d, dec, _pol, _dr, _sub = stores
    raw = _dispute(dispute_id, value=value, stage=stage)
    d.claim(dispute_id, "poll", raw, full=True)
    out = DecisionOutput.model_validate({
        "decision": decision,
        "risk_score": 10,
        "evidence_summary": ACCEPT_NOTE,
        "reasoning": [
            {"point": "Small claim.", "cites": cites or ["F1", "P2"]},
            {"point": "No links.", "cites": ["F1"]},
            {"point": "Policy applies.", "cites": ["F1"]},
        ],
    })
    dec.save(
        dispute_id,
        DecisionResult("ok", "m", 1, output=out, facts=[{"id": "F1", "text": "x", "source": "paypal"}]),
        {"linked_disputes": []},
        [{"id": "P2", "text": "Accept under 15.00 USD"}],
    )
    return raw


def _audit_snapshot(db_path: Path) -> dict[str, list[tuple]]:
    with sqlite3.connect(db_path) as conn:
        return {
            tbl: conn.execute(f"SELECT * FROM {tbl}").fetchall()
            for tbl in ("autopilot_v2_settings", "autopilot_v2_jobs", "autopilot_v2_daily", "autopilot_v2_audit")
        }


# --- Hard rules (a), (b), (c) -------------------------------------------------------------------

def test_autopilot_module_has_no_paypal_write_references():
    src = (ROOT / "disputedesk" / "autopilot.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for forbidden_mod in ("disputedesk.submissions", "disputedesk.disputes", "httpx", "requests", "urllib3"):
        assert forbidden_mod not in imported, forbidden_mod
    for forbidden_sym in ("submitter.submit", "send_action", "build_action_request",
                          "provide_evidence", "provide-evidence", "accept-claim"):
        assert forbidden_sym not in src, forbidden_sym
    assert MODE == "shadow"
    assert BLOCKING_SUBMISSION_STATES == submissions_mod.BLOCKING


def test_v1_tables_are_retired_as_stale_and_enabled_forced_zero(tmp_path):
    db = tmp_path / "x.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE autopilot_settings (id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL, updated_at TEXT)")
        conn.execute("INSERT INTO autopilot_settings VALUES (1, 1, '2026-11-10T00:00:00Z')")
        conn.execute("CREATE TABLE autopilot_jobs (dispute_id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        conn.execute("INSERT INTO autopilot_jobs VALUES ('PP-R-OLD-1', 'pending')")
        conn.execute("INSERT INTO autopilot_jobs VALUES ('PP-R-OLD-2', 'executed')")

    clock = [datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)]
    store = AutopilotStore(db, now_fn=lambda: clock[0])
    try:
        assert store.settings()["enabled"] is False
        assert store.job("PP-R-OLD-1")["state"] == "stale"
        assert store.job("PP-R-OLD-2")["state"] == "stale"
        assert [a["kind"] for a in store.audit_for("PP-R-OLD-1")] == ["v1_retired"]
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT enabled FROM autopilot_settings WHERE id = 1").fetchone()[0] == 0
    finally:
        store.close()

    # Second open does not re-retire
    store2 = AutopilotStore(db, now_fn=lambda: clock[0])
    try:
        assert len(store2.audit_for("PP-R-OLD-1")) == 1
    finally:
        store2.close()


def test_p2_eligibility_enforces_chargeback_amount_links_and_buyer_urls():
    policy = PolicySettings(accept_under_enabled=True, accept_under_amount=50.0)
    assert p2_eligibility(_dispute(value="14.99"), _decision(), policy, None) == (True, None)
    assert p2_eligibility(_dispute(stage="INQUIRY"), _decision(), policy, None) == (False, "inquiry_stage")
    assert p2_eligibility(_dispute(value="15.00"), _decision(), policy, None) == (False, "amount_not_under_limit")
    assert p2_eligibility(_dispute(currency="EUR"), _decision(), policy, None) == (False, "invalid_amount")
    assert p2_eligibility(_dispute(), _decision(decision="DEFEND"), policy, None) == (False, "not_accept_decision")
    assert p2_eligibility(_dispute(), _decision(cites=["F1", "P4"]), policy, None) == (False, "not_p2_decision")
    assert p2_eligibility(_dispute(), _decision(cites=["F1", "P2", "P3"]), policy, None) == (False, "not_p2_decision")
    assert p2_eligibility(_dispute(), _decision(linked=[{"id": "SEED-1"}]), policy, None) == (False, "linked_disputes_present")
    assert p2_eligibility(_dispute(messages=[{"posted_by": "BUYER", "content": "See https://evil.example/x"}]),
                          _decision(), policy, None) == (False, "external_link_in_claim")
    assert p2_eligibility(_dispute(evidences=[{"source": "REQUESTED_FROM_BUYER",
                                               "documents": [{"name": "a.png", "url": "https://api-m.sandbox.paypal.com/1"}]}]),
                          _decision(), policy, None) == (False, "external_link_in_claim")
    assert p2_eligibility(_dispute(has_accept=False), _decision(), policy, None) == (False, "not_submittable")


# --- Runner, state machine, restart recovery, concurrency, daily cap ----------------------------

@pytest.fixture
def env(tmp_path):
    clock = [datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)]
    db = tmp_path / "x.db"
    d = DisputeStore(db)
    dec = DecisionStore(db)
    pol = PolicyStore(db)
    dr = DraftStore(db)
    sub = SubmissionStore(db)
    ap_store = AutopilotStore(db, boot_id="boot-1", now_fn=lambda: clock[0])
    fake_client = FakeDisputesClient()
    submitter = Submitter(d, dec, dr, sub, client=None)
    dry_runs: list[str] = []

    def dry_run(dispute_id: str):
        dry_runs.append(dispute_id)
        return submitter.dry_run(dispute_id)

    ap = Autopilot(ap_store, d, dec, pol, sub, dry_run=dry_run, fetch_fresh=fake_client.get_dispute)
    yield {
        "db": db, "stores": (d, dec, pol, dr, sub), "ap_store": ap_store, "ap": ap,
        "clock": clock, "fake_client": fake_client, "dry_runs": dry_runs, "submitter": submitter,
        "tmp": tmp_path,
    }
    ap_store.close()
    for s in (d, dec, pol, dr, sub):
        s.close()


def test_runner_works_with_no_http_client(env):
    d, dec, pol, _dr, sub = env["stores"]
    _seed_case(env["stores"], "PP-R-NOHTTP")
    ap_store = env["ap_store"]
    ap_no_http = Autopilot(ap_store, d, dec, pol, sub, dry_run=env["submitter"].dry_run, fetch_fresh=None)
    ap_no_http.set_enabled(True)
    ap_no_http.on_decision("PP-R-NOHTTP")
    assert ap_store.job("PP-R-NOHTTP")["state"] == "armed"

    env["clock"][0] += timedelta(seconds=31)
    assert ap_no_http.tick() == 1
    job = ap_store.job("PP-R-NOHTTP")
    assert job["state"] == "cancelled" and job["reason"] == "status_check_unavailable"
    assert env["fake_client"].send_action_calls == []


def test_restart_marks_armed_and_executing_stale_and_never_fires(env):
    _seed_case(env["stores"], "PP-R-RST-1")
    _seed_case(env["stores"], "PP-R-RST-2")
    ap_store, ap = env["ap_store"], env["ap"]
    ap.set_enabled(True)
    ap.on_decision("PP-R-RST-1")
    ap.on_decision("PP-R-RST-2")
    # Force PP-R-RST-2 into executing via claim_due
    env["clock"][0] += timedelta(seconds=31)
    # Only move PP-R-RST-2 manually to executing to simulate mid-execution crash, leaving RST-1 armed
    with sqlite3.connect(env["db"]) as conn:
        conn.execute("UPDATE autopilot_v2_jobs SET state = 'executing' WHERE dispute_id = 'PP-R-RST-2'")

    restarted = AutopilotStore(env["db"], boot_id="boot-2", now_fn=lambda: env["clock"][0])
    try:
        assert restarted.job("PP-R-RST-1")["state"] == "stale"
        assert restarted.job("PP-R-RST-2")["state"] == "stale"
        assert restarted.audit_for("PP-R-RST-1")[-1]["kind"] == "stale"
        assert restarted.audit_for("PP-R-RST-2")[-1]["kind"] == "stale"
        assert restarted.claim_due() == []
        assert env["dry_runs"] == [] and env["fake_client"].send_action_calls == []
    finally:
        restarted.close()


def test_state_machine_transitions_and_audit_counts(env):
    s = env["ap_store"]
    assert s.arm("D-1", 10.0, "USD", "2026-11-11T12:00:00+00:00", None, "analyzer") is not None
    assert s.arm("D-1", 10.0, "USD", "2026-11-11T12:00:00+00:00", None, "analyzer") is None  # already armed
    assert len(s.audit_for("D-1")) == 1

    assert s.finish("D-1", "shadow_done", None, "msg") is False  # not executing yet
    assert s.cancel("D-1", "merchant", "cancelled_by_merchant") is True
    assert s.cancel("D-1", "merchant", "again") is False  # already cancelled
    assert s.arm("D-1", 10.0, "USD", "2026-11-11T12:00:00+00:00", None, "analyzer") is None  # cannot re-arm cancelled
    assert [a["kind"] for a in s.audit_for("D-1")] == ["armed", "cancelled"]


def test_six_armed_with_limit_five_gives_five_shadow_done_and_one_capped(env):
    ap, ap_store, fake_client, clock = env["ap"], env["ap_store"], env["fake_client"], env["clock"]
    ap.set_enabled(True)
    for i in range(1, 7):
        did = f"PP-R-CAP-{i}"
        fake_client.disputes[did] = _seed_case(env["stores"], did, value="10.00")
        ap.on_decision(did)
        assert ap_store.job(did)["state"] == "armed"

    clock[0] += timedelta(seconds=31)
    claimed = ap.tick()
    assert claimed == DAILY_LIMIT  # 5 executing claimed; 6th transitioned armed -> capped
    assert ap_store.daily_used() == 5
    for i in range(1, 6):
        did = f"PP-R-CAP-{i}"
        assert ap_store.job(did)["state"] == "shadow_done"
        audit = ap_store.audit_for(did)
        assert [a["kind"] for a in audit] == ["armed", "executing", "shadow_done"]
        assert audit[-1]["message"] == f"Autopilot preview: would have accepted {did}. Nothing was sent."
    assert ap_store.job("PP-R-CAP-6")["state"] == "capped"
    assert [a["kind"] for a in ap_store.audit_for("PP-R-CAP-6")] == ["armed", "capped"]
    assert len(env["dry_runs"]) == 5
    assert fake_client.send_action_calls == []


def test_two_simultaneous_executions_produce_one_audit_and_one_dry_run(env):
    # Two AutopilotStore instances = two SQLite connections to the same file, each with its own
    # Python lock, so only BEGIN IMMEDIATE and the guarded UPDATEs can serialise the two claims.
    # The second store is opened before arming: opening a store marks already-armed jobs stale.
    d, dec, pol, _dr, sub = env["stores"]
    other_store = AutopilotStore(env["db"], boot_id="boot-other", now_fn=lambda: env["clock"][0])
    try:
        other_ap = Autopilot(other_store, d, dec, pol, sub, dry_run=env["ap"]._dry_run,
                             fetch_fresh=env["fake_client"].get_dispute)
        assert other_store._db is not env["ap_store"]._db and other_store._lock is not env["ap_store"]._lock

        did = "PP-R-RACE-1"
        env["fake_client"].disputes[did] = _seed_case(env["stores"], did, value="11.00")
        env["ap"].set_enabled(True)
        env["ap"].on_decision(did)
        env["clock"][0] += timedelta(seconds=31)

        barrier = threading.Barrier(2)
        results: list[int] = []
        errors: list[BaseException] = []

        def run_tick(ap):
            try:
                barrier.wait()
                results.append(ap.tick())
            except BaseException as exc:  # surfaced below; a failure must fail the test, not vanish
                errors.append(exc)

        t1 = threading.Thread(target=run_tick, args=(env["ap"],))
        t2 = threading.Thread(target=run_tick, args=(other_ap,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert errors == []
        assert sorted(results) == [0, 1]
        assert env["dry_runs"] == [did]
        assert env["fake_client"].send_action_calls == []
        kinds = [a["kind"] for a in env["ap_store"].audit_for(did)]
        assert kinds == ["armed", "executing", "shadow_done"]
        assert [a["kind"] for a in other_store.audit_for(did)] == kinds  # both connections see one history
        assert env["ap_store"].daily_used() == 1  # the loser's cap slot rolled back with its claim
        assert env["ap_store"].settings()["enabled"] is True  # no breaker trip from lock contention
    finally:
        other_store.close()


def test_execution_rechecks_fresh_status_and_decision_changes(env):
    did = "PP-R-FRESH"
    raw = _seed_case(env["stores"], did, value="11.00")
    env["fake_client"].disputes[did] = dict(raw, status="UNDER_REVIEW")
    env["ap"].set_enabled(True)
    env["ap"].on_decision(did)
    env["clock"][0] += timedelta(seconds=31)
    assert env["ap"].tick() == 1
    job = env["ap_store"].job(did)
    assert job["state"] == "cancelled" and job["reason"] == "fresh_not_submittable"
    assert env["dry_runs"] == [] and env["fake_client"].send_action_calls == []


def test_circuit_breaker_and_kill_switch_trip_and_audit(env):
    did = "PP-R-BRK"
    _seed_case(env["stores"], did, value="11.00")
    env["ap"].set_enabled(True)
    env["ap"].on_decision(did)
    assert env["ap_store"].job(did)["state"] == "armed"

    # Kill switch trips breaker and cancels armed jobs
    st = env["ap"].kill()
    assert st["enabled"] is False and st["trip_reason"] == "kill_switch"
    assert env["ap_store"].job(did)["state"] == "cancelled"

    # Exception in fetch_fresh trips breaker
    did2 = "PP-R-BRK-2"
    _seed_case(env["stores"], did2, value="11.00")
    env["ap"].set_enabled(True)
    env["ap"].on_decision(did2)
    env["clock"][0] += timedelta(seconds=31)
    # did2 not in fake_client.disputes -> KeyError in get_dispute -> trips breaker
    assert env["ap"].tick() == 1
    st2 = env["ap"].state()
    assert st2["enabled"] is False and st2["trip_reason"] == "exception:KeyError"
    assert env["ap_store"].job(did2)["state"] == "failed"
    assert env["fake_client"].send_action_calls == []


def test_analyzer_and_poller_arm_and_gets_never_write(env):
    d, dec, pol, dr, sub = env["stores"]
    did = "PP-R-PIPE-1"
    raw = _dispute(did, value="10.00")
    d.claim(did, "poll", raw, full=True)
    env["fake_client"].disputes[did] = raw

    out_json = json.dumps({
        "decision": "ACCEPT",
        "risk_score": 12,
        "reasoning": [
            {"point": "Small amount under P2.", "cites": ["F1", "P2"]},
            {"point": "Buyer id.", "cites": ["F2"]},
            {"point": "No links.", "cites": ["F7"]},
        ],
        "evidence_summary": ACCEPT_NOTE,
    })

    def fake_llm(**_kw):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=out_json))])

    env["ap"].set_enabled(True)
    bus = EventBus()
    app = create_app(
        verifier=object(),
        store=EventStore(env["tmp"] / "logs"),
        dispute_store=d,
        bus=bus,
        decision_store=dec,
        policy_store=pol,
        draft_store=dr,
        submitter=env["submitter"],
        autopilot_store=env["ap_store"],
        autopilot=env["ap"],
    )
    analyzer = Analyzer(d, dec, lambda: GraphStore(FakeGraph([context_row()])), "m",
                        decide_fn=lambda *a: DecisionResult("ok", "m", 1,
                                                            output=DecisionOutput.model_validate_json(out_json),
                                                            facts=[{"id": "F1", "text": "x", "source": "paypal"}]),
                        rules_provider=pol.rules, on_decision=env["ap"].on_decision)
    analyzer.analyze(did)
    assert env["ap_store"].job(did)["state"] == "armed"

    # Poller dispute_updated also triggers arming when stale
    with sqlite3.connect(env["db"]) as conn:
        conn.execute("UPDATE autopilot_v2_jobs SET state = 'stale' WHERE dispute_id = ?", (did,))
    bus.emit(DisputeUpdated(did, "poll"))
    assert env["ap_store"].job(did)["state"] == "armed"

    # GETs never write any autopilot rows (even with an un-evaluated eligible dispute)
    _seed_case(env["stores"], "PP-R-UNARMED", value="9.00")
    before = _audit_snapshot(env["db"])
    client = TestClient(app)
    assert client.get(f"/api/decisions/{did}").status_code == 200
    assert client.get("/api/decisions/PP-R-UNARMED").status_code == 200
    assert client.get("/api/autopilot").status_code == 200
    assert client.get("/api/disputes").status_code == 200
    assert _audit_snapshot(env["db"]) == before
    assert env["ap_store"].job("PP-R-UNARMED") is None

    # Saving policies cancels all armed jobs
    assert client.put("/api/policies", json={"accept_under_amount": 14.0}).status_code == 200
    assert env["ap_store"].job(did)["state"] == "cancelled"
    assert env["fake_client"].send_action_calls == []


def test_demo_and_scratch_modes_return_403_and_hide_ui(tmp_path, monkeypatch):
    src = tmp_path / "src.db"
    d, dec = DisputeStore(src), DecisionStore(src)
    d.claim("PP-R-TST-1", "poll", _dispute("PP-R-TST-1", value="10.00"), full=True)
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

    def check_blocked(client: TestClient, expected_code: str, check_no_live: bool = False):
        for method, path, body in (
            ("get", "/api/autopilot", None),
            ("put", "/api/autopilot/settings", {"enabled": True}),
            ("post", "/api/autopilot/PP-R-TST-1/cancel", {}),
            ("post", "/api/autopilot/kill", {}),
        ):
            r = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
            assert r.status_code == 403 and r.json()["error"] == expected_code
        if check_no_live:
            assert client.post("/api/autopilot/live", json={}).status_code == 404
        assert not any("/live" in getattr(r, "path", "") for r in client.app.routes)
        case_body = client.get("/api/decisions/PP-R-TST-1").json()
        assert case_body["autopilot"] is None and case_body["autopilot_audit"] == []

    # Demo mode
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("DEMO_SNAPSHOT", str(snap_path))
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        with TestClient(create_app()) as client:
            check_blocked(client, "demo_mode")
        assert router.calls.call_count == 0
    monkeypatch.delenv("DEMO_MODE")
    monkeypatch.delenv("DEMO_SNAPSHOT")

    # Scratch mode
    scratch_db = tmp_path / "scratch.db"
    scratch_db.write_bytes(src.read_bytes())
    monkeypatch.setenv("DISPUTEDESK_DB", str(scratch_db))
    monkeypatch.setenv("DISPUTEDESK_GRAPH", "dd_scratch_test")
    with TestClient(create_app(verifier=object(), store=EventStore(tmp_path / "logs"), demo=False)) as client:
        assert client.get("/api/mode").json()["scratch"] is True
        check_blocked(client, "scratch_mode", check_no_live=True)

    # UI checks: labels, demo & scratch hiding, kill switch on every page via common.js, no /live
    static = ROOT / "disputedesk" / "web" / "static"
    common = (static / "common.js").read_text(encoding="utf-8")
    assert "Autopilot preview (never sends)" in common
    assert "Would accept in ${secs} s (preview only)" in common
    assert "Stop Autopilot" in common
    assert "m.demo || m.scratch" in common and "mode.demo || mode.scratch" in common
    assert "/api/autopilot/live" not in common
    policies = (static / "policies.js").read_text(encoding="utf-8")
    assert "!DD.mode.scratch" in policies
    activity = (static / "activity.js").read_text(encoding="utf-8")
    assert "!DD.mode.demo && !DD.mode.scratch" in activity

