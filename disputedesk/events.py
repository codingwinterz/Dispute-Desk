"""In-process events.

dispute_detected: emitted once per new dispute.
dispute_updated: emitted when the poller stores newer data for a dispute it already had
(full details backfilled after a webhook, or a changed status/stage/links).
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("disputedesk.events")


@dataclass(frozen=True)
class DisputeDetected:
    dispute_id: str
    source: str  # "poll" | "webhook"
    name: str = "dispute_detected"


@dataclass(frozen=True)
class DisputeUpdated:
    dispute_id: str
    source: str  # "poll" or "submit"
    name: str = "dispute_updated"


Event = DisputeDetected | DisputeUpdated
Subscriber = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[Subscriber] = []

    def subscribe(self, fn: Subscriber) -> None:
        with self._lock:
            self._subscribers.append(fn)

    def emit(self, event: Event) -> None:
        log.info("%s dispute=%s source=%s", event.name, event.dispute_id, event.source)
        with self._lock:
            subscribers = list(self._subscribers)
        for fn in subscribers:
            try:
                fn(event)
            except Exception as exc:  # a broken subscriber must not break the poller or webhook
                log.error("subscriber %s failed: %s", getattr(fn, "__name__", fn), type(exc).__name__)
