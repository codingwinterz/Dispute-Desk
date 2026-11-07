import json

import httpx
import respx

from disputedesk.http_log import make_client

TOKEN_URL = "https://api-m.sandbox.paypal.com/v1/oauth2/token"


@respx.mock
def test_secrets_are_redacted(tmp_path):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "A21-secret-token", "expires_in": 1, "nonce": "n"})
    )
    log_path = tmp_path / "http.jsonl"
    with make_client(log_path, secrets=("test-client-secret",)) as client:
        client.post(
            TOKEN_URL,
            auth=("test-client-id", "test-client-secret"),
            data={"grant_type": "client_credentials", "echo": "test-client-secret"},
        )

    text = log_path.read_text()
    assert "A21-secret-token" not in text
    assert "test-client-secret" not in text
    assert "Basic " not in text
    record = json.loads(text)
    assert record["request"]["headers"]["authorization"] == "[REDACTED]"
    assert record["response"]["body"]["access_token"] == "[REDACTED]"
    assert record["response"]["status"] == 200
