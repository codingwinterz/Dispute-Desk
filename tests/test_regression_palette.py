"""Palette pass, phase 5: what the colour and pointer-depth pass must not have changed.

Pinned to the exact text it had before the pass began (commit a7884a9): the Approve dialog and its single
POST, the verdict stamp, the demo badge and disabled demo controls, the queue grid's columns, and the
queue wording. Styling may change around these; their behaviour and words may not.
"""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "disputedesk" / "web"
STATIC = WEB / "static"
CASE = (STATIC / "case.js").read_text(encoding="utf-8")
COMMON = (STATIC / "common.js").read_text(encoding="utf-8")
QUEUE = (STATIC / "queue.js").read_text(encoding="utf-8")
STAMP = (STATIC / "stamp.js").read_text(encoding="utf-8")
POLICIES = (STATIC / "policies.js").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")


def between(text, start, end):
    return text.split(start, 1)[1].split(end, 1)[0]


def test_approve_dialog_flow_is_unchanged():
    dialog = between(CASE, "function openConfirm(d, submit)", "function citeTitle(")
    # one POST, only from the confirm button, with an explicit confirmation
    assert dialog.count("fetch(") == 1 and 'method: "POST"' in dialog
    assert "body: JSON.stringify({ confirm: true, action: submit.action, text: submit.text })" in dialog
    assert "fetch(`/api/submissions/${encodeURIComponent(d.dispute_id)}`" in dialog
    assert 'const cancel = el("button", { class: "secondary", type: "button", text: "Cancel", autofocus: true });' in dialog
    assert 'const confirm = el("button", { class: "primary approve", type: "button", text: submit.label });' in dialog
    for words in ('el("dt", { text: "Action" })', 'el("dt", { text: "Dispute" })', 'el("dt", { text: "Amount" })',
                  "It can't be withdrawn.", "It can't be undone.", 'status.textContent = "Sending to PayPal…";',
                  "lastResult = result;", "dialog.showModal();"):
        assert words in dialog, words
    # nothing from the pointer or lighting passes reaches the dialog
    for later in ("DDPointer", "--glow", "--shadow", "tilt"):
        assert later not in dialog
    assert "dialog" not in CSS.split("palette pass, phase 2: depth lighting")[1]


def test_approve_button_is_still_gated_and_says_demo_mode_in_demo():
    panel = between(CASE, "function approvePanel(d, submit)", "function openConfirm(")
    assert 'disabled: demo || !submit.can_submit,' in panel and "text: demo ? DD.DEMO_LABEL : submit.label" in panel
    assert "if (!demo) approve.addEventListener(\"click\", () => openConfirm(d, submit));" in panel
    assert 'const DEMO_LABEL = "Demo mode: recorded sandbox data";' in COMMON


def test_verdict_stamp_is_unchanged():
    assert 'provide_evidence: "Defended", accept_claim: "Accepted"' in STAMP
    assert "!DD.reducedMotion() && !played.has(key)" in STAMP and '"aria-hidden": "true"' in STAMP
    wiring = between(CASE, "// Verdict stamp:", "const band = el(")
    assert 'lastResult && lastResult.ok && sent && sent.state === "succeeded"' in wiring
    assert CASE.count("DDStamp") == 1


def test_demo_badge_and_disabled_demo_controls_on_every_page():
    assert 'el("span", { class: "demo-badge", title: `Recorded sandbox data from ${recorded}. Nothing is sent to PayPal.`, text: "Demo data" })' in COMMON
    for page in WEB.glob("*.html"):
        assert '<script src="/static/common.js"></script>' in page.read_text(encoding="utf-8"), page.name  # renders the badge
    assert 'const save = el("button", { class: "primary", type: "button", disabled: DD.mode.demo,' in CASE  # draft save
    assert "button.disabled = true;\n        button.textContent = DD.DEMO_LABEL;" in POLICIES
    assert ".demo-badge {" in CSS


def test_queue_grid_columns_are_unchanged_and_rows_are_not_styled():
    headers = re.findall(r'headerName: "([^"]+)"', QUEUE)
    assert headers == ["Respond within", "Dispute", "Reason", "Amount", "Status", "Recommended", "Risk", "Source"]
    for phase in ("palette pass, phase 2", "palette pass, phase 3", "palette pass, phase 4"):
        assert ".ag-row" not in CSS.split(phase)[1]
    assert "DDPointer" not in QUEUE


def test_queue_wording_is_unchanged():
    assert 'WAITING_FOR_SELLER_RESPONSE: ["Waiting on you", "waiting"]' in COMMON
    assert 'UNDER_REVIEW: ["With PayPal", "review"]' in COMMON
    assert 'title.textContent = n ? `${n} ${n === 1 ? "dispute" : "disputes"} waiting on you` : "Nothing waiting on you";' in QUEUE
    summary = between(QUEUE, "function updateSummary()", "async function refresh()")
    for words in ("waiting on you. ", "with PayPal. ", "waiting on the buyer. ", "Next response due in ",
                  "still being analyzed.", "Every waiting dispute has a recommendation."):
        assert words in summary, words
