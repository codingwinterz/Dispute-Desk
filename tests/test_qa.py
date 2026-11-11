import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import respx
from fastapi.testclient import TestClient

from disputedesk import decision as decision_mod
from disputedesk.decision import DecisionOutput, DecisionResult, InvalidOutput
from disputedesk.decision_store import DecisionStore
from disputedesk.demo import DEMO_MESSAGE, export_snapshot, scan
from disputedesk.dispute_store import DisputeStore
from disputedesk.graph import GraphStore, context_to_facts
from disputedesk.qa import QA_SYSTEM_PROMPT, QA_TIMEOUT_SECONDS, answer_case_question, parse_qa_output
from disputedesk.webhooks import EventStore, create_app
from tests.fakes import FakeGraph, context_row, sample_dispute

ROOT = Path(__file__).resolve().parent.parent
FACTS = [
    {"id": "F1", "text": "Dispute PP-R-TST-1: reason MERCHANDISE_OR_SERVICE_NOT_RECEIVED.", "source": "paypal", "ref": "dispute"},
    {"id": "F2", "text": "Buyer PayPal payer_id BUYER1.", "source": "paypal", "ref": "customer"},
    {"id": "F3", "text": "Shipment S-1 via UPS: status DELIVERED, signed by buyer: yes.", "source": "seeded", "ref": "shipment"},
]
ALLOWED = {f["id"] for f in FACTS}


class FakeLLM:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])


def qa_json(**over):
    body = {
        "answered": True,
        "answer": "UPS delivered the parcel and the buyer signed for it.",
        "cites": ["F3"],
    }
    body.update(over)
    return json.dumps(body)


def test_qa_answers_from_facts_without_tools_or_refs():
    llm = FakeLLM(qa_json(cites=["F3", "F3", "F1"]))
    res = answer_case_question("PP-R-TST-1", "  Was it signed by the buyer?  ", FACTS, "gemini/test", completion=llm)
    assert res.status == "ok" and res.attempts == 1
    assert res.output.answered is True
    assert res.output.cites == ["F3", "F1"]
    call = llm.calls[0]
    assert call["model"] == "gemini/test" and call["temperature"] == 0
    assert call["response_format"] == {"type": "json_object"}
    assert "tools" not in call and "tool_choice" not in call and "functions" not in call
    sent = json.loads(call["messages"][1]["content"])
    assert sent["question"] == "Was it signed by the buyer?"
    assert sent["facts"] == [{"id": f["id"], "text": f["text"], "source": f["source"]} for f in FACTS]
    assert all("ref" not in f for f in sent["facts"])
    assert "policy_rules" not in sent
    assert "Use ONLY the numbered facts" in QA_SYSTEM_PROMPT


def test_qa_allows_unanswerable_question_when_answered_is_false():
    llm = FakeLLM(qa_json(answered=False, answer="The supplied facts do not state the parcel weight.", cites=[]))
    res = answer_case_question("PP-R-TST-1", "What did the parcel weigh?", FACTS, "m", completion=llm)
    assert res.status == "ok" and res.output.answered is False and res.output.cites == []


@pytest.mark.parametrize("bad_body,expected_code", [
    (qa_json(cites=[]), "missing_citation"),
    (qa_json(cites=["F99"]), "unknown_citation"),
    (qa_json(cites=["P1"]), "unknown_citation"),
    (qa_json(answer="This looks like fraud."), "forbidden_word"),
])
def test_qa_rejects_bad_citations_and_forbidden_words(bad_body, expected_code):
    with pytest.raises(InvalidOutput) as exc:
        parse_qa_output(bad_body, ALLOWED)
    assert exc.value.code == expected_code


def test_qa_retries_once_on_invalid_output_and_never_logs_raw(caplog):
    caplog.set_level("DEBUG")
    llm = FakeLLM(qa_json(cites=["F99"]), qa_json(cites=["F3"]))
    res = answer_case_question("PP-R-TST-1", "Was it delivered?", FACTS, "m", completion=llm)
    assert res.status == "ok" and res.attempts == 2 and len(llm.calls) == 2

    bad_llm = FakeLLM("RAW-SECRET-OUT", "RAW-SECRET-OUT")
    err = answer_case_question("PP-R-TST-1", "Was it delivered?", FACTS, "m", completion=bad_llm)
    assert err.status == "error" and err.error == "invalid_output:invalid_json"
    assert "RAW-SECRET-OUT" not in caplog.text


