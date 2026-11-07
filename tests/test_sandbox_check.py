import importlib.util
from pathlib import Path

import httpx
import respx

from disputedesk.auth import TokenProvider
from disputedesk.config import SANDBOX_BASE_URL
from disputedesk.disputes import DisputesClient
from tests.conftest import TOKEN_URL

_SPEC = importlib.util.spec_from_file_location(
    "sandbox_check", Path(__file__).resolve().parent.parent / "scripts" / "sandbox_check.py"
)
sandbox_check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sandbox_check)

DISPUTES_URL = f"{SANDBOX_BASE_URL}/v1/customer/disputes"

SAMPLE = {
    "dispute_id": "PP-D-1",
    "status": "WAITING_FOR_SELLER_RESPONSE",
    "dispute_life_cycle_stage": "INQUIRY",
    "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
    "create_time": "2026-11-01T10:00:00.000Z",
    "seller_response_due_date": "2026-11-21T10:00:00.000Z",
    "dispute_amount": {"currency_code": "USD", "value": "12.34"},
    "disputed_transactions": [{"seller_transaction_id": "S-TXN", "buyer_transaction_id": "B-TXN"}],
    "messages": [
        {"posted_by": "BUYER", "time_posted": "2026-11-01T10:00:00.000Z", "content": "Where is it?"},
        {"posted_by": "SELLER", "time_posted": "2026-11-02T10:00:00.000Z", "content": "Shipped."},
    ],
    "links": [
        {"href": "https://x/self", "rel": "self", "method": "GET"},
        {"href": "https://x/offer", "rel": "make_offer", "method": "POST"},
    ],
}


def token_ok() -> httpx.Response:
    return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600, "token_type": "Bearer"})


def test_format_dispute_full():
    lines = sandbox_check.format_dispute(SAMPLE)
    assert "dispute_id=PP-D-1" in lines
    assert "dispute_stage=INQUIRY" in lines
    assert "seller_response_due_date=2026-11-21T10:00:00.000Z" in lines
    assert "dispute_amount=12.34 USD" in lines
    assert "  seller_transaction_id=S-TXN buyer_transaction_id=B-TXN" in lines
    assert "  2026-11-01T10:00:00.000Z: Where is it?" in lines
    assert not any("Shipped." in line for line in lines)
    assert "  self GET" in lines
    assert "  make_offer POST" in lines
    assert not any("https://x/" in line for line in lines)


def test_format_dispute_minimal():
    lines = sandbox_check.format_dispute({"dispute_id": "PP-D-2"})
    assert "status=None" in lines
    assert "dispute_amount=None" in lines
    assert "disputed_transactions: none" in lines
    assert "buyer_messages: none" in lines
    assert "links: none" in lines


@respx.mock
def test_show_dispute_only_gets(settings, http, capsys):
    respx.post(TOKEN_URL).mock(return_value=token_ok())
    respx.get(f"{DISPUTES_URL}/PP-D-1").mock(return_value=httpx.Response(200, json=SAMPLE))
    client = DisputesClient(settings, TokenProvider(settings, http), http)

    assert sandbox_check.show_dispute(client, "PP-D-1") == 0

    dispute_calls = [c.request for c in respx.calls if str(c.request.url).startswith(DISPUTES_URL)]
    assert dispute_calls and all(r.method == "GET" for r in dispute_calls)
    assert "dispute_amount=12.34 USD" in capsys.readouterr().out


@respx.mock
def test_show_dispute_not_found(settings, http, capsys):
    respx.post(TOKEN_URL).mock(return_value=token_ok())
    respx.get(f"{DISPUTES_URL}/PP-D-404").mock(
        return_value=httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND", "message": "nope", "debug_id": "d1"})
    )
    client = DisputesClient(settings, TokenProvider(settings, http), http)

    assert sandbox_check.show_dispute(client, "PP-D-404") == 1
    assert "RESOURCE_NOT_FOUND" in capsys.readouterr().out
