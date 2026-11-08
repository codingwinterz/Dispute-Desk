"""Approved responses to PayPal: the only code that calls a PayPal write endpoint.

Called only from POST /api/submissions/{id} after the merchant confirms in the dialog. The poller,
the webhook path, the analyzer and app startup never construct a request here or call submit().

Submit flow (every refusal happens before anything is sent):
  1. the stored decision is ok and the requested action matches it (DEFEND -> provide_evidence,
     ACCEPT -> accept_claim);
  2. the text is the current one: the effective draft for provide_evidence, re-checked against the
     2000-character cap and the sandbox sentence at the start; the fixed note for accept_claim;
     and it equals the text the merchant confirmed;
  3. the dispute is claimed in SQLite, so a second submit is blocked (unless the last attempt was a
     definite PayPal rejection, a 4xx, which means nothing changed on PayPal's side);
  4. the dispute is read fresh from PayPal (GET) and must be WAITING_FOR_SELLER_RESPONSE with the
     action's link;
  5. the request is sent once; then the dispute is read again and stored.
Outcomes: succeeded (2xx), rejected (4xx: retry allowed), unknown (network error or 5xx: blocked,
because PayPal may have processed it).
"""

import hashlib
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from disputedesk.config import SANDBOX_BASE_URL
from disputedesk.decision_store import DecisionStore
from disputedesk.dispute_store import DisputeStore
from disputedesk.disputes import (ACCEPT_CLAIM, ACCEPT_NOTE, PROVIDE_EVIDENCE, DisputesClient,
                                  build_action_request)
from disputedesk.drafts import DraftError, DraftStore, effective_draft, validate_draft
from disputedesk.events import DisputeUpdated, EventBus

log = logging.getLogger("disputedesk.submissions")

