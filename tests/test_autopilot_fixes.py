"""Autopilot v2 fix-forward: fail-closed linked disputes, malformed records, runner hardening, kill switch UI.

Fake clock, fake PayPal client and no LLM, as in tests/test_autopilot.py (whose fixture and helpers are
reused). The kill switch test runs the real common.js in Node with a stub DOM.
"""

import itertools
import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from disputedesk.autopilot import p2_eligibility
from disputedesk.policies import PolicySettings
from tests.test_autopilot import _decision, _dispute, _seed_case, env  # noqa: F401  (env is a fixture)

ROOT = Path(__file__).resolve().parent.parent
POLICY = PolicySettings()


# --- item 2: linked disputes fail closed ---------------------------------------------------------

@pytest.mark.parametrize("ctx", [
    "MISSING", None, {}, {"linked_disputes": None}, {"linked_disputes": "none"}, {"linked_disputes": {}},
    {"linked_disputes": 0}, "not a dict", ["linked"],
])
def test_missing_or_non_list_linked_disputes_is_not_eligible(ctx):
    dec = _decision()
    if ctx == "MISSING":
        del dec["graph_context"]
    else:
        dec["graph_context"] = ctx
    assert p2_eligibility(_dispute(), dec, POLICY, None) == (False, "linked_disputes_unknown")


def test_linked_disputes_list_still_decides():
    assert p2_eligibility(_dispute(), _decision(linked=[]), POLICY, None) == (True, None)
    assert p2_eligibility(_dispute(), _decision(linked=[{"id": "PP-R-OTHER"}]), POLICY, None) == \
        (False, "linked_disputes_present")


def test_runner_does_not_arm_when_graph_context_lacks_linked_disputes(env):
    did = "PP-R-NOCTX"
    _seed_case(env["stores"], did, value="10.00")
    _d, dec, *_ = env["stores"]
    with dec._lock, dec._db:  # the stored context loses its linked_disputes key
        dec._db.execute("UPDATE decisions SET graph_context = ? WHERE dispute_id = ?", (json.dumps({}), did))
    env["ap"].set_enabled(True)
    env["ap"].on_decision(did)
    assert env["ap_store"].job(did) is None
    assert env["ap"].state()["enabled"] is True  # a refusal, not a crash: the breaker did not trip


# --- item 3: malformed records are ineligible and never raise ------------------------------------

def _with(base: dict, **fields) -> dict:
    out = dict(base)
    out.update(fields)
    return out


MALFORMED_DISPUTES = [
    {"dispute_amount": "10.00 USD"},
    {"dispute_amount": ["10.00", "USD"]},
    {"dispute_amount": 10},
    {"links": {"rel": "accept_claim"}},
    {"links": "accept_claim"},
    {"messages": "buyer text"},
    {"messages": {"posted_by": "BUYER"}},
    {"evidences": "doc"},
    {"status": 5},
    {"dispute_life_cycle_stage": ["CHARGEBACK"]},
]
MALFORMED_DECISIONS = [
    {"reasoning": "P2"},
    {"reasoning": ["P2"]},
    {"reasoning": [{"point": "x", "cites": "P2"}]},
    {"reasoning": [{"point": "x", "cites": [2]}]},
    {"reasoning": [{"point": "x", "cites": [["P2"]]}]},
    {"reasoning": {"cites": ["P2"]}},
    {"status": 1},
    {"decision": ["ACCEPT"]},
]


@pytest.mark.parametrize("fields", MALFORMED_DISPUTES)
def test_malformed_dispute_fields_are_malformed_record(fields):
    assert p2_eligibility(_with(_dispute(), **fields), _decision(), POLICY, None) == (False, "malformed_record")


@pytest.mark.parametrize("fields", MALFORMED_DECISIONS)
def test_malformed_decision_fields_are_malformed_record(fields):
    assert p2_eligibility(_dispute(), _with(_decision(), **fields), POLICY, None) == (False, "malformed_record")


