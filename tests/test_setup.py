"""Setup failures come back as one clear line with the fix, not a stack trace."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from disputedesk import config, webhooks

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


start = load("start")
preflight = load("preflight")
GOOD_ENV = "PAYPAL_CLIENT_ID=a\nPAYPAL_CLIENT_SECRET=b\nPAYPAL_WEBHOOK_ID=c\nGEMINI_API_KEY=d\n"


def pf_with_graph(up: bool):
    """The real preflight checks, with the FalkorDB check stubbed (no socket)."""
    return SimpleNamespace(**{k: getattr(preflight, k) for k in ("check_exists", "check_encoding", "check_parse",
                                                               "read_values", "REQUIRED_VARS")},
                           check_falkordb=lambda: preflight.Result("PASS" if up else "FAIL", "x"))


def test_missing_env_file_says_how_to_create_it(tmp_path):
    blocking, _ = start.problems(env=tmp_path / ".env", pf=pf_with_graph(True))
    assert blocking == [("There is no .env file.", start.FIXES["env"])]
    assert "copy .env.example .env" in start.FIXES["env"]


def test_missing_key_names_the_variable_and_where_to_get_it(tmp_path):
    env = tmp_path / ".env"
    env.write_text(GOOD_ENV.replace("GEMINI_API_KEY=d\n", ""), encoding="utf-8")
    blocking, _ = start.problems(env=env, pf=pf_with_graph(False))
    messages = dict(blocking)
    assert messages["GEMINI_API_KEY is missing from .env."] == start.FIXES["GEMINI_API_KEY"]
    assert messages["The graph database (FalkorDB) is not running on localhost:6379."] == "docker compose up -d falkordb"


def test_placeholder_webhook_id_is_a_warning_not_a_blocker(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(GOOD_ENV.replace("PAYPAL_WEBHOOK_ID=c", "PAYPAL_WEBHOOK_ID=placeholder"), encoding="utf-8")
    monkeypatch.setattr("disputedesk.graph.connect", lambda _s: SimpleNamespace(query=lambda q: SimpleNamespace(result_set=[[5]])))
    blocking, warnings = start.problems(env=env, pf=pf_with_graph(True))
    assert blocking == [] and any("placeholder" in m for m, _ in warnings)


def test_main_prints_fixes_and_never_starts_on_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(start, "problems", lambda: ([("There is no .env file.", start.FIXES["env"])], []))
    monkeypatch.setattr("sys.argv", ["start.py"])
    assert start.main() == 1
    out = capsys.readouterr().out
    assert "FAIL  There is no .env file." in out and "Fix: copy .env.example .env" in out
    assert "python scripts/demo.py" in out


def test_load_settings_explains_a_missing_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False)
    with pytest.raises(RuntimeError) as exc:
        config.load_settings(tmp_path / ".env")
    assert "copy .env.example .env" in str(exc.value) and "scripts/demo.py" in str(exc.value)


def test_app_without_env_exits_with_one_line_not_a_traceback(monkeypatch, capsys):
    def no_env():
        raise RuntimeError("there is no .env file. Fix: copy .env.example .env and fill it in")
    monkeypatch.setattr(webhooks, "load_settings", no_env)
    with pytest.raises(SystemExit) as exc:
        webhooks.create_app(demo=False)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.strip() == "DisputeDesk can't start: there is no .env file. Fix: copy .env.example .env and fill it in"
    assert "Traceback" not in err


def test_compose_pins_falkordb_and_binds_locally():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "image: falkordb/falkordb:6.0.3" in text and ":latest" not in text
    assert '"127.0.0.1:6379:6379"' in text
