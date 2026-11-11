"""P2 Autopilot: opt-in auto-accept for small, unlinked P2 claims with a 30-second cancel window.

Safety invariants:
  - Off by default (opt-in stored in SQLite, never enabled automatically).
  - Eligible ONLY when policy P2 is enabled, the stored decision is ok/ACCEPT citing P2, the
    disputed amount is strictly under $15.00 USD (and under the P2 threshold), there are no linked
    disputes in graph_context, and there are no external links or documents in the buyer's claim.
  - Every eligible case enters a 30-second 'pending' cancel window before POST /api/submissions/{id}
    can execute it; the merchant may cancel at any time before execute_at.
  - Enforces a SQLite-backed daily cap of 5 auto-accept executions per UTC day.
  - Writes clear entries to autopilot_activity for the Activity log and case timeline.
  - Never imports or calls PayPal write APIs directly; execution still goes through Submitter.submit.
"""

import re
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from disputedesk.attachments import _is_buyer, buyer_documents
from disputedesk.disputes import ACCEPT_CLAIM, ACCEPT_NOTE, DisputesClient
from disputedesk.policies import PolicySettings
from disputedesk.submissions import BLOCKING, WAITING

DAILY_AUTO_ACCEPT_LIMIT = 5
CANCEL_WINDOW_SECONDS = 30
MAX_P2_AMOUNT_USD = 15.0

_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _strings_in(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, list):
        out: list[str] = []
        for item in obj:
            out.extend(_strings_in(item))
        return out
    if isinstance(obj, dict):
        out = []
        for v in obj.values():
            out.extend(_strings_in(v))
        return out
    return []


def has_buyer_external_links(dispute_data: dict[str, Any]) -> bool:
    """True when the buyer's claim contains buyer documents/attachments or URLs in buyer text."""
    if buyer_documents(dispute_data):
        return True
    for msg in dispute_data.get("messages") or []:
        if isinstance(msg, dict) and _is_buyer(msg.get("posted_by")):
            if any(_URL_RE.search(s) for s in _strings_in(msg)):
                return True
    for ev in dispute_data.get("evidences") or []:
        if isinstance(ev, dict) and _is_buyer(ev.get("source")):
            if any(_URL_RE.search(s) for s in _strings_in(ev)):
                return True
    for key in ("buyer_note", "reason_note", "note"):
        val = dispute_data.get(key)
        if isinstance(val, str) and _URL_RE.search(val):
            return True
    return False


def _amount_usd(dispute_data: dict[str, Any]) -> tuple[float | None, str | None]:
    money = dispute_data.get("dispute_amount") or {}
    currency = money.get("currency_code")
    try:
        value = float(money["value"])
    except (KeyError, TypeError, ValueError):
        return None, currency
    return value, currency


def p2_eligibility(
    dispute_data: dict[str, Any] | None,
    decision: dict[str, Any] | None,
    policy: PolicySettings,
    submission: dict[str, Any] | None,
) -> tuple[bool, str | None]:
    """Return (eligible, reason_if_not) for P2 Autopilot."""
    if not policy.accept_under_enabled:
        return False, "p2_disabled"
    if not dispute_data or not decision:
        return False, "missing_decision"
    if decision.get("status") != "ok" or decision.get("decision") != "ACCEPT":
        return False, "not_accept_decision"
    cited = {c for p in (decision.get("reasoning") or []) for c in (p.get("cites") or [])}
    if "P2" not in cited or "P3" in cited:
        return False, "not_p2_decision"
    amount, currency = _amount_usd(dispute_data)
    if currency != "USD" or amount is None or amount < 0:
        return False, "invalid_amount"
    if amount >= MAX_P2_AMOUNT_USD or amount >= policy.accept_under_amount:
        return False, "amount_not_under_limit"
    ctx = decision.get("graph_context") or {}
    if ctx.get("linked_disputes"):
        return False, "linked_disputes_present"
    if has_buyer_external_links(dispute_data):
        return False, "external_link_in_claim"
    if dispute_data.get("status") != WAITING or not DisputesClient.has_link(dispute_data, ACCEPT_CLAIM):
        return False, "not_submittable"
    if submission and submission.get("state") in BLOCKING:
        return False, "already_submitted"
    return True, None


