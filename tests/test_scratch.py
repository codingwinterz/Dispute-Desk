"""Scratch mode: a copy of the store and its own graph. FalkorDB and the LLM are mocked; no PayPal call."""

import hashlib
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient

from disputedesk import config
from disputedesk.config import DISPUTE_DB, GRAPH_NAME, AgentSettings, load_agent_settings, targets
from disputedesk.decision import DecisionOutput, DecisionResult
from disputedesk.decision_store import DecisionStore
from disputedesk.dispute_store import DisputeStore
from disputedesk.graph import GraphStore, connect
from disputedesk.merchant_events import MerchantEventStore
from disputedesk.webhooks import EventStore, create_app
from tests.fakes import FakeGraph, context_row, sample_dispute

ROOT = Path(__file__).resolve().parent.parent
YOV = "PP-R-YOV-10196864"
YOV_TXN = "5GU29781PX414531M"  # the transaction the sample event names (seed/events/yov_delivery_scan.json)
YOV_EVENT = ROOT / "seed" / "events" / "yov_delivery_scan.json"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_script", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def result(decision="ACCEPT", risk=80):
    out = DecisionOutput.model_validate({"decision": decision, "risk_score": risk, "evidence_summary": "summary",
                                         "reasoning": [{"point": f"{decision} because F1.", "cites": ["F1"]}] * 3})
    return DecisionResult("ok", "test-model", 1, output=out, facts=[{"id": "F1", "text": "x", "source": "paypal"}])


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def real_db(tmp_path):
    """Stands in for the real store: the YOV dispute with one decision."""
    path = tmp_path / "real.db"
    d, dec = DisputeStore(path), DecisionStore(path)
    d.claim(YOV, "poll", sample_dispute(YOV, txn=YOV_TXN), full=True)
    dec.save(YOV, result("ACCEPT", 80), {"before": True})
    d.close()
    dec.close()
    return path


def scratch_env(monkeypatch, db: Path, graph: str = "disputedesk_test_scratch") -> None:
    monkeypatch.setenv("DISPUTEDESK_DB", str(db))
    monkeypatch.setenv("DISPUTEDESK_GRAPH", graph)


# --- selection ---------------------------------------------------------------------------------

def test_defaults_are_the_real_store_and_graph():
    t = targets()
    assert (t.db, t.graph, t.scratch) == (DISPUTE_DB, GRAPH_NAME, False)
    assert AgentSettings().graph_name == GRAPH_NAME


def test_both_variables_select_scratch_and_a_relative_path_is_under_the_project(monkeypatch):
    scratch_env(monkeypatch, Path("data/scratch.db"), "dd_scratch")
    t = targets()
    assert t.scratch and t.graph == "dd_scratch" and t.db == config.PROJECT_ROOT / "data" / "scratch.db"


@pytest.mark.parametrize("db,graph,message", [
    ("data/scratch.db", None, "only one is set"),
    (None, "dd_scratch", "only one is set"),
    (str(DISPUTE_DB), "dd_scratch", "real database"),
    ("data/../data/disputedesk.db", "dd_scratch", "real database"),
    ("data/scratch.db", GRAPH_NAME, "real graph"),
    ("data/scratch.db", "bad name;DROP", "letters, digits"),
])
def test_unsafe_or_half_settings_are_refused(monkeypatch, db, graph, message):
    if db:
        monkeypatch.setenv("DISPUTEDESK_DB", db)
    if graph:
        monkeypatch.setenv("DISPUTEDESK_GRAPH", graph)
    with pytest.raises(RuntimeError, match=message):
        targets()


