"""Start DisputeDesk in full mode (real PayPal sandbox, Gemini, FalkorDB), after checking the setup.

    python scripts/start.py [--port 8000]

Each problem is reported as one line with the command that fixes it, instead of a stack trace.
No secret values are printed. For a no-setup look at the dashboard, run: python scripts/demo.py
"""

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXES = {
    "python": "Use Python 3.12: py -3.12 -m venv .venv  then  .venv\\Scripts\\python -m pip install -r requirements.txt",
    "deps": ".venv\\Scripts\\python -m pip install -r requirements.txt",
    "env": "copy .env.example .env   (macOS/Linux: cp .env.example .env), then fill in the values",
    "encoding": "Re-save .env as UTF-8 without BOM (in VS Code: 'Save with Encoding' > UTF-8)",
    "parse": "Fix the listed .env lines so each reads NAME=value",
    "PAYPAL_CLIENT_ID": "Sandbox app credentials: developer.paypal.com > Apps & Credentials > Sandbox",
    "PAYPAL_CLIENT_SECRET": "Sandbox app credentials: developer.paypal.com > Apps & Credentials > Sandbox",
    "PAYPAL_WEBHOOK_ID": "The webhook ID on that sandbox app (Apps & Credentials > your app > Webhooks)",
    "GEMINI_API_KEY": "Create a key at aistudio.google.com and put it in .env as GEMINI_API_KEY",
    "falkordb": "docker compose up -d falkordb",
    "seed": ".venv\\Scripts\\python scripts/seed_graph.py",
}


def load_preflight():
    spec = importlib.util.spec_from_file_location("preflight", ROOT / "scripts" / "preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def problems(env: Path | None = None, pf=None) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(blocking problems, warnings) as (message, fix) pairs. `env`/`pf` are for tests."""
    blocking: list[tuple[str, str]] = []
    warnings: list[tuple[str, str]] = []
    if sys.version_info[:2] < (3, 12) or sys.version_info[:2] >= (3, 15):
        blocking.append((f"Python {sys.version_info.major}.{sys.version_info.minor} is not supported "
                         "(LiteLLM's dependencies need 3.12 to 3.14).", FIXES["python"]))
        return blocking, warnings
    try:
        import falkordb  # noqa: F401
        import fastapi  # noqa: F401
        import litellm  # noqa: F401  (import only; no network)
    except ImportError as exc:
        blocking.append((f"A dependency is missing ({exc.name}).", FIXES["deps"]))
        return blocking, warnings

    pf = pf or load_preflight()
    env = env or ROOT / ".env"
    if pf.check_exists(env).verdict != "PASS":
        blocking.append(("There is no .env file.", FIXES["env"]))
        return blocking, warnings
    if pf.check_encoding(env).verdict == "FAIL":
        blocking.append(("The .env file is not plain UTF-8.", FIXES["encoding"]))
        return blocking, warnings
    parsed = pf.check_parse(env)
    if parsed.verdict == "FAIL":
        blocking.append((parsed.reason, FIXES["parse"]))
    values = pf.read_values(env)
    for name in pf.REQUIRED_VARS:
        if not values.get(name):
            blocking.append((f"{name} is missing from .env.", FIXES[name]))
    if values.get("PAYPAL_WEBHOOK_ID") == "placeholder":
        warnings.append(("PAYPAL_WEBHOOK_ID is 'placeholder', so webhooks can't be verified. Polling still detects disputes.",
                         FIXES["PAYPAL_WEBHOOK_ID"]))

    graph = pf.check_falkordb()
    if graph.verdict != "PASS":
        blocking.append(("The graph database (FalkorDB) is not running on localhost:6379.", FIXES["falkordb"]))
    else:
        try:
            from disputedesk.config import load_agent_settings
            from disputedesk.graph import connect
            rows = connect(load_agent_settings()).query("MATCH (n) RETURN count(n)").result_set
            if not rows or not rows[0][0]:
                warnings.append(("The graph is empty, so analyses will have no shipment or device facts.", FIXES["seed"]))
        except Exception as exc:  # an empty graph key errors on some versions; treat as unseeded
            warnings.append((f"Couldn't read the graph ({type(exc).__name__}); it may not be seeded yet.", FIXES["seed"]))
    return blocking, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    blocking, warnings = problems()
    for message, fix in warnings:
        print(f"WARN  {message}\n      Fix: {fix}")
    if blocking:
        for message, fix in blocking:
            print(f"FAIL  {message}\n      Fix: {fix}")
        print("\nDisputeDesk did not start. To look at the dashboard without any setup: python scripts/demo.py")
        return 1

    import uvicorn
    print(f"DisputeDesk: http://127.0.0.1:{args.port}/  (Overview; the queue is at /queue. PayPal sandbox; polling every POLL_SECONDS)")
    uvicorn.run("disputedesk.webhooks:create_app", factory=True, host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
