"""Run the dashboard in demo mode: recorded sandbox data, no PayPal, Gemini or FalkorDB.

    python scripts/demo.py [--port 8000]

Serves data/demo_snapshot.json. Needs no .env. Approve and every other write are disabled.
Open http://127.0.0.1:8000/ afterwards (the Overview; the queue is at /queue).
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    snapshot = ROOT / "data" / "demo_snapshot.json"
    if not snapshot.exists():
        print("FAIL: data/demo_snapshot.json is missing. It ships with the repository; restore it with:\n"
              "  git checkout -- data/demo_snapshot.json")
        return 1
    try:
        import uvicorn
    except ImportError:
        print("FAIL: dependencies are not installed. Run:\n  .venv/Scripts/python -m pip install -r requirements.txt")
        return 1

    os.environ["DEMO_MODE"] = "1"
    print(f"DisputeDesk demo: http://127.0.0.1:{args.port}/  (Overview; the queue is at /queue. Recorded sandbox data; nothing is sent to PayPal)")
    uvicorn.run("disputedesk.webhooks:create_app", factory=True, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
