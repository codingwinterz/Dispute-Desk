"""Autopilot v2, preview only: a server-side runner that shows what it would auto-accept and never sends.

Scope of this build: shadow mode only. The runner ends every job with the existing dry-run builder
(injected as `dry_run`, the same function behind GET /api/submissions/{id}/dry-run), which renders the
request an approval would send and sends nothing. The execution-time status re-check uses an injected
read-only fetch (`fetch_fresh`, wired to a GET in webhooks.py). This module imports no PayPal client,
no HTTP library and nothing from the submissions module; tests/test_autopilot.py fails if it ever does.

Safety invariants:
  - Off by default. Settings live in autopilot_v2_settings. Rows left by Autopilot v1 (tables
    autopilot_settings and autopilot_jobs) are retired on first start: every v1 job becomes a stale v2
    job with an audit entry and the v1 enabled flag is forced to 0. The v1 enabled value is never read.
  - Eligibility, each with a reason code: P2 enabled; stored decision ok/ACCEPT citing P2 and not P3;
    CHARGEBACK stage (inquiry_stage otherwise); USD amount under $15 and under the policy threshold; no
    linked disputes; no buyer documents or URLs; status WAITING_FOR_SELLER_RESPONSE with an
    accept_claim link; no blocking submission.
  - Arming happens only from the analyzer (a new ok decision) and the poller (dispute_updated), never
    from a GET; the merchant's policy save cancels every armed job.
  - State machine in SQLite: one BEGIN IMMEDIATE per transition, every UPDATE guarded by the previous
    state and checked by rowcount, the daily cap taken in the same transaction as the claim.
  - Every transition writes exactly one audit row.
  - Any exception in the runner trips the breaker (enabled = 0, armed jobs cancelled); the kill switch
    on every page does the same on demand.
  - Restart: every armed or executing row is marked stale when the store opens, so nothing armed by an
    earlier process ever fires. The next decision or poller update may arm it again with a full window.
"""

import logging
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from disputedesk.attachments import _is_buyer, buyer_documents
from disputedesk.policies import PolicySettings

log = logging.getLogger("disputedesk.autopilot")

MODE = "shadow"
DAILY_LIMIT = 5
WINDOW_SECONDS = 30
MAX_AMOUNT_USD = 15.0
CHARGEBACK = "CHARGEBACK"
WAITING = "WAITING_FOR_SELLER_RESPONSE"
ACCEPT_LINK = "accept_claim"
# Mirrors submissions.BLOCKING (asserted equal in tests) without importing the write module.
BLOCKING_SUBMISSION_STATES = ("sending", "succeeded", "unknown")
UNAVAILABLE_MESSAGE = "Autopilot preview is not available in demo or scratch mode."

STATES = ("armed", "executing", "shadow_done", "failed", "cancelled", "stale", "capped")
GLOBAL = "-"  # dispute_id of audit rows about the setting itself

_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_boot_id() -> str:
    return secrets.token_hex(6)


# --- eligibility -------------------------------------------------------------------------


def _strings_in(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, list):
        return [s for item in obj for s in _strings_in(item)]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings_in(v)]
    return []


def has_buyer_external_links(dispute: dict[str, Any]) -> bool:
    """True when the buyer's claim carries documents, or a URL in any buyer text."""
    if buyer_documents(dispute):
        return True
    for msg in dispute.get("messages") or []:
        if isinstance(msg, dict) and _is_buyer(msg.get("posted_by")) and any(_URL_RE.search(s) for s in _strings_in(msg)):
            return True
    for ev in dispute.get("evidences") or []:
        if isinstance(ev, dict) and _is_buyer(ev.get("source")) and any(_URL_RE.search(s) for s in _strings_in(ev)):
            return True
    return any(isinstance(dispute.get(k), str) and _URL_RE.search(dispute[k]) for k in ("buyer_note", "reason_note", "note"))


def amount_usd(dispute: Any) -> tuple[float | None, str | None]:
    """(value, currency) from dispute_amount; (None, None) for any malformed shape. Never raises."""
    money = dispute.get("dispute_amount") if isinstance(dispute, dict) else None
    if not isinstance(money, dict):
        return None, None
    currency = money.get("currency_code") if isinstance(money.get("currency_code"), str) else None
    try:
        return float(money["value"]), currency
    except (KeyError, TypeError, ValueError):
        return None, currency


