import json

import httpx
import pytest
import respx

from disputedesk.auth import TokenProvider
from disputedesk.config import SANDBOX_BASE_URL
from disputedesk.dispute_store import DisputeStore
from disputedesk.disputes import DisputesClient
from disputedesk.events import DisputeDetected, EventBus
from disputedesk.poller import DisputePoller
from tests.conftest import TOKEN_URL

DISPUTES_URL = f"{SANDBOX_BASE_URL}/v1/customer/disputes"
INTERVAL = 10.0


def token(value: str = "tok") -> httpx.Response:
    return httpx.Response(200, json={"access_token": value, "expires_in": 3600})


def page(*ids: str) -> httpx.Response:
    return httpx.Response(200, json={"items": [{"dispute_id": i, "status": "OPEN"} for i in ids], "links": []})


def detail(dispute_id: str) -> httpx.Response:
    return httpx.Response(200, json={"dispute_id": dispute_id, "status": "OPEN", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"})


@pytest.fixture
def paypal():
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(return_value=token())
        yield router


@pytest.fixture
def store(tmp_path):
    s = DisputeStore(tmp_path / "disputes.sqlite3")
    yield s
    s.close()


@pytest.fixture
def events():
    return []


@pytest.fixture
def poller(settings, http, store, events, tmp_path):
    bus = EventBus()
    bus.subscribe(events.append)
    client = DisputesClient(settings, TokenProvider(settings, http), http)
    return DisputePoller(client, store, bus, tmp_path / "disputes", interval=INTERVAL)


def only_gets(router) -> bool:
    return all(c.request.method == "GET" for c in router.calls if str(c.request.url).startswith(DISPUTES_URL))


def test_new_dispute_is_fetched_saved_and_emitted(paypal, poller, store, events, tmp_path):
    paypal.get(DISPUTES_URL).mock(return_value=page("PP-D-1"))
    get_one = paypal.get(f"{DISPUTES_URL}/PP-D-1").mock(return_value=detail("PP-D-1"))

    assert poller.poll_once() == INTERVAL

    assert get_one.call_count == 1
    assert events == [DisputeDetected("PP-D-1", "poll")]
    assert store.source_of("PP-D-1") == "poll"
    saved = json.loads((tmp_path / "disputes" / "PP-D-1.json").read_text(encoding="utf-8"))
    assert saved["reason"] == "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
    assert only_gets(paypal)


def test_duplicate_is_ignored(paypal, poller, store, events):
    paypal.get(DISPUTES_URL).mock(return_value=page("PP-D-1", "PP-D-2"))
    get_1 = paypal.get(f"{DISPUTES_URL}/PP-D-1").mock(return_value=detail("PP-D-1"))
    get_2 = paypal.get(f"{DISPUTES_URL}/PP-D-2").mock(return_value=detail("PP-D-2"))
    store.claim("PP-D-2", "webhook")  # already processed via the webhook path

    poller.poll_once()
    poller.poll_once()  # second poll returns the same IDs

    assert get_1.call_count == 1
    assert get_2.call_count == 0
    assert events == [DisputeDetected("PP-D-1", "poll")]


def test_401_refreshes_token_once_and_retries(paypal, poller, events):
    tokens = paypal.post(TOKEN_URL).mock(side_effect=[token("old"), token("new")])
    listing = paypal.get(DISPUTES_URL).mock(side_effect=[httpx.Response(401), page("PP-D-1")])
    paypal.get(f"{DISPUTES_URL}/PP-D-1").mock(return_value=detail("PP-D-1"))

    assert poller.poll_once() == INTERVAL

    assert tokens.call_count == 2
    assert listing.calls[0].request.headers["authorization"] == "Bearer old"
    assert listing.calls[1].request.headers["authorization"] == "Bearer new"
    assert events == [DisputeDetected("PP-D-1", "poll")]


def test_401_twice_backs_off_without_raising(paypal, poller):
    paypal.get(DISPUTES_URL).mock(return_value=httpx.Response(401))

    assert poller.poll_once() == INTERVAL * 2


def test_5xx_backs_off_exponentially_then_recovers(paypal, poller, events):
    paypal.get(DISPUTES_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(500), page("PP-D-1")]
    )
    paypal.get(f"{DISPUTES_URL}/PP-D-1").mock(return_value=detail("PP-D-1"))

    assert poller.poll_once() == INTERVAL * 2
    assert poller.poll_once() == INTERVAL * 4
    assert poller.poll_once() == INTERVAL
    assert events == [DisputeDetected("PP-D-1", "poll")]


def test_5xx_on_detail_backs_off_and_retries_next_poll(paypal, poller, store, events):
    paypal.get(DISPUTES_URL).mock(return_value=page("PP-D-1"))
    paypal.get(f"{DISPUTES_URL}/PP-D-1").mock(side_effect=[httpx.Response(502), detail("PP-D-1")])

    assert poller.poll_once() == INTERVAL * 2
    assert not store.seen("PP-D-1")
    assert poller.poll_once() == INTERVAL
    assert events == [DisputeDetected("PP-D-1", "poll")]


def test_429_honours_retry_after(paypal, poller):
    paypal.get(DISPUTES_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "120"}))

    assert poller.poll_once() == 120.0


def test_backoff_is_capped(paypal, settings, http, store, tmp_path):
    client = DisputesClient(settings, TokenProvider(settings, http), http)
    p = DisputePoller(client, store, EventBus(), tmp_path, interval=INTERVAL, max_backoff=30.0)
    paypal.get(DISPUTES_URL).mock(return_value=httpx.Response(500))

    delays = [p.poll_once() for _ in range(4)]

    assert delays == [20.0, 30.0, 30.0, 30.0]


def test_network_error_backs_off(paypal, poller):
    paypal.get(DISPUTES_URL).mock(side_effect=httpx.ConnectError("down"))

    assert poller.poll_once() == INTERVAL * 2


def test_token_not_in_logs(paypal, poller, caplog):
    paypal.post(TOKEN_URL).mock(return_value=token("super-secret-token"))
    paypal.get(DISPUTES_URL).mock(return_value=page())

    with caplog.at_level("DEBUG"):
        poller.poll_once()

    assert "poll ok count=0" in caplog.text
    assert "super-secret-token" not in caplog.text
