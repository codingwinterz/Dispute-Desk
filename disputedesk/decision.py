"""LLM decision step (LiteLLM): DEFEND or ACCEPT, grounded in numbered graph facts and policy rules.

The model sees only the facts built from the graph (F1..Fn) and the policy rules (P1..). Output is
validated against a schema; every reasoning point must cite at least one supplied ID, and an ID that
was not supplied makes the output invalid. Invalid output is retried once, then recorded as an error.
Raw model output is never logged.
"""

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger("disputedesk.decision")

MAX_ATTEMPTS = 2  # first try + one retry on invalid output

SYSTEM_PROMPT = """You are DisputeDesk, an analyst helping a merchant answer a PayPal dispute.
Decide DEFEND (contest the dispute with evidence) or ACCEPT (accept the claim and refund).

Use ONLY the numbered facts (F1, F2, ...) and policy rules (P1, P2, ...) in the input. Facts marked
source "paypal" come from PayPal; facts marked "seeded" are the merchant's own records. Do not use any
other knowledge about the dispute, and do not state anything that no supplied fact says.

Reply with ONE JSON object and nothing else:
{
  "decision": "DEFEND" | "ACCEPT",
  "risk_score": integer 0-100, the merchant's estimated chance of LOSING if it defends,
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
    return out


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
) -> DecisionResult:
    """One decision. Never raises: LLM or validation failures come back as status="error"."""
    allowed = {f["id"] for f in facts} | {r["id"] for r in rules}
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({"dispute_id": dispute_id, "facts": facts, "policy_rules": rules})},
    ]
    try:
        completion = completion or _litellm_completion()
    except Exception as exc:
        return DecisionResult("error", model, 0, error=f"llm_unavailable:{type(exc).__name__}", facts=facts)

    error = "no_attempt"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = completion(model=model, messages=messages, temperature=0,
                              response_format={"type": "json_object"})
            text = resp.choices[0].message.content or ""
        except Exception as exc:  # network, auth, quota: not retried here; type name only
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
        log.info("decision dispute=%s: %s risk=%d", dispute_id, out.decision, out.risk_score)
        return DecisionResult("ok", model, attempt, output=out, facts=facts)
    return DecisionResult("error", model, MAX_ATTEMPTS, error=error, facts=facts)
