import base64
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from disputedesk.auth import TokenProvider
from tests.conftest import TOKEN_URL, FakeClock


def token_response(token: str, expires_in: int = 3600) -> httpx.Response:
    return httpx.Response(200, json={"access_token": token, "token_type": "Bearer", "expires_in": expires_in})


@respx.mock
def test_requests_client_credentials_with_basic_auth(settings, http):
    route = respx.post(TOKEN_URL).mock(return_value=token_response("tok-1"))

    assert TokenProvider(settings, http).get_token() == "tok-1"

    request = route.calls.last.request
    expected = base64.b64encode(b"test-client-id:test-client-secret").decode()
    assert request.headers["authorization"] == f"Basic {expected}"
    assert parse_qs(request.content.decode()) == {"grant_type": ["client_credentials"]}


@respx.mock
def test_token_is_cached_until_near_expiry(settings, http):
    route = respx.post(TOKEN_URL).mock(side_effect=[token_response("tok-1", 3600), token_response("tok-2", 3600)])
    clock = FakeClock()
    provider = TokenProvider(settings, http, skew_seconds=60, clock=clock)

    assert provider.get_token() == "tok-1"
    clock.now += 3600 - 61
    assert provider.get_token() == "tok-1"
    assert route.call_count == 1

    clock.now += 2  # now inside the 60s skew window
    assert provider.get_token() == "tok-2"
    assert route.call_count == 2


@respx.mock
def test_invalidate_forces_refetch(settings, http):
    route = respx.post(TOKEN_URL).mock(side_effect=[token_response("tok-1"), token_response("tok-2")])
    provider = TokenProvider(settings, http)

    provider.get_token()
    provider.invalidate()

    assert provider.get_token() == "tok-2"
    assert route.call_count == 2


@respx.mock
def test_token_error_raises_and_caches_nothing(settings, http):
    respx.post(TOKEN_URL).mock(
        side_effect=[httpx.Response(401, json={"error": "invalid_client"}), token_response("tok-1")]
    )
    provider = TokenProvider(settings, http)

    with pytest.raises(httpx.HTTPStatusError):
        provider.get_token()
    assert provider.get_token() == "tok-1"