@pytest.mark.parametrize("dispute,decision,submission", [
    (["not", "a", "dict"], _decision(), None),
    (_dispute(), "not a dict", None),
    (_dispute(), _decision(), "not a dict"),
    (_dispute(evidences=[{"source": "BUYER", "documents": "not a list"}]), _decision(), None),
    (_dispute(evidences=[{"source": "BUYER", "documents": [None, 3]}]), _decision(), None),
])
def test_odd_shapes_never_pass_and_never_raise(dispute, decision, submission):
    ok, reason = p2_eligibility(dispute, decision, POLICY, submission)
    assert ok is False and reason == "malformed_record"


def test_combinations_of_bad_fields_never_raise():
    values = [None, "x", 3, [], {}, ["x"], {"value": None}, [{"a": 1}]]
    keys = ["dispute_amount", "links", "messages", "evidences", "status", "dispute_life_cycle_stage"]
    for key, val in itertools.product(keys, values):
        for dec_val in (None, "x", [], ["P2"], [{"cites": None}], [{"cites": "P2"}]):
            ok, reason = p2_eligibility(_with(_dispute(), **{key: val}), _with(_decision(), reasoning=dec_val),
                                        POLICY, None)
            assert isinstance(ok, bool) and (ok is False or reason is None)
            assert ok is False  # every combination breaks at least one rule


def test_malformed_stored_record_does_not_trip_the_breaker(env):
    did = "PP-R-BAD-AMT"
    raw = _seed_case(env["stores"], did, value="10.00")
    d, *_ = env["stores"]
    raw["dispute_amount"] = "10.00 USD"
    d.update_full(did, raw)
    env["ap"].set_enabled(True)
    env["ap"].on_decision(did)
    assert env["ap_store"].job(did) is None
    assert env["ap"].state()["enabled"] is True and env["ap"].state()["tripped_at"] is None


# --- item 5: runner re-reads the breaker per job; nothing stays executing -------------------------

def _arm_three(env, prefix):
    ids = [f"{prefix}-{i}" for i in (1, 2, 3)]
    for did in ids:
        env["fake_client"].disputes[did] = _seed_case(env["stores"], did, value="10.00")
    env["ap"].set_enabled(True)
    for did in ids:
        env["ap"].on_decision(did)
        assert env["ap_store"].job(did)["state"] == "armed"
    env["clock"][0] += timedelta(seconds=31)
    return ids


def _no_executing(env):
    return [j for j in env["ap_store"].state()["jobs"].values() if j["state"] == "executing"] == []


def test_trip_between_jobs_cancels_the_rest_of_the_pass(env):
    ids = _arm_three(env, "PP-R-MID")
    real_dry_run = env["ap"]._dry_run

    def dry_run_then_kill(dispute_id):
        out = real_dry_run(dispute_id)
        env["ap"].kill()  # the merchant presses the kill switch while the first job runs
        return out

    env["ap"]._dry_run = dry_run_then_kill
    assert env["ap"].tick() == 3
    assert env["ap_store"].job(ids[0])["state"] == "shadow_done"
    for did in ids[1:]:
        job = env["ap_store"].job(did)
        assert job["state"] == "cancelled" and job["reason"] == "breaker_tripped"
        assert [a["kind"] for a in env["ap_store"].audit_for(did)] == ["armed", "executing", "cancelled"]
    assert env["dry_runs"] == [ids[0]]
    assert _no_executing(env)
    assert env["fake_client"].send_action_calls == []


