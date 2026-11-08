"""Merchant records: imported from a JSON file, shown with source tags, available in demo mode."""

import importlib.util
import json
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient

from disputedesk import webhooks
from disputedesk.demo import export_snapshot, scan
from disputedesk.dispute_store import DisputeStore
from disputedesk.merchant_records import NOTE, MerchantRecordStore, RecordsError, import_records, parse_file, records_view
from tests.fakes import FakeGraph, sample_dispute

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "seed" / "merchant_data.json"
REAL_IDS = ("PP-R-JTQ-10195324", "PP-R-YOV-10196864", "PP-R-QQW-10196399", "PP-R-IQA-10196863",
            "PP-R-CTG-10197156", "PP-R-WZR-10197157")


def stored(missing=()):
    return {i: sample_dispute(i, txn=f"TXN-{i[-5:]}") for i in REAL_IDS if i not in missing}


def test_example_seed_file_validates():
    parsed = parse_file(SEED)
    assert set(parsed.disputes) == set(REAL_IDS) and len(parsed.past_disputes) == 3


@pytest.mark.parametrize("content,expected", [
    ("{not json", "not valid JSON"),
    ('{"disputes": {"PP-R-X": {"shipment": {"id": "S1"}}}}', "disputes.PP-R-X.shipment.carrier: Field required"),
    ('{"disputes": {}, "surprise": 1}', "surprise: Extra inputs are not permitted"),
])
def test_bad_files_explain_each_problem(tmp_path, content, expected):
    path = tmp_path / "records.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(RecordsError) as exc:
        parse_file(path)
    assert any(expected in p for p in exc.value.problems)


def test_import_takes_transaction_ids_from_dispute_json_and_reports_unknowns():
    rows, report = import_records(parse_file(SEED), stored(missing=("PP-R-YOV-10196864",)), "merchant_data.json")
    shipments = {r["dispute_id"]: r for r in rows if r["kind"] == "shipment"}
    assert shipments["PP-R-JTQ-10195324"]["transaction_id"] == "TXN-95324"
    assert "PP-R-YOV-10196864" not in shipments
    assert any(line.startswith("skipped PP-R-YOV-10196864") for line in report)
    assert all(r["source"] == "seeded" for r in rows)
    assert {r["kind"] for r in rows} == {"shipment", "telemetry", "past_dispute"}


def test_store_and_view_group_records_by_transaction(tmp_path):
    store = MerchantRecordStore(tmp_path / "x.db")
    rows, _ = import_records(parse_file(SEED), stored(), "merchant_data.json")
    store.replace_all(rows)
    store.replace_all(rows)  # re-import replaces, never duplicates
    view = records_view(store.rows(), stored())
    assert view["note"] == NOTE and view["input_file"] == "merchant_data.json"
    assert len(view["transactions"]) == 6 and len(view["past_disputes"]) == 3
    qqw = next(t for t in view["transactions"] if t["dispute_id"] == "PP-R-QQW-10196399")
    assert qqw["transaction_source"] == "paypal" and qqw["shipment"]["source"] == "seeded"
    assert qqw["telemetry"]["device_hash"] == "dev-9e0d55" and qqw["dispute"]["source"] == "paypal"
    store.close()