class AutopilotStore:
    """SQLite-backed P2 Autopilot settings, 30s cancel window jobs, daily cap, and activity log."""

    def __init__(
        self,
        path: Path,
        now_fn: Callable[[], datetime] = _utc_now,
        window_seconds: int = CANCEL_WINDOW_SECONDS,
        daily_limit: int = DAILY_AUTO_ACCEPT_LIMIT,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._now_fn = now_fn
        self._window_seconds = window_seconds
        self._daily_limit = daily_limit
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS autopilot_settings ("
                " id INTEGER PRIMARY KEY CHECK (id = 1),"
                " enabled INTEGER NOT NULL DEFAULT 0,"
                " updated_at TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS autopilot_jobs ("
                " dispute_id TEXT PRIMARY KEY,"
                " state TEXT NOT NULL CHECK (state IN ('pending', 'cancelled', 'executed', 'failed', 'capped')),"
                " armed_at TEXT NOT NULL,"
                " execute_at TEXT NOT NULL,"
                " cancelled_at TEXT,"
                " executed_at TEXT,"
                " amount_value REAL,"
                " currency TEXT,"
                " error TEXT)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS autopilot_activity ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " dispute_id TEXT NOT NULL,"
                " kind TEXT NOT NULL,"
                " message TEXT NOT NULL,"
                " created_at TEXT NOT NULL)"
            )

    def _now(self) -> datetime:
        return self._now_fn()

    def _log(self, dispute_id: str, kind: str, message: str, when: datetime) -> None:
        self._db.execute(
            "INSERT INTO autopilot_activity (dispute_id, kind, message, created_at) VALUES (?, ?, ?, ?)",
            (dispute_id, kind, message, when.isoformat()),
        )

    def _get_settings(self) -> tuple[bool, str | None]:
        row = self._db.execute("SELECT enabled, updated_at FROM autopilot_settings WHERE id = 1").fetchone()
        if row is None:
            return False, None
        return bool(row[0]), row[1]

    def _daily_used(self, day_utc: str) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) FROM autopilot_jobs WHERE state = 'executed' AND substr(executed_at, 1, 10) = ?",
            (day_utc,),
        ).fetchone()
        return int(row[0]) if row else 0

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            enabled, updated_at = self._get_settings()
        return {
            "enabled": enabled,
            "updated_at": updated_at,
            "daily_limit": self._daily_limit,
            "window_seconds": self._window_seconds,
        }

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        now = self._now()
        now_iso = now.isoformat()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO autopilot_settings (id, enabled, updated_at) VALUES (1, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
                (int(bool(enabled)), now_iso),
            )
            if not enabled:
                pending = self._db.execute(
                    "SELECT dispute_id FROM autopilot_jobs WHERE state = 'pending'"
                ).fetchall()
                for (dispute_id,) in pending:
                    self._db.execute(
                        "DELETE FROM autopilot_jobs WHERE dispute_id = ? AND state = 'pending'",
                        (dispute_id,),
                    )
                    self._log(dispute_id, "autopilot_cancelled",
                              "Autopilot disabled before auto-accept ran.", now)
        return self.state()

    def sync_eligible(
        self,
        disputes: dict[str, dict[str, Any]],
        decisions: dict[str, dict[str, Any] | None],
        policy: PolicySettings,
        submissions: dict[str, dict[str, Any] | None],
    ) -> dict[str, Any]:
        """Arm eligible P2 cases with a 30-second cancel window when Autopilot is enabled."""
        now = self._now()
        day_utc = now.date().isoformat()
        with self._lock, self._db:
            enabled, _ = self._get_settings()
            pending_rows = self._db.execute(
                "SELECT dispute_id FROM autopilot_jobs WHERE state = 'pending'"
            ).fetchall()
            for (did,) in pending_rows:
                ok, reason = p2_eligibility(disputes.get(did), decisions.get(did), policy, submissions.get(did))
                if not enabled or not ok:
                    self._db.execute("DELETE FROM autopilot_jobs WHERE dispute_id = ? AND state = 'pending'", (did,))
                    if not enabled:
                        self._log(did, "autopilot_cancelled", "Autopilot disabled before auto-accept ran.", now)
                    elif reason == "already_submitted":
                        self._log(did, "autopilot_cancelled",
                                  "Autopilot stood down because the dispute was already submitted.", now)

            if enabled:
                used = self._daily_used(day_utc)
                existing = {r[0]: r[1] for r in self._db.execute("SELECT dispute_id, state FROM autopilot_jobs").fetchall()}
                pending_count = sum(1 for s in existing.values() if s == "pending")
                for did in sorted(disputes):
                    if did in existing:
                        continue
                    ok, _ = p2_eligibility(disputes[did], decisions.get(did), policy, submissions.get(did))
                    if not ok:
                        continue
                    amount, currency = _amount_usd(disputes[did])
                    if used + pending_count >= self._daily_limit:
                        self._db.execute(
                            "INSERT INTO autopilot_jobs (dispute_id, state, armed_at, execute_at, amount_value, currency)"
                            " VALUES (?, 'capped', ?, ?, ?, ?)",
                            (did, now.isoformat(), now.isoformat(), amount, currency),
                        )
                        self._log(
                            did,
                            "autopilot_capped",
                            f"Autopilot skipped P2 auto-accept: daily limit of {self._daily_limit} reached.",
                            now,
                        )
                        continue
                    execute_at = now + timedelta(seconds=self._window_seconds)
                    self._db.execute(
                        "INSERT INTO autopilot_jobs (dispute_id, state, armed_at, execute_at, amount_value, currency)"
                        " VALUES (?, 'pending', ?, ?, ?, ?)",
                        (did, now.isoformat(), execute_at.isoformat(), amount, currency),
                    )
                    self._log(
                        did,
                        "autopilot_armed",
                        f"P2 Autopilot armed (${amount:.2f} {currency}): {self._window_seconds}s cancel window started.",
                        now,
                    )
                    pending_count += 1
        return self.state()

    def cancel(self, dispute_id: str) -> dict[str, Any] | None:
        """Abort a pending 30-second auto-accept window."""
        now = self._now()
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT state FROM autopilot_jobs WHERE dispute_id = ?", (dispute_id,)
            ).fetchone()
            if row is None or row[0] != "pending":
                return None
            self._db.execute(
                "UPDATE autopilot_jobs SET state = 'cancelled', cancelled_at = ? WHERE dispute_id = ?",
                (now.isoformat(), dispute_id),
            )
            self._log(
                dispute_id,
                "autopilot_cancelled",
                f"Merchant cancelled P2 Autopilot during the {self._window_seconds}-second window.",
                now,
            )
        return self.job(dispute_id)

    def check_executable(
        self,
        dispute_id: str,
        dispute_data: dict[str, Any] | None,
        decision: dict[str, Any] | None,
        policy: PolicySettings,
        submission: dict[str, Any] | None,
    ) -> tuple[bool, int, str, str]:
        """Verify all Autopilot safety invariants before Submitter.submit runs."""
        now = self._now()
        day_utc = now.date().isoformat()
        with self._lock:
            enabled, _ = self._get_settings()
            if not enabled:
                return False, 409, "autopilot_disabled", "P2 Autopilot is turned off."
            row = self._db.execute(
                "SELECT state, execute_at FROM autopilot_jobs WHERE dispute_id = ?", (dispute_id,)
            ).fetchone()
            if row is None:
                return False, 409, "not_armed", "This dispute is not armed for P2 Autopilot."
            state, execute_at_iso = row
            if state == "cancelled":
                return False, 409, "autopilot_cancelled", "Autopilot was cancelled for this dispute."
            if state != "pending":
                return False, 409, "autopilot_not_pending", f"Autopilot job is {state}, not pending."
            execute_at = datetime.fromisoformat(execute_at_iso)
            if now < execute_at:
                remaining = max(1, int((execute_at - now).total_seconds() + 0.999))
                return False, 409, "countdown_active", f"Cancel window active ({remaining}s remaining)."
            used = self._daily_used(day_utc)
            if used >= self._daily_limit:
                return False, 429, "daily_cap_reached", f"Daily Autopilot cap ({self._daily_limit}/day) reached."
        ok, reason = p2_eligibility(dispute_data, decision, policy, submission)
        if not ok:
            return False, 409, f"not_p2_eligible:{reason}", "Dispute no longer qualifies for P2 Autopilot."
        return True, 200, "ok", "ok"

    def mark_executed(self, dispute_id: str, amount_value: float | None, currency: str | None) -> None:
        now = self._now()
        day_utc = now.date().isoformat()
        with self._lock, self._db:
            self._db.execute(
                "UPDATE autopilot_jobs SET state = 'executed', executed_at = ?, error = NULL WHERE dispute_id = ?",
                (now.isoformat(), dispute_id),
            )
            used = self._daily_used(day_utc)
            amt_text = f"${amount_value:.2f} {currency}" if amount_value is not None and currency else "P2 claim"
            self._log(
                dispute_id,
                "autopilot_executed",
                f"Autopilot auto-accepted {amt_text} ({used}/{self._daily_limit} today).",
                now,
            )

    def mark_failed(self, dispute_id: str, error_code: str) -> None:
        now = self._now()
        with self._lock, self._db:
            self._db.execute(
                "UPDATE autopilot_jobs SET state = 'failed', error = ? WHERE dispute_id = ?",
                (error_code, dispute_id),
            )
            self._log(
                dispute_id,
                "autopilot_failed",
                f"Autopilot auto-accept failed ({error_code}).",
                now,
            )

    def job(self, dispute_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT dispute_id, state, armed_at, execute_at, cancelled_at, executed_at, amount_value, currency, error"
                " FROM autopilot_jobs WHERE dispute_id = ?",
                (dispute_id,),
            ).fetchone()
        return self._format_job(row, self._now()) if row else None

    def _format_job(self, row: tuple[Any, ...], now: datetime) -> dict[str, Any]:
        cols = ("dispute_id", "state", "armed_at", "execute_at", "cancelled_at",
                "executed_at", "amount_value", "currency", "error")
        item = dict(zip(cols, row))
        exec_at = datetime.fromisoformat(item["execute_at"])
        remaining = max(0.0, (exec_at - now).total_seconds()) if item["state"] == "pending" else 0.0
        item["remaining_seconds"] = round(remaining, 2)
        item["window_seconds"] = self._window_seconds
        return item

    def activity_for(self, dispute_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, dispute_id, kind, message, created_at FROM autopilot_activity"
                " WHERE dispute_id = ? ORDER BY id ASC",
                (dispute_id,),
            ).fetchall()
        return [
            {"id": r[0], "dispute_id": r[1], "kind": r[2], "message": r[3], "created_at": r[4]}
            for r in rows
        ]

    def all_activity(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, dispute_id, kind, message, created_at FROM autopilot_activity"
                " ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"id": r[0], "dispute_id": r[1], "kind": r[2], "message": r[3], "created_at": r[4]}
            for r in rows
        ]

    def state(self) -> dict[str, Any]:
        now = self._now()
        day_utc = now.date().isoformat()
        with self._lock:
            enabled, updated_at = self._get_settings()
            used = self._daily_used(day_utc)
            rows = self._db.execute(
                "SELECT dispute_id, state, armed_at, execute_at, cancelled_at, executed_at, amount_value, currency, error"
                " FROM autopilot_jobs ORDER BY armed_at ASC"
            ).fetchall()
        jobs = [self._format_job(r, now) for r in rows]
        pending = [j for j in jobs if j["state"] == "pending"]
        return {
            "enabled": enabled,
            "updated_at": updated_at,
            "daily_used": used,
            "daily_limit": self._daily_limit,
            "daily_remaining": max(0, self._daily_limit - used),
            "window_seconds": self._window_seconds,
            "accept_note": ACCEPT_NOTE,
            "pending": pending,
            "jobs": {j["dispute_id"]: j for j in jobs},
        }

    def close(self) -> None:
        with self._lock:
            self._db.close()
