"""Phase 3: approved responses to PayPal. Every HTTP call is mocked with respx; nothing reaches PayPal."""

import json
import re
import threading
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from disputedesk.auth import TokenProvider
from disputedesk.config import SANDBOX_BASE_URL
from disputedesk.decision import SANDBOX_NOTE, DecisionOutput, DecisionResult
from disputedesk.decision_store import DecisionStore
from disputedesk.dispute_store import DisputeStore
from disputedesk.disputes import ACCEPT_NOTE, DisputesClient
from disputedesk.drafts import DraftStore
from disputedesk.http_log import make_client
from disputedesk.submissions import NOTES_ONLY, SubmissionStore, Submitter
from disputedesk.webhooks import EventStore, create_app
from tests.conftest import TOKEN_URL
from tests.fakes import sample_dispute

DID = "PP-R-TST-1"
URL = f"{SANDBOX_BASE_URL}/v1/customer/disputes/{DID}"
EVIDENCE_URL = f"{URL}/provide-evidence"
ACCEPT_URL = f"{URL}/accept-claim"
TEXT = f"{SANDBOX_NOTE} UPS delivered the parcel on 2026-11-03 and the buyer signed for it."
JSON_HDR = {"Content-Type": "application/json"}


def output(decision="DEFEND", summary=TEXT):
    return DecisionOutput.model_validate({
        "decision": decision, "risk_score": 12, "evidence_summary": summary,
        "reasoning": [{"point": "a", "cites": ["F1"]}, {"point": "b", "cites": ["F2"]}, {"point": "c", "cites": ["F3"]}],
    })


@pytest.fixture
def paypal():
    # assert_all_mocked: any request to an unmocked URL raises instead of leaving the machine.
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-SECRET", "expires_in": 3600}))
        yield router


@pytest.fixture
def env(tmp_path, settings):
    db = tmp_path / "x.db"
    d, dec, dr, sub = DisputeStore(db), DecisionStore(db), DraftStore(db), SubmissionStore(db)
    http = make_client(tmp_path / "submit_http.jsonl", (settings.client_id, settings.client_secret))
    client = DisputesClient(settings, TokenProvider(settings, http), http)
    submitter = Submitter(d, dec, dr, sub, client)
    app = create_app(verifier=object(), store=EventStore(tmp_path / "logs"), dispute_store=d, decision_store=dec,
                     draft_store=dr, submitter=submitter)
    yield {"api": TestClient(app), "d": d, "dec": dec, "dr": dr, "sub": sub, "submitter": submitter,
           "log": tmp_path / "submit_http.jsonl", "tmp": tmp_path}
    http.close()
    for s in (d, dec, dr, sub):
        s.close()


def decide(env, decision="DEFEND", summary=TEXT):
    env["d"].claim(DID, "poll", sample_dispute(DID), full=True)
    env["dec"].save(DID, DecisionResult("ok", "m", 1, output=output(decision, summary), facts=[]), None, [])


def post(env, **body):
    return env["api"].post(f"/api/submissions/{DID}", json=body)


def writes(router) -> list:
    return [c for c in router.calls if c.request.method == "POST" and c.request.url.path.endswith(("-evidence", "-claim"))]


def fresh(status="WAITING_FOR_SELLER_RESPONSE", links=True):
    d = sample_dispute(DID, status=status)
    if not links:
        d["links"] = [link for link in d["links"] if link["method"] == "GET"]
    return httpx.Response(200, json=d)


def input_part(body: bytes) -> dict:
    m = re.search(rb'name="input"\r\nContent-Type: application/json\r\n\r\n(.*?)\r\n--', body, re.S)
    assert m, body[:300]
    return json.loads(m.group(1))


# --- nothing is sent without confirmation or on a mismatch -----------------------------------

@pytest.mark.parametrize("body", [{}, {"action": "provide_evidence", "text": TEXT}, {"confirm": False},
                                  {"confirm": "true", "action": "provide_evidence", "text": TEXT}])
def test_no_confirmation_sends_nothing(env, paypal, body):
    decide(env)
    resp = env["api"].post(f"/api/submissions/{DID}", json=body)
    assert resp.status_code == 400 and resp.json()["error"] == "confirmation required"
    assert paypal.calls.call_count == 0


