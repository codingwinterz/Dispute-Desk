"""Run the decision step for stored disputes, from the command line.

    python scripts/analyze.py --all --facts-only     (graph facts only; no LLM call)
    python scripts/analyze.py --dispute-id PP-R-...  (re-run one dispute: always a fresh LLM decision)
    python scripts/analyze.py --all [--force]        (--force re-decides disputes that already have one)

Reads disputes from data/disputedesk.db and context from the local FalkorDB graph (seed it first
with scripts/seed_graph.py). Makes no PayPal calls. GEMINI_API_KEY is read by LiteLLM, never printed.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from disputedesk.analyzer import Analyzer  # noqa: E402
from disputedesk.config import DISPUTE_DB, load_agent_settings  # noqa: E402
from disputedesk.decision_store import DecisionStore  # noqa: E402
from disputedesk.dispute_store import DisputeStore  # noqa: E402
from disputedesk.graph import GraphStore, connect, context_to_facts  # noqa: E402
from disputedesk.policies import PolicyStore  # noqa: E402


def print_decision(rec: dict) -> None:
    if rec["status"] != "ok":
        print(f"  ERROR {rec['error']} (model {rec['model']}, attempts {rec['attempts']})")
        return
    print(f"  {rec['decision']}  risk_score={rec['risk_score']}  (model {rec['model']}, attempts {rec['attempts']})")
    for p in rec["reasoning"]:
        print(f"   - {p['point']}  [{', '.join(p['cites'])}]")
    print(f"  evidence draft: {rec['evidence_summary']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--dispute-id", metavar="ID", help="re-run one dispute (replaces its stored decision)")
    target.add_argument("--all", action="store_true", help="every stored dispute with full details")
    parser.add_argument("--facts-only", action="store_true", help="print graph facts; do not call the LLM")
    parser.add_argument("--force", action="store_true", help="re-decide even if an ok decision exists")
    args = parser.parse_args()

    agent = load_agent_settings()
    disputes, decisions = DisputeStore(DISPUTE_DB), DecisionStore(DISPUTE_DB)
    policies = PolicyStore(DISPUTE_DB)  # the merchant's saved policy rules (defaults if never saved)
    analyzer = Analyzer(disputes, decisions, lambda: GraphStore(connect(agent)), agent.llm_model,
                        rules_provider=policies.rules)
    ids = sorted(disputes.full_disputes()) if args.all else [args.dispute_id]
    failed = False
    try:
        for dispute_id in ids:
            print(dispute_id)
            if args.facts_only:
                status, ctx = analyzer.context(dispute_id)
                if ctx is None:
                    print(f"  {status}")
                    failed = True
                    continue
                for f in context_to_facts(ctx):
                    print(f"  {f['id']} [{f['source']}] {f['text']}")
                continue
            # Naming one dispute means re-running it, so --dispute-id implies --force.
            outcome = analyzer.analyze(dispute_id, force=args.force or bool(args.dispute_id))
            rec = decisions.get(dispute_id)
            if outcome == "already_decided":
                print("  (already decided; use --force to re-run)")
            if rec and outcome not in ("missing", "waiting_for_full_details"):
                print_decision(rec)
            else:
                print(f"  {outcome}")
            failed |= not rec or rec["status"] != "ok"
    finally:
        disputes.close()
        decisions.close()
        policies.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
