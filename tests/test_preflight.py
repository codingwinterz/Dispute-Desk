import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "preflight", Path(__file__).resolve().parent.parent / "scripts" / "preflight.py"
)
preflight = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(preflight)

SECRET = "s3cr3t-value-never-printed"
GOOD_ENV = (
    f"PAYPAL_CLIENT_ID=client-{SECRET}\n"
    f"PAYPAL_CLIENT_SECRET={SECRET}\n"
    "PAYPAL_WEBHOOK_ID=WH-123\n"
    f"GEMINI_API_KEY=gem-{SECRET}\n"
)


def write(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / ".env"
    path.write_bytes(data)
    return path


class FakeSocket:
    def __init__(self, reply: bytes):
        self.reply = reply
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, _n: int) -> bytes:
        return self.reply


@pytest.fixture
def falkor_ok(monkeypatch):
    sock = FakeSocket(b"*3\r\n*1\r\n$1\r\n1\r\n*1\r\n*1\r\n:1\r\n*0\r\n")
    monkeypatch.setattr(preflight.socket, "create_connection", lambda *a, **k: sock)
    return sock


def test_encoding_detects_boms(tmp_path):
    assert preflight.check_encoding(write(tmp_path, GOOD_ENV.encode())).verdict == "PASS"
    assert preflight.check_encoding(write(tmp_path, b"\xef\xbb\xbf" + GOOD_ENV.encode())).verdict == "WARN"
    assert preflight.check_encoding(write(tmp_path, GOOD_ENV.encode("utf-16"))).verdict == "FAIL"


def test_parse_reports_line_numbers_only(tmp_path):
    env = write(tmp_path, f"A=1\nnot valid {SECRET}\nB=2\n".encode())
    result = preflight.check_parse(env)
    assert result.verdict == "FAIL"
    assert "2" in result.reason
    assert SECRET not in result.reason


def test_vars_report_length_not_value():
    result, lines = preflight.check_vars({
        "PAYPAL_CLIENT_ID": SECRET, "PAYPAL_CLIENT_SECRET": SECRET,
        "PAYPAL_WEBHOOK_ID": "placeholder", "GEMINI_API_KEY": "",
    })
    assert result.verdict == "FAIL" and "GEMINI_API_KEY" in result.reason
    assert f"PAYPAL_CLIENT_SECRET: set (length {len(SECRET)})" in lines
    assert "GEMINI_API_KEY: missing" in lines
    assert any("placeholder" in line for line in lines)
    assert all(SECRET not in line for line in lines)


def test_placeholder_webhook_warns():
    result, _ = preflight.check_vars({n: "x" for n in preflight.REQUIRED_VARS} | {"PAYPAL_WEBHOOK_ID": "placeholder"})
    assert result.verdict == "WARN"


def test_falkordb_pass_sends_graph_query(falkor_ok):
    assert preflight.check_falkordb().verdict == "PASS"
    assert b"GRAPH.QUERY" in falkor_ok.sent and b"RETURN 1" in falkor_ok.sent


def test_falkordb_unreachable(monkeypatch):
    def refuse(*a, **k):
        raise ConnectionRefusedError
    monkeypatch.setattr(preflight.socket, "create_connection", refuse)
    assert preflight.check_falkordb().verdict == "FAIL"


def test_falkordb_without_module(monkeypatch):
    sock = FakeSocket(b"-ERR unknown command 'GRAPH.QUERY'\r\n")
    monkeypatch.setattr(preflight.socket, "create_connection", lambda *a, **k: sock)
    assert preflight.check_falkordb().verdict == "FAIL"


def test_sandbox_only_flags_live_url_by_name(tmp_path):
    (tmp_path / "disputedesk").mkdir()
    (tmp_path / "disputedesk" / "x.py").write_text(f'URL = "{preflight.SANDBOX_BASE_URL}"\n')
    ok = preflight.check_sandbox_only({"OTHER": "1"}, tmp_path)
    assert ok.verdict == "PASS"

    live = "https://api-m." + "paypal.com"
    bad = preflight.check_sandbox_only({"PAYPAL_BASE_URL": live}, tmp_path)
    assert bad.verdict == "FAIL" and "PAYPAL_BASE_URL" in bad.reason and live not in bad.reason

    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "y.py").write_text(f'URL = "{live}"\n')
    assert "scripts/y.py" in preflight.check_sandbox_only({}, tmp_path).reason


def test_real_repo_code_is_sandbox_only():
    assert preflight.check_sandbox_only({}).verdict == "PASS"


def test_run_end_to_end_never_prints_secrets(tmp_path, capsys, falkor_ok):
    env = write(tmp_path, GOOD_ENV.encode())
    assert preflight.run(env) == 0
    out = capsys.readouterr().out
    assert SECRET not in out
    assert out.count("[PASS]") == 6


def test_run_without_env_fails(tmp_path, capsys, falkor_ok):
    assert preflight.run(tmp_path / ".env") == 1
    assert "not found" in capsys.readouterr().out
