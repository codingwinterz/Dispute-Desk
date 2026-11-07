"""Merchant policy settings and the rules (P1..P4) the decision step may cite.

The merchant edits settings on /policies; they are stored in SQLite (same file as the disputes).
build_rules() turns settings into the exact rule text the agent reads. Rule IDs are stable, so a
citation like "P2" always means the same kind of rule; a disabled rule is simply not supplied, and
citing it then fails validation like any unknown ID.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PolicySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # P1: delivery confirmation answers an "item not received" claim.
    defend_delivered_not_received: bool = True
    # P2: below this amount, contesting costs more than the dispute is worth.
    accept_under_enabled: bool = True
    accept_under_amount: float = Field(default=15.00, ge=0, le=100_000)
    # P3: a device hash or IP shared with this many other disputes.
    defend_linked_enabled: bool = True
    defend_linked_min: int = Field(default=2, ge=1, le=20)
    # P4: no carrier scan after the label means no proof of shipment.
    accept_no_scan_not_received: bool = True


DEFAULT_SETTINGS = PolicySettings()


def build_rules(s: PolicySettings) -> list[dict[str, str]]:
    rules: list[dict[str, str]] = []
    if s.defend_delivered_not_received:
        rules.append({"id": "P1", "text": "For MERCHANDISE_OR_SERVICE_NOT_RECEIVED: DEFEND when the carrier shows the "
                                          "parcel DELIVERED to the shipping destination, strongest with a buyer signature."})
    if s.accept_under_enabled:
        rules.append({"id": "P2", "text": f"Recommend ACCEPT when the disputed amount is under {s.accept_under_amount:.2f} "
                                          "USD and no other dispute shares the checkout device or IP: contesting costs "
                                          "more than the amount."})
    if s.defend_linked_enabled:
        others = "one other dispute" if s.defend_linked_min == 1 else f"{s.defend_linked_min} or more other disputes"
        text = (f"DEFEND when the checkout device hash or IP is shared with {others}: a repeat pattern across "
                "disputes justifies contesting. A pattern is not proof.")
        if s.accept_under_enabled:
            text += " P3 takes precedence over P2."
        rules.append({"id": "P3", "text": text})
    if s.accept_no_scan_not_received:
        rules.append({"id": "P4", "text": "For MERCHANDISE_OR_SERVICE_NOT_RECEIVED: ACCEPT when there is no carrier scan "
                                          "after the label was created, because there is no proof of shipment or delivery."})
    return rules


DEFAULT_RULES = build_rules(DEFAULT_SETTINGS)


class PolicyStore:
    """One row of merchant settings. Missing row = defaults."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS policy_settings ("
                " id INTEGER PRIMARY KEY CHECK (id = 1),"
                " data TEXT NOT NULL,"
                " updated_at TEXT NOT NULL)"
            )

    def get(self) -> tuple[PolicySettings, str | None]:
        with self._lock:
            row = self._db.execute("SELECT data, updated_at FROM policy_settings WHERE id = 1").fetchone()
        if row is None:
            return DEFAULT_SETTINGS, None
        # Fields added in later versions fall back to their defaults.
        stored: dict[str, Any] = json.loads(row[0])
        known = {k: v for k, v in stored.items() if k in PolicySettings.model_fields}
        return PolicySettings.model_validate(known), row[1]

    def save(self, settings: PolicySettings) -> str:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO policy_settings (id, data, updated_at) VALUES (1, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                (settings.model_dump_json(), now),
            )
        return now

    def rules(self) -> list[dict[str, str]]:
        return build_rules(self.get()[0])

    def close(self) -> None:
        with self._lock:
            self._db.close()
