import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from disputedesk.auth import TokenProvider
from disputedesk.webhooks import EventStore, PayPalSignatureVerifier, create_app
from tests.conftest import TOKEN_URL, VERIFY_URL

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
def client(settings, http, store):
    verifier = PayPalSignatureVerifier(settings, TokenProvider(settings, http), http)
    return TestClient(create_app(verifier=verifier, store=store))


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
