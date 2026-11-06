"""PayPal webhook receiver: POST /webhooks/paypal.

Run with:  uvicorn --factory disputedesk.webhooks:create_app --port 8000

The app also starts the dispute poller (disputedesk.poller) as a background task. Disputes seen
through either path go through one DisputeStore, so dispute_detected is emitted once per dispute.
"""

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

import httpx
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from disputedesk.auth import TokenProvider
from disputedesk.config import LOG_DIR, Settings, load_settings
from disputedesk.dispute_store import DisputeStore, safe_filename
from disputedesk.disputes import DisputesClient
from disputedesk.events import DisputeDetected, EventBus
from disputedesk.http_log import make_client
from disputedesk.poller import DisputePoller

log = logging.getLogger("disputedesk.webhooks")

# Header -> verify-webhook-signature body field.
# https://developer.paypal.com/api/webhooks/v1/#verify-webhook-signature_post
SIGNATURE_HEADERS = {
    "paypal-auth-algo": "auth_algo",
    "paypal-cert-url": "cert_url",
    "paypal-transmission-id": "transmission_id",
    "paypal-transmission-sig": "transmission_sig",
    "paypal-transmission-time": "transmission_time",
}


class VerificationUnavailable(Exception):
    """PayPal's verify API could not be reached or errored; the sender should retry."""


class Verifier(Protocol):
    def verify(self, headers: dict[str, str], event: dict[str, Any]) -> bool: ...


