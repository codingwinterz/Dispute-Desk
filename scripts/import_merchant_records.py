"""Import merchant records (shipments, checkout devices, earlier disputes) from a JSON file.

    python scripts/import_merchant_records.py --file seed/merchant_data.json
    python scripts/import_merchant_records.py --file my_store_export.json --no-graph

Validates the file, stores the records for the Merchant records page (data/disputedesk.db) and, unless
--no-graph, seeds the FalkorDB graph from the same file so analyses can use them. Real transaction IDs
come from the stored PayPal disputes, never from the file. Makes no PayPal calls.
In production your store supplies this; in the sandbox it is simulated.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from disputedesk.config import DISPUTE_DB, load_agent_settings, targets  # noqa: E402
from disputedesk.dispute_store import DisputeStore  # noqa: E402
from disputedesk.merchant_records import MerchantRecordStore, RecordsError, import_records, parse_file  # noqa: E402


def store_path() -> Path:
    """The scratch copy in scratch mode (DISPUTEDESK_DB + DISPUTEDESK_GRAPH), else the real store."""
    t = targets()
    return t.db if t.scratch else DISPUTE_DB


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True, type=Path, help="input JSON (same shape as seed/merchant_data.json)")
    parser.add_argument("--no-graph", action="store_true", help="store the records but don't seed FalkorDB")
    args = parser.parse_args()

    try:
        parsed = parse_file(args.file)
    except RecordsError as exc:
        print(f"FAIL: {args.file.name} was not imported:")
        for problem in exc.problems:
            print(f"      {problem}")
        print("      Fix: correct the file (see seed/merchant_data.json for the expected shape) and run again.")
        return 1

    db = store_path()
    disputes = DisputeStore(db)
    records = MerchantRecordStore(db)
    try:
        stored = disputes.full_disputes()
        rows, report = import_records(parsed, stored, args.file.name)
        records.replace_all(rows)
        for line in report:
            print(line)
        print(f"stored {len(rows)} record(s) for the Merchant records page")
        if args.no_graph:
            print("graph not updated (--no-graph)")
            return 0
        try:
            from disputedesk.graph import GraphStore, connect
            from disputedesk.seed import seed_graph
            data = json.loads(args.file.read_text(encoding="utf-8"))
            for line in seed_graph(GraphStore(connect(load_agent_settings())), stored, data):
                print(f"graph: {line}")
        except Exception as exc:  # the FalkorDB client raises redis.exceptions.*
            print(f"FAIL: the records were stored, but the graph was not updated ({type(exc).__name__}).")
            print("      Fix: docker compose up -d falkordb   then run this command again")
            return 1
    finally:
        disputes.close()
        records.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
