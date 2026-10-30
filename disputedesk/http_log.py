"""Writes raw HTTP exchanges to logs/ as JSON lines, with secrets redacted."""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

REDACTED = "[REDACTED]"
SECRET_HEADERS = {"authorization", "paypal-auth-assertion", "cookie", "set-cookie"}
SECRET_KEYS = {"access_token", "refresh_token", "id_token", "client_secret", "nonce"}
MAX_BODY_CHARS = 20_000


def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: REDACTED if k.lower() in SECRET_KEYS else _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    return obj


def _body(raw: bytes, content_type: str, secrets: tuple[str, ...]) -> Any:
    if not raw:
        return None
    if "multipart" in content_type:
        return f"<multipart body, {len(raw)} bytes>"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"<binary body, {len(raw)} bytes>"
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    try:
        return _redact_obj(json.loads(text))
    except ValueError:
        return text[:MAX_BODY_CHARS]


def _headers(headers: httpx.Headers) -> dict[str, str]:
    return {k: REDACTED if k.lower() in SECRET_HEADERS else v for k, v in headers.items()}


class HttpLogger:
    """httpx response hook: one JSON line per request/response pair."""

    def __init__(self, path: Path, secrets: tuple[str, ...] = ()):
        self.path = path
        self.secrets = secrets
        self._lock = threading.Lock()

    def __call__(self, response: httpx.Response) -> None:
        response.read()
        req = response.request
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "request": {
                "method": req.method,
                "url": str(req.url),
                "headers": _headers(req.headers),
                "body": _body(req.content, req.headers.get("content-type", ""), self.secrets),
            },
            "response": {
                "status": response.status_code,
                "headers": _headers(response.headers),
                "body": _body(response.content, response.headers.get("content-type", ""), self.secrets),
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


def make_client(log_path: Path | None, secrets: tuple[str, ...] = (), **kwargs: Any) -> httpx.Client:
    hooks = {"response": [HttpLogger(log_path, secrets)]} if log_path else {}
    return httpx.Client(timeout=30.0, event_hooks=hooks, **kwargs)
