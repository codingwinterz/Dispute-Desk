import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from disputedesk.analyzer import Analyzer
from disputedesk.decision import SANDBOX_NOTE, DecisionOutput, DecisionResult, InvalidOutput, parse_output
from disputedesk.decision_store import DecisionStore
from disputedesk.dispute_store import DisputeStore
from disputedesk.drafts import MAX_DRAFT_CHARS, DraftStore, effective_draft
from disputedesk.graph import GraphStore
from disputedesk.policies import DEFAULT_RULES, PolicySettings, PolicyStore, build_rules
from disputedesk.webhooks import EventStore, create_app
from tests.fakes import FakeGraph, context_row, sample_dispute

DRAFT = f"UPS delivered the parcel on 2026-11-03 with the buyer's signature. {SANDBOX_NOTE}"
JSON_HDR = {"Content-Type": "application/json"}


def out(cites_rule="P1"):
    return DecisionOutput.model_validate({
        "decision": "DEFEND", "risk_score": 15, "evidence_summary": DRAFT,
        "reasoning": [{"point": "a", "cites": ["F1"]}, {"point": "b", "cites": [cites_rule]}, {"point": "c", "cites": ["F2"]}],
    })


@pytest.fixture
def db(tmp_path):
    return tmp_path / "x.db"


@pytest.fixture
def stores(db):
    s = (DisputeStore(db), DecisionStore(db), PolicyStore(db), DraftStore(db))
    yield s
    for x in s:
        x.close()


@pytest.fixture
def client(stores, tmp_path):
    d, dec, pol, dr = stores
    return TestClient(create_app(verifier=object(), store=EventStore(tmp_path / "logs"), dispute_store=d,
                                 decision_store=dec, policy_store=pol, draft_store=dr))


# --- policy rules ---------------------------------------------------------------------------

def test_defaults_match_the_phase0_rules():
    assert [r["id"] for r in DEFAULT_RULES] == ["P1", "P2", "P3", "P4"]
    assert "15.00 USD" in DEFAULT_RULES[1]["text"] and "2 or more" in DEFAULT_RULES[2]["text"]


def test_rule_text_follows_settings():
    rules = {r["id"]: r["text"] for r in build_rules(PolicySettings(accept_under_amount=42.5, defend_linked_min=1))}
    assert "42.50 USD" in rules["P2"] and "one other dispute" in rules["P3"]
    rules = {r["id"]: r["text"] for r in build_rules(PolicySettings(accept_under_enabled=False, defend_linked_min=3))}
    assert "P2" not in rules and "3 or more" in rules["P3"] and "precedence" not in rules["P3"]
    assert build_rules(PolicySettings(defend_delivered_not_received=False, accept_under_enabled=False,
                                      defend_linked_enabled=False, accept_no_scan_not_received=False)) == []


@pytest.mark.parametrize("settings", [PolicySettings(), PolicySettings(defend_linked_min=1, accept_under_enabled=False)])
def test_no_rule_text_uses_forbidden_words(settings):
    import re
    from disputedesk.decision import FORBIDDEN_WORDS
    assert not any(FORBIDDEN_WORDS.search(r["text"]) for r in build_rules(settings))
    assert not any(re.search(r"\bfraud", r["text"], re.I) for r in build_rules(settings))


def test_settings_validation():
    for bad in ({"accept_under_amount": -1}, {"defend_linked_min": 0}, {"defend_linked_min": 21}, {"surprise": True}):
        with pytest.raises(ValueError):
            PolicySettings.model_validate(bad)


def test_policy_store_round_trip_and_unknown_keys(stores, db):
    pol = stores[2]
    assert pol.get() == (PolicySettings(), None)
    pol.save(PolicySettings(accept_under_amount=20))
    settings, updated = pol.get()
    assert settings.accept_under_amount == 20 and updated
    with sqlite3.connect(db) as raw:  # a key from some other version is ignored, not fatal
        raw.execute("UPDATE policy_settings SET data = ?", (json.dumps({"accept_under_amount": 9, "old_key": 1}),))
    assert pol.get()[0].accept_under_amount == 9


def test_citing_a_disabled_rule_is_rejected():
    rules = build_rules(PolicySettings(defend_delivered_not_received=False))
    allowed = {"F1", "F2"} | {r["id"] for r in rules}
    with pytest.raises(InvalidOutput) as exc:
        parse_output(out("P1").model_dump_json(), allowed)
    assert exc.value.code == "unknown_citation"


def test_analyzer_reads_current_rules_and_stores_them(stores):
    d, dec, pol, _ = stores
    d.claim("PP-R-TST-1", "poll", sample_dispute(), full=True)
    seen = []

    def decide(dispute_id, facts, rules, model):
        seen.append([r["id"] for r in rules])
        return DecisionResult("ok", model, 1, output=out(), facts=facts)

    a = Analyzer(d, dec, lambda: GraphStore(FakeGraph([context_row()])), "m", decide_fn=decide, rules_provider=pol.rules)
    a.analyze("PP-R-TST-1")
    pol.save(PolicySettings(accept_under_enabled=False))
    a.analyze("PP-R-TST-1", force=True)
    assert seen == [["P1", "P2", "P3", "P4"], ["P1", "P3", "P4"]]
    assert [r["id"] for r in dec.get("PP-R-TST-1")["rules"]] == ["P1", "P3", "P4"]