def test_exception_in_a_job_marks_it_failed_trips_and_cancels_the_rest(env):
    ids = _arm_three(env, "PP-R-EXC")

    def boom(_dispute_id):
        raise ValueError("dry run broke")

    env["ap"]._dry_run = boom
    assert env["ap"].tick() == 3
    first = env["ap_store"].job(ids[0])
    assert first["state"] == "failed" and first["reason"] == "ValueError"
    assert [a["kind"] for a in env["ap_store"].audit_for(ids[0])] == ["armed", "executing", "failed"]
    for did in ids[1:]:
        job = env["ap_store"].job(did)
        assert job["state"] == "cancelled" and job["reason"] == "breaker_tripped"
        assert [a["kind"] for a in env["ap_store"].audit_for(did)] == ["armed", "executing", "cancelled"]
    st = env["ap"].state()
    assert st["enabled"] is False and st["trip_reason"] == "exception:ValueError"
    assert [a["kind"] for a in env["ap_store"].audit_for("-")].count("tripped") == 1
    assert _no_executing(env)
    assert env["fake_client"].send_action_calls == []


def test_claim_error_mid_pass_cancels_what_was_already_claimed(env):
    ids = _arm_three(env, "PP-R-CLM")
    store = env["ap_store"]
    real_format = store._format
    calls = {"n": 0}

    def flaky_format(row, now):
        if row is not None and row[1] == "executing":
            calls["n"] += 1
            if calls["n"] == 2:  # the second claim fails inside its transaction
                raise RuntimeError("disk I/O")
        return real_format(row, now)

    store._format = flaky_format
    assert env["ap"].tick() == 1
    store._format = real_format
    first = store.job(ids[0])
    assert first["state"] == "cancelled" and first["reason"] == "claim_failed"
    assert [a["kind"] for a in store.audit_for(ids[0])] == ["armed", "executing", "cancelled"]
    for did in ids[1:]:  # never claimed: the failed claim rolled back, then the trip cancelled them
        assert store.job(did)["state"] == "cancelled"
        assert [a["kind"] for a in store.audit_for(did)] == ["armed", "cancelled"]
    assert store.daily_used() == 1  # the failed claim's slot rolled back with it
    assert env["ap"].state()["enabled"] is False
    assert _no_executing(env)
    assert env["dry_runs"] == [] and env["fake_client"].send_action_calls == []


def test_disabled_before_the_pass_claims_nothing(env):
    ids = _arm_three(env, "PP-R-OFF")
    env["ap_store"].trip("kill_switch", "merchant")
    assert env["ap"].tick() == 0
    assert all(env["ap_store"].job(did)["state"] == "cancelled" for did in ids)
    assert env["dry_runs"] == []


def test_every_transition_in_a_hardened_pass_writes_one_audit_row(env):
    ids = _arm_three(env, "PP-R-AUD")
    env["ap"]._dry_run = lambda _d: (_ for _ in ()).throw(KeyError("x"))
    env["ap"].tick()
    for did in ids:
        audit = env["ap_store"].audit_for(did)
        pairs = [(a["state_from"], a["state_to"]) for a in audit]
        assert len(pairs) == len(set(pairs)) == 3  # armed, executing, then one terminal row
        assert pairs[0] == (None, "armed") and pairs[1] == ("armed", "executing")
        assert pairs[2][0] == "executing" and pairs[2][1] in ("failed", "cancelled")


# --- item 4: kill switch UI hides only after the server confirms ----------------------------------

