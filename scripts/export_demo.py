"""Write data/demo_snapshot.json from the local store, for demo mode.

    python scripts/export_demo.py

Reads data/disputedesk.db read-only. Keeps only what the dashboard shows, pseudonymises buyer and
merchant identifiers, and refuses to write the file if a safety check fails (email address, original
identifier, token-like string, link URL). Makes no network calls.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from disputedesk.config import DISPUTE_DB  # noqa: E402
from disputedesk.demo import SNAPSHOT_PATH, export_snapshot, scan  # noqa: E402


def main() -> int:
    if not DISPUTE_DB.exists():
        print(f"FAIL: no local store at {DISPUTE_DB.relative_to(DISPUTE_DB.parent.parent)}; run the app once to collect disputes.")
        return 1
    snapshot, mapping = export_snapshot(DISPUTE_DB)
    failed = scan(snapshot, list(mapping))
    if failed:
        print(f"FAIL: snapshot not written; safety checks failed: {', '.join(failed)}")
        return 1
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {SNAPSHOT_PATH.relative_to(SNAPSHOT_PATH.parent.parent).as_posix()}: "
          f"{len(snapshot['disputes'])} disputes, {len(snapshot['decisions'])} decisions, "
          f"{len(snapshot['submissions'])} submissions; {len(mapping)} identifiers pseudonymised; all safety checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
