"""Polling made visible: GET /api/poller, the top-bar chip wording and the case timeline's detection line.

HTTP is mocked with respx and time with FakeClock; the chip and timeline wording run in Node against the
real common.js (skipped when Node is not installed)."""

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from disputedesk.auth import TokenProvider
from disputedesk.config import SANDBOX_BASE_URL
from disputedesk.dispute_store import DisputeStore
from disputedesk.disputes import DisputesClient
from disputedesk.events import EventBus
from disputedesk.poller import MAX_ERROR_CHARS, DisputePoller, redact_error
from disputedesk.webhooks import EventStore, create_app
from tests.conftest import TOKEN_URL, FakeClock
from tests.test_demo import demo_client, no_outside_access, snapshot_file  # noqa: F401  (fixtures)

DISPUTES_URL = f"{SANDBOX_BASE_URL}/v1/customer/disputes"
INTERVAL = 10.0
WEB = Path(__file__).resolve().parent.parent / "disputedesk" / "web"


def page(*ids: str) -> httpx.Response:
    return httpx.Response(200, json={"items": [{"dispute_id": i, "status": "OPEN"} for i in ids], "links": []})


def detail(dispute_id: str) -> httpx.Response:
    return httpx.Response(200, json={"dispute_id": dispute_id, "status": "OPEN", "create_time": "2026-11-09T10:00:00.000Z"})


@pytest.fixture
def paypal():
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}))
        yield router


@pytest.fixture
def store(tmp_path):
    s = DisputeStore(tmp_path / "disputes.sqlite3")
    yield s
    s.close()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def poller(settings, http, store, clock, tmp_path):
    client = DisputesClient(settings, TokenProvider(settings, http), http)
    return DisputePoller(client, store, EventBus(), tmp_path / "disputes", interval=INTERVAL, clock=clock,
                         secrets=(settings.client_id, settings.client_secret))


# --- poller status ---------------------------------------------------------------------------

def test_status_before_the_first_poll_is_starting(poller):
    s = poller.status()
    assert s["state"] == "starting" and s["polling"] is True and s["demo"] is False
    assert s["interval_seconds"] == INTERVAL
    assert s["last_poll_at"] is None and s["seconds_since_last_poll"] is None and s["next_poll_in_seconds"] is None
    assert s["disputes_returned"] is None and s["new_ids"] == [] and s["consecutive_errors"] == 0 and s["last_error"] is None


def test_status_after_a_poll_reports_count_new_ids_and_timing(paypal, poller, store, clock):
    store.claim("PP-D-OLD", "poll", {"dispute_id": "PP-D-OLD", "status": "OPEN"}, full=True)
    paypal.get(DISPUTES_URL).mock(return_value=page("PP-D-OLD", "PP-D-NEW"))
    paypal.get(f"{DISPUTES_URL}/PP-D-NEW").mock(return_value=detail("PP-D-NEW"))

    assert poller.poll_once() == INTERVAL
    clock.now += 3
    s = poller.status()

    assert s["state"] == "ok"
    assert s["disputes_returned"] == 2 and s["new_ids"] == ["PP-D-NEW"]
    assert s["seconds_since_last_poll"] == 3.0 and s["next_poll_in_seconds"] == INTERVAL - 3
    assert s["last_poll_at"] == "1970-01-01T00:16:40+00:00" and s["last_success_at"] == s["last_poll_at"]
    assert s["consecutive_errors"] == 0


def test_status_never_calls_paypal(paypal, poller):
    paypal.get(DISPUTES_URL).mock(return_value=page())
    poller.poll_once()
    calls = len(paypal.calls)
    for _ in range(5):
        poller.status()
    assert len(paypal.calls) == calls


def test_failing_polls_report_the_error_count_backoff_and_last_error(paypal, poller, clock):
    route = paypal.get(DISPUTES_URL).mock(return_value=httpx.Response(503))
    poller.poll_once()
    first = poller.poll_once()  # second failure: backoff doubles again
    s = poller.status()
    assert s["state"] == "error" and s["consecutive_errors"] == 2
    assert s["last_error"] == "HTTP 503" and s["next_poll_in_seconds"] == first == INTERVAL * 4

    route.mock(return_value=page("PP-D-1"))
    paypal.get(f"{DISPUTES_URL}/PP-D-1").mock(return_value=detail("PP-D-1"))
    clock.now += first
    poller.poll_once()
    s = poller.status()
    assert s["state"] == "ok" and s["consecutive_errors"] == 0 and s["disputes_returned"] == 1
    assert s["last_error"] == "HTTP 503"  # kept for diagnosis; the chip only shows it while failing


def test_network_error_is_reported_by_type_only(paypal, poller):
    paypal.get(DISPUTES_URL).mock(side_effect=httpx.ConnectTimeout("timed out https://x/?access_token=abc"))
    poller.poll_once()
    assert poller.status()["last_error"] == "ConnectTimeout"


