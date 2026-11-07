"""List Gemini models that support generateContent. Read-only helper; not part of pytest.

    python scripts/list_models.py

Reads GEMINI_API_KEY from .env via python-dotenv and sends it only in the x-goog-api-key header,
so it never appears in a URL. The key and .env contents are never printed.
"""

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# GET https://generativelanguage.googleapis.com/v1beta/models ("models.list").
# Response: {"models": [{"name": "models/...", "supportedGenerationMethods": [...], ...}], "nextPageToken": "..."}
# https://ai.google.dev/api/models#method:-models.list
MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
MAX_PAGES = 20


def redact(text: str, key: str) -> str:
    return text.replace(key, "[REDACTED]") if key else text


def error_message(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error") or {}
        return str(err.get("message") or err.get("status") or "no error message")
    except ValueError:
        return resp.text[:300] or "no error message"


def list_generate_models(http: httpx.Client, key: str) -> tuple[list[str] | None, str | None]:
    names: list[str] = []
    params: dict[str, str] = {"pageSize": "1000"}
    for _ in range(MAX_PAGES):
        resp = http.get(MODELS_URL, params=params, headers={"x-goog-api-key": key})
        if resp.is_error:
            return None, f"HTTP {resp.status_code}: {redact(error_message(resp), key)}"
        body = resp.json()
        for m in body.get("models") or []:
            if "generateContent" in (m.get("supportedGenerationMethods") or []):
                names.append(str(m.get("name", "")).removeprefix("models/"))
        token = body.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    return sorted(set(names)), None


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    key = os.getenv("GEMINI_API_KEY") or ""
    if not key:
        print("GEMINI_API_KEY is missing from .env")
        return 2
    try:
        with httpx.Client(timeout=30.0) as http:
            names, error = list_generate_models(http, key)
    except httpx.HTTPError as exc:
        print(f"request failed: {type(exc).__name__}")
        return 1
    if error:
        print(error)
        return 1
    print(f"{len(names)} models support generateContent:")
    for name in names:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
