"""Buyer attachments: listed by index, fetched server-side with checks, served only as JPEG/PNG/GIF. Mocked HTTP."""

from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from disputedesk.attachments import MAX_BYTES, AttachmentFetcher, buyer_documents, listing
from disputedesk.auth import TokenProvider
from disputedesk.dispute_store import DisputeStore
from disputedesk.webhooks import EventStore, create_app
from tests.conftest import TOKEN_URL
from tests.fakes import sample_dispute
from tests.test_demo import demo_client, no_outside_access, snapshot_file  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parent.parent
DID = "PP-R-ATT-1"
TOKEN = "A21AAattachment-token-never-leaves"
BASE = "https://api-m.sandbox.paypal.com/v2/content/documents"
EV_URL = f"{BASE}/DOC-EV-1/files/F-1/download"
MSG_URL = f"{BASE}/DOC-MSG-2/files/F-2/download"
SELLER_URL = f"{BASE}/DOC-SELLER/files/F-9/download"
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64


def dispute(ev_url=EV_URL, msg_url=MSG_URL):
    data = sample_dispute(DID)
    data["evidences"] = [
        {"evidence_type": "OTHER", "source": "SUBMITTED_BY_BUYER", "documents": [{"name": "photo-ev.jpeg", "url": ev_url}]},
        {"evidence_type": "OTHER", "source": "SUBMITTED_BY_BUYER", "documents": [{"name": "no-link.jpeg"}]},
        {"evidence_type": "PROOF_OF_SHIPMENT", "source": "SUBMITTED_BY_SELLER", "documents": [{"name": "label.pdf", "url": SELLER_URL}]},
    ]
    data["messages"] = [
        {"posted_by": "BUYER", "content": "photos", "documents": [{"name": "photo-msg.jpeg", "url": msg_url},
                                                                   {"name": "photo-ev.jpeg", "url": ev_url}]},  # repeat
    ]
    return data


@pytest.fixture
def paypal():
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3600}))
        yield router


def make_client(tmp_path, settings, http, data=None):
    store = DisputeStore(tmp_path / "a.db")
    store.claim(DID, "poll", data or dispute(), full=True)
    fetcher = AttachmentFetcher(TokenProvider(settings, http).auth_headers, http)
    app = create_app(verifier=object(), store=EventStore(tmp_path / "logs"), dispute_store=store, attachments=fetcher)
    return TestClient(app)


@pytest.fixture
def client(tmp_path, settings, http, paypal):
    return make_client(tmp_path, settings, http)


# --- listing ---------------------------------------------------------------------------------

def test_listing_has_indexes_only_buyer_documents_once_each():
    assert buyer_documents(dispute()) == [EV_URL, None, MSG_URL]  # seller document left out, repeat merged
    assert listing(dispute()) == [{"index": 0, "available": True}, {"index": 1, "available": False},
                                  {"index": 2, "available": True}]
    assert listing({"dispute_id": "x"}) == [] and listing(None) == []


def test_case_response_carries_no_link_name_or_token(client, paypal):
    body = client.get(f"/api/decisions/{DID}")
    assert body.json()["attachments"] == listing(dispute())
    for secret in (TOKEN, "api-m.sandbox.paypal.com/v2", "DOC-EV-1", "photo-ev", "no-link", "label.pdf"):
        assert secret not in body.text, secret
    assert len(paypal.calls) == 0  # listing never calls PayPal


# --- allowed and rejected types ------------------------------------------------------------------

@pytest.mark.parametrize("payload,ctype", [(JPG, "image/jpeg"), (PNG, "image/png"), (GIF, "image/gif")])
def test_allowed_images_are_served_with_safe_headers(client, paypal, payload, ctype):
    route = paypal.get(EV_URL).mock(return_value=httpx.Response(200, content=payload, headers={"Content-Type": "text/html"}))
    resp = client.get(f"/api/attachments/{DID}/0")
    assert resp.status_code == 200 and resp.content == payload
    assert resp.headers["content-type"] == ctype  # from the file's own bytes, not PayPal's header
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-security-policy"] == "default-src 'none'"
    assert resp.headers["cache-control"] == "no-store"
    assert route.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(resp.headers) and "paypal.com" not in str(resp.headers)


@pytest.mark.parametrize("payload", [
    b"%PDF-1.7\n...",
    b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
    b"<?xml version='1.0'?><svg/>",
    b"<html><body>login</body></html>",
    b"",
])
def test_anything_but_jpeg_png_gif_is_rejected(client, paypal, payload):
    paypal.get(EV_URL).mock(return_value=httpx.Response(200, content=payload, headers={"Content-Type": "image/png"}))
    resp = client.get(f"/api/attachments/{DID}/0")
    assert resp.status_code == 415 and resp.json()["error"] == "unsupported_type"
    assert resp.headers["content-type"].startswith("application/json")  # never served as the file
    assert "<script" not in resp.text and "<svg" not in resp.text and "%PDF" not in resp.text


# --- size cap ---------------------------------------------------------------------------------------

