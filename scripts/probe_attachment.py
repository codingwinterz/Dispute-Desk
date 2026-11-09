"""Probe one stored attachment link with a single read-only GET, to learn how PayPal serves it.

    python scripts/probe_attachment.py PP-R-CTG-10197156

Reads the dispute from the local store (read-only; the scratch copy when DISPUTEDESK_DB and
DISPUTEDESK_GRAPH are set) and takes the link of its first message document, or of its first
evidence document if no message has one. It refuses unless the link is https on
api-m.sandbox.paypal.com; only then is the bearer token fetched and attached. One GET is sent with
redirects disabled.

Prints only: the HTTP status, the Location host of a redirect, the content type, the byte count and
the file type detected from the first bytes (jpg, png, gif, pdf or other). The token, the link and
the file are never printed, logged or saved: the bytes are counted and discarded. Makes no PayPal
write call. The PayPal docs call document download "a limited release" that may need setup.
"""

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from disputedesk.auth import TokenProvider  # noqa: E402
from disputedesk.config import DISPUTE_DB, Settings, load_settings, targets  # noqa: E402

ALLOWED_HOST = "api-m.sandbox.paypal.com"
SNIFF_BYTES = 16
MAX_BYTES = 20 * 1024 * 1024  # stop reading (and say so) past this; the file is discarded anyway
_SIGNATURES = (  # leading bytes of each allowed type
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"%PDF-", "pdf"),
)


class ProbeRefused(Exception):
    """The probe will not send a request; the message never contains the link."""


def store_path() -> Path:
    t = targets()
    return t.db if t.scratch else DISPUTE_DB


def stored_dispute(db: Path, dispute_id: str) -> dict | None:
    """The stored PayPal JSON, opened read-only (the store is never written)."""
    if not db.exists():
        return None
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT data FROM disputes WHERE dispute_id = ?", (dispute_id,)).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def pick_link(dispute: dict) -> tuple[str, str] | None:
    """(where, url): the first message document with a url, else the first evidence document with one."""
    for where, items in (("message document", dispute.get("messages")), ("evidence document", dispute.get("evidences"))):
        for item in items or []:
            for doc in (item.get("documents") or []) if isinstance(item, dict) else []:
                if isinstance(doc, dict) and isinstance(doc.get("url"), str) and doc["url"]:
                    return where, doc["url"]
    return None


def check_link(url: str) -> None:
    """Refuse anything but https://api-m.sandbox.paypal.com/... (exact host, default port, no userinfo)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ProbeRefused("the stored link is not a valid URL") from None
    if parts.scheme != "https":
        raise ProbeRefused("the stored link is not https")
    if parts.username is not None or parts.password is not None:
        raise ProbeRefused("the stored link carries user information")
    if (parts.hostname or "").lower() != ALLOWED_HOST:
        raise ProbeRefused(f"the stored link is not on {ALLOWED_HOST}")
    if port not in (None, 443):
        raise ProbeRefused("the stored link uses a non-default port")


def detect_type(head: bytes) -> str:
    for signature, kind in _SIGNATURES:
        if head.startswith(signature):
            return kind
    return "other"


def location_host(resp: httpx.Response) -> str:
    loc = resp.headers.get("location")
    if not loc:
        return "(no Location header)"
    host = urlsplit(loc).hostname
    return host or "(relative: same host)"


def probe(url: str, auth_headers, http: httpx.Client) -> dict:
    """One GET, redirects disabled. Returns only the reportable fields; the body is counted and dropped."""
    check_link(url)  # before any token is fetched
    headers = auth_headers()
    with http.stream("GET", url, headers=headers, follow_redirects=False) as resp:
        head, count, truncated = b"", 0, False
        for chunk in resp.iter_bytes():
            if len(head) < SNIFF_BYTES:
                head += chunk[: SNIFF_BYTES - len(head)]
            count += len(chunk)
            if count > MAX_BYTES:
                truncated = True
                break
        return {
            "status": resp.status_code,
            "redirect": resp.is_redirect,
            "location_host": location_host(resp) if resp.is_redirect else None,
            "content_type": resp.headers.get("content-type", "(none)"),
            "bytes": count,
            "truncated": truncated,
            "detected": detect_type(head) if count else "(empty body)",
        }


def main(argv: list[str] | None = None, http: httpx.Client | None = None, settings: Settings | None = None,
         db_path: Path | None = None) -> int:
    """CLI entry point. Tests pass their own HTTP client, settings and database."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dispute_id")
    args = parser.parse_args(argv)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs request URLs at INFO
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    dispute = stored_dispute(db_path or store_path(), args.dispute_id)
    if dispute is None:
        print(f"FAIL: {args.dispute_id} is not in the local store.")
        return 1
    picked = pick_link(dispute)
    if picked is None:
        print("FAIL: no message or evidence document of this dispute has a stored link; nothing was sent.")
        return 1
    where, url = picked
    try:
        check_link(url)
    except ProbeRefused as exc:
        print(f"REFUSED: {exc}; nothing was sent.")
        return 1

    own_client = http is None
    # A plain client with no logging hooks: the link must not reach logs/*.jsonl.
    http = http or httpx.Client(timeout=30.0)
    try:
        settings = settings or load_settings()
        tokens = TokenProvider(settings, http)
        try:
            result = probe(url, tokens.auth_headers, http)
        except httpx.HTTPError as exc:  # messages can include the URL: print the type only
            print(f"FAIL: request error ({type(exc).__name__}); nothing was saved.")
            return 1
    finally:
        if own_client:
            http.close()

    print(f"probed the {where} of {args.dispute_id} (one GET, redirects not followed; bytes discarded)")
    print(f"  status:        {result['status']}")
    if result["redirect"]:
        print(f"  location host: {result['location_host']}")
    print(f"  content type:  {result['content_type']}")
    print(f"  bytes:         {result['bytes']}{' (stopped reading at the 20 MB probe limit)' if result['truncated'] else ''}")
    print(f"  detected type: {result['detected']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