def test_action_mismatch_sends_nothing(env, paypal):
    decide(env)
    resp = post(env, confirm=True, action="accept_claim", text=ACCEPT_NOTE)
    assert resp.status_code == 409 and resp.json()["error"] == "action_mismatch"
    assert paypal.calls.call_count == 0


def test_text_mismatch_sends_nothing(env, paypal):
    decide(env)
    resp = post(env, confirm=True, action="provide_evidence", text=TEXT + " (edited after review)")
    assert resp.status_code == 409 and resp.json()["error"] == "text_changed"
    assert paypal.calls.call_count == 0


def test_draft_without_sandbox_sentence_first_sends_nothing(env, paypal):
    old = "UPS delivered the parcel. " + SANDBOX_NOTE  # note at the end (older agent draft)
    decide(env, summary=old)
    resp = post(env, confirm=True, action="provide_evidence", text=old)
    assert resp.status_code == 422 and resp.json()["error"] == "draft_invalid"
    assert paypal.calls.call_count == 0


def test_draft_over_2000_characters_is_refused_at_submit(env, paypal):
    decide(env)
    long = SANDBOX_NOTE + " " + "x" * 2000
    with env["dr"]._lock, env["dr"]._db:  # bypass the save-time check to prove submit re-checks
        env["dr"]._db.execute("INSERT INTO evidence_drafts VALUES (?, ?, ?)", (DID, long, "t"))
    resp = post(env, confirm=True, action="provide_evidence", text=long)
    assert resp.status_code == 422 and "2000" in resp.json()["message"]
    assert paypal.calls.call_count == 0


def test_without_paypal_credentials_submitting_is_off(tmp_path, paypal):
    db = tmp_path / "y.db"
    d, dec, dr = DisputeStore(db), DecisionStore(db), DraftStore(db)
    d.claim(DID, "poll", sample_dispute(DID), full=True)
    dec.save(DID, DecisionResult("ok", "m", 1, output=output(), facts=[]), None, [])
    api = TestClient(create_app(verifier=object(), store=EventStore(tmp_path / "l"), dispute_store=d,
                                decision_store=dec, draft_store=dr))
    assert api.post(f"/api/submissions/{DID}", json={"confirm": True, "action": "provide_evidence", "text": TEXT}).status_code == 503
    check = api.get(f"/api/decisions/{DID}").json()["submit"]
    assert check["can_submit"] is False and any("credentials" in r for r in check["reasons"])
    assert paypal.calls.call_count == 0


# --- fresh status and link check ------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [{"status": "UNDER_REVIEW"}, {"links": False}])
def test_fresh_check_refuses_before_sending(env, paypal, kwargs):
    decide(env)
    get = paypal.get(URL).mock(return_value=fresh(**kwargs))
    resp = post(env, confirm=True, action="provide_evidence", text=TEXT)
    assert resp.status_code == 409 and resp.json()["error"] == "not_allowed_now"
    assert get.call_count == 1 and writes(paypal) == []
    assert env["sub"].get(DID) is None  # claim released: nothing was sent
    if "status" in kwargs:
        assert env["d"].get(DID)["status"] == "UNDER_REVIEW"  # stored state refreshed


def test_status_check_failure_sends_nothing(env, paypal):
    decide(env)
    paypal.get(URL).mock(side_effect=httpx.ConnectError("down"))
    resp = post(env, confirm=True, action="provide_evidence", text=TEXT)
    assert resp.status_code == 502 and resp.json()["error"] == "status_check_failed"
    assert writes(paypal) == [] and env["sub"].get(DID) is None


# --- the one send, and blocking a second -----------------------------------------------------

def test_provide_evidence_sends_once_then_blocks(env, paypal):
    decide(env)
    paypal.get(URL).mock(side_effect=[fresh(), fresh(status="UNDER_REVIEW", links=False)])
    send = paypal.post(EVIDENCE_URL).mock(return_value=httpx.Response(200, json={"links": []}))
    assert env["api"].get(f"/api/decisions/{DID}").json()["submit"]["unverified"] is True

    resp = post(env, confirm=True, action="provide_evidence", text=TEXT)

    assert resp.status_code == 200 and resp.json()["result"] == "succeeded"
    assert resp.json()["dispute"]["status"] == "UNDER_REVIEW"  # badge flips from the refreshed dispute
    assert send.call_count == 1
    req = send.calls[0].request
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert input_part(req.content) == {"evidences": [{"evidence_type": "OTHER", "notes": TEXT}]}
    assert b"filename=" not in req.content  # notes only, no file part
    assert env["sub"].get(DID)["state"] == "succeeded"
    assert env["sub"].verified(NOTES_ONLY)["dispute_id"] == DID
    check = env["api"].get(f"/api/decisions/{DID}").json()["submit"]
    assert check["unverified"] is False and check["can_submit"] is False

    again = post(env, confirm=True, action="provide_evidence", text=TEXT)
    assert again.status_code == 409 and again.json()["error"] == "already_submitted"
    assert send.call_count == 1  # the app blocked it; PayPal never saw a second request
    edit = env["api"].put(f"/api/drafts/{DID}", json={"text": TEXT + " More."})
    assert edit.status_code == 409


