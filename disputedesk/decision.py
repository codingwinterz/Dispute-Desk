"""LLM decision step (LiteLLM): DEFEND or ACCEPT, grounded in numbered graph facts and policy rules.

The model sees only the facts built from the graph (F1..Fn) and the policy rules (P1..). Output is
validated against a schema; every reasoning point must cite at least one supplied ID, and an ID that
was not supplied makes the output invalid, as does a forbidden word. Invalid output is retried once,
then recorded as an error. A 429 or 503 from the LLM call is retried with exponential backoff.
Raw model output is never logged.

risk_score: likelihood, 0 to 100, that the dispute is invalid or fraudulent based only on the
supplied facts. It is not the decision: a clean case accepted for cost reasons scores LOW.
"""

import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from disputedesk.graph import LINKED_PREFIX

log = logging.getLogger("disputedesk.decision")

MAX_ATTEMPTS = 2  # first try + one retry on invalid output
LLM_CALL_ATTEMPTS = 3  # per model turn, only for 429 / 503
BACKOFF_BASE_SECONDS = 1.0  # waits 1s, then 2s
RETRY_STATUS = {429, 503}

# Words the output must not contain (whole words, any case). Linked disputes are described as
# sharing a device hash or IP, never with a label.
FORBIDDEN_WORDS = re.compile(r"\b(fraud\w*|abuse[sd]?|abusers?|rings?)\b", re.IGNORECASE)

SANDBOX_NOTE = "Shipment and device records cited here are merchant-supplied sandbox test data."
PATTERN_NOTE = "A shared device hash or IP is a pattern, not proof that this dispute is invalid."

SYSTEM_PROMPT = """You are DisputeDesk, an analyst helping a merchant answer a PayPal dispute.
Decide DEFEND (contest the dispute with evidence) or ACCEPT (accept the claim and refund).

Use ONLY the numbered facts (F1, F2, ...) and policy rules (P1, P2, ...) in the input. Facts marked
source "paypal" come from PayPal; facts marked "seeded" are the merchant's own records. Do not use any
other knowledge about the dispute, and do not state anything that no supplied fact says.

risk_score is the likelihood, 0 to 100, that this dispute is invalid or fraudulent based only on the
supplied facts. It is separate from the decision: a clean, low-risk case that you ACCEPT for cost
reasons must get a LOW score.

Wording: never use the words "fraud", "abuse", "abuser" or "ring" (or their plurals) anywhere in
your reply. Describe linked disputes only as disputes that "share this device hash" or "share this
IP". If the draft mentions linked disputes, it must say that such a pattern is not proof.

Reply with ONE JSON object and nothing else:
{
  "decision": "DEFEND" | "ACCEPT",
  "risk_score": integer 0-100 as defined above,
  "reasoning": [ {"point": "one sentence", "cites": ["F3", "P1"]} ],   // 3 to 5 points
  "evidence_summary": "a draft statement to PayPal (under 1200 characters) using only cited facts"
}
Every reasoning point must cite at least one fact or rule ID from the input, and only those IDs."""


class ReasonPoint(BaseModel):
    model_config = ConfigDict(extra="ignore")
    point: str = Field(min_length=1, max_length=500)
    cites: list[str] = Field(min_length=1)


class DecisionOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    decision: Literal["DEFEND", "ACCEPT"]
    risk_score: int = Field(ge=0, le=100)
    reasoning: list[ReasonPoint] = Field(min_length=3, max_length=5)
    evidence_summary: str = Field(min_length=1, max_length=2000)


