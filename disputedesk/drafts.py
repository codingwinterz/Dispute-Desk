"""Merchant-edited evidence drafts, one per dispute (same SQLite file as the disputes).

The agent writes a draft into its decision; the merchant may edit and save it on the case page.
A saved merchant draft is kept apart from the decision, so re-running the agent never overwrites it.
effective_draft() is the single answer to "what text would be submitted": the merchant's saved
text if there is one, otherwise the agent's draft. Nothing here calls PayPal.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from disputedesk.decision import SANDBOX_NOTE

# App-side cap on the draft length. UNVERIFIED: PayPal's own limit for evidence notes; it is checked
# against the provide-evidence docs before anything is submitted (Phase 3).
MAX_DRAFT_CHARS = 4000


class DraftError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_draft(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise DraftError("empty", "The draft is empty. Write the response you want PayPal to read.")
    if len(text) > MAX_DRAFT_CHARS:
        raise DraftError("too_long", f"The draft is {len(text)} characters; the limit is {MAX_DRAFT_CHARS}.")
    if SANDBOX_NOTE not in text:
        # Shipment and device records are seeded test data; the draft must keep saying so.
        raise DraftError("missing_sandbox_note", f'Keep this sentence in the draft: "{SANDBOX_NOTE}"')
    return text


class DraftStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS evidence_drafts ("
                " dispute_id TEXT PRIMARY KEY,"
                " text TEXT NOT NULL,"
                " updated_at TEXT NOT NULL)"
            )

    def get(self, dispute_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT text, updated_at FROM evidence_drafts WHERE dispute_id = ?", (dispute_id,)
            ).fetchone()
        return {"text": row[0], "updated_at": row[1]} if row else None

    def save(self, dispute_id: str, text: str) -> dict[str, Any]:
        text = validate_draft(text)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO evidence_drafts (dispute_id, text, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(dispute_id) DO UPDATE SET text = excluded.text, updated_at = excluded.updated_at",
                (dispute_id, text, now),
            )
        return {"text": text, "updated_at": now}

    def close(self) -> None:
        with self._lock:
            self._db.close()


def effective_draft(saved: dict[str, Any] | None, decision: dict[str, Any] | None) -> dict[str, Any] | None:
    """The text that would be submitted, and where it came from."""
    if saved:
        return {"text": saved["text"], "updated_at": saved["updated_at"], "edited": True}
    if decision and decision.get("status") == "ok" and decision.get("evidence_summary"):
        return {"text": decision["evidence_summary"], "updated_at": decision.get("created_at"), "edited": False}
    return None
