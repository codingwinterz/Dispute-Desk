import json
from types import SimpleNamespace

import pytest

from disputedesk.decision import InvalidOutput, decide, parse_output
from disputedesk.policies import DEFAULT_RULES

FACTS = [{"id": f"F{i}", "text": f"fact {i}", "source": "seeded"} for i in range(1, 6)]
ALLOWED = {f["id"] for f in FACTS} | {r["id"] for r in DEFAULT_RULES}


def good(**over):
    body = {
        "decision": "DEFEND",
        "risk_score": 20,
        "reasoning": [
            {"point": "Carrier shows delivery.", "cites": ["F3"]},
            {"point": "Signed by buyer.", "cites": ["F3", "P1"]},
            {"point": "No linked disputes.", "cites": ["F5"]},
        ],
        "evidence_summary": "UPS delivered the parcel on 2026-11-01 with the buyer's signature.",
    }
    body.update(over)
    return json.dumps(body)


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


def test_valid_output_is_ok_first_try():
    llm = FakeLLM(good())
    r = decide("D1", FACTS, DEFAULT_RULES, "gemini/test", completion=llm)
    assert r.status == "ok" and r.attempts == 1 and r.output.decision == "DEFEND"
    assert llm.calls[0]["model"] == "gemini/test" and llm.calls[0]["temperature"] == 0
    sent = json.loads(llm.calls[0]["messages"][1]["content"])
    assert sent["facts"] == FACTS and [r["id"] for r in sent["policy_rules"]] == ["P1", "P2", "P3", "P4"]


def test_invalid_then_valid_retries_once():
    llm = FakeLLM("not json", good())
    r = decide("D1", FACTS, DEFAULT_RULES, "m", completion=llm)
    assert r.status == "ok" and r.attempts == 2 and len(llm.calls) == 2
    assert "not a single valid JSON" in llm.calls[1]["messages"][-1]["content"]


def test_invalid_twice_is_an_error_without_third_call():
    llm = FakeLLM("nope", good(decision="MAYBE"))
    r = decide("D1", FACTS, DEFAULT_RULES, "m", completion=llm)
    assert r.status == "error" and r.error == "invalid_output:schema" and len(llm.calls) == 2


def test_citing_a_fact_not_in_the_input_is_rejected():
    bad = good(reasoning=[{"point": "a", "cites": ["F1"]}, {"point": "b", "cites": ["F2"]},
                          {"point": "c", "cites": ["F99"]}])
    with pytest.raises(InvalidOutput) as exc:
        parse_output(bad, ALLOWED)
    assert exc.value.code == "unknown_citation" and "F99" in exc.value.detail


def test_uncited_point_and_wrong_counts_are_rejected():
    for bad in (good(reasoning=[{"point": "a", "cites": []}] * 3),
                good(reasoning=[{"point": "a", "cites": ["F1"]}] * 2),
                good(reasoning=[{"point": "a", "cites": ["F1"]}] * 6),
                good(risk_score=101)):
        with pytest.raises(InvalidOutput):
            parse_output(bad, ALLOWED)


def test_fenced_json_is_accepted():
    assert parse_output("```json\n" + good() + "\n```", ALLOWED).risk_score == 20


def test_llm_exception_is_an_error_not_a_crash(caplog):
    r = decide("D1", FACTS, DEFAULT_RULES, "m", completion=FakeLLM(RuntimeError("boom with secret-ish text")))
    assert r.status == "error" and r.error == "llm_call_failed:RuntimeError"
    assert "secret-ish" not in caplog.text


def test_raw_model_output_is_not_logged(caplog):
    caplog.set_level("DEBUG")
    decide("D1", FACTS, DEFAULT_RULES, "m", completion=FakeLLM("RAW-MODEL-TEXT", "RAW-MODEL-TEXT"))
    assert "RAW-MODEL-TEXT" not in caplog.text
