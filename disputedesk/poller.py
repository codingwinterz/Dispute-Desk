"""Background poller: GET /v1/customer/disputes every POLL_SECONDS, read-only.

For each dispute ID not in the DisputeStore it fetches GET /v1/customer/disputes/{id}, saves the
JSON to logs/disputes/<id>.json and emits dispute_detected with source="poll".

For disputes already stored, it compares the list item's status, stage, update_time and action
links with the stored data. Only when one differs does it GET the dispute again, store the new
data and emit dispute_updated. Unchanged disputes cost no extra request.

Only DisputesClient.list_disputes and get_dispute are called; never a mutating endpoint.
Both build their URL from Settings.base_url, which only accepts the sandbox base URL.
"""

import asyncio
import json
import logging
from pathlib import Path

import httpx

from disputedesk.dispute_store import DisputeStore, safe_filename
from disputedesk.disputes import DisputesClient
from disputedesk.events import DisputeDetected, DisputeUpdated, EventBus

log = logging.getLogger("disputedesk.poller")

MAX_BACKOFF_SECONDS = 300.0


# Fields compared between a list item and the stored dispute. Observed in real sandbox responses to
# GET /v1/customer/disputes, whose items carry status, dispute_life_cycle_stage, update_time and links:
# https://developer.paypal.com/docs/api/customer-disputes/v1/#disputes_list
_STATE_FIELDS = ("status", "dispute_life_cycle_stage", "update_time")


def _link_set(d: dict) -> set[tuple[str, str]]:
    return {(str(link.get("rel")), str(link.get("method", "")).upper())
            for link in d.get("links") or [] if isinstance(link, dict)}


def changed_fields(item: dict, stored: dict) -> list[str]:
    """Fields where the list item differs from the stored dispute. A field missing from the list
    item is not compared, so a sparse list response never forces a refresh."""
    changed = [f for f in _STATE_FIELDS if item.get(f) is not None and item.get(f) != stored.get(f)]
    if "links" in item and _link_set(item) != _link_set(stored):
        changed.append("links")
    return changed


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


def _retry_after(resp: httpx.Response) -> float | None:
    # UNVERIFIED: whether PayPal sends Retry-After on 429. Honoured if present and numeric.
    # Rate limiting: https://developer.paypal.com/api/rest/reference/rate-limiting/
    try:
        return float(resp.headers["retry-after"])
    except (KeyError, ValueError):
        return None


