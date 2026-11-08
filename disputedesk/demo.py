"""Demo mode: a committed snapshot of recorded sandbox data, served with no PayPal, Gemini or FalkorDB.

export_snapshot() reads the local SQLite store and keeps only what the dashboard shows, with an
allowlist: dispute fields the pages use, link rel/method (no URLs), per-transaction IDs and amounts.
Buyer and seller objects, messages, evidence documents and fund movements are never copied. Buyer and
merchant identifiers are replaced by pseudonyms everywhere they appear (fact text, graph snapshot,
reasoning, drafts). scan() then fails the export if an email, an original identifier or a token-like
string is still present; it reports only which check failed, never the value.

load_snapshot() writes a snapshot into a fresh SQLite file so the existing stores and routes serve it.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from disputedesk.config import DATA_DIR

SNAPSHOT_PATH = DATA_DIR / "demo_snapshot.json"
FORMAT = 1
DEMO_MESSAGE = "Demo mode: recorded sandbox data"

_DISPUTE_FIELDS = ("dispute_id", "status", "dispute_life_cycle_stage", "reason", "dispute_amount",
                   "seller_response_due_date", "create_time", "update_time", "allowed_response_options")
_TXN_FIELDS = ("seller_transaction_id", "buyer_transaction_id", "create_time", "gross_amount",
               "seller_protection_eligible", "seller_protection_type", "transaction_status")

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SECRET_PATTERNS = {
    "paypal_access_token": re.compile(r"A21AA[A-Za-z0-9_-]{20,}"),
    "bearer_token": re.compile(r"Bearer\s+(?!\[REDACTED\])[A-Za-z0-9._-]{12,}"),
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_-]{30,}|\bAQ\.[A-Za-z0-9_-]{30,}"),
    "secret_field": re.compile(r"(?i)\"(client_secret|access_token|refresh_token|api_key|password)\"\s*:\s*\"[^\"]+\""),
}


def _allowlist_dispute(data: dict[str, Any]) -> dict[str, Any]:
    out = {k: data[k] for k in _DISPUTE_FIELDS if k in data}
    out["links"] = [{"rel": link.get("rel"), "method": link.get("method")}
                    for link in data.get("links") or [] if isinstance(link, dict)]
    out["disputed_transactions"] = [{k: t[k] for k in _TXN_FIELDS if k in t}
                                    for t in data.get("disputed_transactions") or [] if isinstance(t, dict)]
    return out


def _identifiers(disputes: list[dict[str, Any]]) -> dict[str, str]:
    """Original buyer/merchant identifiers -> pseudonyms (stable within one export)."""
    mapping: dict[str, str] = {}
    buyers: dict[str, str] = {}
    merchants: dict[str, str] = {}

    def names(person: dict[str, Any]) -> list[str]:
        n = person.get("name")
        if isinstance(n, dict):
            return [str(v) for v in n.values() if v]
        return [str(n)] if n else []

    for d in disputes:
        for t in d.get("disputed_transactions") or []:
            buyer, seller = t.get("buyer") or {}, t.get("seller") or {}
            if buyer.get("payer_id"):
                mapping[buyer["payer_id"]] = buyers.setdefault(buyer["payer_id"], f"sandbox-buyer-{len(buyers) + 1}")
            if seller.get("merchant_id"):
                mapping[seller["merchant_id"]] = merchants.setdefault(seller["merchant_id"], f"sandbox-merchant-{len(merchants) + 1}")
            for email in (buyer.get("email"), seller.get("email")):
                if email:
                    mapping[email] = "[email removed]"
            for n in names(buyer):
                mapping[n] = "Sandbox buyer"
            for n in names(seller):
                mapping[n] = "Sandbox merchant"
    return mapping


def _replace(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        for original in sorted(mapping, key=len, reverse=True):  # longest first: full names before parts
            if original and original in value:
                value = value.replace(original, mapping[original])
        return EMAIL.sub("[email removed]", value)
    if isinstance(value, list):
        return [_replace(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: _replace(v, mapping) for k, v in value.items()}
    return value


def _rows(db: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return []
    cur = db.execute(f"SELECT * FROM {table}")
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _json_cols(row: dict[str, Any], cols: tuple[str, ...]) -> dict[str, Any]:
    return {k: (json.loads(v) if k in cols and isinstance(v, str) and v else v) for k, v in row.items()}


def export_snapshot(db_path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """Build the snapshot from the local store. Returns (snapshot, identifier mapping used)."""
    db = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        raw_disputes = [(r["dispute_id"], r["source"], r["detected_at"], json.loads(r["data"]), r["full_fetched"])
                        for r in _rows(db, "disputes")]
        mapping = _identifiers([d for _, _, _, d, _ in raw_disputes])
        disputes = [{"dispute_id": i, "source": "paypal", "detected_via": via, "detected_at": at,
                     "full_fetched": full, "data": _allowlist_dispute(data)}
                    for i, via, at, data, full in raw_disputes]
        decisions = [_json_cols(r, ("reasoning", "facts", "graph_context", "rules")) for r in _rows(db, "decisions")]
        drafts = _rows(db, "evidence_drafts")
        submissions = [_json_cols(r, ("paypal_error",)) for r in _rows(db, "submissions")]
        verified = _rows(db, "verified_behaviours")
        policy = _rows(db, "policy_settings")
        merchant = [_json_cols(r, ("data",)) for r in _rows(db, "merchant_records")]
    finally:
        db.close()
    snapshot = {
        "format": FORMAT,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "about": ("Recorded PayPal sandbox disputes and DisputeDesk's decisions, for demo mode. Buyer and merchant "
                  "identifiers are pseudonymised; shipments, devices and past disputes are seeded test data."),
        "disputes": disputes,
        "decisions": decisions,
        "drafts": drafts,
        "submissions": submissions,
        "verified_behaviours": verified,
        "policy_settings": json.loads(policy[0]["data"]) if policy else None,
        "merchant_records": merchant,
    }
    return _replace(snapshot, mapping), mapping


def scan(snapshot: dict[str, Any], originals: list[str]) -> list[str]:
    """Names of the checks the snapshot fails (empty = clean). Never returns the offending values."""
    text = json.dumps(snapshot, ensure_ascii=False)
    failed = []
    if EMAIL.search(text):
        failed.append("email_address")
    if any(o and o in text for o in originals):
        failed.append("original_identifier")
    failed += [name for name, pattern in SECRET_PATTERNS.items() if pattern.search(text)]
    if any(link.get("href") for d in snapshot.get("disputes", []) for link in d["data"].get("links", [])):
        failed.append("link_url")
    return failed


def load_snapshot(snapshot: dict[str, Any], db_path: Path) -> None:
    """Write the snapshot into the SQLite file at db_path (tables must already exist)."""
    if snapshot.get("format") != FORMAT:
        raise ValueError(f"unsupported demo snapshot format {snapshot.get('format')!r}")
    dump = lambda v: json.dumps(v) if v is not None else None  # noqa: E731
    with sqlite3.connect(db_path) as db:
        db.executemany(
            "INSERT INTO disputes (dispute_id, source, detected_at, full_fetched, data) VALUES (?, ?, ?, ?, ?)",
            [(d["dispute_id"], d["detected_via"], d["detected_at"], d.get("full_fetched", 1), json.dumps(d["data"]))
             for d in snapshot["disputes"]])
        db.executemany(
            "INSERT INTO decisions (dispute_id, status, decision, risk_score, reasoning, evidence_summary, facts,"
            " graph_context, model, attempts, error, created_at, rules) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r["dispute_id"], r["status"], r.get("decision"), r.get("risk_score"), dump(r.get("reasoning")),
              r.get("evidence_summary"), json.dumps(r.get("facts") or []), dump(r.get("graph_context")), r["model"],
              r["attempts"], r.get("error"), r["created_at"], dump(r.get("rules")))
             for r in snapshot["decisions"]])
        db.executemany("INSERT INTO evidence_drafts (dispute_id, text, updated_at) VALUES (?, ?, ?)",
                       [(r["dispute_id"], r["text"], r["updated_at"]) for r in snapshot["drafts"]])
        db.executemany(
            "INSERT INTO submissions (dispute_id, action, state, text_sha256, attempts, started_at, finished_at,"
            " http_status, paypal_error) VALUES (?,?,?,?,?,?,?,?,?)",
            [(r["dispute_id"], r["action"], r["state"], r["text_sha256"], r["attempts"], r["started_at"],
              r.get("finished_at"), r.get("http_status"), dump(r.get("paypal_error"))) for r in snapshot["submissions"]])
        db.executemany("INSERT INTO verified_behaviours (key, verified_at, dispute_id) VALUES (?, ?, ?)",
                       [(r["key"], r["verified_at"], r["dispute_id"]) for r in snapshot["verified_behaviours"]])
        db.executemany(
            "INSERT INTO merchant_records (record_id, kind, dispute_id, transaction_id, data, source, imported_at,"
            " input_file) VALUES (?,?,?,?,?,?,?,?)",
            [(r["record_id"], r["kind"], r.get("dispute_id"), r.get("transaction_id"), json.dumps(r["data"]),
              r["source"], r["imported_at"], r["input_file"]) for r in snapshot.get("merchant_records", [])])
        if snapshot.get("policy_settings") is not None:
            db.execute("INSERT INTO policy_settings (id, data, updated_at) VALUES (1, ?, ?)",
                       (json.dumps(snapshot["policy_settings"]), snapshot["recorded_at"]))


def read_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
