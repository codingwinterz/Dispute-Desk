"""SQLite store of agent decisions, one row per dispute (same file as the dispute store)."""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from disputedesk.decision import DecisionResult


class DecisionStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS decisions ("
                " dispute_id TEXT PRIMARY KEY,"
                " status TEXT NOT NULL CHECK (status IN ('ok', 'error')),"
                " decision TEXT CHECK (decision IN ('DEFEND', 'ACCEPT')),"
                " risk_score INTEGER,"
                " reasoning TEXT,"          # JSON list of {point, cites}
                " evidence_summary TEXT,"
                " facts TEXT NOT NULL,"     # JSON list of the F* facts the model was given
                " graph_context TEXT,"      # JSON snapshot of the graph query result
                " model TEXT NOT NULL,"
                " attempts INTEGER NOT NULL,"
                " error TEXT,"
                " created_at TEXT NOT NULL)"
            )
            # Added in Phase 2: the policy rules the decision was made under (JSON list of {id, text}).
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(decisions)")}
            if "rules" not in cols:
                self._db.execute("ALTER TABLE decisions ADD COLUMN rules TEXT")

    def save(self, dispute_id: str, result: DecisionResult, graph_context: dict[str, Any] | None,
             rules: list[dict[str, str]] | None = None) -> None:
        out = result.output
        row = (
            dispute_id, result.status,
            out.decision if out else None,
            out.risk_score if out else None,
            json.dumps([p.model_dump() for p in out.reasoning]) if out else None,
            out.evidence_summary if out else None,
            json.dumps(result.facts),
            json.dumps(graph_context) if graph_context is not None else None,
            result.model, result.attempts, result.error,
            datetime.now(timezone.utc).isoformat(),
            json.dumps(rules) if rules is not None else None,
        )
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO decisions (dispute_id, status, decision, risk_score, reasoning,"
                " evidence_summary, facts, graph_context, model, attempts, error, created_at, rules)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row)

    def get(self, dispute_id: str) -> dict[str, Any] | None:
        with self._lock:
            cur = self._db.execute("SELECT * FROM decisions WHERE dispute_id = ?", (dispute_id,))
            row = cur.fetchone()
            cols = [c[0] for c in cur.description]
        if row is None:
            return None
        rec = dict(zip(cols, row))
        for key in ("reasoning", "facts", "graph_context", "rules"):
            rec[key] = json.loads(rec[key]) if rec[key] else None
        return rec

    def briefs(self) -> dict[str, dict[str, Any]]:
        """dispute_id -> {status, decision, risk_score, created_at} for the queue."""
        with self._lock:
            rows = self._db.execute(
                "SELECT dispute_id, status, decision, risk_score, created_at FROM decisions"
            ).fetchall()
        return {r[0]: {"status": r[1], "decision": r[2], "risk_score": r[3], "created_at": r[4]} for r in rows}

    def has_ok(self, dispute_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM decisions WHERE dispute_id = ? AND status = 'ok'", (dispute_id,)
            ).fetchone()
        return row is not None

    def close(self) -> None:
        with self._lock:
            self._db.close()
