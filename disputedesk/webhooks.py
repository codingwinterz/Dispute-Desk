"""DisputeDesk app: PayPal webhook receiver (POST /webhooks/paypal), dashboard and read-only API.

Run with:  uvicorn --factory disputedesk.webhooks:create_app --port 8000
Dashboard: http://127.0.0.1:8000/  (queue)  and  /case/{dispute_id}. Local-only, like /api/*.

The app also starts the dispute poller (disputedesk.poller) as a background task. Disputes seen
through either path go through one DisputeStore, so dispute_detected is emitted once per dispute.
The analyzer (disputedesk.analyzer) runs the decision step for new or updated disputes on its own
worker thread, and sweeps stored disputes without a decision at startup.
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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from disputedesk.analyzer import Analyzer
from disputedesk.auth import TokenProvider
from disputedesk.config import DISPUTE_DB, LOG_DIR, Settings, load_agent_settings, load_settings
from disputedesk.decision import SANDBOX_NOTE
from disputedesk.decision_store import DecisionStore
from disputedesk.dispute_store import DisputeStore, safe_filename
from disputedesk.drafts import DraftError, DraftStore, effective_draft
from disputedesk.disputes import DisputesClient
from disputedesk.events import DisputeDetected, EventBus
from disputedesk.graph import GraphStore, connect, context_to_facts
from disputedesk.http_log import make_client
from disputedesk.policies import PolicySettings, PolicyStore, build_rules
from disputedesk.poller import DisputePoller
from disputedesk.submissions import BLOCKING, SubmissionStore, SubmitError, Submitter

log = logging.getLogger("disputedesk.webhooks")

WEBHOOK_PATH = "/webhooks/paypal"  # the only route reachable through the ngrok tunnel
WEB_DIR = Path(__file__).resolve().parent / "web"  # dashboard HTML, CSS and JS (no build step)

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
    analyzer: Analyzer | None = None,
    decision_store: DecisionStore | None = None,
    policy_store: PolicyStore | None = None,
    draft_store: DraftStore | None = None,
    submitter: Submitter | None = None,
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
    dispute_store = dispute_store or DisputeStore(DISPUTE_DB)
    # Decisions live in the same SQLite file as the disputes.
    decision_store = decision_store or DecisionStore(dispute_store.path)
    policy_store = policy_store or PolicyStore(dispute_store.path)
    draft_store = draft_store or DraftStore(dispute_store.path)
    bus = bus or EventBus()
    if poller is None and settings is not None:
        poll_http = make_client(LOG_DIR / "poll_http.jsonl", secrets)
        poll_client = DisputesClient(settings, TokenProvider(settings, poll_http), poll_http)
        poller = DisputePoller(poll_client, dispute_store, bus, LOG_DIR / "disputes", settings.poll_seconds)
    if analyzer is None and settings is not None:
        agent = load_agent_settings()
        analyzer = Analyzer(dispute_store, decision_store, lambda: GraphStore(connect(agent)), agent.llm_model,
                            rules_provider=policy_store.rules)
    if analyzer is not None:
        bus.subscribe(analyzer.on_event)
    if submitter is None:
        submit_client = None
        if settings is not None:
            # Its own client so every approved write and its response is logged (redacted) apart.
            submit_http = make_client(LOG_DIR / "submit_http.jsonl", secrets)
            submit_client = DisputesClient(settings, TokenProvider(settings, submit_http), submit_http)
        submitter = Submitter(dispute_store, decision_store, draft_store, SubmissionStore(dispute_store.path),
                              submit_client, bus)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        if analyzer:
            analyzer.start()
            await run_in_threadpool(analyzer.sweep)
        task = asyncio.create_task(poller.run(stop)) if poller else None
        try:
            yield
        finally:
            stop.set()
            if task:
                await task
            if analyzer:
                await run_in_threadpool(analyzer.stop)

    app = FastAPI(title="DisputeDesk", lifespan=lifespan)
    app.state.bus = bus
    app.state.dispute_store = dispute_store
    app.state.decision_store = decision_store

    @app.middleware("http")
    async def local_only(request: Request, call_next: Any) -> Any:
        # Only the PayPal webhook is public. The dashboard, its static files and every /api route
        # are local-only. This app is exposed through ngrok for webhooks, and ngrok adds
        # X-Forwarded-For to every tunnelled request, so such requests are refused here.
        # UNVERIFIED for non-default ngrok configs; put real auth here before any wider exposure.
        if request.url.path != WEBHOOK_PATH and "x-forwarded-for" in request.headers:
            return JSONResponse({"error": "local access only"}, status_code=403)
        return await call_next(request)

    # --- dashboard -------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    @app.get("/", include_in_schema=False)
    async def queue_page() -> FileResponse:
        return FileResponse(WEB_DIR / "queue.html")

    @app.get("/policies", include_in_schema=False)
    async def policies_page() -> FileResponse:
        return FileResponse(WEB_DIR / "policies.html")

    @app.get("/case/{dispute_id}", include_in_schema=False)
    async def case_page(dispute_id: str) -> FileResponse:
        # The page fetches /api/decisions/{id} itself; the ID never touches the HTML.
        return FileResponse(WEB_DIR / "case.html")

    # --- read-only API (local store only; no PayPal calls) -----------------------

    def _queue_rows() -> list[dict[str, Any]]:
        briefs = decision_store.briefs()
        return [row | {"decision": briefs.get(row["dispute_id"])} for row in dispute_store.summaries()]

    @app.get("/api/disputes")
    async def stored_disputes() -> JSONResponse:
        return JSONResponse({"disputes": await run_in_threadpool(_queue_rows)})

    @app.get("/api/decisions/{dispute_id}")
    async def dispute_decision(dispute_id: str) -> JSONResponse:
        dispute = await run_in_threadpool(dispute_store.summary, dispute_id)
        if dispute is None:
            return JSONResponse({"error": "unknown dispute"}, status_code=404)
        decision = await run_in_threadpool(decision_store.get, dispute_id)
        saved = await run_in_threadpool(draft_store.get, dispute_id)
        check = await run_in_threadpool(submitter.check, dispute_id)
        return JSONResponse({"dispute": dispute, "decision": _with_fact_refs(decision),
                             "draft": effective_draft(saved, decision), "submit": check})

    # --- approved responses to PayPal (the only PayPal writes) ------------------------

    @app.get("/api/submissions/{dispute_id}/dry-run")
    async def submission_dry_run(dispute_id: str) -> JSONResponse:
        """The exact request an approval would send, built from stored state. Never sent."""
        if await run_in_threadpool(dispute_store.summary, dispute_id) is None:
            return JSONResponse({"error": "unknown dispute"}, status_code=404)
        return JSONResponse(await run_in_threadpool(submitter.dry_run, dispute_id))

    @app.post("/api/submissions/{dispute_id}")
    async def submit_response(dispute_id: str, request: Request) -> JSONResponse:
        refused = _refuse_write(request)
        if refused:
            return refused
        if await run_in_threadpool(dispute_store.summary, dispute_id) is None:
            return JSONResponse({"error": "unknown dispute"}, status_code=404)
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or body.get("confirm") is not True:
            # Nothing is sent without an explicit confirmation from the dialog.
            return JSONResponse({"error": "confirmation required",
                                 "message": 'Confirm in the dialog first (send {"confirm": true, "action", "text"}).'},
                                status_code=400)
        action, text = body.get("action"), body.get("text")
        if not isinstance(action, str) or not isinstance(text, str):
            return JSONResponse({"error": "invalid body", "message": "action and text are required."}, status_code=422)
        try:
            result = await run_in_threadpool(submitter.submit, dispute_id, action, text)
        except SubmitError as exc:
            return JSONResponse({"error": exc.code, "message": exc.message} | (exc.extra or {}), status_code=exc.http)
        return JSONResponse(result)

    # --- merchant edits (local store only; never PayPal) ---------------------------

    @app.get("/api/policies")
    async def get_policies() -> JSONResponse:
        settings, updated_at = await run_in_threadpool(policy_store.get)
        return JSONResponse(_policy_body(settings, updated_at))

    @app.post("/api/policies/preview")
    async def preview_policies(request: Request) -> JSONResponse:
        """Rule text for unsaved settings, so the page shows exactly what the agent would read."""
        settings = await _parse_settings(request)
        if isinstance(settings, JSONResponse):
            return settings
        return JSONResponse({"rules": build_rules(settings)})

    @app.put("/api/policies")
    async def put_policies(request: Request) -> JSONResponse:
        settings = await _parse_settings(request)
        if isinstance(settings, JSONResponse):
            return settings
        updated_at = await run_in_threadpool(policy_store.save, settings)
        log.info("policy settings saved")
        return JSONResponse(_policy_body(settings, updated_at))

    @app.put("/api/drafts/{dispute_id}")
    async def put_draft(dispute_id: str, request: Request) -> JSONResponse:
        refused = _refuse_write(request)
        if refused:
            return refused
        if await run_in_threadpool(dispute_store.summary, dispute_id) is None:
            return JSONResponse({"error": "unknown dispute"}, status_code=404)
        sub = await run_in_threadpool(submitter.store.get, dispute_id)
        if sub and sub["state"] in BLOCKING:
            return JSONResponse({"error": "already_submitted",
                                 "message": "This dispute's response has been sent (or its outcome is unknown); the draft can't change now."},
                                status_code=409)
        try:
            body = await request.json()
            text = body["text"] if isinstance(body, dict) and isinstance(body.get("text"), str) else None
        except ValueError:
            text = None
        if text is None:
            return JSONResponse({"error": "invalid body", "message": 'Send JSON like {"text": "..."}.'}, status_code=422)
        try:
            saved = await run_in_threadpool(draft_store.save, dispute_id, text)
        except DraftError as exc:
            body = {"error": exc.code, "message": exc.message}
            if exc.code == "missing_sandbox_note":
                body["note"] = SANDBOX_NOTE  # lets the page offer to add it back
            return JSONResponse(body, status_code=422)
        log.info("evidence draft saved dispute=%s chars=%d", dispute_id, len(saved["text"]))
        return JSONResponse({"draft": saved | {"edited": True}})

    @app.post(WEBHOOK_PATH)
    async def paypal_webhook(request: Request) -> JSONResponse:
        raw = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        # The verify call is blocking HTTP; keep it off the event loop.
        return await run_in_threadpool(_handle, headers, raw, verifier, store, dispute_store, bus)

    return app


def _refuse_write(request: Request) -> JSONResponse | None:
    """Writes accept JSON from this app's own pages only. A JSON body forces a CORS preflight for any
    cross-site request (which this app never approves), and a foreign Origin is refused outright."""
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return JSONResponse({"error": "unsupported media type", "message": "Send application/json."}, status_code=415)
    origin = request.headers.get("origin")
    if origin and httpx.URL(origin).netloc.decode() != request.headers.get("host", ""):
        return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
    return None


async def _parse_settings(request: Request) -> PolicySettings | JSONResponse:
    refused = _refuse_write(request)
    if refused:
        return refused
    try:
        return PolicySettings.model_validate(await request.json())
    except ValueError as exc:  # pydantic ValidationError is a ValueError; so is bad JSON
        problems = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]             if isinstance(exc, ValidationError) else ["body is not valid JSON"]
        return JSONResponse({"error": "invalid settings", "problems": problems}, status_code=422)


def _policy_body(settings: PolicySettings, updated_at: str | None) -> dict[str, Any]:
    return {"settings": settings.model_dump(), "updated_at": updated_at, "rules": build_rules(settings)}


def _with_fact_refs(decision: dict[str, Any] | None) -> dict[str, Any] | None:
    """Decisions stored before facts carried a graph `ref` get it back from their own graph snapshot.
    Facts are numbered deterministically from that snapshot; a ref is copied only when the fact ID
    and text both match, so a mismatch leaves the fact without a ref rather than mislabelled."""
    if not decision or not decision.get("facts") or not decision.get("graph_context"):
        return decision
    if all(f.get("ref") for f in decision["facts"]):
        return decision
    try:
        rebuilt = {f["id"]: f for f in context_to_facts(decision["graph_context"])}
    except (KeyError, TypeError):
        return decision
    facts = [f if f.get("ref") or rebuilt.get(f["id"], {}).get("text") != f.get("text")
             else f | {"ref": rebuilt[f["id"]]["ref"]} for f in decision["facts"]]
    return decision | {"facts": facts}


def _detect_from_webhook(event: dict[str, Any], dispute_store: DisputeStore, bus: EventBus) -> None:
    # Dispute webhooks (CUSTOMER.DISPUTE.CREATED / UPDATED / RESOLVED) carry the dispute as `resource`.
    # https://developer.paypal.com/api/rest/webhooks/event-names/#disputes
    # UNVERIFIED: that resource.dispute_id is present on every CUSTOMER.DISPUTE.* event.
    if not str(event.get("event_type", "")).startswith("CUSTOMER.DISPUTE."):
        return
    resource = event.get("resource") or {}
    dispute_id = resource.get("dispute_id") if isinstance(resource, dict) else None
    if not dispute_id:
        return
    try:
        # The resource is stored as a snapshot; the poller later replaces it with the full GET
        # response (full=False marks it as not yet fetched).
        if dispute_store.claim(dispute_id, "webhook", resource, full=False):
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