def test_error_text_has_secrets_removed():
    raw = ("HTTP 401 for https://api-m.sandbox.paypal.com/v1/x?client_secret=s3cr3t Authorization: Bearer A21AAtoken.abc "
           "token=xyz my-client-id-123 Basic dGVzdDpzZWNyZXQ=")
    out = redact_error(raw, ("my-client-id-123",))
    for leaked in ("s3cr3t", "A21AAtoken", "xyz", "my-client-id-123", "dGVzdDpzZWNyZXQ="):
        assert leaked not in out, leaked
    assert out.startswith("HTTP 401 for https://api-m.sandbox.paypal.com/v1/x")
    assert len(redact_error("x" * 1000)) == MAX_ERROR_CHARS


def test_detection_is_unchanged_by_status_tracking(paypal, poller, store):
    """Same IDs, same dedupe: a second poll with no changes fetches nothing new."""
    paypal.get(DISPUTES_URL).mock(return_value=page("PP-D-1"))
    get_one = paypal.get(f"{DISPUTES_URL}/PP-D-1").mock(return_value=detail("PP-D-1"))
    poller.poll_once()
    poller.poll_once()
    assert get_one.call_count == 1 and store.source_of("PP-D-1") == "poll"
    assert poller.status()["new_ids"] == []  # found nothing new on the last poll


# --- GET /api/poller -------------------------------------------------------------------------

def test_endpoint_returns_the_poller_status(paypal, poller, store, clock, tmp_path):
    paypal.get(DISPUTES_URL).mock(return_value=page("PP-D-1"))
    paypal.get(f"{DISPUTES_URL}/PP-D-1").mock(return_value=detail("PP-D-1"))
    poller.poll_once()
    clock.now += 4
    app = create_app(verifier=object(), store=EventStore(tmp_path / "logs"), dispute_store=store, poller=poller)
    client = TestClient(app)  # no lifespan: the poller loop is not started; status is read only
    calls = len(paypal.calls)

    body = client.get("/api/poller").json()
    assert body["state"] == "ok" and body["disputes_returned"] == 1 and body["new_ids"] == ["PP-D-1"]
    assert body["seconds_since_last_poll"] == 4.0 and body["next_poll_in_seconds"] == 6.0
    assert len(paypal.calls) == calls  # the route never polls

    refused = client.get("/api/poller", headers={"X-Forwarded-For": "203.0.113.9"})
    assert refused.status_code == 403  # local-only, like every other route


def test_endpoint_without_a_poller_says_off(store, tmp_path):
    app = create_app(verifier=object(), store=EventStore(tmp_path / "logs"), dispute_store=store)
    body = TestClient(app).get("/api/poller").json()
    assert body["state"] == "off" and body["polling"] is False


def test_demo_mode_reports_recorded_data_and_makes_no_network_call(demo_client):  # noqa: F811
    client, router = demo_client
    body = client.get("/api/poller").json()
    assert body["state"] == "recorded" and body["demo"] is True and body["polling"] is False
    assert body["message"] == "Recorded data, polling disabled" and body["recorded_at"]
    assert len(router.calls) == 0


def test_case_summary_carries_create_time(store):
    store.claim("PP-D-1", "poll", {"dispute_id": "PP-D-1", "create_time": "2026-11-09T10:00:00.000Z"}, full=True)
    assert store.summary("PP-D-1")["create_time"] == "2026-11-09T10:00:00.000Z"


# --- chip and timeline wording (real common.js in Node) -------------------------------------

_PROBE = r"""
const fs = require("fs"), vm = require("vm");
const recordedAt = "2026-11-06T17:05:00Z";
const stubNode = () => ({ classList: { toggle() {}, add() {} }, setAttribute() {}, append() {}, style: {} });
globalThis.window = { matchMedia: () => ({ matches: false }) };
globalThis.document = { querySelector: () => null, querySelectorAll: () => [], getElementById: () => null,
  addEventListener() {}, createElement: stubNode, documentElement: { classList: { toggle() {} }, append() {} }, hidden: false };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "", color: "" });
globalThis.setInterval = () => 0;
globalThis.fetch = (url) => Promise.resolve({ ok: true, json: () => Promise.resolve(
  url === "/api/mode" ? { demo: process.argv[2] === "demo", recorded_at: recordedAt } : {}) });
vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8") + "\nglobalThis.DD = DD;");
setTimeout(() => {
  const ok = { state: "ok", interval_seconds: 10, seconds_since_last_poll: 3, disputes_returned: 6, new_ids: [], next_poll_in_seconds: 7 };
  const out = {
    ok: DD.pollChipText(ok, 0),
    okLater: DD.pollChipText(ok, 2400),
    okOld: DD.pollChipText({ ...ok, seconds_since_last_poll: 300 }, 0),
    okNew: DD.pollChipText({ ...ok, disputes_returned: 1, new_ids: ["PP-D-9"] }, 0),
    starting: DD.pollChipText({ state: "starting", interval_seconds: 2.5, seconds_since_last_poll: null }, 0),
    error: DD.pollChipText({ state: "error", consecutive_errors: 3, next_poll_in_seconds: 40, last_error: "HTTP 503" }, 1000),
    error1: DD.pollChipText({ state: "error", consecutive_errors: 1, next_poll_in_seconds: null, last_error: null }, 0),
    off: DD.pollChipText({ state: "off" }, 0),
    unknown: DD.pollChipText({ state: "unknown" }, 0),
    recorded: DD.pollChipText({ state: "recorded" }, 0),
    clockIsRecorded: DD.clock() === Date.parse(recordedAt),
    recordedLabel: new Date(recordedAt).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }),
    gap7: DD.detectionText("2026-11-09T10:00:00.000Z", "2026-11-09T10:00:07.400+00:00", "poll"),
    gap1: DD.detectionText("2026-11-09T10:00:00Z", "2026-11-09T10:00:01Z", "webhook"),
    gap185: DD.detectionText("2026-11-09T10:00:00Z", "2026-11-09T10:03:05Z", "poll"),
    gap600: DD.detectionText("2026-11-09T10:00:00Z", "2026-11-09T10:10:00Z", "poll"),
    gap601: DD.detectionText("2026-11-09T10:00:00Z", "2026-11-09T10:10:01Z", "poll"),
    negative: DD.detectionText("2026-11-09T10:00:05Z", "2026-11-09T10:00:00Z", "poll"),
    missing: DD.detectionText(null, "2026-11-09T10:00:00Z", "poll"),
  };
  console.log(JSON.stringify(out));
}, 10);
"""