WAITING = "WAITING_FOR_SELLER_RESPONSE"
ACTION_FOR = {"DEFEND": PROVIDE_EVIDENCE, "ACCEPT": ACCEPT_CLAIM}
LABELS = {PROVIDE_EVIDENCE: "Send evidence to PayPal", ACCEPT_CLAIM: "Accept the claim and refund the buyer"}
BLOCKING = ("sending", "succeeded", "unknown")
# Behaviour not shown in PayPal's docs; recorded the first time PayPal accepts it.
NOTES_ONLY = "provide_evidence_notes_only"
DISPUTES_BASE = f"{SANDBOX_BASE_URL}/v1/customer/disputes"
REDACTED_AUTH = {"Authorization": "Bearer [REDACTED]"}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SubmissionStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS submissions ("
                " dispute_id TEXT PRIMARY KEY,"
                " action TEXT NOT NULL,"
                " state TEXT NOT NULL CHECK (state IN ('sending', 'succeeded', 'rejected', 'unknown')),"
                " text_sha256 TEXT NOT NULL,"
                " attempts INTEGER NOT NULL,"
                " started_at TEXT NOT NULL,"
                " finished_at TEXT,"
                " http_status INTEGER,"
                " paypal_error TEXT)"   # JSON {name, message, debug_id, issues} or {network: TypeName}
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS verified_behaviours ("
                " key TEXT PRIMARY KEY, verified_at TEXT NOT NULL, dispute_id TEXT NOT NULL)"
            )

    _COLS = ("dispute_id", "action", "state", "text_sha256", "attempts", "started_at", "finished_at",
             "http_status", "paypal_error")

    def _get(self, dispute_id: str) -> dict[str, Any] | None:
        row = self._db.execute(f"SELECT {', '.join(self._COLS)} FROM submissions WHERE dispute_id = ?",
                               (dispute_id,)).fetchone()
        if row is None:
            return None
        rec = dict(zip(self._COLS, row))
        rec["paypal_error"] = json.loads(rec["paypal_error"]) if rec["paypal_error"] else None
        return rec

    def get(self, dispute_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._get(dispute_id)

    def claim(self, dispute_id: str, action: str, text: str) -> tuple[bool, dict[str, Any] | None]:
        """Atomically mark the dispute as sending. Returns (claimed, previous record)."""
        with self._lock, self._db:
            prev = self._get(dispute_id)
            if prev and prev["state"] in BLOCKING:
                return False, prev
            self._db.execute(
                "INSERT INTO submissions (dispute_id, action, state, text_sha256, attempts, started_at)"
                " VALUES (?, ?, 'sending', ?, 1, ?)"
                " ON CONFLICT(dispute_id) DO UPDATE SET action = excluded.action, state = 'sending',"
                " text_sha256 = excluded.text_sha256, attempts = submissions.attempts + 1,"
                " started_at = excluded.started_at, finished_at = NULL, http_status = NULL, paypal_error = NULL",
                (dispute_id, action, sha256(text), _now()),
            )
            return True, prev

    def release(self, dispute_id: str, prev: dict[str, Any] | None) -> None:
        """Undo a claim when nothing was sent (a pre-send check refused)."""
        with self._lock, self._db:
            if prev is None:
                self._db.execute("DELETE FROM submissions WHERE dispute_id = ? AND state = 'sending'", (dispute_id,))
            else:
                self._db.execute(
                    "UPDATE submissions SET action = ?, state = ?, text_sha256 = ?, attempts = ?, started_at = ?,"
                    " finished_at = ?, http_status = ?, paypal_error = ? WHERE dispute_id = ?",
                    (prev["action"], prev["state"], prev["text_sha256"], prev["attempts"], prev["started_at"],
                     prev["finished_at"], prev["http_status"],
                     json.dumps(prev["paypal_error"]) if prev["paypal_error"] else None, dispute_id),
                )

    def finish(self, dispute_id: str, state: str, http_status: int | None, error: dict[str, Any] | None) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE submissions SET state = ?, finished_at = ?, http_status = ?, paypal_error = ? WHERE dispute_id = ?",
                (state, _now(), http_status, json.dumps(error) if error else None, dispute_id),
            )

    def mark_verified(self, key: str, dispute_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO verified_behaviours VALUES (?, ?, ?)", (key, _now(), dispute_id))

    def verified(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT verified_at, dispute_id FROM verified_behaviours WHERE key = ?",
                                   (key,)).fetchone()
        return {"verified_at": row[0], "dispute_id": row[1]} if row else None

    def close(self) -> None:
        with self._lock:
            self._db.close()


@dataclass
class SubmitError(Exception):
    """A refusal (nothing sent) or a PayPal failure, with the HTTP status the API should answer."""
    http: int
    code: str
    message: str
    extra: dict[str, Any] | None = None


def paypal_error(resp: httpx.Response) -> dict[str, Any]:
    """PayPal's error fields only (name, message, debug_id, issues); bodies carry no secrets."""
    try:
        body = resp.json()
    except ValueError:
        return {"message": f"HTTP {resp.status_code} with a non-JSON body"}
    issues = [d.get("issue") or d.get("description") for d in body.get("details") or [] if isinstance(d, dict)]
    return {k: v for k, v in {"name": body.get("name"), "message": body.get("message"),
                              "debug_id": body.get("debug_id"), "issues": [i for i in issues if i]}.items() if v}


def render_request(req: httpx.Request) -> dict[str, Any]:
    body = req.read()
    return {
        "method": req.method,
        "url": str(req.url),
        "headers": {k: v for k, v in req.headers.items() if k.lower() != "host"},
        "body": body.decode("utf-8", errors="replace"),
    }


class Submitter:
    def __init__(self, disputes: DisputeStore, decisions: DecisionStore, drafts: DraftStore,
                 store: SubmissionStore, client: DisputesClient | None, bus: EventBus | None = None):
        self._disputes = disputes
        self._decisions = decisions
        self._drafts = drafts
        self.store = store
        self._client = client  # None when the app has no PayPal credentials (tests, dashboard preview)
        self._bus = bus

    # --- shared by the case page, dry run and submit ---------------------------------

    def _plan(self, dispute_id: str) -> tuple[str | None, str | None, list[str]]:
        """(action, text, problems) from stored state. Problems block the Approve button."""
        decision = self._decisions.get(dispute_id)
        if not decision:
            return None, None, ["The agent hasn't recommended an action for this dispute yet."]
        if decision["status"] != "ok":
            return None, None, ["The last analysis failed, so there is no recommendation to approve."]
        action = ACTION_FOR[decision["decision"]]
        if action == ACCEPT_CLAIM:
            return action, ACCEPT_NOTE, []
        draft = effective_draft(self._drafts.get(dispute_id), decision)
        text = draft["text"] if draft else ""
        try:
            validate_draft(text)
        except DraftError as exc:
            return action, text, [f"Fix the draft first: {exc.message} Edit it and save."]
        return action, text, []

    def check(self, dispute_id: str) -> dict[str, Any]:
        action, text, problems = self._plan(dispute_id)
        data = self._disputes.get(dispute_id) or {}
        status = data.get("status")
        if action and status != WAITING:
            problems.append(f"PayPal shows this dispute as {status or 'unknown'}. Responses are only accepted "
                            "while it is waiting for your response.")
        if action and not DisputesClient.has_link(data, action):
            problems.append("PayPal isn't offering this action on the dispute right now.")
        submission = self.store.get(dispute_id)
        if submission and submission["state"] in BLOCKING:
            problems.insert(0, self._blocked_message(submission))
        if self._client is None:
            problems.append("Submitting is off: the app was started without PayPal credentials.")
        verified = self.store.verified(NOTES_ONLY)
        return {
            "action": action,
            "label": LABELS.get(action or ""),
            "text": text,
            "can_submit": bool(action) and not problems,
            "reasons": problems,
            # Notes-only provide-evidence is not shown in PayPal's docs; UNVERIFIED until one succeeds.
            "unverified": action == PROVIDE_EVIDENCE and verified is None,
            "verified": verified if action == PROVIDE_EVIDENCE else None,
            "submission": submission,
        }

    @staticmethod
    def _blocked_message(sub: dict[str, Any]) -> str:
        if sub["state"] == "succeeded":
            when = (sub["finished_at"] or "")[:16].replace("T", " ")
            return f"Already sent to PayPal ({sub['action'].replace('_', ' ')}) on {when} UTC."
        if sub["state"] == "sending":
            return "A submission for this dispute is in progress."
        return ("The last attempt's outcome is unknown (network error or PayPal server error), so another "
                "submit is blocked. Check the dispute in the PayPal sandbox before doing anything else.")

    def dry_run(self, dispute_id: str) -> dict[str, Any]:
        """Build the exact request from stored state. Nothing is sent and no token is fetched."""
        check = self.check(dispute_id)
        request = None
        if check["action"] and check["text"]:
            req = build_action_request(DISPUTES_BASE, check["action"], dispute_id, check["text"], REDACTED_AUTH)
            request = render_request(req)
        return {"request": request, "check": check, "sent": False}

    # --- the one write path ------------------------------------------------------------

    def submit(self, dispute_id: str, action: str, confirmed_text: str) -> dict[str, Any]:
        if self._client is None:
            raise SubmitError(503, "not_configured", "Submitting is off: the app was started without PayPal credentials.")
        planned, text, problems = self._plan(dispute_id)
        if planned is None:
            raise SubmitError(409, "no_decision", problems[0])
        if action != planned:
            raise SubmitError(409, "action_mismatch",
                              f"The recommendation is {LABELS[planned].lower()}, not {action.replace('_', ' ')}.")
        if problems:  # draft over the cap or not starting with the sandbox sentence
            raise SubmitError(422, "draft_invalid", problems[0])
        if confirmed_text != text:
            raise SubmitError(409, "text_changed", "The text changed since you reviewed it. Review it again and confirm.")

        claimed, prev = self.store.claim(dispute_id, action, text)
        if not claimed:
            raise SubmitError(409, "already_submitted", self._blocked_message(prev))

        try:
            fresh = self._client.get_dispute(dispute_id)  # read-only GET, never a write
        except httpx.HTTPError as exc:
            self.store.release(dispute_id, prev)
            raise SubmitError(502, "status_check_failed",
                              f"Couldn't read the dispute from PayPal before sending ({type(exc).__name__}). Nothing was sent.")
        self._disputes.update_full(dispute_id, fresh)
        if fresh.get("status") != WAITING or not DisputesClient.has_link(fresh, action):
            self.store.release(dispute_id, prev)
            raise SubmitError(409, "not_allowed_now",
                              f"PayPal now shows status {fresh.get('status')}"
                              f"{'' if DisputesClient.has_link(fresh, action) else ' without this action'}. Nothing was sent.")

        log.info("submitting dispute=%s action=%s chars=%d", dispute_id, action, len(text))
        try:
            resp = self._client.send_action(action, dispute_id, text)
        except httpx.HTTPError as exc:
            self.store.finish(dispute_id, "unknown", None, {"network": type(exc).__name__})
            raise SubmitError(502, "outcome_unknown",
                              f"The request may or may not have reached PayPal ({type(exc).__name__}). Another submit is "
                              "blocked; check the dispute in the PayPal sandbox.")
        if resp.is_success:
            self.store.finish(dispute_id, "succeeded", resp.status_code, None)
            if action == PROVIDE_EVIDENCE:
                self.store.mark_verified(NOTES_ONLY, dispute_id)
            return {"result": "succeeded", "http_status": resp.status_code, "dispute": self._refresh(dispute_id)}
        error = paypal_error(resp)
        state = "rejected" if 400 <= resp.status_code < 500 else "unknown"
        self.store.finish(dispute_id, state, resp.status_code, error)
        if state == "rejected":
            raise SubmitError(502, "paypal_rejected",
                              f"PayPal refused the request (HTTP {resp.status_code}). Nothing changed on PayPal; "
                              "you can fix the problem and try again.", {"paypal": error})
        raise SubmitError(502, "outcome_unknown",
                          f"PayPal answered HTTP {resp.status_code}; it may have processed the request. Another submit "
                          "is blocked; check the dispute in the PayPal sandbox.", {"paypal": error})

    def _refresh(self, dispute_id: str) -> dict[str, Any] | None:
        """Read the dispute after a successful write so the status badge updates. Read-only."""
        try:
            fresh = self._client.get_dispute(dispute_id)
        except httpx.HTTPError as exc:
            log.warning("refresh after submit failed dispute=%s: %s", dispute_id, type(exc).__name__)
            return self._disputes.summary(dispute_id)
        self._disputes.update_full(dispute_id, fresh)
        if self._bus:
            self._bus.emit(DisputeUpdated(dispute_id, "submit"))
        return self._disputes.summary(dispute_id)
