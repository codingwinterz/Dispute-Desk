"""Settings loaded from .env (via python-dotenv). Sandbox only."""

import os
import re
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

# Scratch mode: a copy of the store and its own graph, for rehearsing (scripts/scratch.py creates them).
# Both must be set together, in the shell (never in .env), and neither may name the real store or graph.
DB_ENV = "DISPUTEDESK_DB"
GRAPH_ENV = "DISPUTEDESK_GRAPH"
_GRAPH_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class Targets:
    db: Path
    graph: str
    scratch: bool


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def targets() -> Targets:
    """The SQLite file and FalkorDB graph to use. Defaults to the real ones; scratch mode when both
    DISPUTEDESK_DB and DISPUTEDESK_GRAPH are set. Raises (never falls back) on a half or unsafe setting."""
    raw_db, raw_graph = (os.getenv(DB_ENV) or "").strip(), (os.getenv(GRAPH_ENV) or "").strip()
    if not raw_db and not raw_graph:
        return Targets(DISPUTE_DB, GRAPH_NAME, False)
    if not (raw_db and raw_graph):
        raise RuntimeError(f"scratch mode needs both {DB_ENV} and {GRAPH_ENV}, but only one is set. "
                           "Fix: set both (python scripts/scratch.py create prints them) or unset both")
    return Targets(check_scratch(Path(raw_db), raw_graph), raw_graph, True)


def check_scratch(db: Path, graph: str) -> Path:
    """The scratch database path (made absolute), or RuntimeError if it or the graph is the real one."""
    if not db.is_absolute():
        db = PROJECT_ROOT / db
    if _same_path(db, DISPUTE_DB):
        raise RuntimeError(f"{DB_ENV} names the real database; scratch mode must use a copy")
    if graph == GRAPH_NAME:
        raise RuntimeError(f"{GRAPH_ENV} names the real graph; scratch mode must use its own graph")
    if not _GRAPH_NAME_RE.match(graph):
        raise RuntimeError(f"{GRAPH_ENV} may only hold letters, digits, '_' and '-' (up to 64)")
    return db


def _load_env_file(path: Path) -> None:
    """load_dotenv, refusing a .env that sets the scratch variables: they must come from the shell, so
    the database (resolved before .env loads) and the graph (after) can never disagree."""
    before = (os.getenv(DB_ENV), os.getenv(GRAPH_ENV))
    load_dotenv(path)
    if (os.getenv(DB_ENV), os.getenv(GRAPH_ENV)) != before:
        for name, value in zip((DB_ENV, GRAPH_ENV), before):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        raise RuntimeError(f"{DB_ENV} and {GRAPH_ENV} can't be set in .env. "
                           "Fix: remove them from .env and set them in the shell for a scratch session")


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
    path = env_file or PROJECT_ROOT / ".env"
    if not path.exists() and not os.getenv("PAYPAL_CLIENT_ID"):
        raise RuntimeError("there is no .env file. Fix: copy .env.example .env and fill it in "
                           "(or run python scripts/demo.py for demo mode, which needs no .env)")
    _load_env_file(path)
    missing = [
        name
        for name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_WEBHOOK_ID")
        if not os.getenv(name)
    ]
    if missing:
        # Only the variable names are reported, never their values.
        raise RuntimeError(f"missing environment variables: {', '.join(missing)}. "
                           "Fix: fill them in .env (see .env.example), or run python scripts/demo.py for demo mode")
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
    graph_name: str = GRAPH_NAME


def load_agent_settings(env_file: Path | None = None) -> AgentSettings:
    """LLM model and FalkorDB location. GEMINI_API_KEY is not read here: LiteLLM reads it itself."""
    _load_env_file(env_file or PROJECT_ROOT / ".env")
    try:
        port = int(os.getenv("FALKORDB_PORT") or DEFAULT_FALKORDB_PORT)
    except ValueError:
        raise RuntimeError("FALKORDB_PORT must be an integer") from None
    return AgentSettings(
        llm_model=os.getenv("LLM_MODEL") or DEFAULT_LLM_MODEL,
        falkordb_host=os.getenv("FALKORDB_HOST") or DEFAULT_FALKORDB_HOST,
        falkordb_port=port,
        graph_name=targets().graph,
    )
