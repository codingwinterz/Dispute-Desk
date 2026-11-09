"""scripts/export_demo.py --exclude: an excluded dispute appears nowhere in the written snapshot. Mocked store only."""

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from disputedesk.decision import SANDBOX_NOTE, DecisionOutput, DecisionResult
from disputedesk.decision_store import DecisionStore
from disputedesk.demo import EXCLUDED_DISPUTE, export_snapshot, scan
from disputedesk.dispute_store import DisputeStore
from disputedesk.drafts import DraftStore
from disputedesk.merchant_events import MerchantEventStore, parse_event
from disputedesk.merchant_records import MerchantRecordStore
from disputedesk.submissions import SubmissionStore
from tests.fakes import sample_dispute

ROOT = Path(__file__).resolve().parent.parent
KEEP, EXCL = "PP-R-KEP-10000001", "PP-R-EXC-10000009"
KEEP_TXN, EXCL_TXN = "TXNKEEP0000001", "TXNEXCL0000009"
EXCL_BUYER_TXN = f"B-{EXCL_TXN}"


def decision(dispute_id, decision, risk, extra_fact=None, linked=None):
    facts = [{"id": "F1", "text": f"Dispute {dispute_id} reason not received.", "source": "paypal"}]
    if extra_fact:
        facts.append({"id": "F2", "text": extra_fact, "source": "seeded"})
    out = DecisionOutput.model_validate({"decision": decision, "risk_score": risk, "evidence_summary": f"{SANDBOX_NOTE} ok",
        "reasoning": [{"point": f"See {linked or dispute_id}.", "cites": ["F1"]}] * 3})
    ctx = {"dispute": {"id": dispute_id}, "linked": [{"id": linked, "transaction_id": EXCL_TXN, "via": "device_hash"}] if linked else []}
    return DecisionResult("ok", "gemini/test", 1, output=out, facts=facts), ctx


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "store.db"
    d, dec, ev = DisputeStore(path), DecisionStore(path), MerchantEventStore(path)
    drafts, subs, recs = DraftStore(path), SubmissionStore(path), MerchantRecordStore(path)
    d.claim(KEEP, "poll", sample_dispute(KEEP, txn=KEEP_TXN), full=True)
    d.claim(EXCL, "poll", sample_dispute(EXCL, txn=EXCL_TXN), full=True)
    # The excluded dispute has something in every table, including a re-decision after an event.
    dec.save(EXCL, *decision(EXCL, "ACCEPT", 80))
    event = parse_event({"event_id": "EVT-EXC-1", "transaction_id": EXCL_TXN, "kind": "carrier_scan", "status": "DELIVERED",
                         "location": "Denver, CO, US", "time": "2026-11-07T19:42:00Z"})
    ev.add(event, EXCL)
    dec.save(EXCL, *decision(EXCL, "DEFEND", 15), trigger="merchant_event:EVT-EXC-1")
    drafts.save(EXCL, f"{SANDBOX_NOTE} draft for {EXCL}")
    subs.claim(EXCL, "provide_evidence", f"{SANDBOX_NOTE} sent")
    subs.finish(EXCL, "succeeded", 200, None)
    subs.mark_verified("provide_evidence_notes_only", EXCL)
    # The kept dispute mentions the excluded one in a linked-dispute fact, its graph snapshot and reasoning,
    # and in its own history.
    dec.save(KEEP, *decision(KEEP, "ACCEPT", 60, extra_fact=f"Linked dispute {EXCL} shares a device.", linked=EXCL))
    dec.save(KEEP, *decision(KEEP, "DEFEND", 30, extra_fact=f"Linked dispute {EXCL} via {EXCL_TXN}.", linked=EXCL),
             trigger="manual")
    for s in (d, dec, ev, drafts, subs, recs):
        s.close()
    conn = sqlite3.connect(path)
    with conn:
        conn.executemany(
            "INSERT INTO merchant_records (record_id, kind, dispute_id, transaction_id, data, source, imported_at, input_file)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [("SHP-EXC", "shipment", EXCL, EXCL_TXN, json.dumps({"carrier": "UPS"}), "seeded", "2026-11-08", "seed.json"),
             ("SHP-KEP", "shipment", KEEP, KEEP_TXN, json.dumps({"carrier": "USPS"}), "seeded", "2026-11-08", "seed.json"),
             ("PAST-1", "past_dispute", None, "TXNPAST1", json.dumps({"note": f"similar to {EXCL}"}), "seeded", "2026-11-08", "seed.json")])
    conn.close()
    return path


