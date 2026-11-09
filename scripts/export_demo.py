"""Write data/demo_snapshot.json from the local store, for demo mode.

    python scripts/export_demo.py
    python scripts/export_demo.py --out some/other.json   (write elsewhere, e.g. to inspect a scratch run)

Reads data/disputedesk.db read-only (or the scratch copy when DISPUTEDESK_DB and DISPUTEDESK_GRAPH are
set; see scripts/scratch.py). Keeps only what the dashboard shows, including the decision history and
the simulated merchant events, pseudonymises buyer and merchant identifiers (also inside fact text),
and refuses to write the file if a safety check fails (email address or "@", sandbox account name,
original identifier, token-like string, link URL). Makes no network calls.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from disputedesk.config import DISPUTE_DB, PROJECT_ROOT, targets  # noqa: E402
from disputedesk.demo import SNAPSHOT_PATH, export_snapshot, scan  # noqa: E402


def _shown(path: Path) -> str:
    """A path relative to the project when it is inside it (never print a local absolute path)."""
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=SNAPSHOT_PATH, help="where to write (default data/demo_snapshot.json)")
    args = parser.parse_args(argv)

    t = targets()
    db = t.db if t.scratch else DISPUTE_DB
    if t.scratch:
        print(f"scratch mode: reading {_shown(db)}")
    if not db.exists():
        print(f"FAIL: no local store at {_shown(db)}; run the app once to collect disputes.")
        return 1
    snapshot, mapping = export_snapshot(db)
    failed = scan(snapshot, list(mapping))
    if failed:
        print(f"FAIL: snapshot not written; safety checks failed: {', '.join(failed)}")
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {_shown(args.out)}: "
          f"{len(snapshot['disputes'])} disputes, {len(snapshot['decisions'])} decisions, "
          f"{len(snapshot['decision_history'])} earlier decisions, {len(snapshot['merchant_events'])} merchant events, "
          f"{len(snapshot['submissions'])} submissions; {len(mapping)} identifiers pseudonymised; all safety checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