def load_script():
    spec = importlib.util.spec_from_file_location("import_merchant_records", ROOT / "scripts" / "import_merchant_records.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script_db(tmp_path, monkeypatch):
    db = tmp_path / "x.db"
    d = DisputeStore(db)
    for i, data in stored().items():
        d.claim(i, "poll", data, full=True)
    d.close()
    script = load_script()
    monkeypatch.setattr(script, "DISPUTE_DB", db)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:  # no PayPal (or any HTTP) call
        yield script, db, router


def test_script_imports_without_graph(script_db, monkeypatch, capsys):
    script, db, router = script_db
    monkeypatch.setattr("sys.argv", ["import", "--file", str(SEED), "--no-graph"])
    assert script.main() == 0
    out = capsys.readouterr().out
    assert "stored 15 record(s)" in out and "graph not updated" in out
    assert len(MerchantRecordStore(db).rows()) == 15 and router.calls.call_count == 0


def test_script_seeds_the_graph_from_the_same_file(script_db, monkeypatch, capsys):
    script, db, _ = script_db
    fake = FakeGraph()
    monkeypatch.setattr("disputedesk.graph.connect", lambda _s: fake)
    monkeypatch.setattr("sys.argv", ["import", "--file", str(SEED)])
    assert script.main() == 0
    assert "graph: seeded 3 past dispute(s)" in capsys.readouterr().out
    assert any("Shipment" in q for q, _ in fake.calls)


def test_script_reports_a_stopped_graph_with_the_fix(script_db, monkeypatch, capsys):
    script, db, _ = script_db
    def down(_s):
        raise ConnectionError("refused")
    monkeypatch.setattr("disputedesk.graph.connect", down)
    monkeypatch.setattr("sys.argv", ["import", "--file", str(SEED)])
    assert script.main() == 1
    out = capsys.readouterr().out
    assert "records were stored" in out and "docker compose up -d falkordb" in out
    assert len(MerchantRecordStore(db).rows()) == 15  # still stored for the page


def test_script_rejects_a_bad_file(script_db, tmp_path, monkeypatch, capsys):
    script, _, _ = script_db
    bad = tmp_path / "bad.json"
    bad.write_text('{"disputes": {"X": {"telemetry": {"id": "T"}}}}', encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["import", "--file", str(bad)])
    assert script.main() == 1
    out = capsys.readouterr().out
    assert "was not imported" in out and "disputes.X.telemetry.ip: Field required" in out and "Fix:" in out


def test_api_and_page(tmp_path):
    db = tmp_path / "x.db"
    d = DisputeStore(db)
    for i, data in stored().items():
        d.claim(i, "poll", data, full=True)
    rows, _ = import_records(parse_file(SEED), stored(), "merchant_data.json")
    MerchantRecordStore(db).replace_all(rows)
    client = TestClient(webhooks.create_app(verifier=object(), store=webhooks.EventStore(tmp_path / "l"), dispute_store=d))
    body = client.get("/api/merchant-records").json()
    assert body["note"] == "In production your store supplies this; in the sandbox it is simulated."
    assert len(body["transactions"]) == 6
    page = client.get("/merchant-records")
    assert page.status_code == 200 and "In production your store supplies this; in the sandbox it is simulated." in page.text
    assert client.get("/merchant-records", headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 403
    assert client.get("/api/merchant-records", headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 403
    assert '["records", "/merchant-records", "Merchant records"]' in client.get("/static/common.js").text


def test_empty_records_view():
    view = records_view([], {})
    assert view["transactions"] == [] and view["past_disputes"] == [] and view["note"] == NOTE


def test_demo_snapshot_carries_merchant_records(tmp_path, monkeypatch):
    db = tmp_path / "src.db"
    d = DisputeStore(db)
    for i, data in stored().items():
        d.claim(i, "poll", data, full=True)
    d.close()
    from disputedesk.decision_store import DecisionStore
    from disputedesk.submissions import SubmissionStore
    DecisionStore(db).close(); SubmissionStore(db).close()
    rows, _ = import_records(parse_file(SEED), stored(), "merchant_data.json")
    MerchantRecordStore(db).replace_all(rows)
    snap, mapping = export_snapshot(db)
    assert len(snap["merchant_records"]) == 15 and scan(snap, list(mapping)) == []
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    monkeypatch.setenv("DEMO_SNAPSHOT", str(path))
    with TestClient(webhooks.create_app(demo=True)) as client:
        body = client.get("/api/merchant-records").json()
        assert len(body["transactions"]) == 6 and len(body["past_disputes"]) == 3