_KILL_PROBE = r"""
const fs = require("fs"), vm = require("vm");
const scenario = process.argv[2];
function stub(tag) {
  const node = { tag, attrs: {}, handlers: {}, children: [], hidden: false, disabled: false, className: "", _text: "",
    classList: { toggle() {}, add() {} }, style: {},
    setAttribute(k, v) { this.attrs[k] = v; if (k === "hidden") this.hidden = true; if (k === "disabled") this.disabled = true; },
    append(...c) { this.children.push(...c); }, prepend(...c) { this.children.unshift(...c); },
    addEventListener(t, fn) { this.handlers[t] = fn; },
  };
  Object.defineProperty(node, "textContent", { get() { return this._text; }, set(v) { this._text = String(v); } });
  Object.defineProperty(node, "innerHTML", { get() { return ""; }, set() { throw new Error("innerHTML used"); } });
  return node;
}
const events = [];
globalThis.window = { matchMedia: () => ({ matches: false }), addEventListener() {}, dispatchEvent(e) { events.push(e.type); } };
globalThis.CustomEvent = class { constructor(type) { this.type = type; } };
globalThis.document = { querySelector: () => null, querySelectorAll: () => [], getElementById: () => null,
  addEventListener() {}, createElement: stub, documentElement: { classList: { toggle() {} }, append() {} }, hidden: false };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "", color: "" });
globalThis.setInterval = () => 0;
const killed = { calls: 0 };
globalThis.fetch = (url, opts) => {
  if (url === "/api/mode") return Promise.resolve({ ok: true, json: () => Promise.resolve({ demo: false }) });
  if (url === "/api/autopilot") return Promise.resolve({ ok: true, json: () => Promise.resolve({ enabled: true, pending: [] }) });
  if (url === "/api/autopilot/kill") {
    killed.calls += 1;
    if (scenario === "network") return Promise.reject(new TypeError("Failed to fetch"));
    if (scenario === "error") return Promise.resolve({ ok: false, status: 500,
      json: () => Promise.resolve({ error: "exception", message: "database is locked" }) });
    if (scenario === "errorNoBody") return Promise.resolve({ ok: false, status: 503, json: () => Promise.reject(new Error("no json")) });
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ enabled: false, pending: [] }) });
  }
  return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
};
vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8") + "\nglobalThis.DD = DD;");
(async () => {
  await DD.modeReady;
  const bar = stub("div");
  const ks = DD.mountTopbarKillSwitch(bar);
  await new Promise((r) => setTimeout(r, 10));
  const before = { hidden: ks.button.hidden };
  await ks.button.handlers.click();
  console.log(JSON.stringify({ before, btnHidden: ks.button.hidden, btnDisabled: ks.button.disabled,
    errHidden: ks.error.hidden, errText: ks.error.textContent, errRole: ks.error.attrs.role,
    kills: killed.calls, events }));
})().catch((e) => { console.log(JSON.stringify({ crash: String(e) })); });
"""


def _kill_probe(scenario: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    done = subprocess.run([node, "-e", _KILL_PROBE, str(ROOT / "disputedesk" / "web" / "static" / "common.js"), scenario],
                          capture_output=True, text=True, timeout=30, check=True)
    return json.loads(done.stdout)


def test_kill_switch_hides_only_after_the_server_confirms():
    out = _kill_probe("ok")
    assert "crash" not in out
    assert out["before"]["hidden"] is False and out["kills"] == 1
    assert out["btnHidden"] is True and out["errHidden"] is True and out["errText"] == ""
    assert out["events"] == ["dd:autopilot-changed"]


@pytest.mark.parametrize("scenario,text", [
    ("error", "Autopilot was not stopped: database is locked"),
    ("errorNoBody", "Autopilot was not stopped: The server answered HTTP 503."),
    ("network", "Autopilot was not stopped: can't reach the DisputeDesk server."),
])
def test_kill_switch_failure_keeps_button_and_shows_error_text(scenario, text):
    out = _kill_probe(scenario)
    assert "crash" not in out  # the stub DOM throws if innerHTML is ever assigned
    assert out["btnHidden"] is False and out["btnDisabled"] is False
    assert out["errHidden"] is False and out["errText"] == text and out["errRole"] == "alert"
    assert out["events"] == []


def test_kill_switch_source_uses_text_only():
    common = (ROOT / "disputedesk" / "web" / "static" / "common.js").read_text(encoding="utf-8")
    block = common.split("function mountTopbarKillSwitch(statusBar)", 1)[1].split("function mountAutopilot(", 1)[0]
    assert "innerHTML" not in block and "insertAdjacentHTML" not in block and "outerHTML" not in block
    assert "if (!resp.ok) throw new Error(" in block
    assert block.index("if (!resp.ok)") < block.index("btn.hidden = true;\n        window.dispatchEvent")
