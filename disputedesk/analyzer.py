"""Runs the decision step automatically, off the poller and webhook threads.

Event subscribers only enqueue a dispute ID; one worker thread does the graph and LLM work, so a
slow LLM call never delays a poll or a webhook response. A dispute is analyzed once it has full
details stored, and only until it has an "ok" decision (an existing decision is not re-run).
"""

import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

from disputedesk.decision import DecisionResult, decide
from disputedesk.decision_store import DecisionStore
from disputedesk.dispute_store import DisputeStore
from disputedesk.events import Event
from disputedesk.graph import GraphStore, context_to_facts
from disputedesk.policies import DEFAULT_RULES

log = logging.getLogger("disputedesk.analyzer")

_STOP = object()


class Analyzer:
    def __init__(
        self,
        disputes: DisputeStore,
        decisions: DecisionStore,
        graph_factory: Callable[[], GraphStore],
        model: str,
        decide_fn: Callable[..., DecisionResult] = decide,
        rules: list[dict[str, str]] | None = None,
    ):
        self._disputes = disputes
        self._decisions = decisions
        self._graph_factory = graph_factory
        self._graph: GraphStore | None = None
        self._model = model
        self._decide = decide_fn
        self._rules = rules if rules is not None else DEFAULT_RULES
        self._queue: queue.Queue[Any] = queue.Queue()
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # --- event side (cheap, never blocks) --------------------------------------

    def on_event(self, event: Event) -> None:
        self.enqueue(event.dispute_id)

    def enqueue(self, dispute_id: str) -> None:
        with self._lock:
            if dispute_id in self._pending:
                return
            self._pending.add(dispute_id)
        self._queue.put(dispute_id)

    def sweep(self) -> int:
        """Queue every stored, fully fetched dispute that has no ok decision yet."""
        ids = [i for i in self._disputes.full_disputes() if not self._decisions.has_ok(i)]
        for dispute_id in ids:
            self.enqueue(dispute_id)
        log.info("analyzer sweep queued=%d", len(ids))
        return len(ids)

    # --- worker -----------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="disputedesk-analyzer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread:
            self._queue.put(_STOP)
            self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            with self._lock:
                self._pending.discard(item)
            try:
                self.analyze(item)
            except Exception as exc:  # analyze() handles its own errors; this guards the thread
                log.error("analyzer error dispute=%s: %s", item, type(exc).__name__)

    def graph(self) -> GraphStore:
        if self._graph is None:
            self._graph = self._graph_factory()
        return self._graph

    def context(self, dispute_id: str) -> tuple[str, dict[str, Any] | None]:
        """Upsert the stored dispute into the graph and return (status, context)."""
        data = self._disputes.get(dispute_id)
        if data is None:
            return "missing", None
        if self._disputes.needs_full(dispute_id):
            return "waiting_for_full_details", None
        try:
            graph = self.graph()
            graph.upsert_paypal_dispute(data)  # also refreshes status/stage on later updates
            ctx = graph.dispute_context(dispute_id)
        except Exception as exc:
            self._graph = None  # reconnect next time
            log.warning("graph unavailable for dispute=%s: %s", dispute_id, type(exc).__name__)
            return f"graph_unavailable:{type(exc).__name__}", None
        return ("ok", ctx) if ctx else ("no_graph_context", None)

    def analyze(self, dispute_id: str, force: bool = False) -> str:
        status, ctx = self.context(dispute_id)
        if status in ("missing", "waiting_for_full_details"):
            log.info("analyzer dispute=%s: %s", dispute_id, status)
            return status
        if not force and self._decisions.has_ok(dispute_id):
            return "already_decided"
        if ctx is None:
            self._decisions.save(dispute_id, DecisionResult("error", self._model, 0, error=status), None)
            return status
        facts = context_to_facts(ctx)
        result = self._decide(dispute_id, facts, self._rules, self._model)
        self._decisions.save(dispute_id, result, ctx)
        return result.status if result.status == "ok" else (result.error or "error")