def test_qa_uses_decision_module_completion_hook(monkeypatch):
    llm = FakeLLM(qa_json())
    monkeypatch.setattr(decision_mod, "_litellm_completion", lambda: llm)
    res = answer_case_question("PP-R-TST-1", "Delivered?", FACTS, "m")
    assert res.status == "ok" and len(llm.calls) == 1


@pytest.fixture
def stores(tmp_path):
    db = tmp_path / "x.db"
    d, dec = DisputeStore(db), DecisionStore(db)
    yield db, d, dec
    d.close()
    dec.close()


def _seed_decided(stores, dispute_id="PP-R-TST-1"):
    _db, d, dec = stores
    raw = sample_dispute(dispute_id)
    raw["messages"] = [{"posted_by": "BUYER", "content": "SECRET_BUYER_FREE_TEXT never send to LLM"}]
    raw["disputed_transactions"][0]["buyer"]["email"] = "buyer-secret@example.com"
    d.claim(dispute_id, "poll", raw, full=True)
    ctx = GraphStore(FakeGraph([context_row()])).dispute_context(dispute_id)
    facts = context_to_facts(ctx)
    out = DecisionOutput.model_validate({
        "decision": "DEFEND",
        "risk_score": 15,
        "evidence_summary": "Signed delivery on file.",
        "reasoning": [
            {"point": "Delivered.", "cites": ["F5"]},
            {"point": "Signed.", "cites": ["F5", "P1"]},
            {"point": "No links.", "cites": ["F7"]},
        ],
    })
    dec.save(dispute_id, DecisionResult("ok", "gemini/test", 1, output=out, facts=facts), ctx)
    return facts