class PayPalSignatureVerifier:
    def __init__(self, settings: Settings, tokens: TokenProvider, http: httpx.Client):
        self._settings = settings
        self._tokens = tokens
        self._http = http

    def verify(self, headers: dict[str, str], event: dict[str, Any]) -> bool:
        body: dict[str, Any] = {field: headers[h] for h, field in SIGNATURE_HEADERS.items()}
        body["webhook_id"] = self._settings.webhook_id
        body["webhook_event"] = event
        # POST /v1/notifications/verify-webhook-signature -> {"verification_status": "SUCCESS" | "FAILURE"}
        # https://developer.paypal.com/api/webhooks/v1/#verify-webhook-signature_post
        try:
            resp = self._http.post(
                f"{self._settings.base_url}/v1/notifications/verify-webhook-signature",
                json=body,
                headers=self._tokens.auth_headers(),
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise VerificationUnavailable(type(exc).__name__) from exc
        return resp.json().get("verification_status") == "SUCCESS"


class EventStore:
    """Deduplicates by event id and keeps the raw body of each new event in logs/events/."""

    def __init__(self, root: Path):
        self.root = root
        self.events_dir = root / "events"
        self.seen_file = root / "webhook_seen_ids.txt"
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        if self.seen_file.exists():
            self._seen = set(self.seen_file.read_text(encoding="utf-8").split())

    def seen(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._seen

    def claim(self, event_id: str, raw: bytes) -> bool:
        """Record a new event. Returns False if another delivery already claimed it."""
        with self._lock:
            if event_id in self._seen:
                return False
            self._seen.add(event_id)
            with self.seen_file.open("a", encoding="utf-8") as fh:
                fh.write(event_id + "\n")
        (self.events_dir / f"{safe_filename(event_id)}.json").write_bytes(raw)
        return True


def _dispute_state(event: dict[str, Any]) -> str:
    resource = event.get("resource") or {}
    return (
        f"dispute={resource.get('dispute_id')} "
        f"stage={resource.get('dispute_life_cycle_stage')} status={resource.get('status')}"
    )


def create_app(
    verifier: Verifier | None = None,
    store: EventStore | None = None,
    dispute_store: DisputeStore | None = None,
    bus: EventBus | None = None,
    poller: DisputePoller | None = None,
) -> FastAPI:
    """Build the app. With no verifier, settings come from the environment and the real
    poller is started; tests inject their own pieces and get no poller unless they pass one."""
    settings: Settings | None = None
    if verifier is None:
        settings = load_settings()
        secrets = (settings.client_id, settings.client_secret)
        http = make_client(LOG_DIR / "webhook_http.jsonl", secrets)
        verifier = PayPalSignatureVerifier(settings, TokenProvider(settings, http), http)
    store = store or EventStore(LOG_DIR)
    dispute_store = dispute_store or DisputeStore(store.root / "disputes.sqlite3")
    bus = bus or EventBus()
    if poller is None and settings is not None:
        poll_http = make_client(LOG_DIR / "poll_http.jsonl", secrets)
        poll_client = DisputesClient(settings, TokenProvider(settings, poll_http), poll_http)
        poller = DisputePoller(poll_client, dispute_store, bus, LOG_DIR / "disputes", settings.poll_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task = asyncio.create_task(poller.run(stop)) if poller else None
        try:
            yield
        finally:
            stop.set()
            if task:
                await task

    app = FastAPI(title="DisputeDesk webhook receiver", lifespan=lifespan)
    app.state.bus = bus
    app.state.dispute_store = dispute_store

    @app.post("/webhooks/paypal")
    async def paypal_webhook(request: Request) -> JSONResponse:
        raw = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        # The verify call is blocking HTTP; keep it off the event loop.
        return await run_in_threadpool(_handle, headers, raw, verifier, store, dispute_store, bus)

    return app


def _detect_from_webhook(event: dict[str, Any], dispute_store: DisputeStore, bus: EventBus) -> None:
    # Dispute webhooks (CUSTOMER.DISPUTE.CREATED / UPDATED / RESOLVED) carry the dispute as `resource`.
    # https://developer.paypal.com/api/rest/webhooks/event-names/#disputes
    # UNVERIFIED: that resource.dispute_id is present on every CUSTOMER.DISPUTE.* event.
    if not str(event.get("event_type", "")).startswith("CUSTOMER.DISPUTE."):
        return
    dispute_id = (event.get("resource") or {}).get("dispute_id")
    if not dispute_id:
        return
    try:
        if dispute_store.claim(dispute_id, "webhook"):
            bus.emit(DisputeDetected(dispute_id, "webhook"))
        else:
            log.info("dispute=%s already processed via %s; not emitted again",
                     dispute_id, dispute_store.source_of(dispute_id))
    except Exception as exc:  # the event is stored already; don't turn this into a 500
        log.error("dispute detection failed for dispute=%s: %s", dispute_id, type(exc).__name__)


def _handle(
    headers: dict[str, str],
    raw: bytes,
    verifier: Verifier,
    store: EventStore,
    dispute_store: DisputeStore,
    bus: EventBus,
) -> JSONResponse:
    if any(h not in headers for h in SIGNATURE_HEADERS):
        log.warning("rejected webhook: missing signature headers")
        return JSONResponse({"error": "missing signature headers"}, status_code=400)
    try:
        event = json.loads(raw)
        event_id = event["id"]
    except (ValueError, KeyError, TypeError):
        log.warning("rejected webhook: body is not a PayPal event")
        return JSONResponse({"error": "invalid body"}, status_code=400)

    # Duplicates skip the verify API call. This is safe: a duplicate causes no side effects.
    if store.seen(event_id):
        log.info("duplicate webhook %s %s", event_id, _dispute_state(event))
        return JSONResponse({"status": "duplicate"})

    try:
        valid = verifier.verify(headers, event)
    except VerificationUnavailable as exc:
        log.error("signature verification unavailable (%s) for %s", exc, event_id)
        return JSONResponse({"error": "verification unavailable"}, status_code=503)
    if not valid:
        log.warning("rejected webhook %s: signature verification FAILURE", event_id)
        return JSONResponse({"error": "invalid signature"}, status_code=400)

    if not store.claim(event_id, raw):
        return JSONResponse({"status": "duplicate"})
    log.info("accepted webhook %s type=%s %s", event_id, event.get("event_type"), _dispute_state(event))
    _detect_from_webhook(event, dispute_store, bus)
    return JSONResponse({"status": "ok"})
