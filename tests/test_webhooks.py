import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from disputedesk.auth import TokenProvider
from disputedesk.config import SANDBOX_BASE_URL
from disputedesk.dispute_store import DisputeStore
from disputedesk.disputes import DisputesClient
from disputedesk.events import DisputeDetected, EventBus
from disputedesk.poller import DisputePoller
from disputedesk.webhooks import EventStore, PayPalSignatureVerifier, create_app
from tests.conftest import TOKEN_URL, VERIFY_URL

DISPUTES_URL = f"{SANDBOX_BASE_URL}/v1/customer/disputes"

SIG_HEADERS = {
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-TEST",
    "PAYPAL-TRANSMISSION-ID": "tx-123",
    "PAYPAL-TRANSMISSION-SIG": "sig-abc",
    "PAYPAL-TRANSMISSION-TIME": "2026-10-30T10:00:00Z",
}


def event(event_id: str = "WH-EVT-1") -> dict:
    return {
        "id": event_id,
        "event_type": "CUSTOMER.DISPUTE.CREATED",
        "resource": {"dispute_id": "PP-D-1", "status": "OPEN", "dispute_life_cycle_stage": "INQUIRY"},
    }


@pytest.fixture
def paypal():
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        )
        yield router


@pytest.fixture
def store(tmp_path):
    return EventStore(tmp_path)


@pytest.fixture
def dispute_store(tmp_path):
    s = DisputeStore(tmp_path / "disputedesk.db")
    yield s
    s.close()


@pytest.fixture
def detected():
    return []


@pytest.fixture
def bus(detected):
    b = EventBus()
    b.subscribe(detected.append)
    return b


@pytest.fixture
def client(settings, http, store, dispute_store, bus):
    verifier = PayPalSignatureVerifier(settings, TokenProvider(settings, http), http)
    return TestClient(create_app(verifier=verifier, store=store, dispute_store=dispute_store, bus=bus))


def post(client, body: dict, headers=SIG_HEADERS):
    return client.post("/webhooks/paypal", content=json.dumps(body), headers=headers)


def test_valid_signature_is_accepted_and_logged(paypal, client, store):
    verify = paypal.post(VERIFY_URL).mock(
        return_value=httpx.Response(200, json={"verification_status": "SUCCESS"})
    )

    resp = post(client, event())

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    sent = json.loads(verify.calls.last.request.content)
    assert sent == {
        "auth_algo": "SHA256withRSA",
        "cert_url": SIG_HEADERS["PAYPAL-CERT-URL"],
        "transmission_id": "tx-123",
        "transmission_sig": "sig-abc",
        "transmission_time": "2026-10-30T10:00:00Z",
        "webhook_id": "WH-TEST",
        "webhook_event": event(),
    }
    assert verify.calls.last.request.headers["authorization"] == "Bearer tok"
    assert json.loads((store.events_dir / "WH-EVT-1.json").read_bytes()) == event()


def test_failed_signature_is_rejected(paypal, client, store):
    paypal.post(VERIFY_URL).mock(return_value=httpx.Response(200, json={"verification_status": "FAILURE"}))

    resp = post(client, event())

    assert resp.status_code == 400
    assert list(store.events_dir.iterdir()) == []
    assert not store.seen("WH-EVT-1")


def test_missing_signature_headers_rejected_without_calling_paypal(paypal, client):
    verify = paypal.post(VERIFY_URL)
    headers = {k: v for k, v in SIG_HEADERS.items() if k != "PAYPAL-TRANSMISSION-SIG"}

    resp = post(client, event(), headers=headers)

    assert resp.status_code == 400
    assert not verify.called


def test_non_json_body_rejected(paypal, client):
    resp = client.post("/webhooks/paypal", content=b"not json", headers=SIG_HEADERS)
    assert resp.status_code == 400


def test_verify_api_error_returns_503_so_sender_retries(paypal, client, store):
    paypal.post(VERIFY_URL).mock(return_value=httpx.Response(500))

    resp = post(client, event())

    assert resp.status_code == 503
    assert not store.seen("WH-EVT-1")


def test_duplicate_event_is_acknowledged_once(paypal, client, store):
    verify = paypal.post(VERIFY_URL).mock(
        return_value=httpx.Response(200, json={"verification_status": "SUCCESS"})
    )

    first = post(client, event())
    second = post(client, event())

    assert first.json() == {"status": "ok"}
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate"}
    assert verify.call_count == 1
    assert len(list(store.events_dir.iterdir())) == 1


def test_dedupe_survives_restart(tmp_path):
    EventStore(tmp_path).claim("WH-EVT-9", b"{}")

    restarted = EventStore(tmp_path)

    assert restarted.seen("WH-EVT-9")
    assert restarted.claim("WH-EVT-9", b"{}") is False


def verified(paypal):
    paypal.post(VERIFY_URL).mock(return_value=httpx.Response(200, json={"verification_status": "SUCCESS"}))


def test_webhook_dispute_emits_dispute_detected(paypal, client, dispute_store, detected):
    verified(paypal)

    post(client, event())

    assert detected == [DisputeDetected("PP-D-1", "webhook")]
    assert dispute_store.source_of("PP-D-1") == "webhook"


def test_webhook_then_poll_processes_dispute_once(paypal, client, settings, http, dispute_store, bus, detected, tmp_path):
    verified(paypal)
    paypal.get(DISPUTES_URL).mock(return_value=httpx.Response(200, json={"items": [{"dispute_id": "PP-D-1"}]}))
    get_one = paypal.get(f"{DISPUTES_URL}/PP-D-1")
    poller = DisputePoller(DisputesClient(settings, TokenProvider(settings, http), http),
                           dispute_store, bus, tmp_path / "disputes", interval=10)

    post(client, event())
    poller.poll_once()

    assert detected == [DisputeDetected("PP-D-1", "webhook")]
    assert not get_one.called


def test_poll_then_webhook_processes_dispute_once(paypal, client, dispute_store, detected):
    verified(paypal)
    dispute_store.claim("PP-D-1", "poll", {"dispute_id": "PP-D-1"}, full=True)

    resp = post(client, event("WH-EVT-2"))

    assert resp.json() == {"status": "ok"}
    assert detected == []


def test_non_dispute_webhook_emits_nothing(paypal, client, detected):
    verified(paypal)
    body = {"id": "WH-EVT-3", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {"id": "CAP-1"}}

    post(client, body)

    assert detected == []


def test_app_lifespan_starts_and_stops_poller(settings, store, dispute_store, bus):
    calls = []

    class FakePoller:
        async def run(self, stop):
            calls.append("started")
            await stop.wait()
            calls.append("stopped")

    app = create_app(verifier=object(), store=store, dispute_store=dispute_store, bus=bus, poller=FakePoller())
    with TestClient(app):
        assert calls == ["started"]
    assert calls == ["started", "stopped"]
