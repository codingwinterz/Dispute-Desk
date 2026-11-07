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


# --- wording, draft notes, risk definition --------------------------------------------------

from disputedesk.decision import PATTERN_NOTE, SANDBOX_NOTE, SYSTEM_PROMPT  # noqa: E402
from disputedesk.graph import LINKED_PREFIX  # noqa: E402


@pytest.mark.parametrize("word", ["fraud", "Fraudulent", "abuse", "abuser", "ring", "rings"])
def test_forbidden_words_are_rejected(word):
    with pytest.raises(InvalidOutput) as exc:
        parse_output(good(evidence_summary=f"This looks like a {word} case."), ALLOWED)
    assert exc.value.code == "forbidden_word"
    pts = [{"point": f"Device {word}.", "cites": ["F1"]}, {"point": "b", "cites": ["F2"]}, {"point": "c", "cites": ["F3"]}]
    with pytest.raises(InvalidOutput):
        parse_output(good(reasoning=pts), ALLOWED)


def test_ordinary_words_containing_forbidden_text_pass():
    out = parse_output(good(evidence_summary="During delivery the string of scans was complete."), ALLOWED)
    assert out.decision == "DEFEND"


def test_forbidden_word_retry_then_clean_reply_succeeds():
    llm = FakeLLM(good(evidence_summary="A fraud ring."), good())
    r = decide("D1", FACTS, DEFAULT_RULES, "m", completion=llm)
    assert r.status == "ok" and r.attempts == 2
    assert "sharing this device hash" in llm.calls[1]["messages"][-1]["content"] and "not proof" in llm.calls[1]["messages"][-1]["content"]


def test_prompt_defines_risk_score_and_wording():
    prompt = " ".join(SYSTEM_PROMPT.split())
    assert "risk_score is the likelihood, 0 to 100, that the buyer's claim is valid" in prompt
    assert "HIGH means the evidence favours the buyer" in prompt
    # anchors
    assert "delivered with a buyer signature and no contradicting facts: 5 to 20" in prompt
    assert "no carrier scan, or a label-only shipment: 70 to 90" in prompt
    assert "at most 15 points" in prompt
    # ACCEPT on evidence = high; cost-only ACCEPT scored on the facts
    assert "If you ACCEPT because the evidence favours the buyer, the score must be HIGH" in prompt
    assert "under the P2 threshold, score the facts as they are" in prompt
    assert "invalid or fraudulent" not in prompt and "LOW score" not in prompt
    assert "share this device hash" in prompt
    assert not any(w in r["text"].lower() for r in DEFAULT_RULES for w in ("fraud", "abuse", "ring "))


def test_every_draft_starts_with_the_sandbox_note_once():
    r = decide("D1", FACTS, DEFAULT_RULES, "m", completion=FakeLLM(good()))
    assert r.output.evidence_summary.startswith(SANDBOX_NOTE + " UPS delivered")
    assert r.output.evidence_summary.count(SANDBOX_NOTE) == 1
    assert PATTERN_NOTE not in r.output.evidence_summary  # no linked disputes in FACTS
    # The model put the note at the end, or in the middle: it moves to the start, once.
    for body in ("Delivered. " + SANDBOX_NOTE, "Delivered. " + SANDBOX_NOTE + " Signed by the buyer."):
        r2 = decide("D1", FACTS, DEFAULT_RULES, "m", completion=FakeLLM(good(evidence_summary=body)))
        assert r2.output.evidence_summary.startswith(SANDBOX_NOTE + " Delivered.")
        assert r2.output.evidence_summary.count(SANDBOX_NOTE) == 1
        assert "  " not in r2.output.evidence_summary


def test_linked_disputes_add_pattern_note_unless_draft_says_it():
    linked = FACTS + [{"id": "F6", "text": f"{LINKED_PREFIX}SEED-D-1 shares the device hash.", "source": "seeded"}]
    r = decide("D1", linked, DEFAULT_RULES, "m", completion=FakeLLM(good()))
    assert PATTERN_NOTE in r.output.evidence_summary
    said = good(evidence_summary="Two disputes share this device hash; this pattern is not proof.")
    r2 = decide("D1", linked, DEFAULT_RULES, "m", completion=FakeLLM(said))
    assert PATTERN_NOTE not in r2.output.evidence_summary


# --- backoff on 429 / 503 -------------------------------------------------------------------


class HTTPish(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize("status", [429, 503])
def test_retryable_status_backs_off_then_succeeds(status):
    sleeps = []
    llm = FakeLLM(HTTPish(status), HTTPish(status), good())
    r = decide("D1", FACTS, DEFAULT_RULES, "m", completion=llm, sleep=sleeps.append)
    assert r.status == "ok" and len(llm.calls) == 3 and sleeps == [1.0, 2.0]


def test_three_retryable_failures_give_up():
    sleeps = []
    llm = FakeLLM(HTTPish(503), HTTPish(429), HTTPish(503))
    r = decide("D1", FACTS, DEFAULT_RULES, "m", completion=llm, sleep=sleeps.append)
    assert r.status == "error" and r.error == "llm_call_failed:HTTPish"
    assert len(llm.calls) == 3 and sleeps == [1.0, 2.0]


def test_non_retryable_status_is_not_retried():
    sleeps = []
    llm = FakeLLM(HTTPish(400))
    r = decide("D1", FACTS, DEFAULT_RULES, "m", completion=llm, sleep=sleeps.append)
    assert r.status == "error" and len(llm.calls) == 1 and sleeps == []
