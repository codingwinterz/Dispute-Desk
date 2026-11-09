import httpx
import pytest

from disputedesk.config import SANDBOX_BASE_URL, Settings

TOKEN_URL = f"{SANDBOX_BASE_URL}/v1/oauth2/token"
VERIFY_URL = f"{SANDBOX_BASE_URL}/v1/notifications/verify-webhook-signature"


@pytest.fixture
def settings() -> Settings:
    return Settings(client_id="test-client-id", client_secret="test-client-secret", webhook_id="WH-TEST")


@pytest.fixture
def http():
    with httpx.Client() as client:
        yield client


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _no_scratch_env(monkeypatch):
    """A scratch session in the developer's shell must never leak into the tests."""
    monkeypatch.delenv("DISPUTEDESK_DB", raising=False)
    monkeypatch.delenv("DISPUTEDESK_GRAPH", raising=False)
