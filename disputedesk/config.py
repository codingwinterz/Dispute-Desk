"""Settings loaded from .env (via python-dotenv). Sandbox only."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Sandbox base URL. This is deliberately not configurable so live credentials can never be used.
# https://developer.paypal.com/api/rest/authentication/
SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"


@dataclass(frozen=True)
class Settings:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    webhook_id: str = field(repr=False)
    base_url: str = SANDBOX_BASE_URL

    def __post_init__(self) -> None:
        if self.base_url != SANDBOX_BASE_URL:
            raise ValueError("DisputeDesk spike is sandbox-only")


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or PROJECT_ROOT / ".env")
    missing = [
        name
        for name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_WEBHOOK_ID")
        if not os.getenv(name)
    ]
    if missing:
        # Only the variable names are reported, never their values.
        raise RuntimeError(f"missing environment variables: {', '.join(missing)}")
    return Settings(
        client_id=os.environ["PAYPAL_CLIENT_ID"],
        client_secret=os.environ["PAYPAL_CLIENT_SECRET"],
        webhook_id=os.environ["PAYPAL_WEBHOOK_ID"],
    )
