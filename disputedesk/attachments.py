"""Buyer attachments on the case page: listed by index, fetched server-side, served only as images.

The browser only ever sees {index, available} for each buyer document. The route
GET /api/attachments/{dispute_id}/{index} looks the link up in the stored dispute JSON, refuses unless
it is https on api-m.sandbox.paypal.com, and only then attaches the bearer token. One GET is sent with
redirects disabled, at most 5 MB is read, and the file's own first bytes must be JPEG, PNG or GIF
(never PDF, SVG or anything else). Nothing here feeds the decision step: attachments are display only.

Where the links come from (PayPal Customer Disputes v1 schema): document {name, url} inside
evidences[].documents[] and messages[].documents[]. The schema notes that document download "may
require some configuration setup and available as a limited release", so PayPal may refuse it; the
page then says the attachment could not be loaded. UNVERIFIED: how the sandbox answers these links.
"""

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

ALLOWED_HOST = "api-m.sandbox.paypal.com"
MAX_BYTES = 5 * 1024 * 1024
_SIGNATURES = (  # leading bytes -> content type; the only types ever served
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
# Sent with every image this route serves.
SAFE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'",
    "Cache-Control": "no-store",
}


class AttachmentError(Exception):
    """A refusal or failure. `message` never contains the link, the token or a file name."""

    def __init__(self, code: str, http: int, message: str):
        super().__init__(message)
        self.code, self.http, self.message = code, http, message


def _is_buyer(source: Any) -> bool:
    return "BUYER" in str(source or "").upper()


def buyer_documents(dispute: dict[str, Any]) -> list[str | None]:
    """The link of each buyer document (None when PayPal gave no link), in a stable order: evidence
    documents, then message documents. A document listed in both places appears once."""
    found: list[tuple[str | None, str]] = []
    for item in dispute.get("evidences") or []:
        if isinstance(item, dict) and _is_buyer(item.get("source")):
            found += [(doc.get("url") or None, str(doc.get("name") or "")) for doc in item.get("documents") or []
                      if isinstance(doc, dict)]
    for item in dispute.get("messages") or []:
        if isinstance(item, dict) and _is_buyer(item.get("posted_by")):
            found += [(doc.get("url") or None, str(doc.get("name") or "")) for doc in item.get("documents") or []
                      if isinstance(doc, dict)]
    links: list[str | None] = []
    seen: set[str] = set()
    for url, name in found:
        key = f"url:{url}" if url else f"name:{name}"  # the name is only a server-side dedupe key
        if key in seen:
            continue
        seen.add(key)
        links.append(url if isinstance(url, str) else None)
    return links


def listing(dispute: dict[str, Any] | None) -> list[dict[str, Any]]:
    """What the case page gets: an index and whether a link exists. Never a name or URL."""
    return [{"index": i, "available": bool(url)} for i, url in enumerate(buyer_documents(dispute or {}))]


def check_link(url: str) -> None:
    """Refuse anything but https://api-m.sandbox.paypal.com/... (exact host, default port, no userinfo)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise AttachmentError("refused", 403, "The stored link is not a valid URL.") from None
    if (parts.scheme != "https" or parts.username is not None or parts.password is not None
            or (parts.hostname or "").lower() != ALLOWED_HOST or port not in (None, 443)):
        raise AttachmentError("refused", 403, f"The stored link is not https on {ALLOWED_HOST}; nothing was sent.")


def image_type(head: bytes) -> str | None:
    for signature, content_type in _SIGNATURES:
        if head.startswith(signature):
            return content_type
    return None


class AttachmentFetcher:
    """Fetches one checked link with the bearer token. Uses its own plain client (no request log)."""

    def __init__(self, auth_headers: Callable[[], dict[str, str]], http: httpx.Client, max_bytes: int = MAX_BYTES):
        self._auth_headers = auth_headers
        self._http = http
        self._max = max_bytes

    def fetch(self, url: str) -> tuple[bytes, str]:
        check_link(url)  # before the token is fetched or attached
        try:
            headers = self._auth_headers()
            with self._http.stream("GET", url, headers=headers, follow_redirects=False) as resp:
                if resp.is_redirect:
                    raise AttachmentError("redirect", 502, "PayPal answered with a redirect, which is not followed.")
                if resp.status_code != 200:
                    raise AttachmentError("upstream", 502, f"PayPal answered HTTP {resp.status_code}.")
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self._max:
                    raise AttachmentError("too_large", 502, "The attachment is larger than 5 MB.")
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > self._max:
                        raise AttachmentError("too_large", 502, "The attachment is larger than 5 MB.")
        except httpx.HTTPError as exc:  # messages can include the URL: report the type only
            raise AttachmentError("unreachable", 502, f"PayPal could not be reached ({type(exc).__name__}).") from None
        content_type = image_type(bytes(body[:16]))
        if content_type is None:
            raise AttachmentError("unsupported_type", 415, "Only JPEG, PNG and GIF images are shown.")
        return bytes(body), content_type
