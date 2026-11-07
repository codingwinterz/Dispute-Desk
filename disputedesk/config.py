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
# Local state (SQLite store of seen disputes). Git-ignored.
DATA_DIR = PROJECT_ROOT / "data"
DISPUTE_DB = DATA_DIR / "disputedesk.db"
DEFAULT_POLL_SECONDS = 10.0

# LLM used by the decision step, via LiteLLM. LiteLLM reads GEMINI_API_KEY from the environment.
DEFAULT_LLM_MODEL = "gemini/gemini-3.5-flash-lite"
# Local FalkorDB (Docker). Graph that holds the merchant context.
DEFAULT_FALKORDB_HOST = "localhost"
DEFAULT_FALKORDB_PORT = 6379
GRAPH_NAME = "disputedesk"


@dataclass(frozen=True)
class Settings:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    webhook_id: str = field(repr=False)
    base_url: str = SANDBOX_BASE_URL
    # Seconds between GET /v1/customer/disputes polls (env POLL_SECONDS).
    poll_seconds: float = DEFAULT_POLL_SECONDS

    def __post_init__(self) -> None:
        if self.base_url != SANDBOX_BASE_URL:
            raise ValueError("DisputeDesk spike is sandbox-only")
        if not self.poll_seconds > 0:
            raise ValueError("poll_seconds must be positive")


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
    try:
        poll_seconds = float(os.getenv("POLL_SECONDS") or DEFAULT_POLL_SECONDS)
        if not poll_seconds > 0:
            raise ValueError
    except ValueError:
        raise RuntimeError("POLL_SECONDS must be a positive number") from None
    return Settings(
        client_id=os.environ["PAYPAL_CLIENT_ID"],
        client_secret=os.environ["PAYPAL_CLIENT_SECRET"],
        webhook_id=os.environ["PAYPAL_WEBHOOK_ID"],
        poll_seconds=poll_seconds,
    )


@dataclass(frozen=True)
class AgentSettings:
    llm_model: str = DEFAULT_LLM_MODEL
    falkordb_host: str = DEFAULT_FALKORDB_HOST
    falkordb_port: int = DEFAULT_FALKORDB_PORT


def load_agent_settings(env_file: Path | None = None) -> AgentSettings:
    """LLM model and FalkorDB location. GEMINI_API_KEY is not read here: LiteLLM reads it itself."""
    load_dotenv(env_file or PROJECT_ROOT / ".env")
    try:
        port = int(os.getenv("FALKORDB_PORT") or DEFAULT_FALKORDB_PORT)
    except ValueError:
        raise RuntimeError("FALKORDB_PORT must be an integer") from None
    return AgentSettings(
        llm_model=os.getenv("LLM_MODEL") or DEFAULT_LLM_MODEL,
        falkordb_host=os.getenv("FALKORDB_HOST") or DEFAULT_FALKORDB_HOST,
        falkordb_port=port,
    )