def test_ask_endpoint_is_read_only_no_paypal_no_buyer_text(stores, tmp_path, monkeypatch):
    db, d, dec = stores
    _seed_decided(stores)
    llm = FakeLLM(qa_json(cites=["F5"]))
    monkeypatch.setattr(decision_mod, "_litellm_completion", lambda: llm)

    def snapshot_counts():
        with sqlite3.connect(db) as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in sorted(tables)}

    app = create_app(verifier=object(), store=EventStore(tmp_path / "logs"), dispute_store=d, decision_store=dec,
                     llm_model="gemini/test")
    before = snapshot_counts()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        client = TestClient(app)
        resp = client.post("/api/decisions/PP-R-TST-1/ask", json={"question": "Was it signed?"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["cites"] == ["F5"] and body["answered"] is True and body["model"] == "gemini/test"
        assert router.calls.call_count == 0
    assert snapshot_counts() == before
    sent_payload = llm.calls[0]["messages"][1]["content"]
    assert "SECRET_BUYER_FREE_TEXT" not in sent_payload
    assert "buyer-secret@example.com" not in sent_payload


def test_ask_endpoint_guards_and_errors(stores, tmp_path, monkeypatch):
    _db, d, dec = stores
    _seed_decided(stores)
    d.claim("PP-R-NO-DEC", "poll", sample_dispute("PP-R-NO-DEC"), full=True)

    app = create_app(verifier=object(), store=EventStore(tmp_path / "logs"), dispute_store=d, decision_store=dec,
                     llm_model="gemini/test")
    client = TestClient(app)

    assert client.post("/api/decisions/PP-R-NOPE/ask", json={"question": "Why?"}).status_code == 404
    assert client.post("/api/decisions/PP-R-NO-DEC/ask", json={"question": "Why?"}).status_code == 409
    assert client.post("/api/decisions/PP-R-TST-1/ask", json={"question": "   "}).status_code == 422
    assert client.post("/api/decisions/PP-R-TST-1/ask", content=b"question=hi",
                       headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.post("/api/decisions/PP-R-TST-1/ask", json={"question": "Why?"},
                       headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 403
    assert client.post("/api/decisions/PP-R-TST-1/ask", json={"question": "Why?"},
                       headers={"Origin": "https://evil.example", "Host": "127.0.0.1:8000"}).status_code == 403

    monkeypatch.setattr(decision_mod, "_litellm_completion", lambda: FakeLLM(RuntimeError("secret key xyz")))
    fail = client.post("/api/decisions/PP-R-TST-1/ask", json={"question": "Why?"})
    assert fail.status_code == 502 and "secret key xyz" not in fail.text


def test_demo_mode_blocks_qa_and_ui_hides_it(tmp_path, monkeypatch):
    src = tmp_path / "src.db"
    d, dec = DisputeStore(src), DecisionStore(src)
    d.claim("PP-R-TST-1", "poll", sample_dispute("PP-R-TST-1"), full=True)
    ctx = GraphStore(FakeGraph([context_row()])).dispute_context("PP-R-TST-1")
    facts = context_to_facts(ctx)
    out = DecisionOutput.model_validate({
        "decision": "DEFEND", "risk_score": 10, "evidence_summary": "Delivered.",
        "reasoning": [{"point": "a", "cites": ["F1"]}, {"point": "b", "cites": ["F5"]}, {"point": "c", "cites": ["F7"]}],
    })
    dec.save("PP-R-TST-1", DecisionResult("ok", "gemini/test", 1, output=out, facts=facts), ctx)
    d.close()
    dec.close()

    snap, mapping = export_snapshot(src)
    assert scan(snap, list(mapping)) == []
    snap_path = tmp_path / "demo_snapshot.json"
    snap_path.write_text(json.dumps(snap), encoding="utf-8")

    def boom(*_a, **_k):
        raise AssertionError("demo mode must not call LLM")

    monkeypatch.setattr(decision_mod, "_litellm_completion", boom)
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("DEMO_SNAPSHOT", str(snap_path))
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        with TestClient(create_app()) as client:
            resp = client.post("/api/decisions/PP-R-TST-1/ask", json={"question": "Delivered?"})
            assert resp.status_code == 403 and resp.json() == {"error": "demo_mode", "message": DEMO_MESSAGE}
        assert router.calls.call_count == 0

    case_js = (ROOT / "disputedesk" / "web" / "static" / "case.js").read_text(encoding="utf-8")
    qa_fn_src = case_js.split("function qaPanel(d, rec)", 1)[1].split("// ---- render loop", 1)[0]
    assert "if (DD.mode.demo ||" in qa_fn_src and "return null;" in qa_fn_src
    assert "DD.DEMO_LABEL" not in qa_fn_src  # completely hidden in demo mode, not a disabled control
    assert "href: `#fact-${c}`" in qa_fn_src



class FakeTimeout(Exception):
    """Stands in for the provider's timeout exception: no HTTP status, so it is never retried."""


def test_qa_passes_explicit_timeout_and_a_timeout_is_a_502(stores, tmp_path, monkeypatch):
    llm = FakeLLM(qa_json())
    res = answer_case_question("PP-R-TST-1", "Delivered?", FACTS, "m", completion=llm)
    assert res.status == "ok"
    assert llm.calls[0]["timeout"] == QA_TIMEOUT_SECONDS == 30

    slow = FakeLLM(FakeTimeout("upstream url with ?key=secret"))
    res = answer_case_question("PP-R-TST-1", "Delivered?", FACTS, "m", completion=slow, sleep=lambda _s: None)
    assert res.status == "error" and res.error == "llm_call_failed:FakeTimeout"
    assert len(slow.calls) == 1  # a timeout is not retried

    _db, d, dec = stores
    _seed_decided(stores)
    app = create_app(verifier=object(), store=EventStore(tmp_path / "logs"), dispute_store=d, decision_store=dec,
                     llm_model="gemini/test")
    client = TestClient(app)
    monkeypatch.setattr(decision_mod, "_litellm_completion", lambda: FakeLLM(FakeTimeout("upstream url with ?key=secret")))
    resp = client.post("/api/decisions/PP-R-TST-1/ask", json={"question": "Delivered?"})
    assert resp.status_code == 502
    assert resp.json()["error"] == "llm_call_failed:FakeTimeout"
    assert "secret" not in resp.text