def _malformed(dispute: dict[str, Any], decision: dict[str, Any]) -> bool:
    """True when a field eligibility reads has the wrong type. Missing (None) fields are judged by the rules."""
    reasoning = decision.get("reasoning")
    if reasoning is not None:
        if not isinstance(reasoning, list):
            return True
        for point in reasoning:
            if not isinstance(point, dict):
                return True
            cites = point.get("cites")
            if cites is not None and (not isinstance(cites, list) or not all(isinstance(c, str) for c in cites)):
                return True
    for key in ("status", "decision"):
        if decision.get(key) is not None and not isinstance(decision.get(key), str):
            return True
    if dispute.get("dispute_amount") is not None and not isinstance(dispute.get("dispute_amount"), dict):
        return True
    for key in ("status", "dispute_life_cycle_stage"):
        if dispute.get(key) is not None and not isinstance(dispute.get(key), str):
            return True
    for key in ("links", "messages", "evidences"):
        if dispute.get(key) is not None and not isinstance(dispute.get(key), list):
            return True
    # Buyer messages and evidence entries, and their documents, must be objects: a document list that
    # isn't one could hide an attachment from the buyer-documents check.
    for key in ("messages", "evidences"):
        for item in dispute.get(key) or []:
            if not isinstance(item, dict):
                return True
            docs = item.get("documents")
            if docs is not None and (not isinstance(docs, list) or not all(isinstance(doc, dict) for doc in docs)):
                return True
    return False


def _has_accept_link(dispute: dict[str, Any]) -> bool:
    return any(isinstance(link, dict) and link.get("rel") == ACCEPT_LINK for link in dispute.get("links") or [])


def p2_eligibility(
    dispute: dict[str, Any] | None,
    decision: dict[str, Any] | None,
    policy: PolicySettings,
    submission: dict[str, Any] | None,
) -> tuple[bool, str | None]:
    """(eligible, reason_code_if_not). Every refusal names why, for the audit trail and the case page.
    Never raises: a wrongly typed field is "malformed_record", and so is anything unexpected below."""
    try:
        return _p2_eligibility(dispute, decision, policy, submission)
    except Exception:  # fail closed on any shape the explicit checks did not foresee
        return False, "malformed_record"


def _p2_eligibility(dispute: Any, decision: Any, policy: PolicySettings, submission: Any) -> tuple[bool, str | None]:
    if not policy.accept_under_enabled:
        return False, "p2_disabled"
    if not dispute or not decision:
        return False, "missing_decision"
    if not isinstance(dispute, dict) or not isinstance(decision, dict) or _malformed(dispute, decision):
        return False, "malformed_record"
    if submission is not None and not isinstance(submission, dict):
        return False, "malformed_record"
    if decision.get("status") != "ok" or decision.get("decision") != "ACCEPT":
        return False, "not_accept_decision"
    cited = {c for p in (decision.get("reasoning") or []) for c in (p.get("cites") or [])}
    if "P2" not in cited or "P3" in cited:
        return False, "not_p2_decision"
    if dispute.get("dispute_life_cycle_stage") != CHARGEBACK:
        return False, "inquiry_stage"
    amount, currency = amount_usd(dispute)
    if currency != "USD" or amount is None or amount < 0:
        return False, "invalid_amount"
    if amount >= MAX_AMOUNT_USD or amount >= policy.accept_under_amount:
        return False, "amount_not_under_limit"
    # Fail closed: without a graph context that lists linked disputes, "none linked" can't be shown.
    ctx = decision.get("graph_context")
    linked = ctx.get("linked_disputes") if isinstance(ctx, dict) else None
    if not isinstance(linked, list):
        return False, "linked_disputes_unknown"
    if linked:
        return False, "linked_disputes_present"
    if has_buyer_external_links(dispute):
        return False, "external_link_in_claim"
    if dispute.get("status") != WAITING or not _has_accept_link(dispute):
        return False, "not_submittable"
    if submission and submission.get("state") in BLOCKING_SUBMISSION_STATES:
        return False, "already_submitted"
    return True, None


# --- store: the state machine ------------------------------------------------------------