def _probe(mode: str = "live") -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    done = subprocess.run([node, "-e", _PROBE, str(WEB / "static" / "common.js"), mode],
                          capture_output=True, text=True, timeout=30, check=True)
    return json.loads(done.stdout)


def test_chip_wording_from_the_real_status():
    out = _probe()
    assert out["ok"]["text"] == "Polling every 10s. Last 3s ago. 6 disputes." and out["ok"]["tone"] == ""
    assert out["okLater"]["text"] == "Polling every 10s. Last 5s ago. 6 disputes."  # ticks between fetches
    assert out["okOld"]["text"] == "Polling every 10s. Last 5 min ago. 6 disputes."
    assert out["okNew"]["text"] == "Polling every 10s. Last 3s ago. 1 dispute, 1 new." and "PP-D-9" in out["okNew"]["title"]
    assert out["starting"]["text"] == "Polling every 2.5s. First poll running."
    assert out["off"]["text"] == "Polling off" and out["unknown"]["text"] == "Polling status unknown"


def test_chip_turns_to_the_error_state_when_polling_fails():
    out = _probe()
    assert out["error"] == {"text": "Polling failing: 3 errors in a row. Retry in 39s.", "tone": "error",
                            "title": "Last error: HTTP 503. The poller backs off and retries on its own."}
    assert out["error1"]["text"] == "Polling failing: 1 error in a row." and out["error1"]["tone"] == "error"


def test_chip_reads_recorded_data_in_demo_mode_using_the_demo_clock():
    out = _probe("demo")
    assert out["clockIsRecorded"] is True
    assert out["recorded"]["text"] == "Recorded data" and out["recorded"]["tone"] == "recorded"
    assert out["recordedLabel"] in out["recorded"]["title"] and "Polling is disabled" in out["recorded"]["title"]


def test_timeline_detection_wording():
    out = _probe()
    assert out["gap7"] == "Detected 7 seconds after PayPal created it, via poll"
    assert out["gap1"] == "Detected 1 second after PayPal created it, via webhook"
    assert out["gap185"] == "Detected 3 min 5 s after PayPal created it, via poll"
    assert out["gap600"].startswith("Detected 10 min 0 s")
    assert out["gap601"] == "" and out["negative"] == "" and out["missing"] == ""  # over 10 minutes, reversed, unknown


# --- page wiring -----------------------------------------------------------------------------

def test_chip_sits_next_to_live_and_is_motionless_and_palette_only():
    common = (WEB / "static" / "common.js").read_text(encoding="utf-8")
    css = (WEB / "static" / "app.css").read_text(encoding="utf-8")
    assert 'id: "poll-chip"' in common and 'fetch("/api/poller"' in common
    nav = common.split("function renderNav", 1)[1].split("modeReady.then", 1)[0]
    assert nav.index("live,") < nav.index("poll-chip") < nav.index('class: "env"')
    chip_rules = [body for sel, body in __import__("re").findall(r"([^{}]+)\{([^{}]*)\}", css) if "poll-chip" in sel]
    assert chip_rules and not any("animation" in b or "transition" in b or "--danger" in b for b in chip_rules)


def test_case_timeline_uses_the_detection_line_and_every_analysis_shows_verdict_and_risk():
    js = (WEB / "static" / "case.js").read_text(encoding="utf-8")
    body = js.split("function caseTimeline", 1)[1].split("function verdictChange", 1)[0]
    assert "DD.detectionText(d.create_time, d.detected_at, d.detected_via)" in body
    assert '"Analyzed by the agent"' not in body  # the bare label the latest entry used to show
    assert body.count("${outcome(") == 3  # earlier entries, the current one, and the re-analysis after an event
