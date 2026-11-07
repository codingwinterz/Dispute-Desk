import json

import httpx
import pytest
import respx

from disputedesk.auth import TokenProvider
from disputedesk.config import SANDBOX_BASE_URL
from disputedesk.dispute_store import DisputeStore
from disputedesk.disputes import DisputesClient
from disputedesk.events import DisputeDetected, DisputeUpdated, EventBus
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
    store.claim("PP-D-2", "webhook", {"dispute_id": "PP-D-2", "status": "OPEN"}, full=True)  # already fully processed, unchanged

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


def test_pagination_follows_next_link(paypal, poller, events, caplog):
    next_href = f"{DISPUTES_URL}?page_size=50&next_page_token=TOKEN-2"
    first = httpx.Response(200, json={
        "items": [{"dispute_id": "PP-D-1"}],
        "links": [{"rel": "self", "href": DISPUTES_URL, "method": "GET"},
                  {"rel": "next", "href": next_href, "method": "GET"}],
    })
    listing = paypal.get(DISPUTES_URL).mock(side_effect=[first, page("PP-D-2")])
    paypal.get(f"{DISPUTES_URL}/PP-D-1").mock(return_value=detail("PP-D-1"))
    paypal.get(f"{DISPUTES_URL}/PP-D-2").mock(return_value=detail("PP-D-2"))

    with caplog.at_level("INFO"):
        assert poller.poll_once() == INTERVAL

    assert listing.call_count == 2
    assert listing.calls[1].request.url.params["next_page_token"] == "TOKEN-2"
    assert events == [DisputeDetected("PP-D-1", "poll"), DisputeDetected("PP-D-2", "poll")]
    assert "poll ok count=2 new=2" in caplog.text


def test_pagination_ignores_next_link_to_other_host(paypal, poller, events):
    first = httpx.Response(200, json={
        "items": [{"dispute_id": "PP-D-1"}],
        "links": [{"rel": "next", "href": "https://evil.example/v1/customer/disputes?next_page_token=x"}],
    })
    listing = paypal.get(DISPUTES_URL).mock(return_value=first)
    paypal.get(f"{DISPUTES_URL}/PP-D-1").mock(return_value=detail("PP-D-1"))

    poller.poll_once()

    assert listing.call_count == 1
    assert all(c.request.url.host == "api-m.sandbox.paypal.com" for c in paypal.calls)


def test_pagination_stops_on_repeating_next_link(paypal, poller):
    looping = httpx.Response(200, json={
        "items": [],
        "links": [{"rel": "next", "href": f"{DISPUTES_URL}?next_page_token=SAME"}],
    })
    listing = paypal.get(DISPUTES_URL).mock(return_value=looping)

    assert poller.poll_once() == INTERVAL
    assert listing.call_count == 2  # first page, then the token page once


def test_poll_log_counts_new_ids(paypal, poller, store, caplog):
    store.claim("PP-D-1", "poll", {"dispute_id": "PP-D-1"}, full=True)
    paypal.get(DISPUTES_URL).mock(return_value=page("PP-D-1", "PP-D-2"))
    paypal.get(f"{DISPUTES_URL}/PP-D-2").mock(return_value=detail("PP-D-2"))

    with caplog.at_level("INFO"):
        poller.poll_once()

    assert "poll ok count=2 new=1" in caplog.text


def test_webhook_first_dispute_is_backfilled_without_new_event(paypal, poller, store, events, tmp_path):
    store.claim("PP-D-1", "webhook", {"dispute_id": "PP-D-1", "status": "OPEN"}, full=False)
    paypal.get(DISPUTES_URL).mock(return_value=page("PP-D-1"))
    get_one = paypal.get(f"{DISPUTES_URL}/PP-D-1").mock(return_value=detail("PP-D-1"))

    poller.poll_once()
    poller.poll_once()

    assert get_one.call_count == 1  # fetched once to backfill, then not again
    # Backfill emits dispute_updated (so deferred analysis runs), never a second dispute_detected.
    assert events == [DisputeUpdated("PP-D-1", "poll")]
    assert not store.needs_full("PP-D-1")
    assert store.source_of("PP-D-1") == "webhook"
    assert (tmp_path / "disputes" / "PP-D-1.json").exists()


def listed(dispute_id: str, **fields) -> httpx.Response:
    return httpx.Response(200, json={"items": [{"dispute_id": dispute_id, **fields}], "links": []})


STORED = {"dispute_id": "PP-D-1", "status": "WAITING_FOR_SELLER_RESPONSE", "dispute_life_cycle_stage": "CHARGEBACK",
          "update_time": "2026-11-01T00:00:00Z",
          "links": [{"rel": "self", "method": "GET"}, {"rel": "provide_evidence", "method": "POST"}]}


def test_unchanged_stored_dispute_costs_no_extra_request(paypal, poller, store, events):
    paypal.get(DISPUTES_URL).mock(return_value=listed("PP-D-1", **{k: v for k, v in STORED.items() if k != "dispute_id"}))
    get_one = paypal.get(f"{DISPUTES_URL}/PP-D-1").mock(return_value=detail("PP-D-1"))
    store.claim("PP-D-1", "poll", STORED, full=True)
    poller.poll_once()
    assert get_one.call_count == 0 and events == []


def test_status_change_refreshes_stored_dispute(paypal, poller, store, events, tmp_path, caplog):
    caplog.set_level("INFO")
    paypal.get(DISPUTES_URL).mock(return_value=listed("PP-D-1", status="UNDER_REVIEW", update_time="2026-11-08T00:00:00Z"))
    fresh = dict(STORED, status="UNDER_REVIEW", update_time="2026-11-08T00:00:00Z", links=[{"rel": "self", "method": "GET"}])
    get_one = paypal.get(f"{DISPUTES_URL}/PP-D-1").mock(return_value=httpx.Response(200, json=fresh))
    store.claim("PP-D-1", "poll", STORED, full=True)

    poller.poll_once()

    assert get_one.call_count == 1
    assert store.get("PP-D-1")["status"] == "UNDER_REVIEW"
    assert store.summaries()[0]["actions"] == []  # provide_evidence link gone after refresh
    assert events == [DisputeUpdated("PP-D-1", "poll")]
    assert "changed: status, update_time" in caplog.text
    assert only_gets(paypal)


def test_links_change_alone_triggers_refresh(paypal, poller, store, events):
    paypal.get(DISPUTES_URL).mock(return_value=listed("PP-D-1", status=STORED["status"],
                                                      links=[{"rel": "self", "method": "GET"}]))
    get_one = paypal.get(f"{DISPUTES_URL}/PP-D-1").mock(return_value=httpx.Response(200, json=STORED))
    store.claim("PP-D-1", "poll", STORED, full=True)
    poller.poll_once()
    assert get_one.call_count == 1 and events == [DisputeUpdated("PP-D-1", "poll")]


def test_refresh_does_not_emit_detected_again(paypal, poller, store, events):
    paypal.get(DISPUTES_URL).mock(return_value=listed("PP-D-1", status="RESOLVED"))
    paypal.get(f"{DISPUTES_URL}/PP-D-1").mock(return_value=httpx.Response(200, json=dict(STORED, status="RESOLVED")))
    store.claim("PP-D-1", "poll", STORED, full=True)
    poller.poll_once()
    poller.poll_once()  # stored now matches the list: no second refresh
    assert events == [DisputeUpdated("PP-D-1", "poll")]
    assert not any(isinstance(e, DisputeDetected) for e in events)