def test_declared_size_over_5_mb_is_refused(client, paypal):
    paypal.get(EV_URL).mock(return_value=httpx.Response(200, content=PNG, headers={"Content-Length": str(MAX_BYTES + 1)}))
    resp = client.get(f"/api/attachments/{DID}/0")
    assert resp.status_code == 502 and resp.json()["error"] == "too_large"


def test_streamed_size_over_5_mb_is_refused_even_without_a_header(client, paypal):
    big = PNG + b"\x00" * MAX_BYTES  # one chunk stream with no Content-Length
    paypal.get(EV_URL).mock(return_value=httpx.Response(200, stream=httpx.ByteStream(big)))
    resp = client.get(f"/api/attachments/{DID}/0")
    assert resp.status_code == 502 and resp.json()["error"] == "too_large"


def test_exactly_5_mb_is_allowed(client, paypal):
    paypal.get(EV_URL).mock(return_value=httpx.Response(200, content=PNG + b"\x00" * (MAX_BYTES - len(PNG))))
    assert client.get(f"/api/attachments/{DID}/0").status_code == 200


# --- host check, redirects, missing links ----------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "http://api-m.sandbox.paypal.com/v2/content/documents/D/files/F/download",
    "https://api-m.paypal.com/v2/content/documents/D/files/F/download",
    "https://api-m.sandbox.paypal.com.evil.example/x",
    "https://user:pw@api-m.sandbox.paypal.com/x",
    "https://api-m.sandbox.paypal.com:8443/x",
    "https://evil.example/?u=https://api-m.sandbox.paypal.com/",
])
def test_host_check_refuses_before_the_token(tmp_path, settings, http, paypal, bad):
    client = make_client(tmp_path, settings, http, dispute(ev_url=bad))
    resp = client.get(f"/api/attachments/{DID}/0")
    assert resp.status_code == 403 and resp.json()["error"] == "refused"
    assert len(paypal.calls) == 0  # not even the token request
    assert bad not in resp.text


def test_redirects_are_not_followed(client, paypal):
    target = paypal.get("https://files.example.net/blob").mock(return_value=httpx.Response(200, content=PNG))
    paypal.get(EV_URL).mock(return_value=httpx.Response(302, headers={"Location": "https://files.example.net/blob?sig=S"}))
    resp = client.get(f"/api/attachments/{DID}/0")
    assert resp.status_code == 502 and resp.json()["error"] == "redirect"
    assert target.call_count == 0 and "files.example.net" not in resp.text


def test_missing_link_index_or_dispute(client, paypal):
    resp = client.get(f"/api/attachments/{DID}/1")
    assert resp.status_code == 404 and resp.json() == {"error": "not_available", "message": "Buyer attachment, not available to download."}
    assert client.get(f"/api/attachments/{DID}/3").status_code == 404
    assert client.get(f"/api/attachments/{DID}/-1").status_code == 404
    assert client.get("/api/attachments/PP-R-NOPE/0").status_code == 404
    assert client.get(f"/api/attachments/{DID}/x").status_code == 422  # an index, never a name or URL
    assert len(paypal.calls) == 0


def test_upstream_errors_never_echo_the_link_or_token(client, paypal):
    paypal.get(EV_URL).mock(side_effect=[httpx.Response(403, text=f"denied {EV_URL} {TOKEN}"),
                                         httpx.ConnectError(f"failed {EV_URL}")])
    for expected in ("upstream", "unreachable"):
        resp = client.get(f"/api/attachments/{DID}/0")
        assert resp.status_code == 502 and resp.json()["error"] == expected
        assert EV_URL not in resp.text and TOKEN not in resp.text and "DOC-EV-1" not in resp.text


def test_route_is_local_only(client, paypal):
    resp = client.get(f"/api/attachments/{DID}/0", headers={"X-Forwarded-For": "203.0.113.9"})
    assert resp.status_code == 403 and len(paypal.calls) == 0


# --- demo mode, the agent, the page -------------------------------------------------------------------

def test_demo_mode_lists_nothing_and_fetches_nothing(demo_client):  # noqa: F811
    client, router = demo_client
    assert client.get("/api/decisions/PP-R-TST-1").json()["attachments"] == []
    assert client.get("/api/attachments/PP-R-TST-1/0").status_code == 404
    assert len(router.calls) == 0


def test_the_decision_step_never_sees_attachments():
    for name in ("analyzer.py", "decision.py", "graph.py", "decision_store.py"):
        src = (ROOT / "disputedesk" / name).read_text(encoding="utf-8")
        assert "attachment" not in src.lower() and "documents" not in src, name


def test_case_page_shows_thumbnails_by_index_with_the_label():
    js = (ROOT / "disputedesk" / "web" / "static" / "case.js").read_text(encoding="utf-8")
    panel = js.split("function attachmentsPanel", 1)[1].split("function disposeGraph", 1)[0]
    assert 'const ATTACHMENT_LABEL = "Buyer attachment. Not analysed by the agent.";' in js
    assert '"Buyer attachment, not available to download"' in panel
    assert "if (!items || !items.length) return null;" in panel  # hidden when there are none
    assert "/api/attachments/${encodeURIComponent(disputeId)}/${a.index}" in panel
    assert ".name" not in panel and ".url" not in panel and "innerHTML" not in panel