def test_scratch_variables_in_env_file_are_refused(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("DISPUTEDESK_GRAPH=dd_scratch\nDISPUTEDESK_DB=data/scratch.db\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="can't be set in .env"):
        load_agent_settings(env)
    assert targets().scratch is False  # nothing leaked into the process


def test_agent_settings_and_connect_use_the_scratch_graph(tmp_path, monkeypatch):
    scratch_env(monkeypatch, tmp_path / "s.db", "dd_scratch")
    settings = load_agent_settings(tmp_path / "missing.env")
    assert settings.graph_name == "dd_scratch"
    selected = []
    fake = types.ModuleType("falkordb")
    fake.FalkorDB = lambda **_kw: types.SimpleNamespace(select_graph=lambda name: selected.append(name) or FakeGraph())
    monkeypatch.setitem(sys.modules, "falkordb", fake)
    connect(settings)
    assert selected == ["dd_scratch"]


# --- scripts/scratch.py ---------------------------------------------------------------------------

def test_create_copies_read_only_and_seeds_the_scratch_graph_the_same_way(real_db, tmp_path, capsys):
    events = MerchantEventStore(real_db)  # one event already in the source store: replayed into the graph
    from disputedesk.merchant_events import parse_event_file
    events.add(parse_event_file(YOV_EVENT), YOV)
    events.close()
    before = digest(real_db)
    graph = FakeGraph()
    dest = tmp_path / "scratch.db"

    code = load("scratch").main(["create", "--db", str(dest), "--graph", "dd_scratch"],
                                graph_factory=lambda: GraphStore(graph), source=real_db)

    assert code == 0 and digest(real_db) == before  # the real store is only read
    assert graph.deleted  # the scratch graph starts empty
    queries = " ".join(q for q, _ in graph.calls)
    assert "Dispute" in queries and "Shipment" in queries and "HAS_EVENT" in queries
    copy, dec = DisputeStore(dest), DecisionStore(dest)
    assert copy.source_of(YOV) == "poll" and dec.get(YOV)["decision"] == "ACCEPT"
    copy.close()
    dec.close()
    out = capsys.readouterr().out
    assert f"seeded {YOV}" in out and "1 merchant event(s)" in out
    assert f'$env:DISPUTEDESK_DB = "{dest}"' in out and "DISPUTEDESK_GRAPH=dd_scratch" in out  # usable as printed


def test_create_refuses_an_existing_copy_and_reset_recreates_it(real_db, tmp_path):
    dest = tmp_path / "scratch.db"
    scratch = load("scratch")
    run = lambda cmd: scratch.main([cmd, "--db", str(dest), "--graph", "dd_scratch"],  # noqa: E731
                                   graph_factory=lambda: GraphStore(FakeGraph()), source=real_db)
    assert run("create") == 0
    db = sqlite3.connect(dest)  # a rehearsal changed the copy
    with db:
        db.execute("DELETE FROM decisions")
    db.close()
    assert run("create") == 1
    assert run("reset") == 0
    dec = DecisionStore(dest)
    assert dec.get(YOV)["decision"] == "ACCEPT"  # back to a fresh copy of the real store
    dec.close()


@pytest.mark.parametrize("args", [["--graph", GRAPH_NAME], ["--db", str(DISPUTE_DB)]])
def test_scratch_script_never_targets_the_real_store_or_graph(real_db, tmp_path, args, capsys):
    graph = FakeGraph()
    argv = ["reset", "--db", str(tmp_path / "s.db"), "--graph", "dd_scratch"] + args
    code = load("scratch").main(argv, graph_factory=lambda: GraphStore(graph), source=real_db)
    assert code == 1 and not graph.deleted and not graph.calls
    assert "FAIL" in capsys.readouterr().out


# --- the other scripts and the app follow the scratch selection -------------------------------------

def test_merchant_event_in_scratch_mode_writes_only_the_copy(real_db, tmp_path, monkeypatch, capsys):
    dest = tmp_path / "scratch.db"
    scratch = load("scratch")
    assert scratch.main(["create", "--db", str(dest), "--graph", "dd_scratch"],
                        graph_factory=lambda: GraphStore(FakeGraph()), source=real_db) == 0
    before = digest(real_db)
    scratch_env(monkeypatch, dest, "dd_scratch")
    script = load("merchant_event")
    monkeypatch.setattr(script, "DISPUTE_DB", real_db)  # the "real" store this run must not touch
    graph = FakeGraph([context_row()])
    code = script.main(["--file", str(YOV_EVENT)], graph_factory=lambda: GraphStore(graph),
                       decide_fn=lambda *_a: result("ACCEPT", 70), model="test-model")
    assert code == 0 and "scratch mode: database scratch.db, graph dd_scratch" in capsys.readouterr().out
    assert digest(real_db) == before
    assert MerchantEventStore(real_db).for_dispute(YOV) == []
    assert [e["event_id"] for e in MerchantEventStore(dest).for_dispute(YOV)] == ["EVT-YOV-SCAN-1"]
    assert DecisionStore(dest).get(YOV)["trigger"] == "merchant_event:EVT-YOV-SCAN-1"


@pytest.mark.parametrize("name", ["analyze", "seed_graph", "import_merchant_records", "merchant_event"])
def test_every_store_script_resolves_the_scratch_copy(name, tmp_path, monkeypatch):
    mod = load(name)
    assert mod.store_path() == DISPUTE_DB
    scratch_env(monkeypatch, tmp_path / "s.db")
    assert mod.store_path() == tmp_path / "s.db"


def test_app_in_scratch_mode_uses_the_copy_and_says_so(tmp_path, monkeypatch):
    dest = tmp_path / "scratch.db"
    scratch_env(monkeypatch, dest, "dd_scratch")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        app = create_app(verifier=object(), store=EventStore(tmp_path / "logs"))
        mode = TestClient(app).get("/api/mode").json()
    assert app.state.dispute_store.path == dest
    assert mode == {"demo": False, "scratch": True, "scratch_db": "scratch.db", "scratch_graph": "dd_scratch"}
    assert len(router.calls) == 0


def test_app_refuses_to_start_on_a_half_scratch_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPUTEDESK_GRAPH", "dd_scratch")
    with pytest.raises(SystemExit):
        create_app(verifier=object(), store=EventStore(tmp_path / "logs"))


def test_scratch_badge_in_the_top_bar():
    common = (ROOT / "disputedesk" / "web" / "static" / "common.js").read_text(encoding="utf-8")
    css = (ROOT / "disputedesk" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    assert 'class: "scratch-badge"' in common and 'text: "Scratch data"' in common and "m.scratch" in common
    assert ".scratch-badge {" in css


def test_scratch_files_are_git_ignored():
    import subprocess
    assert subprocess.run(["git", "check-ignore", "-q", "data/scratch.db"], cwd=ROOT).returncode == 0


def test_export_script_reads_the_scratch_copy(real_db, tmp_path, monkeypatch, capsys):
    dest = tmp_path / "scratch.db"
    assert load("scratch").main(["create", "--db", str(dest), "--graph", "dd_scratch"],
                                graph_factory=lambda: GraphStore(FakeGraph()), source=real_db) == 0
    scratch_env(monkeypatch, dest, "dd_scratch")
    out_file = tmp_path / "snap.json"
    assert load("export_demo").main(["--out", str(out_file)]) == 0
    snap = json.loads(out_file.read_text(encoding="utf-8"))
    assert [d["dispute_id"] for d in snap["disputes"]] == [YOV]
    assert "scratch mode" in capsys.readouterr().out