def test_accept_claim_sends_the_fixed_note(env, paypal):
    decide(env, decision="ACCEPT")
    paypal.get(URL).mock(side_effect=[fresh(), fresh(status="RESOLVED", links=False)])
    send = paypal.post(ACCEPT_URL).mock(return_value=httpx.Response(200, json={"links": []}))
    resp = post(env, confirm=True, action="accept_claim", text=ACCEPT_NOTE)
    assert resp.status_code == 200 and send.call_count == 1
    assert json.loads(send.calls[0].request.content) == {"note": ACCEPT_NOTE}
    assert writes(paypal) == send.calls[:]  # nothing else was written
    assert env["sub"].verified(NOTES_ONLY) is None  # accept-claim doesn't verify notes-only evidence


def test_paypal_4xx_is_shown_and_allows_a_retry(env, paypal):
    decide(env)
    paypal.get(URL).mock(return_value=fresh())
    send = paypal.post(EVIDENCE_URL).mock(side_effect=[
        httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "message": "The requested action could not be performed.",
                                  "debug_id": "dbg123", "details": [{"issue": "INVALID_EVIDENCE_FILE"}]}),
        httpx.Response(200, json={"links": []}),
    ])
    first = post(env, confirm=True, action="provide_evidence", text=TEXT)
    assert first.status_code == 502 and first.json()["error"] == "paypal_rejected"
    assert first.json()["paypal"] == {"name": "UNPROCESSABLE_ENTITY", "message": "The requested action could not be performed.",
                                      "debug_id": "dbg123", "issues": ["INVALID_EVIDENCE_FILE"]}
    assert env["sub"].get(DID)["state"] == "rejected"
    assert env["api"].get(f"/api/decisions/{DID}").json()["submit"]["can_submit"] is True
    second = post(env, confirm=True, action="provide_evidence", text=TEXT)
    assert second.status_code == 200 and send.call_count == 2
    assert env["sub"].get(DID)["attempts"] == 2