class _Rollback(Exception):
    """Raised inside a transaction to roll it back without surfacing an error."""


class AutopilotStore:
    """SQLite state machine for preview jobs, the daily cap and the audit log.

    The connection runs in autocommit (isolation_level=None) so every transition is an explicit
    BEGIN IMMEDIATE ... COMMIT under the store lock. Other stores share the file on their own
    connections; BEGIN IMMEDIATE takes the write lock up front, so a transition never half-applies."""

    def __init__(
        self,
        path: Path,
        boot_id: str | None = None,
        now_fn: Callable[[], datetime] = _utc_now,
        window_seconds: int = WINDOW_SECONDS,
        daily_limit: int = DAILY_LIMIT,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.boot_id = boot_id or new_boot_id()
        self._now_fn = now_fn
        self.window_seconds = window_seconds
        self.daily_limit = daily_limit
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10, isolation_level=None)
        self._create_tables()
        self._retire_v1()
        self._recover()

    # -- setup ------------------------------------------------------------------------

    def _create_tables(self) -> None:
        with self._tx():
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS autopilot_v2_settings ("
                " id INTEGER PRIMARY KEY CHECK (id = 1),"
                " enabled INTEGER NOT NULL DEFAULT 0,"
                " mode TEXT NOT NULL DEFAULT 'shadow',"
                " updated_at TEXT NOT NULL,"
                " tripped_at TEXT, trip_reason TEXT,"
                " v1_retired_at TEXT, last_boot_id TEXT)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS autopilot_v2_jobs ("
                " dispute_id TEXT PRIMARY KEY,"
                f" state TEXT NOT NULL CHECK (state IN ({', '.join(repr(s) for s in STATES)})),"
                " mode TEXT NOT NULL DEFAULT 'shadow',"
                " armed_at TEXT NOT NULL, execute_at TEXT NOT NULL,"
                " claimed_at TEXT, finished_at TEXT, boot_id TEXT,"
                " amount_value REAL, currency TEXT,"
                " decision_created_at TEXT, policy_updated_at TEXT, reason TEXT)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS autopilot_v2_daily (day TEXT PRIMARY KEY, used INTEGER NOT NULL DEFAULT 0)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS autopilot_v2_audit ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " dispute_id TEXT NOT NULL, kind TEXT NOT NULL,"
                " state_from TEXT, state_to TEXT,"
                " actor TEXT NOT NULL, mode TEXT NOT NULL, boot_id TEXT,"
                " amount_value REAL, currency TEXT,"
                " decision_created_at TEXT, policy_updated_at TEXT,"
                " reason TEXT, message TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            self._db.execute(
                "INSERT OR IGNORE INTO autopilot_v2_settings (id, enabled, updated_at) VALUES (1, 0, ?)",
                (self._now().isoformat(),),
            )
            self._db.execute("UPDATE autopilot_v2_settings SET last_boot_id = ? WHERE id = 1", (self.boot_id,))

    def _retire_v1(self) -> None:
        """First start after v1: every v1 job becomes a stale v2 job (audited) and the v1 flag is forced off."""
        now = self._now()
        with self._tx():
            if self._db.execute("SELECT v1_retired_at FROM autopilot_v2_settings WHERE id = 1").fetchone()[0]:
                return
            tables = {r[0] for r in self._db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "autopilot_jobs" in tables:
                for did, old_state in self._db.execute("SELECT dispute_id, state FROM autopilot_jobs").fetchall():
                    self._db.execute(
                        "INSERT OR IGNORE INTO autopilot_v2_jobs (dispute_id, state, armed_at, execute_at, finished_at, reason)"
                        " VALUES (?, 'stale', ?, ?, ?, 'v1_retired')",
                        (did, now.isoformat(), now.isoformat(), now.isoformat()),
                    )
                    self._audit(did, "v1_retired", old_state, "stale", "startup", now, reason="v1_retired",
                                message="Retired an Autopilot v1 job as stale. Nothing was sent.")
            if "autopilot_settings" in tables:
                self._db.execute("UPDATE autopilot_settings SET enabled = 0")  # never read, only forced off
            self._db.execute("UPDATE autopilot_v2_settings SET v1_retired_at = ? WHERE id = 1", (now.isoformat(),))

    def _recover(self) -> None:
        """A new process: nothing armed by an earlier one may fire. Armed and executing rows become stale."""
        now = self._now()
        with self._tx():
            rows = self._db.execute(
                "SELECT dispute_id, state, amount_value, currency FROM autopilot_v2_jobs WHERE state IN ('armed', 'executing')"
            ).fetchall()
            for did, state, amount, currency in rows:
                cur = self._db.execute(
                    "UPDATE autopilot_v2_jobs SET state = 'stale', finished_at = ?, reason = 'restart'"
                    " WHERE dispute_id = ? AND state = ?",
                    (now.isoformat(), did, state),
                )
                if cur.rowcount == 1:
                    self._audit(did, "stale", state, "stale", "startup", now, amount, currency, reason="restart",
                                message="Autopilot preview window was open when the server restarted; marked stale. "
                                        "Nothing was sent.")
        if rows:
            log.info("autopilot: %d job(s) from an earlier start marked stale", len(rows))

    # -- primitives -------------------------------------------------------------------

    def _now(self) -> datetime:
        return self._now_fn()

    @contextmanager
    def _tx(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except _Rollback:
                self._db.execute("ROLLBACK")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def _audit(self, dispute_id: str, kind: str, state_from: str | None, state_to: str | None, actor: str,
               when: datetime, amount: float | None = None, currency: str | None = None, *,
               reason: str | None = None, message: str, decision_created_at: str | None = None,
               policy_updated_at: str | None = None) -> None:
        self._db.execute(
            "INSERT INTO autopilot_v2_audit (dispute_id, kind, state_from, state_to, actor, mode, boot_id, amount_value,"
            " currency, decision_created_at, policy_updated_at, reason, message, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (dispute_id, kind, state_from, state_to, actor, MODE, self.boot_id, amount, currency,
             decision_created_at, policy_updated_at, reason, message, when.isoformat()),
        )

    def _job_row(self, dispute_id: str) -> tuple[Any, ...] | None:
        return self._db.execute(
            "SELECT dispute_id, state, mode, armed_at, execute_at, claimed_at, finished_at, boot_id, amount_value,"
            " currency, decision_created_at, policy_updated_at, reason FROM autopilot_v2_jobs WHERE dispute_id = ?",
            (dispute_id,),
        ).fetchone()

    @staticmethod
    def _amount_text(amount: float | None, currency: str | None) -> str:
        return f"${amount:.2f} {currency}" if amount is not None and currency else "this P2 claim"

    # -- settings ---------------------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute(
                "SELECT enabled, updated_at, tripped_at, trip_reason FROM autopilot_v2_settings WHERE id = 1"
            ).fetchone()
        return {"enabled": bool(row[0]), "updated_at": row[1], "tripped_at": row[2], "trip_reason": row[3],
                "mode": MODE, "daily_limit": self.daily_limit, "window_seconds": self.window_seconds}

    def set_enabled(self, enabled: bool, actor: str = "merchant") -> None:
        now = self._now()
        with self._tx():
            self._db.execute(
                "UPDATE autopilot_v2_settings SET enabled = ?, updated_at = ?, tripped_at = NULL, trip_reason = NULL WHERE id = 1",
                (int(bool(enabled)), now.isoformat()),
            )
            self._audit(GLOBAL, "enabled" if enabled else "disabled", None, None, actor, now,
                        message="Autopilot preview switched on. It never sends anything." if enabled
                        else "Autopilot preview switched off.")
            if not enabled:
                self._cancel_all_armed_locked(actor, "disabled", now)

    def trip(self, reason: str, actor: str) -> None:
        """Circuit breaker and kill switch: off, armed jobs cancelled, one audit row for the trip."""
        now = self._now()
        with self._tx():
            self._db.execute(
                "UPDATE autopilot_v2_settings SET enabled = 0, updated_at = ?, tripped_at = ?, trip_reason = ? WHERE id = 1",
                (now.isoformat(), now.isoformat(), reason),
            )
            self._audit(GLOBAL, "tripped", None, None, actor, now, reason=reason,
                        message=f"Autopilot preview switched off ({reason}).")
            self._cancel_all_armed_locked(actor, reason, now)

    # -- transitions (each one guarded by the previous state) ---------------------------

    def arm(self, dispute_id: str, amount: float | None, currency: str | None, decision_created_at: str | None,
            policy_updated_at: str | None, actor: str) -> dict[str, Any] | None:
        """(none | stale) -> armed. Any other existing state is left alone and returns None."""
        now = self._now()
        execute_at = now + timedelta(seconds=self.window_seconds)
        with self._tx():
            before = self._db.execute("SELECT state FROM autopilot_v2_jobs WHERE dispute_id = ?", (dispute_id,)).fetchone()
            cur = self._db.execute(
                "INSERT INTO autopilot_v2_jobs (dispute_id, state, mode, armed_at, execute_at, amount_value, currency,"
                " decision_created_at, policy_updated_at)"
                " VALUES (?, 'armed', ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(dispute_id) DO UPDATE SET state = 'armed', armed_at = excluded.armed_at,"
                " execute_at = excluded.execute_at, claimed_at = NULL, finished_at = NULL, boot_id = NULL,"
                " amount_value = excluded.amount_value, currency = excluded.currency,"
                " decision_created_at = excluded.decision_created_at, policy_updated_at = excluded.policy_updated_at,"
                " reason = NULL"
                " WHERE autopilot_v2_jobs.state = 'stale'",
                (dispute_id, MODE, now.isoformat(), execute_at.isoformat(), amount, currency,
                 decision_created_at, policy_updated_at),
            )
            if cur.rowcount != 1:
                raise _Rollback
            self._audit(dispute_id, "armed", before[0] if before else None, "armed", actor, now, amount, currency,
                        decision_created_at=decision_created_at, policy_updated_at=policy_updated_at,
                        message=f"Autopilot preview armed: would accept {self._amount_text(amount, currency)} in "
                                f"{self.window_seconds} s (preview only).")
            return self._format(self._job_row(dispute_id), now)
        return None

    def claim_due(self, into: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """armed -> executing for every due job, taking a daily-cap slot in the same transaction;
        armed -> capped when the day's slots are gone. Returns the claimed jobs. Each claim is appended
        to `into` as soon as it commits, so a caller still knows every executing job if a later claim raises."""
        now = self._now()
        day = now.date().isoformat()
        claimed: list[dict[str, Any]] = into if into is not None else []
        with self._lock:
            due = self._db.execute(
                "SELECT dispute_id, amount_value, currency FROM autopilot_v2_jobs"
                " WHERE state = 'armed' AND execute_at <= ? ORDER BY execute_at, dispute_id",
                (now.isoformat(),),
            ).fetchall()
        for did, amount, currency in due:
            with self._tx():
                self._db.execute("INSERT OR IGNORE INTO autopilot_v2_daily (day, used) VALUES (?, 0)", (day,))
                slot = self._db.execute(
                    "UPDATE autopilot_v2_daily SET used = used + 1 WHERE day = ? AND used < ?", (day, self.daily_limit)
                )
                if slot.rowcount == 1:
                    cur = self._db.execute(
                        "UPDATE autopilot_v2_jobs SET state = 'executing', claimed_at = ?, boot_id = ?"
                        " WHERE dispute_id = ? AND state = 'armed' AND execute_at <= ?",
                        (now.isoformat(), self.boot_id, did, now.isoformat()),
                    )
                    if cur.rowcount != 1:
                        raise _Rollback  # someone else moved it; the slot increment rolls back too
                    self._audit(did, "executing", "armed", "executing", "runner", now, amount, currency,
                                message="Autopilot preview: cancel window ended; building the request it would send.")
                    claimed.append(self._format(self._job_row(did), now))
                else:
                    cur = self._db.execute(
                        "UPDATE autopilot_v2_jobs SET state = 'capped', finished_at = ?, reason = 'daily_cap'"
                        " WHERE dispute_id = ? AND state = 'armed'",
                        (now.isoformat(), did),
                    )
                    if cur.rowcount != 1:
                        raise _Rollback
                    self._audit(did, "capped", "armed", "capped", "runner", now, amount, currency, reason="daily_cap",
                                message=f"Autopilot preview skipped: daily limit of {self.daily_limit} reached. "
                                        "Nothing was sent.")
        return claimed

    def finish(self, dispute_id: str, state: str, reason: str | None, message: str, actor: str = "runner") -> bool:
        """executing -> shadow_done | failed | cancelled. False when the job was not executing."""
        if state not in ("shadow_done", "failed", "cancelled"):
            raise ValueError(state)
        now = self._now()
        with self._tx():
            row = self._job_row(dispute_id)
            if row is None:
                raise _Rollback
            cur = self._db.execute(
                "UPDATE autopilot_v2_jobs SET state = ?, finished_at = ?, reason = ? WHERE dispute_id = ? AND state = 'executing'",
                (state, now.isoformat(), reason, dispute_id),
            )
            if cur.rowcount != 1:
                raise _Rollback
            self._audit(dispute_id, state, "executing", state, actor, now, row[8], row[9], reason=reason, message=message)
            return True
        return False

    def cancel(self, dispute_id: str, actor: str, reason: str) -> bool:
        """armed -> cancelled. False when there was no armed job (too late or never armed)."""
        now = self._now()
        with self._tx():
            return self._cancel_locked(dispute_id, actor, reason, now)
        return False

    def _cancel_locked(self, dispute_id: str, actor: str, reason: str, now: datetime) -> bool:
        row = self._job_row(dispute_id)
        if row is None:
            return False
        cur = self._db.execute(
            "UPDATE autopilot_v2_jobs SET state = 'cancelled', finished_at = ?, reason = ? WHERE dispute_id = ? AND state = 'armed'",
            (now.isoformat(), reason, dispute_id),
        )
        if cur.rowcount != 1:
            return False
        self._audit(dispute_id, "cancelled", "armed", "cancelled", actor, now, row[8], row[9], reason=reason,
                    message=f"Autopilot preview cancelled ({reason.replace('_', ' ')}). Nothing was sent.")
        return True

    def _cancel_all_armed_locked(self, actor: str, reason: str, now: datetime) -> int:
        ids = [r[0] for r in self._db.execute("SELECT dispute_id FROM autopilot_v2_jobs WHERE state = 'armed'").fetchall()]
        return sum(1 for did in ids if self._cancel_locked(did, actor, reason, now))

    def cancel_all_armed(self, actor: str, reason: str) -> int:
        now = self._now()
        with self._tx():
            return self._cancel_all_armed_locked(actor, reason, now)
        return 0

    # -- reads (no writes) ------------------------------------------------------------

    def _format(self, row: tuple[Any, ...] | None, now: datetime) -> dict[str, Any] | None:
        if row is None:
            return None
        cols = ("dispute_id", "state", "mode", "armed_at", "execute_at", "claimed_at", "finished_at", "boot_id",
                "amount_value", "currency", "decision_created_at", "policy_updated_at", "reason")
        item = dict(zip(cols, row))
        remaining = (datetime.fromisoformat(item["execute_at"]) - now).total_seconds() if item["state"] == "armed" else 0.0
        item["remaining_seconds"] = round(max(0.0, remaining), 2)
        item["window_seconds"] = self.window_seconds
        return item

    def job(self, dispute_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._job_row(dispute_id)
        return self._format(row, self._now())

    def daily_used(self, day: str | None = None) -> int:
        day = day or self._now().date().isoformat()
        with self._lock:
            row = self._db.execute("SELECT used FROM autopilot_v2_daily WHERE day = ?", (day,)).fetchone()
        return int(row[0]) if row else 0

    def _audit_rows(self, where: str, params: tuple[Any, ...], limit: int | None) -> list[dict[str, Any]]:
        sql = ("SELECT id, dispute_id, kind, state_from, state_to, actor, mode, boot_id, amount_value, currency, reason,"
               f" message, created_at FROM autopilot_v2_audit {where} ORDER BY id DESC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        cols = ("id", "dispute_id", "kind", "state_from", "state_to", "actor", "mode", "boot_id", "amount_value",
                "currency", "reason", "message", "created_at")
        return [dict(zip(cols, r)) for r in rows]

    def audit_for(self, dispute_id: str) -> list[dict[str, Any]]:
        return list(reversed(self._audit_rows("WHERE dispute_id = ?", (dispute_id,), None)))

    def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._audit_rows("", (), limit)

    def state(self) -> dict[str, Any]:
        now = self._now()
        with self._lock:
            rows = self._db.execute(
                "SELECT dispute_id, state, mode, armed_at, execute_at, claimed_at, finished_at, boot_id, amount_value,"
                " currency, decision_created_at, policy_updated_at, reason FROM autopilot_v2_jobs ORDER BY armed_at, dispute_id"
            ).fetchall()
        jobs = [self._format(r, now) for r in rows]
        used = self.daily_used(now.date().isoformat())
        return self.settings() | {
            "boot_id": self.boot_id,
            "daily_used": used,
            "daily_remaining": max(0, self.daily_limit - used),
            "pending": [j for j in jobs if j and j["state"] == "armed"],
            "jobs": {j["dispute_id"]: j for j in jobs if j},
            "recent": self.recent_audit(20),
        }

    def close(self) -> None:
        with self._lock:
            self._db.close()


# --- coordinator and runner ---------------------------------------------------------------


class Autopilot:
    """Arms from decisions and poller updates, runs the cancel window on its own thread, and in
    shadow mode ends every job by building the dry run. `dry_run` is the only outward call."""

    def __init__(
        self,
        store: AutopilotStore,
        disputes: Any,
        decisions: Any,
        policies: Any,
        submissions: Any,
        dry_run: Callable[[str], dict[str, Any]],
        fetch_fresh: Callable[[str], dict[str, Any]] | None = None,
        interval: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.store = store
        self._disputes = disputes
        self._decisions = decisions
        self._policies = policies
        self._submissions = submissions
        self._dry_run = dry_run
        self._fetch_fresh = fetch_fresh  # read-only GET of the dispute; None = status can't be checked
        self._interval = interval
        self._sleep = sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- arming: analyzer decisions, poller updates, policy saves -------------------------

    def _inputs(self, dispute_id: str) -> tuple[Any, Any, PolicySettings, str | None, Any]:
        policy, policy_at = self._policies.get()
        return (self._disputes.get(dispute_id), self._decisions.get(dispute_id), policy, policy_at,
                self._submissions.get(dispute_id))

    def evaluate(self, dispute_id: str, actor: str) -> dict[str, Any] | None:
        """Arm an eligible dispute (new or stale job) or cancel its armed job when it no longer qualifies."""
        if not self.store.settings()["enabled"]:
            return None
        dispute, decision, policy, policy_at, submission = self._inputs(dispute_id)
        ok, reason = p2_eligibility(dispute, decision, policy, submission)
        job = self.store.job(dispute_id)
        if ok:
            if job is None or job["state"] == "stale":
                amount, currency = amount_usd(dispute or {})
                return self.store.arm(dispute_id, amount, currency, (decision or {}).get("created_at"), policy_at, actor)
            return job
        if job and job["state"] == "armed":
            self.store.cancel(dispute_id, actor, reason or "not_eligible")
        return self.store.job(dispute_id)

    def _guarded(self, actor: str, fn: Callable[[], Any]) -> Any:
        """Any exception in arming trips the breaker; the caller (analyzer, poller, route) never sees it."""
        try:
            return fn()
        except Exception as exc:
            log.error("autopilot %s hook failed: %s", actor, type(exc).__name__)
            try:
                self.store.trip(f"exception:{type(exc).__name__}", "breaker")
            except Exception:
                log.exception("autopilot: could not record the trip")
            return None

    def on_decision(self, dispute_id: str, _result: Any = None) -> None:
        self._guarded("analyzer", lambda: self.evaluate(dispute_id, "analyzer"))

    def on_event(self, event: Any) -> None:
        if getattr(event, "name", "") == "dispute_updated":
            self._guarded("poller", lambda: self.evaluate(event.dispute_id, "poller"))

    def on_policy_change(self) -> int:
        return self._guarded("merchant", lambda: self.store.cancel_all_armed("merchant", "policy_changed")) or 0

    # -- merchant controls ----------------------------------------------------------------

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        self.store.set_enabled(enabled, "merchant")
        return self.state()

    def cancel(self, dispute_id: str) -> bool:
        return self.store.cancel(dispute_id, "merchant", "cancelled_by_merchant")

    def kill(self) -> dict[str, Any]:
        self.store.trip("kill_switch", "merchant")
        return self.state()

    def state(self) -> dict[str, Any]:
        return self.store.state()

    # -- execution ------------------------------------------------------------------------

    def _active(self) -> bool:
        """Enabled and not tripped, read fresh from the store."""
        st = self.store.settings()
        return bool(st["enabled"]) and not st["tripped_at"]

    def tick(self) -> int:
        """One scheduler pass: claim every due job, then run each one after re-reading the enabled flag and
        the breaker. No claimed job is left 'executing' when the pass returns: after a trip or an error
        the rest are cancelled (executing -> cancelled, one audit row each). Returns how many were claimed."""
        if not self._active():
            return 0
        claimed: list[dict[str, Any]] = []
        try:
            self.store.claim_due(into=claimed)
        except Exception as exc:
            log.error("autopilot claim failed: %s", type(exc).__name__)
            self._trip(f"exception:{type(exc).__name__}")
            self._cancel_rest(claimed, "claim_failed")
            return len(claimed)
        for i, job in enumerate(claimed):
            try:
                active = self._active()
            except Exception as exc:
                self._trip(f"exception:{type(exc).__name__}")
                active = False
            if not active:
                self._cancel_rest(claimed[i:], "breaker_tripped")
                break
            if not self._execute(job):
                self._cancel_rest(claimed[i + 1:], "breaker_tripped")
                break
        return len(claimed)

    def _trip(self, reason: str) -> None:
        try:
            self.store.trip(reason, "breaker")
        except Exception:
            log.exception("autopilot: could not record the trip")

    def _cancel_rest(self, jobs: list[dict[str, Any]], reason: str) -> None:
        for job in jobs:
            try:
                self.store.finish(job["dispute_id"], "cancelled", reason,
                                  f"Autopilot preview cancelled before it ran ({reason.replace('_', ' ')}). "
                                  "Nothing was sent.", actor="breaker")
            except Exception:
                log.exception("autopilot: could not cancel dispute=%s", job.get("dispute_id"))

    def _execute(self, job: dict[str, Any]) -> bool:
        """Run one claimed job. True when it ended normally (shadow_done or a stand-down); False when it
        raised, in which case the job is marked failed and the breaker is tripped."""
        did = job["dispute_id"]
        try:
            dispute, decision, policy, _policy_at, submission = self._inputs(did)
            ok, reason = p2_eligibility(dispute, decision, policy, submission)
            if ok and (decision or {}).get("created_at") != job.get("decision_created_at"):
                ok, reason = False, "decision_changed"
            if ok and self._fetch_fresh is None:
                ok, reason = False, "status_check_unavailable"
            if ok:
                # A fresh read of the dispute: still waiting for the seller, still offering accept_claim,
                # and still eligible on everything else. A fetch error raises and trips the breaker.
                fresh = self._fetch_fresh(did)
                ok, reason = p2_eligibility(fresh, decision, policy, submission)
                if not ok:
                    reason = f"fresh_{reason}"
            if not ok:
                self.store.finish(did, "cancelled", reason, f"Autopilot preview stood down at execution time "
                                                            f"({(reason or 'not eligible').replace('_', ' ')}). Nothing was sent.")
                return True
            out = self._dry_run(did)
            if not isinstance(out, dict) or not out.get("request"):
                raise RuntimeError("dry_run_empty")
            self.store.finish(did, "shadow_done", None, f"Autopilot preview: would have accepted {did}. Nothing was sent.")
            return True
        except Exception as exc:
            code = type(exc).__name__ if not isinstance(exc, RuntimeError) else str(exc) or "RuntimeError"
            log.error("autopilot preview failed dispute=%s: %s", did, code)
            try:
                self.store.finish(did, "failed", code, f"Autopilot preview failed ({code}); Autopilot preview switched off. "
                                                       "Nothing was sent.")
            except Exception:
                log.exception("autopilot: could not mark dispute=%s failed", did)
            self._trip(f"exception:{code}")
            return False

    # -- thread ---------------------------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="disputedesk-autopilot", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.tick()
            except Exception as exc:  # the loop must survive; the breaker records why
                log.error("autopilot runner error: %s", type(exc).__name__)
                try:
                    self.store.trip(f"runner:{type(exc).__name__}", "breaker")
                except Exception:
                    log.exception("autopilot: could not record the trip")
