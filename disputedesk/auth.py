"""OAuth 2.0 client-credentials token, cached until shortly before expiry."""

import threading
import time
from collections.abc import Callable

import httpx

from disputedesk.config import Settings


class TokenProvider:
    def __init__(
        self,
        settings: Settings,
        http: httpx.Client,
        skew_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._settings = settings
        self._http = http
        self._skew = skew_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def get_token(self) -> str:
        with self._lock:
            if self._token is None or self._clock() >= self._expires_at - self._skew:
                self._fetch()
            assert self._token is not None
            return self._token

    def invalidate(self) -> None:
        """Forget the cached token, e.g. after PayPal answers 401."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _fetch(self) -> None:
        # POST /v1/oauth2/token, Basic auth, form body grant_type=client_credentials.
        # Response: access_token, token_type, expires_in (seconds).
        # https://developer.paypal.com/api/rest/authentication/
        resp = self._http.post(
            f"{self._settings.base_url}/v1/oauth2/token",
            auth=(self._settings.client_id, self._settings.client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._expires_at = self._clock() + float(body["expires_in"])

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_token()}"}