class InvalidOutput(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(code)
        self.code = code
        self.detail = detail  # sent back to the model on retry; never logged


@dataclass
class DecisionResult:
    status: Literal["ok", "error"]
    model: str
    attempts: int
    output: DecisionOutput | None = None
    error: str | None = None
    facts: list[dict[str, str]] = field(default_factory=list)


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def parse_output(text: str, allowed_ids: set[str]) -> DecisionOutput:
    m = _FENCE.match(text or "")
    body = m.group(1) if m else (text or "")
    try:
        raw = json.loads(body)
    except ValueError:
        raise InvalidOutput("invalid_json", "The reply was not a single valid JSON object.") from None
    try:
        out = DecisionOutput.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
        raise InvalidOutput("schema", f"The JSON did not match the schema: {problems}.") from None
    unknown = sorted({c for p in out.reasoning for c in p.cites} - allowed_ids)
    if unknown:
        raise InvalidOutput("unknown_citation",
                            f"These cited IDs were not in the input: {', '.join(unknown)}. Cite only supplied IDs.")
    texts = [p.point for p in out.reasoning] + [out.evidence_summary]
    found = sorted({m.group(0).lower() for t in texts for m in FORBIDDEN_WORDS.finditer(t)})
    if found:
        raise InvalidOutput("forbidden_word",
                            f"The reply used forbidden words: {', '.join(found)}. Describe linked disputes only as "
                            "sharing this device hash or IP, and say a pattern is not proof.")
    return out


def finish_draft(out: DecisionOutput, facts: list[dict[str, str]]) -> DecisionOutput:
    """Append the required notes to the evidence draft if the model left them out."""
    draft = out.evidence_summary.rstrip()
    if any(f["text"].startswith(LINKED_PREFIX) for f in facts) and "not proof" not in draft.lower():
        draft += " " + PATTERN_NOTE
    if SANDBOX_NOTE not in draft:
        draft += " " + SANDBOX_NOTE
    return out.model_copy(update={"evidence_summary": draft})


def _status_code(exc: Exception) -> int | None:
    # LiteLLM's RateLimitError / ServiceUnavailableError carry status_code (429 / 503).
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def _call_with_backoff(completion: Callable[..., Any], sleep: Callable[[float], None], dispute_id: str,
                       **kwargs: Any) -> Any:
    """Up to LLM_CALL_ATTEMPTS calls; retries only 429 and 503, waiting 1s, 2s, ... between them."""
    for attempt in range(1, LLM_CALL_ATTEMPTS + 1):
        try:
            return completion(**kwargs)
        except Exception as exc:
            status = _status_code(exc)
            if status not in RETRY_STATUS or attempt == LLM_CALL_ATTEMPTS:
                raise
            delay = BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
            log.warning("decision dispute=%s: LLM HTTP %s; retrying in %.0fs (attempt %d/%d)",
                        dispute_id, status, delay, attempt, LLM_CALL_ATTEMPTS)
            sleep(delay)
    raise AssertionError("unreachable")


def _litellm_completion() -> Callable[..., Any]:
    # Use LiteLLM's bundled model price map instead of downloading it at import time.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    litellm.suppress_debug_info = True
    return litellm.completion


def decide(
    dispute_id: str,
    facts: list[dict[str, str]],
    rules: list[dict[str, str]],
    model: str,
    completion: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> DecisionResult:
    """One decision. Never raises: LLM or validation failures come back as status="error"."""
    allowed = {f["id"] for f in facts} | {r["id"] for r in rules}
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({
            "dispute_id": dispute_id,
            "facts": [{k: f[k] for k in ("id", "text", "source")} for f in facts],
            "policy_rules": rules,
        })},
    ]
    try:
        completion = completion or _litellm_completion()
    except Exception as exc:
        return DecisionResult("error", model, 0, error=f"llm_unavailable:{type(exc).__name__}", facts=facts)

    error = "no_attempt"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = _call_with_backoff(completion, sleep, dispute_id, model=model, messages=messages,
                                      temperature=0, response_format={"type": "json_object"})
            text = resp.choices[0].message.content or ""
        except Exception as exc:  # after backoff, or not retryable (auth, bad request): type name only
            log.warning("decision dispute=%s: LLM call failed (%s)", dispute_id, type(exc).__name__)
            return DecisionResult("error", model, attempt, error=f"llm_call_failed:{type(exc).__name__}", facts=facts)
        try:
            out = parse_output(text, allowed)
        except InvalidOutput as exc:
            error = f"invalid_output:{exc.code}"
            log.warning("decision dispute=%s attempt=%d: %s", dispute_id, attempt, error)
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content": f"{exc.detail} Reply again with only the corrected JSON object."},
            ]
            continue
        out = finish_draft(out, facts)
        log.info("decision dispute=%s: %s risk=%d", dispute_id, out.decision, out.risk_score)
        return DecisionResult("ok", model, attempt, output=out, facts=facts)
    return DecisionResult("error", model, MAX_ATTEMPTS, error=error, facts=facts)
