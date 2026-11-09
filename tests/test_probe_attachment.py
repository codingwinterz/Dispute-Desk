"""scripts/probe_attachment.py: host check before the token, no redirects followed, only the allowed fields printed."""

import importlib.util
from pathlib import Path

import httpx
import pytest
import respx

from disputedesk.dispute_store import DisputeStore
from tests.conftest import TOKEN_URL
from tests.fakes import sample_dispute

ROOT = Path(__file__).resolve().parent.parent
DID = "PP-R-TST-1"
TOKEN = "A21AAprobe-token-value-never-printed"
MSG_URL = "https://api-m.sandbox.paypal.com/v2/content/documents/DOC-MSG-111/files/FILE-222/download"
EV_URL = "https://api-m.sandbox.paypal.com/v2/content/documents/DOC-EV-333/files/FILE-444/download"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


def load():
    spec = importlib.util.spec_from_file_location("probe_attachment_script", ROOT / "scripts" / "probe_attachment.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def store(tmp_path, msg_url=MSG_URL, ev_url=EV_URL) -> Path:
    db = tmp_path / "s.db"
    data = sample_dispute(DID)
    data["evidences"] = [{"evidence_type": "OTHER", "source": "SUBMITTED_BY_BUYER",
                          "documents": [{"name": "e.jpeg"} | ({"url": ev_url} if ev_url else {})]}]
    data["messages"] = [{"posted_by": "BUYER", "content": "see photo",
                         "documents": [{"name": "m.jpeg"} | ({"url": msg_url} if msg_url else {})]}]
    s = DisputeStore(db)
    s.claim(DID, "poll", data, full=True)
    s.close()
    return db


@pytest.fixture
def paypal():
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3600}))
        yield router


def run(tmp_path, settings, http, db, capsys):
    code = load().main([DID], http=http, settings=settings, db_path=db)
    return code, capsys.readouterr().out


@pytest.mark.parametrize("bad", [
    "http://api-m.sandbox.paypal.com/v2/content/documents/D/files/F/download",
    "https://api-m.paypal.com/v2/content/documents/D/files/F/download",
    "https://api-m.sandbox.paypal.com.evil.example/v2/x",
    "https://user:pw@api-m.sandbox.paypal.com/v2/x",
    "https://api-m.sandbox.paypal.com:8443/v2/x",
    "https://evil.example/?next=https://api-m.sandbox.paypal.com/",
])
def test_host_check_refuses_before_any_token_or_request(tmp_path, settings, http, paypal, capsys, bad):
    code, out = run(tmp_path, settings, http, store(tmp_path, msg_url=bad), capsys)
    assert code == 1 and out.startswith("REFUSED:") and "nothing was sent" in out
    assert len(paypal.calls) == 0  # not even the token request
    assert bad not in out


def test_redirect_is_not_followed_and_only_its_host_is_printed(tmp_path, settings, http, paypal, capsys):
    target = paypal.get("https://files.example.net/blob/xyz").mock(return_value=httpx.Response(200, content=PNG))
    paypal.get(MSG_URL).mock(return_value=httpx.Response(
        302, headers={"Location": "https://files.example.net/blob/xyz?sig=SECRETSIG"}))
    code, out = run(tmp_path, settings, http, store(tmp_path), capsys)
    assert code == 0 and target.call_count == 0  # one GET only
    assert "status:        302" in out and "location host: files.example.net" in out
    assert "SECRETSIG" not in out and "/blob/" not in out


def test_prints_only_status_type_bytes_and_detected_type(tmp_path, settings, http, paypal, capsys):
    route = paypal.get(MSG_URL).mock(return_value=httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"}))
    code, out = run(tmp_path, settings, http, store(tmp_path), capsys)
    assert code == 0 and route.call_count == 1
    assert route.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"  # attached after the check
    lines = [line.strip() for line in out.strip().splitlines()]
    assert lines[0].startswith("probed the message document of PP-R-TST-1")
    assert lines[1:] == ["status:        200", "content type:  image/png", f"bytes:         {len(PNG)}", "detected type: png"]
    for secret in (TOKEN, MSG_URL, "DOC-MSG-111", "FILE-222", "m.jpeg"):
        assert secret not in out


def test_falls_back_to_the_evidence_document_and_detects_pdf_and_other(tmp_path, settings, http, paypal, capsys):
    paypal.get(EV_URL).mock(side_effect=[httpx.Response(200, content=b"%PDF-1.7 ..."),
                                         httpx.Response(200, content=b"<html>login</html>", headers={"Content-Type": "text/html"})])
    db = store(tmp_path, msg_url=None)
    code, out = run(tmp_path, settings, http, db, capsys)
    assert code == 0 and "evidence document" in out and "detected type: pdf" in out
    code, out = run(tmp_path, settings, http, db, capsys)
    assert "detected type: other" in out and "content type:  text/html" in out and "<html>" not in out


def test_no_stored_link_or_unknown_dispute_sends_nothing(tmp_path, settings, http, paypal, capsys):
    code, out = run(tmp_path, settings, http, store(tmp_path, msg_url=None, ev_url=None), capsys)
    assert code == 1 and "no message or evidence document" in out and len(paypal.calls) == 0
    code = load().main(["PP-R-NOPE"], http=http, settings=settings, db_path=tmp_path / "s.db")
    assert code == 1 and len(paypal.calls) == 0


def test_request_errors_print_the_type_only(tmp_path, settings, http, paypal, capsys):
    paypal.get(MSG_URL).mock(side_effect=httpx.ConnectError(f"failed {MSG_URL}"))
    code, out = run(tmp_path, settings, http, store(tmp_path), capsys)
    assert code == 1 and "ConnectError" in out and MSG_URL not in out


def test_script_never_writes_the_file_or_uses_a_logging_client():
    src = (ROOT / "scripts" / "probe_attachment.py").read_text(encoding="utf-8")
    assert "write_bytes" not in src and '"wb"' not in src and "open(" not in src
    assert "make_client" not in src  # the app's logging client would record the link in logs/*.jsonl
    assert "follow_redirects=False" in src
    assert "/provide-evidence" not in src and "accept-claim" not in src and ".post(" not in src
