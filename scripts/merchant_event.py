"""Import one simulated merchant event (for example a carrier scan) and re-run the agent's decision.

    python scripts/merchant_event.py --file seed/events/yov_delivery_scan.json
    python scripts/merchant_event.py --file my_event.json --no-analyze   (write the event, decide later)

The event names a transaction. Its dispute is found in the stored PayPal disputes (data/disputedesk.db, or
the scratch copy when DISPUTEDESK_DB and DISPUTEDESK_GRAPH are set; see scripts/scratch.py);
an unknown transaction is rejected and nothing is written. The event goes into the FalkorDB graph as
source "seeded", labelled "simulated merchant event", and is kept for the case page's timeline. The
dispute is then decided again with the existing analyzer: the new decision is stored and the one it
replaces stays in the decision history. Makes no PayPal calls. GEMINI_API_KEY is read by LiteLLM,
never printed. In production your store supplies this; in the sandbox it is simulated.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from disputedesk.analyzer import Analyzer  # noqa: E402
from disputedesk.config import DISPUTE_DB, load_agent_settings, targets  # noqa: E402
from disputedesk.decision_store import DecisionStore  # noqa: E402
from disputedesk.dispute_store import DisputeStore  # noqa: E402
from disputedesk.graph import GraphStore, connect  # noqa: E402
from disputedesk.merchant_events import EventError, MerchantEventStore, find_dispute, graph_props, parse_event_file  # noqa: E402
from disputedesk.merchant_records import NOTE  # noqa: E402
from disputedesk.policies import PolicyStore  # noqa: E402

GRAPH_FIX = "      Fix: docker compose up -d falkordb   then run this command again"


def store_path() -> Path:
    """The scratch copy in scratch mode (DISPUTEDESK_DB + DISPUTEDESK_GRAPH), else the real store."""
    t = targets()
    return t.db if t.scratch else DISPUTE_DB


def describe(rec: dict | None) -> str:
    if not rec:
        return "no decision"
    if rec["status"] != "ok":
        return f"analysis failed ({rec.get('error')})"
    return f"{rec['decision']} (risk {rec['risk_score']})"


def main(argv: list[str] | None = None, graph_factory=None, decide_fn=None, db_path: Path | None = None,
         model: str | None = None) -> int:
    """CLI entry point. Tests (and dry runs on a copy) pass their own graph, decision step, database and model."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True, type=Path, help="event JSON (see seed/events/ for the shape)")
    parser.add_argument("--no-analyze", action="store_true", help="write the event but don't re-run the decision")
    args = parser.parse_args(argv)

    try:
        event = parse_event_file(args.file)
    except EventError as exc:
        print(f"FAIL: {args.file.name} was not imported:")
        for problem in exc.problems:
            print(f"      {problem}")
        return 1

    if db_path is None:
        db_path = store_path()
        if db_path != DISPUTE_DB:
            print(f"scratch mode: database {db_path.name}, graph {targets().graph}")
    disputes, decisions, events = DisputeStore(db_path), DecisionStore(db_path), MerchantEventStore(db_path)
    policies = PolicyStore(db_path)
    try:
        try:
            dispute_id = find_dispute(event.transaction_id, disputes.full_disputes())
        except EventError as exc:
            print(f"FAIL: {exc.problems[0]}")
            return 1
        if events.exists(event.event_id):
            print(f"FAIL: event {event.event_id} was already imported; nothing was written. Give a new event_id to send another.")
            return 1

        if graph_factory is None or model is None:
            agent = load_agent_settings()
            graph_factory = graph_factory or (lambda: GraphStore(connect(agent)))
            model = model or agent.llm_model
        try:
            graph_factory().add_merchant_event(event.transaction_id, graph_props(event))
        except Exception as exc:  # the FalkorDB client raises redis.exceptions.*
            print(f"FAIL: the graph could not be updated ({type(exc).__name__}); nothing was stored.")
            print(GRAPH_FIX)
            return 1
        received = events.add(event, dispute_id)
        print(f"event {event.event_id} ({event.kind}: {event.status} at {event.location}, {event.time})")
        print(f"  written to the graph for {dispute_id} (transaction {event.transaction_id}), source seeded,")
        print(f"  labelled 'simulated merchant event', received {received}")
        print(f"  {NOTE}")
        if args.no_analyze:
            print("decision not re-run (--no-analyze)")
            return 0

        before = decisions.get(dispute_id)
        kwargs = {"decide_fn": decide_fn} if decide_fn else {}
        analyzer = Analyzer(disputes, decisions, graph_factory, model, rules_provider=policies.rules, **kwargs)
        outcome = analyzer.analyze(dispute_id, force=True, trigger=f"merchant_event:{event.event_id}")
        after = decisions.get(dispute_id)
        print(f"decision before: {describe(before)}")
        print(f"decision after:  {describe(after)}")
        if after and after["status"] == "ok":
            changed = not before or before.get("decision") != after["decision"]
            print(f"  {'the recommendation changed' if changed else 'the recommendation did not change'}"
                  f"{'' if not before or before.get('risk_score') is None else f'; risk {before['risk_score']} -> {after['risk_score']}'}")
            for p in after["reasoning"]:
                print(f"   - {p['point']}  [{', '.join(p['cites'])}]")
            print("  the previous decision is kept in the decision history")
            return 0
        print(f"  the re-run did not produce a decision ({outcome}); the previous one is kept in the history")
        return 1
    finally:
        disputes.close()
        decisions.close()
        events.close()
        policies.close()


if __name__ == "__main__":
    sys.exit(main())
