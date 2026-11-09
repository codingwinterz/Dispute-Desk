"""Scratch copy of the local store and graph, for rehearsing merchant events and re-analyses.

    python scripts/scratch.py create     (copy data/disputedesk.db to data/scratch.db, seed graph disputedesk_scratch)
    python scripts/scratch.py reset      (throw the scratch copy away and create it again from the real store)
    python scripts/scratch.py create --db data/other.db --graph disputedesk_other

The real database is opened read-only and copied with SQLite's backup API; the scratch graph is seeded
the same way as the real one (scripts/seed_graph.py: seed/merchant_data.json around the stored PayPal
disputes), plus any merchant events already in the copied store. The real graph is never selected.

Then select the copy in the shell (never in .env) for the app and the scripts:
    PowerShell:  $env:DISPUTEDESK_DB = "data/scratch.db"; $env:DISPUTEDESK_GRAPH = "disputedesk_scratch"
    bash:        export DISPUTEDESK_DB=data/scratch.db DISPUTEDESK_GRAPH=disputedesk_scratch
The dashboard shows a "Scratch data" badge while they are set. Unset both to go back to the real store.
Makes no PayPal or LLM calls.
"""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from disputedesk.config import DISPUTE_DB, PROJECT_ROOT, check_scratch, load_agent_settings  # noqa: E402
from disputedesk.dispute_store import DisputeStore  # noqa: E402
from disputedesk.graph import GraphStore, connect  # noqa: E402
from disputedesk.merchant_events import MerchantEventStore, graph_props, parse_event  # noqa: E402
from disputedesk.seed import load_seed, seed_graph  # noqa: E402

DEFAULT_DB = Path("data/scratch.db")
DEFAULT_GRAPH = "disputedesk_scratch"
GRAPH_FIX = "      Fix: docker compose up -d falkordb   then run this command again"


def _shown(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.name


def _env_value(path: Path) -> str:
    """What to put in DISPUTEDESK_DB: project-relative when inside the project, else the full path."""
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def copy_store(source: Path, dest: Path) -> None:
    """A consistent copy of the SQLite store. The source is opened read-only."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def remove_store(dest: Path) -> None:
    for path in (dest, dest.with_name(dest.name + "-journal"), dest.with_name(dest.name + "-wal"),
                 dest.with_name(dest.name + "-shm")):
        path.unlink(missing_ok=True)


def seed_scratch_graph(graph: GraphStore, db: Path) -> list[str]:
    """Same seeding as scripts/seed_graph.py, then the merchant events already in the copied store."""
    disputes, events = DisputeStore(db), MerchantEventStore(db)
    try:
        lines = list(seed_graph(graph, disputes.full_disputes(), load_seed()))
        with closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as conn:
            rows = conn.execute("SELECT transaction_id, data FROM merchant_events ORDER BY received_at").fetchall()
        for txn_id, data in rows:
            graph.add_merchant_event(txn_id, graph_props(parse_event(json.loads(data))))
        if rows:
            lines.append(f"{len(rows)} merchant event(s) from the copied store added to the graph")
        return lines
    finally:
        disputes.close()
        events.close()


def main(argv: list[str] | None = None, graph_factory=None, source: Path = DISPUTE_DB) -> int:
    """CLI entry point. Tests pass their own graph and source database."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("create", "reset"))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"scratch database (default {DEFAULT_DB.as_posix()})")
    parser.add_argument("--graph", default=DEFAULT_GRAPH, help=f"scratch FalkorDB graph (default {DEFAULT_GRAPH})")
    args = parser.parse_args(argv)

    try:
        dest = check_scratch(args.db, args.graph)
    except RuntimeError as exc:
        print(f"FAIL: {exc}")
        return 1
    if not source.exists():
        print(f"FAIL: no real store at {_shown(source)} to copy; run the app once to collect disputes.")
        return 1
    if args.command == "create" and dest.exists():
        print(f"FAIL: {_shown(dest)} already exists. Use: python scripts/scratch.py reset   to recreate it.")
        return 1

    if graph_factory is None:
        settings = replace(load_agent_settings(), graph_name=args.graph)  # always the scratch graph, never the real one
        graph_factory = lambda: GraphStore(connect(settings))  # noqa: E731
    try:
        graph = graph_factory()
        graph.reset()  # the scratch graph only: the name was checked above
    except Exception as exc:  # the FalkorDB client raises redis.exceptions.*
        print(f"FAIL: the scratch graph could not be reset ({type(exc).__name__}); nothing was copied.")
        print(GRAPH_FIX)
        return 1

    remove_store(dest)
    copy_store(source, dest)
    print(f"copied {_shown(source)} -> {_shown(dest)} (read-only copy of the real store)")
    try:
        for line in seed_scratch_graph(graph, dest):
            print(f"graph {args.graph}: {line}")
    except Exception as exc:
        print(f"FAIL: the scratch store was copied, but the graph was not seeded ({type(exc).__name__}).")
        print(GRAPH_FIX)
        return 1
    print("\nScratch copy ready. Select it in this shell (not in .env):")
    print(f'  PowerShell:  $env:DISPUTEDESK_DB = "{_env_value(dest)}"; $env:DISPUTEDESK_GRAPH = "{args.graph}"')
    print(f'  bash:        export DISPUTEDESK_DB="{_env_value(dest)}" DISPUTEDESK_GRAPH={args.graph}')
    print("Unset both to return to the real store.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
