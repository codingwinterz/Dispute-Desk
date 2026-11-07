"""Seed the local FalkorDB graph with merchant context around the stored PayPal disputes.

    python scripts/seed_graph.py            (idempotent: MERGE only)
    python scripts/seed_graph.py --reset    (drop the disputedesk graph first)

Reads disputes from data/disputedesk.db (filled by the poller). Makes no PayPal or LLM calls.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from disputedesk.config import DISPUTE_DB, load_agent_settings  # noqa: E402
from disputedesk.dispute_store import DisputeStore  # noqa: E402
from disputedesk.graph import GraphStore, connect  # noqa: E402
from disputedesk.seed import load_seed, seed_graph  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="delete the disputedesk graph before seeding")
    args = parser.parse_args()

    settings = load_agent_settings()
    store = DisputeStore(DISPUTE_DB)
    try:
        graph = GraphStore(connect(settings))
        if args.reset:
            graph.reset()
            print("graph reset")
        for line in seed_graph(graph, store.full_disputes(), load_seed()):
            print(line)
    except Exception as exc:  # the client raises redis.exceptions.*, not the builtin ConnectionError
        print(f"FAIL: FalkorDB at {settings.falkordb_host}:{settings.falkordb_port}: {type(exc).__name__}")
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
