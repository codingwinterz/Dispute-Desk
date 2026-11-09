"""Activity page: "Today" and "Yesterday" come from DD.clock(), so demo mode uses the recording's day."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parent.parent / "disputedesk" / "web" / "static" / "activity.js"

# Runs the real dayLabel() from activity.js with a stub DD whose clock is the snapshot's recorded time.
# Line endings are normalised first: the file may be checked out with CRLF.
_PROBE = r"""
const src = require("fs").readFileSync(process.argv[1], "utf8").split(String.fromCharCode(13)).join("");
const start = src.indexOf("function dayLabel(iso)");
const end = src.indexOf("\n  }\n", start) + 4;
const recorded = Date.parse("2026-11-08T12:00:00Z");
const dayLabel = new Function("DD", src.slice(start, end) + "\nreturn dayLabel;")({ clock: () => recorded });
console.log(JSON.stringify({
  today: dayLabel("2026-11-08T12:30:00Z"),
  yesterday: dayLabel("2026-11-07T12:00:00Z"),
  older: dayLabel("2026-11-05T12:00:00Z"),
  realToday: dayLabel(new Date().toISOString()),
}));
"""


def test_day_labels_follow_the_shared_clock():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    out = json.loads(subprocess.run([node, "-e", _PROBE, str(JS)], capture_output=True, text=True,
                                    timeout=30, check=True).stdout)
    assert out["today"] == "Today" and out["yesterday"] == "Yesterday"
    assert out["older"] not in ("Today", "Yesterday")
    assert out["realToday"] not in ("Today", "Yesterday")  # the real date is not "today" in a recording


def test_day_labels_no_longer_read_the_real_date():
    body = JS.read_text(encoding="utf-8").split("function dayLabel(iso)", 1)[1].split("\n  }\n", 1)[0]
    assert "DD.clock()" in body and "new Date()" not in body