def load_script():
    spec = importlib.util.spec_from_file_location("export_demo_script", ROOT / "scripts" / "export_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(db, tmp_path, monkeypatch, *args):
    mod = load_script()
    monkeypatch.setattr(mod, "DISPUTE_DB", db)
    out = tmp_path / "snap.json"
    return mod.main(["--out", str(out), *args]), out


def test_excluded_dispute_appears_nowhere_in_the_written_file(db, tmp_path, monkeypatch, capsys):
    code, out = run(db, tmp_path, monkeypatch, "--exclude", EXCL)
    assert code == 0
    text = out.read_text(encoding="utf-8")
    for leaked in (EXCL, EXCL_TXN, EXCL_BUYER_TXN, "EVT-EXC-1", "SHP-EXC"):
        assert leaked not in text, leaked
    snap = json.loads(text)
    # Only the kept dispute remains, in every table.
    assert [d["dispute_id"] for d in snap["disputes"]] == [KEEP]
    assert {r["dispute_id"] for r in snap["decisions"] + snap["decision_history"]} == {KEEP}
    assert snap["merchant_events"] == [] and snap["drafts"] == [] and snap["submissions"] == []
    assert snap["verified_behaviours"] == []
    assert sorted(r["record_id"] for r in snap["merchant_records"]) == ["PAST-1", "SHP-KEP"]
    # Mentions in the kept dispute's facts, graph snapshot, reasoning and history become a placeholder.
    current = snap["decisions"][0]
    assert f"Linked dispute {EXCLUDED_DISPUTE}" in current["facts"][1]["text"]
    assert current["graph_context"]["linked"][0]["id"] == EXCLUDED_DISPUTE
    assert snap["decision_history"][0]["record"]["facts"][1]["text"].startswith(f"Linked dispute {EXCLUDED_DISPUTE}")
    printed = capsys.readouterr().out
    assert "1 disputes, 1 decisions, 1 earlier decisions, 0 merchant events, 0 submissions" in printed
    assert f"excluded 1 dispute(s): {EXCL}" in printed


def test_default_export_is_unchanged(db, tmp_path, monkeypatch):
    code, out = run(db, tmp_path, monkeypatch)
    snap = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0 and sorted(d["dispute_id"] for d in snap["disputes"]) == sorted([KEEP, EXCL])
    assert len(snap["merchant_events"]) == 1 and len(snap["submissions"]) == 1 and len(snap["merchant_records"]) == 3
    assert EXCLUDED_DISPUTE not in out.read_text(encoding="utf-8")


def test_exclude_is_repeatable_and_an_unknown_id_writes_nothing(db, tmp_path, monkeypatch, capsys):
    code, out = run(db, tmp_path, monkeypatch, "--exclude", EXCL, "--exclude", KEEP)
    assert code == 0 and json.loads(out.read_text(encoding="utf-8"))["disputes"] == []
    out.unlink()
    code, out = run(db, tmp_path, monkeypatch, "--exclude", "PP-R-TYPO-1")
    assert code == 1 and not out.exists()
    assert "not in the local store: PP-R-TYPO-1" in capsys.readouterr().out


def test_scan_catches_an_excluded_id_left_behind(db):
    snap, mapping = export_snapshot(db, [EXCL])
    assert scan(snap, list(mapping)) == []
    snap["about"] += f" {EXCL}"
    assert "original_identifier" in scan(snap, list(mapping))
