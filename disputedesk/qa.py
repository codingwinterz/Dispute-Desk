"""Read-only case Q&A (LiteLLM): answers merchant questions grounded strictly in numbered facts.

The model sees only the case's numbered facts (F1..Fn) and the merchant's question — never buyer
free text, policy rules, drafts, attachments, or tools. Output is validated against a schema;
when answered is true, at least one supplied fact ID must be cited, and any unknown ID or
forbidden word fails validation. Invalid output is retried once; raw model output and exception
messages are never logged. Makes no database writes and no PayPal API calls.
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from disputedesk import decision as decision_mod
from disputedesk.decision import (
    FORBIDDEN_WORDS,
    MAX_ATTEMPTS,
    InvalidOutput,
    _FENCE,
    _call_with_backoff,
)

log = logging.getLogger("disputedesk.qa")

MAX_QUESTION_CHARS = 500

QA_SYSTEM_PROMPT = """You are DisputeDesk, answering a merchant's question about one PayPal dispute.

Use ONLY the numbered facts (F1, F2, ...) in the input. Facts marked source "paypal" come from
PayPal; facts marked "seeded" are the merchant's own records. Do not use any other knowledge about
the dispute, buyer messages, attachments, or external assumptions, and do not state anything that
no supplied fact says.

Wording: never use the words "fraud", "abuse", "abuser" or "ring" (or their plurals) anywhere in
your reply. Describe linked disputes only as disputes that "share this device hash" or "share this
IP".

Reply with ONE JSON object and nothing else:
{
  "answered": true | false,
  "answer": "a concise answer using only the supplied facts, or a brief note on what the facts do not say",
  "cites": ["F1"]
}
When "answered" is true, "cites" must contain at least one supplied fact ID (F1, F2, ...) that
supports the answer. When the supplied facts do not contain enough information to answer the
question, set "answered" to false, explain what is missing in "answer", and cite only relevant
supplied fact IDs (or []). Cite only fact IDs from the input."""


class QAOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    answered: bool
    answer: str = Field(min_length=1, max_length=1200)
    cites: list[str] = Field(default_factory=list)


@dataclass
class QAResult:
    status: Literal["ok", "error"]
    model: str
    attempts: int
    output: QAOutput | None = None
    error: str | None = None


def parse_qa_output(text: str, allowed_ids: set[str]) -> QAOutput:
    """Validate and normalize one Q&A JSON reply against the supplied fact IDs."""
    m = _FENCE.match(text or "")
    body = m.group(1) if m else (text or "")
    try:
        raw = json.loads(body)
    except ValueError:
        raise InvalidOutput("invalid_json", "The reply was not a single valid JSON object.") from None
    try:
        out = QAOutput.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
        raise InvalidOutput("schema", f"The JSON did not match the schema: {problems}.") from None
    cites = list(dict.fromkeys(out.cites))
    unknown = sorted(set(cites) - allowed_ids)
    if unknown:
        raise InvalidOutput(
            "unknown_citation",
            f"These cited IDs were not in the input: {', '.join(unknown)}. Cite only supplied fact IDs.",
        )
    if out.answered and not cites:
        raise InvalidOutput(
            "missing_citation",
            "When answered is true, cites must include at least one supplied fact ID (for example F1).",
        )
    found = sorted({match.group(0).lower() for match in FORBIDDEN_WORDS.finditer(out.answer)})
    if found:
        raise InvalidOutput(
            "forbidden_word",
            f"The reply used forbidden words: {', '.join(found)}. Describe linked disputes only as "
            "sharing this device hash or IP.",
        )
    return out.model_copy(update={"cites": cites})


def answer_case_question(
    dispute_id: str,
    question: str,
    facts: list[dict[str, Any]],
    model: str,
    completion: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> QAResult:
    """Answer one question about a case using only its numbered facts. Never raises."""
    clean_question = (question or "").strip()
    if not clean_question or len(clean_question) > MAX_QUESTION_CHARS:
        return QAResult("error", model, 0, error="invalid_question")
    if not facts:
        return QAResult("error", model, 0, error="no_facts")

    allowed = {f["id"] for f in facts}
    fact_payload = [{"id": f["id"], "text": f["text"], "source": f["source"]} for f in facts]
    messages: list[dict[str, str]] = [
        {"role": "system", "content": QA_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({
            "dispute_id": dispute_id,
            "question": clean_question,
            "facts": fact_payload,
        })},
    ]
    try:
        completion = completion or decision_mod._litellm_completion()
    except Exception as exc:
        return QAResult("error", model, 0, error=f"llm_unavailable:{type(exc).__name__}")

    error = "no_attempt"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = _call_with_backoff(
                completion,
                sleep,
                dispute_id,
                model=model,
                messages=messages,
                temperature=0,
                response_format={"type": "json_object"},
            )
            text = resp.choices[0].message.content or ""
        except Exception as exc:
            log.warning("qa dispute=%s: LLM call failed (%s)", dispute_id, type(exc).__name__)
            return QAResult("error", model, attempt, error=f"llm_call_failed:{type(exc).__name__}")
        try:
            out = parse_qa_output(text, allowed)
        except InvalidOutput as exc:
            error = f"invalid_output:{exc.code}"
            log.warning("qa dispute=%s attempt=%d: %s", dispute_id, attempt, error)
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content": f"{exc.detail} Reply again with only the corrected JSON object."},
            ]
            continue
        log.info("qa dispute=%s: answered=%s cites=%d", dispute_id, out.answered, len(out.cites))
        return QAResult("ok", model, attempt, output=out)
    return QAResult("error", model, MAX_ATTEMPTS, error=error)