@pytest.mark.parametrize("failure", [httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"}), httpx.ReadTimeout("slow")])
def test_unknown_outcome_blocks_a_retry(env, paypal, failure):
    decide(env)
    paypal.get(URL).mock(return_value=fresh())
    send = paypal.post(EVIDENCE_URL).mock(**({"return_value": failure} if isinstance(failure, httpx.Response)
                                            else {"side_effect": failure}))
    first = post(env, confirm=True, action="provide_evidence", text=TEXT)
    assert first.status_code == 502 and first.json()["error"] == "outcome_unknown"
    assert env["sub"].get(DID)["state"] == "unknown"
    second = post(env, confirm=True, action="provide_evidence", text=TEXT)
    assert second.status_code == 409 and send.call_count == 1


def test_expired_token_is_refreshed_once_without_double_sending(env, paypal):
    decide(env)
    paypal.get(URL).mock(side_effect=[fresh(), fresh(status="UNDER_REVIEW")])
    send = paypal.post(EVIDENCE_URL).mock(side_effect=[httpx.Response(401), httpx.Response(200, json={"links": []})])
    assert post(env, confirm=True, action="provide_evidence", text=TEXT).status_code == 200
    assert send.call_count == 2  # the 401 was not processed by PayPal; one real send


def test_concurrent_submits_send_once(env, paypal):
    decide(env)
    paypal.get(URL).mock(return_value=fresh())
    send = paypal.post(EVIDENCE_URL).mock(return_value=httpx.Response(200, json={"links": []}))
    barrier, results = threading.Barrier(4), []

    def go():
        barrier.wait()
        try:
            env["submitter"].submit(DID, "provide_evidence", TEXT)
            results.append("ok")
        except Exception as exc:
            results.append(getattr(exc, "code", type(exc).__name__))

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert send.call_count == 1
    assert results.count("ok") == 1 and results.count("already_submitted") == 3


# --- dry run ----------------------------------------------------------------------------------

def test_dry_run_builds_the_exact_request_and_sends_nothing(env, paypal):
    decide(env)
    body = env["api"].get(f"/api/submissions/{DID}/dry-run").json()
    assert paypal.calls.call_count == 0 and body["sent"] is False
    req = body["request"]
    assert req["method"] == "POST" and req["url"] == EVIDENCE_URL
    assert req["headers"]["authorization"] == "Bearer [REDACTED]"
    assert input_part(req["body"].encode()) == {"evidences": [{"evidence_type": "OTHER", "notes": TEXT}]}
    assert body["check"]["unverified"] is True and body["check"]["can_submit"] is True

    # The real submit sends the same body (boundary aside).
    paypal.get(URL).mock(return_value=fresh())
    send = paypal.post(EVIDENCE_URL).mock(return_value=httpx.Response(200, json={"links": []}))
    post(env, confirm=True, action="provide_evidence", text=TEXT)
    assert input_part(send.calls[0].request.content) == input_part(req["body"].encode())


def test_dry_run_for_accept_and_for_unknown_dispute(env, paypal):
    decide(env, decision="ACCEPT")
    req = env["api"].get(f"/api/submissions/{DID}/dry-run").json()["request"]
    assert req["url"] == ACCEPT_URL and json.loads(req["body"]) == {"note": ACCEPT_NOTE}
    assert env["api"].get("/api/submissions/PP-R-NOPE/dry-run").status_code == 404
    assert paypal.calls.call_count == 0


# --- guards, logging, and where writes can come from -----------------------------------------

def test_write_guard_and_tunnel(env, paypal):
    decide(env)
    api, path = env["api"], f"/api/submissions/{DID}"
    ok_body = json.dumps({"confirm": True, "action": "provide_evidence", "text": TEXT})
    assert api.post(path, content=ok_body, headers={"Content-Type": "text/plain"}).status_code == 415
    assert api.post(path, content=ok_body, headers=JSON_HDR | {"Origin": "https://evil.example"}).status_code == 403
    assert api.post(path, content=ok_body, headers=JSON_HDR | {"X-Forwarded-For": "203.0.113.9"}).status_code == 403
    assert api.get(f"{path}/dry-run", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 403
    assert paypal.calls.call_count == 0


def test_submit_response_is_logged_redacted(env, paypal):
    decide(env)
    paypal.get(URL).mock(return_value=fresh())
    paypal.post(EVIDENCE_URL).mock(return_value=httpx.Response(200, json={"links": []}))
    post(env, confirm=True, action="provide_evidence", text=TEXT)
    lines = [json.loads(x) for x in env["log"].read_text(encoding="utf-8").splitlines()]
    sent = [x for x in lines if x["request"]["url"] == EVIDENCE_URL]
    assert len(sent) == 1 and sent[0]["response"]["status"] == 200
    text = env["log"].read_text(encoding="utf-8")
    assert "tok-SECRET" not in text and "test-client-secret" not in text
    assert sent[0]["request"]["headers"]["authorization"] == "[REDACTED]"


def test_only_the_submission_route_can_write():
    pkg = Path(__file__).resolve().parent.parent / "disputedesk"
    sources = {p.name: p.read_text(encoding="utf-8") for p in pkg.glob("*.py")}
    assert [n for n, s in sources.items() if "send_action(" in s] == sorted(["disputes.py", "submissions.py"])
    assert [n for n, s in sources.items() if "build_action_request(" in s] == sorted(["disputes.py", "submissions.py"])
    for name in ("poller.py", "analyzer.py", "events.py", "graph.py", "decision.py"):
        assert not re.search(r"Submitter|\.submit\(|send_action|build_action_request", sources[name]), name
    web = sources["webhooks.py"]
    assert web.count("submitter.submit,") == 1 and "submitter.submit(" not in web  # only in POST /api/submissions/{id}
    lifespan = web.split("async def lifespan", 1)[1].split("app = FastAPI", 1)[0]
    handle = web.split("def _handle(", 1)[1]
    assert "submit" not in lifespan and "submit" not in handle