class DisputePoller:
    def __init__(
        self,
        client: DisputesClient,
        store: DisputeStore,
        bus: EventBus,
        disputes_dir: Path,
        interval: float,
        max_backoff: float = MAX_BACKOFF_SECONDS,
    ):
        self._client = client
        self._store = store
        self._bus = bus
        self._disputes_dir = disputes_dir
        self._interval = interval
        self._max_backoff = max_backoff
        self._failures = 0

    def poll_once(self) -> float:
        """Run one poll. Never raises. Returns the seconds to wait before the next poll."""
        try:
            self._poll()
        except httpx.HTTPStatusError as exc:
            retry_after = _retry_after(exc.response) if exc.response.status_code == 429 else None
            return self._backoff(f"HTTP {exc.response.status_code}", retry_after)
        except httpx.HTTPError as exc:
            # Only the exception type is logged: messages can carry URLs, never headers, but keep it minimal.
            return self._backoff(type(exc).__name__)
        except Exception as exc:
            return self._backoff(f"unexpected {type(exc).__name__}")
        self._failures = 0
        return self._interval

    def _poll(self) -> None:
        # 401 is handled inside DisputesClient._request: token invalidated and the call retried once.
        # The client follows "next" pages itself.
        items = self._client.list_disputes()
        by_id: dict[str, dict] = {}
        for item in items:
            if isinstance(item, dict) and item.get("dispute_id"):
                by_id.setdefault(item["dispute_id"], item)
        ids = list(by_id)
        new_ids = [i for i in ids if not self._store.seen(i)]
        # Webhook-first disputes that still only hold the webhook snapshot.
        backfill = [i for i in ids if i not in new_ids and self._store.needs_full(i)]
        refresh: list[tuple[str, list[str]]] = []
        for i in ids:
            if i in new_ids or i in backfill:
                continue
            stored = self._store.get(i)
            fields = changed_fields(by_id[i], stored) if stored is not None else []
            if fields:
                refresh.append((i, fields))
        log.info("poll ok count=%d new=%d changed=%d", len(items), len(new_ids), len(refresh))
        for dispute_id in new_ids:
            self._process_new(dispute_id)
        for dispute_id in backfill:
            self._backfill(dispute_id)
        for dispute_id, fields in refresh:
            self._refresh(dispute_id, fields)

    def _fetch(self, dispute_id: str) -> dict | None:
        """Full dispute, or None for a non-retryable error. 429/5xx propagate so the loop backs off
        and the dispute is retried on the next poll (it is not claimed yet)."""
        try:
            return self._client.get_dispute(dispute_id)
        except httpx.HTTPStatusError as exc:
            if _retryable(exc.response.status_code):
                raise
            log.warning("could not fetch dispute=%s: HTTP %s; retrying next poll",
                        dispute_id, exc.response.status_code)
            return None

    def _process_new(self, dispute_id: str) -> None:
        dispute = self._fetch(dispute_id)
        if dispute is None:
            return
        self._save(dispute_id, dispute)
        if not self._store.claim(dispute_id, "poll", dispute, full=True):
            # Lost the race to the webhook path while fetching: keep the full data, emit nothing.
            self._store.update_full(dispute_id, dispute)
            log.info("dispute=%s already processed via %s; not emitted again",
                     dispute_id, self._store.source_of(dispute_id))
            return
        self._bus.emit(DisputeDetected(dispute_id, "poll"))

    def _backfill(self, dispute_id: str) -> None:
        dispute = self._fetch(dispute_id)
        if dispute is not None:
            self._save(dispute_id, dispute)
            self._store.update_full(dispute_id, dispute)
            log.info("dispute=%s full details stored (first seen via webhook)", dispute_id)
            self._bus.emit(DisputeUpdated(dispute_id, "poll"))

    def _refresh(self, dispute_id: str, fields: list[str]) -> None:
        dispute = self._fetch(dispute_id)
        if dispute is not None:
            self._save(dispute_id, dispute)
            self._store.update_full(dispute_id, dispute)
            log.info("dispute=%s refreshed (changed: %s) stage=%s status=%s", dispute_id, ", ".join(fields),
                     dispute.get("dispute_life_cycle_stage"), dispute.get("status"))
            self._bus.emit(DisputeUpdated(dispute_id, "poll"))

    def _save(self, dispute_id: str, dispute: dict) -> None:
        self._disputes_dir.mkdir(parents=True, exist_ok=True)
        path = self._disputes_dir / f"{safe_filename(dispute_id)}.json"
        path.write_text(json.dumps(dispute, indent=2), encoding="utf-8")

    def _backoff(self, reason: str, retry_after: float | None = None) -> float:
        self._failures += 1
        delay = min(self._interval * 2 ** self._failures, self._max_backoff)
        if retry_after is not None:
            delay = max(delay, min(retry_after, self._max_backoff))
        log.warning("poll failed (%s); backing off %.1fs (consecutive failures=%d)",
                    reason, delay, self._failures)
        return delay

    async def run(self, stop: asyncio.Event) -> None:
        log.info("poller started interval=%.1fs", self._interval)
        while not stop.is_set():
            try:
                delay = await asyncio.to_thread(self.poll_once)
            except Exception as exc:  # poll_once never raises; this guards the loop regardless
                log.error("poll loop error: %s", type(exc).__name__)
                delay = self._interval
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        log.info("poller stopped")