def test_decision_store_migrates_old_table(db):
    with sqlite3.connect(db) as raw:
        raw.execute("CREATE TABLE decisions (dispute_id TEXT PRIMARY KEY, status TEXT NOT NULL, decision TEXT,"
                    " risk_score INTEGER, reasoning TEXT, evidence_summary TEXT, facts TEXT NOT NULL,"
                    " graph_context TEXT, model TEXT NOT NULL, attempts INTEGER NOT NULL, error TEXT,"
                    " created_at TEXT NOT NULL)")
        raw.execute("INSERT INTO decisions VALUES ('OLD', 'ok', 'ACCEPT', 5, '[]', 'x', '[]', NULL, 'm', 1, NULL, 't')")
    dec = DecisionStore(db)
    assert dec.get("OLD")["rules"] is None  # old row kept
    dec.save("NEW", DecisionResult("ok", "m", 1, output=out(), facts=[]), None, DEFAULT_RULES)
    assert dec.get("NEW")["rules"] == DEFAULT_RULES
    dec.close()


# --- policies API -------------------------------------------------------------------------------

def test_policies_api_get_preview_put(client, stores):
    body = client.get("/api/policies").json()
    assert body["updated_at"] is None and body["settings"]["accept_under_amount"] == 15.0
    changed = dict(body["settings"], accept_under_amount=30)
    preview = client.post("/api/policies/preview", json=changed).json()
    assert "30.00 USD" in preview["rules"][1]["text"]
    assert stores[2].get()[1] is None  # preview never saves
    saved = client.put("/api/policies", json=changed).json()
    assert saved["updated_at"] and "30.00 USD" in saved["rules"][1]["text"]
    assert client.get("/api/policies").json()["settings"]["accept_under_amount"] == 30


def test_policies_api_rejects_bad_values(client):
    resp = client.put("/api/policies", json={"accept_under_amount": -5})
    assert resp.status_code == 422 and any("accept_under_amount" in p for p in resp.json()["problems"])
    assert client.put("/api/policies", json={"bogus": 1}).status_code == 422


@pytest.mark.parametrize("method,path", [("put", "/api/policies"), ("post", "/api/policies/preview"),
                                         ("put", "/api/drafts/PP-R-TST-1")])
def test_writes_need_json_and_same_origin(client, stores, method, path):
    stores[0].claim("PP-R-TST-1", "poll", sample_dispute(), full=True)
    send = getattr(client, method)
    assert send(path, content=b"text=x", headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    assert send(path, content=b"{}", headers=JSON_HDR | {"Origin": "https://evil.example"}).status_code == 403
    assert send(path, content=b"{}", headers=JSON_HDR | {"X-Forwarded-For": "203.0.113.9"}).status_code == 403
    # Same origin passes the guard (then fails or succeeds on content).
    assert send(path, content=b"{}", headers=JSON_HDR | {"Origin": "http://testserver"}).status_code in (200, 422)


def test_policies_page_is_served_and_in_nav(client):
    assert 'id="policies"' in client.get("/policies").text
    assert '"/policies", "Policies"' in client.get("/static/common.js").text
    assert "/api/policies/preview" in client.get("/static/policies.js").text


# --- drafts -------------------------------------------------------------------------------------

def decided(stores):
    d, dec, _, _ = stores
    d.claim("PP-R-TST-1", "poll", sample_dispute(), full=True)
    dec.save("PP-R-TST-1", DecisionResult("ok", "m", 1, output=out(), facts=[]), None, DEFAULT_RULES)


def test_agent_draft_is_effective_until_merchant_saves(client, stores):
    decided(stores)
    draft = client.get("/api/decisions/PP-R-TST-1").json()["draft"]
    assert draft["edited"] is False and draft["text"] == DRAFT
    edited = "Signed delivery confirmed by UPS on 2026-11-03. " + SANDBOX_NOTE
    resp = client.put("/api/drafts/PP-R-TST-1", json={"text": edited})
    assert resp.status_code == 200 and resp.json()["draft"]["edited"] is True
    draft = client.get("/api/decisions/PP-R-TST-1").json()["draft"]
    assert draft["edited"] is True and draft["text"] == edited


def test_rerunning_the_agent_keeps_the_saved_draft(client, stores):
    decided(stores)
    edited = "Merchant wording. " + SANDBOX_NOTE
    client.put("/api/drafts/PP-R-TST-1", json={"text": edited})
    stores[1].save("PP-R-TST-1", DecisionResult("ok", "m", 1, output=out(), facts=[]), None, DEFAULT_RULES)
    assert client.get("/api/decisions/PP-R-TST-1").json()["draft"]["text"] == edited


def test_draft_validation(client, stores):
    decided(stores)
    resp = client.put("/api/drafts/PP-R-TST-1", json={"text": "No sandbox note here."})
    assert resp.status_code == 422 and resp.json()["error"] == "missing_sandbox_note"
    assert resp.json()["note"] == SANDBOX_NOTE
    assert client.put("/api/drafts/PP-R-TST-1", json={"text": "   "}).json()["error"] == "empty"
    long = "x" * MAX_DRAFT_CHARS + SANDBOX_NOTE
    assert client.put("/api/drafts/PP-R-TST-1", json={"text": long}).json()["error"] == "too_long"
    assert client.put("/api/drafts/PP-R-TST-1", json={"words": "x"}).status_code == 422
    assert client.put("/api/drafts/PP-R-NOPE", json={"text": DRAFT}).status_code == 404


def test_effective_draft_rules():
    assert effective_draft(None, None) is None
    assert effective_draft(None, {"status": "error", "evidence_summary": None}) is None
    assert effective_draft({"text": "m", "updated_at": "t"}, {"status": "ok", "evidence_summary": "a"})["text"] == "m"
