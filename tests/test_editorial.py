"""Editorial pass, phase 1: type system, grain, Overview hero and ticker, motion rules."""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "disputedesk" / "web"
CSS = (WEB / "static" / "app.css").read_text(encoding="utf-8")
OVERVIEW = (WEB / "static" / "overview.js").read_text(encoding="utf-8")
COMMON = (WEB / "static" / "common.js").read_text(encoding="utf-8")


def lum(h):
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a, b):
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def over(base, top, alpha):
    """`top` composited at `alpha` over `base` (both #rrggbb)."""
    mix = [round(int(base[i:i + 2], 16) * (1 - alpha) + int(top[i:i + 2], 16) * alpha) for i in (1, 3, 5)]
    return "#" + "".join(f"{c:02x}" for c in mix)


def test_every_page_loads_fonts_without_layout_shift():
    for page in WEB.glob("*.html"):
        html = page.read_text(encoding="utf-8")
        assert "family=Big+Shoulders+Display:wght@800;900" in html, page.name
        assert "display=optional" in html and "display=swap" not in html, page.name


def test_display_font_has_real_fallbacks():
    stack = re.search(r"--font-display:\s*([^;]+);", CSS).group(1)
    assert stack.startswith('"Big Shoulders Display"')
    for fallback in ('"Arial Narrow"', '"Roboto Condensed"', "Impact", "sans-serif"):
        assert fallback in stack


def test_grain_is_at_most_four_percent_and_text_still_passes_contrast():
    grain = re.search(r"--grain:\s*(url\([^;]+\));", CSS).group(1)
    opacity = float(re.search(r"opacity='([0-9.]+)'", grain).group(1))
    assert opacity <= 0.04
    body_rule = re.search(r"(?m)^body \{[^}]+\}", CSS).group(0)  # the standalone body rule
    assert "var(--grain)" in body_rule  # page ground only, under the panels
    tokens = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-f]{6})", CSS))
    for ground in (tokens["ground"], tokens["ground-deep"]):
        for grained in (over(ground, "#ffffff", opacity), over(ground, "#000000", opacity)):
            for text in ("text", "muted", "primary-text", "splash", "danger", "placeholder"):
                assert ratio(tokens[text], grained) >= 4.5, (text, grained)


def test_hero_and_ticker_count_only_disputes_waiting_for_the_seller():
    assert "DD.groupDisputes(rows)" in OVERVIEW and "const waiting = groups.waiting;" in OVERVIEW
    assert 'waiting: pick("WAITING_FOR_SELLER_RESPONSE")' in COMMON
    assert "hero(waiting, heroNext" in OVERVIEW and "ticker(waiting, heroNext)" in OVERVIEW
    assert 'fetch("/api/disputes"' in OVERVIEW and '"POST"' not in OVERVIEW and '"PUT"' not in OVERVIEW
    assert "Awaiting analysis" in OVERVIEW  # shown instead of an invented split


def test_demo_countdowns_use_the_recorded_time_and_say_so():
    assert "mode.demo && mode.recorded_at ? Date.parse(mode.recorded_at) : Date.now()" in COMMON
    assert "function countdown(dueIso, now = clock())" in COMMON
    assert "as recorded" in COMMON and "as of ${DD.recordedLabel()}" in OVERVIEW
    assert "DD.mode.demo || document.hidden" in OVERVIEW  # hero countdown frozen in demo mode


def test_ticker_copy_is_hidden_from_screen_readers_and_motion_rules_exist():
    assert '"aria-hidden": hidden ? "true" : null' in OVERVIEW
    reduced = "".join(CSS.split("@media (prefers-reduced-motion: reduce)")[1:])  # every reduced-motion block
    for rule in (".rise .word", ".ticker-track", ".reveal-pending", ".hero-line"):
        assert rule in reduced, rule
    assert "html.is-hidden *" in CSS and "animation-play-state: paused !important" in CSS
    assert ".ticker:hover .ticker-track" in CSS
    assert "translate3d(-50%, 0, 0)" in CSS  # transform-only loop


def test_reveal_never_hides_content_without_motion():
    body = COMMON.split("function reveal(nodes)", 1)[1].split("\n  }\n", 1)[0]
    assert "reducedMotion()" in body and "IntersectionObserver" in body
    assert "getBoundingClientRect().top < window.innerHeight" in body  # on-screen content is never hidden


# --- phase 2: case cards and the agent's call -------------------------------------------------

CASE = (WEB / "static" / "case.js").read_text(encoding="utf-8")


def between(text, start, end):
    """The source between two markers (both must exist)."""
    return text.split(start, 1)[1].split(end, 1)[0]


def test_case_cards_show_a_verdict_only_for_decided_cases():
    card = between(OVERVIEW, "function caseCard(d)", "function render(rows")
    assert 'const decided = d.decision && d.decision.status === "ok";' in card
    assert 'decided ? el("div", { class: "card-verdict" }' in card and "if (decided)" in card
    assert "DD.caseCaption(d)" in card  # caption metadata with the source tag


def test_main_reason_comes_from_the_existing_read_only_route_and_is_cached():
    body = between(COMMON, "function topReason(row)", "function caseCaption(row)")
    assert "fetch(`/api/decisions/${encodeURIComponent(row.dispute_id)}`" in body
    assert "reasonCache" in body and 'd.status !== "ok"' in body and "method" not in body
    assert "sourceTag(row.source)" in between(COMMON, "function caseCaption(row)", "motion helpers")


def test_card_swap_works_on_focus_and_is_static_without_motion():
    assert ".case-card.has-reason:focus-visible .card-back" in CSS
    assert ".case-card.has-reason:hover .card-back" in CSS
    reduced = "".join(CSS.split("@media (prefers-reduced-motion: reduce)")[1:])
    assert ".card-layer { transition: none; }" in reduced


def test_case_page_tier_one_is_editorial_and_leaves_approval_code_alone():
    assert 'el("span", { class: `verdict-word is-${String(rec.decision).toLowerCase()}`, text: rec.decision })' in CASE
    assert "\"The agent's\"" in CASE and "callHead" in CASE
    for untouched in ("function openConfirm(d, submit)", "function approvePanel(d, submit)", "/dry-run"):
        assert untouched in CASE


# --- phase 3: queue, activity, insights, policies, merchant records --------------------------

def test_insights_charts_open_with_stacked_headers_and_reveal():
    js = (WEB / "static" / "insights.js").read_text(encoding="utf-8")
    for title in ('["The agent\'s", "call"]', '["Risk", "scores"]', '["Why buyers", "disputed"]', '["Time", "left"]'):
        assert title in js
    assert "DD.reveal([rec, risk, byReason, urgency])" in js


def test_activity_and_records_reveal_and_restyle():
    activity = (WEB / "static" / "activity.js").read_text(encoding="utf-8")
    assert "DD.reveal(days)" in activity and "root.replaceChildren(...days)" in activity
    records = (WEB / "static" / "merchant_records.js").read_text(encoding="utf-8")
    assert '{ text: "Earlier" }' in records and "DD.reveal(" in records
    assert ".rule-id { font-family: var(--font-display)" in CSS


def test_queue_grid_columns_untouched_and_rows_not_transformed():
    queue = (WEB / "static" / "queue.js").read_text(encoding="utf-8")
    assert queue.count("headerName:") == 8
    assert ".ag-row" not in CSS.split("editorial pass, phase 1")[1].replace(".ag-row-hover", "")  # no new row rules


# --- phase 4: accessibility fixes -------------------------------------------------------------

def test_ticker_has_a_keyboard_pause_control():
    assert 'class: "ticker-toggle", type: "button", "aria-pressed": "false", text: "Pause"' in OVERVIEW
    assert ".ticker.is-paused .ticker-track { animation-play-state: paused; }" in CSS
    reduced = "".join(CSS.split("@media (prefers-reduced-motion: reduce)")[1:])
    assert ".ticker-toggle { display: none; }" in reduced


def test_case_cards_have_a_clean_accessible_name():
    card = between(OVERVIEW, "function caseCard(d)", "function render(rows")
    assert '"aria-label": `${label}.`' in card and "Main reason: ${text}" in card
    assert "no recommendation yet" in card


# --- queue wording: one definition and one vocabulary everywhere ------------------------------

QUEUE = (WEB / "static" / "queue.js").read_text(encoding="utf-8")
INSIGHTS = (WEB / "static" / "insights.js").read_text(encoding="utf-8")


def test_queue_heading_and_subline_use_the_same_definition_as_overview():
    summary = between(QUEUE, "function updateSummary()", "async function refresh()")
    assert "DD.groupDisputes(rows)" in summary
    assert "waiting on you" in summary and "with PayPal" in summary
    assert 'getElementById("queue-title")' in summary
    assert "const next = waiting.map(" in summary  # next deadline among waiting disputes only
    assert 'id="queue-title"' in (WEB / "queue.html").read_text(encoding="utf-8")


def test_status_vocabulary_is_shared_and_keeps_the_longer_meaning():
    assert 'UNDER_REVIEW: ["With PayPal", "review"]' in COMMON
    assert 'WAITING_FOR_SELLER_RESPONSE: ["Waiting on you", "waiting"]' in COMMON
    assert "PayPal is reviewing" not in COMMON + QUEUE + OVERVIEW + INSIGHTS + CASE
    assert "Under review by PayPal" in COMMON  # tooltip
    assert '"evidence submitted, under review"' in CASE  # case header, only after a recorded submission
    for js in (QUEUE, OVERVIEW, CASE):
        assert "badge ${DD.statusClass" not in js  # every badge goes through DD.statusBadge


def test_deadline_cells_say_with_paypal_and_insights_uses_the_shared_clock():
    assert '"With PayPal"' in between(COMMON, "function deadlineNode(row", "function countdownNode(")
    assert "DD.deadlineNode(p.data)" in QUEUE and "DD.deadlineNode(d)" in OVERVIEW + CASE
    assert "DD.clock()" in INSIGHTS and "Date.now()" not in INSIGHTS
    assert "groups.waiting.filter" in INSIGHTS and '"With PayPal"' in INSIGHTS


# --- visual moments, phase 2: the case map beside the hero ------------------------------------

import json as _json
import shutil as _shutil
import subprocess as _subprocess

import pytest as _pytest

CASEMAP = (WEB / "static" / "casemap.js").read_text(encoding="utf-8")
OVERVIEW_HTML = (WEB / "overview.html").read_text(encoding="utf-8")

_LAYOUT_PROBE = r"""
const DDMap = require(process.argv[1]);
const row = (i, status, decision) => ({ dispute_id: `PP-R-T${i}`, status, seller_response_due_date: status === "WAITING_FOR_SELLER_RESPONSE" ? "2026-11-15T00:00:00Z" : null,
  decision: decision ? { status: "ok", decision } : null });
const out = {};
for (const [waiting, paypal] of [[0, 0], [1, 0], [0, 1], [2, 0], [2, 1], [6, 0], [4, 4], [9, 3]]) {
  const w = Array.from({ length: waiting }, (_, i) => row(i, "WAITING_FOR_SELLER_RESPONSE", i % 3 === 2 ? null : i % 2 ? "ACCEPT" : "DEFEND"));
  const p = Array.from({ length: paypal }, (_, i) => row(100 + i, "UNDER_REVIEW", "DEFEND"));
  for (const compact of [false, true]) {
  const plan = DDMap.layout({ waiting: w, withPayPal: p, withBuyer: [] }, DDMap.MAX_NODES, compact);
  out[`${compact ? "c" : ""}${waiting}+${paypal}`] = { box: [plan.box.W, plan.box.H], more: plan.more, kinds: plan.nodes.map((n) => n.kind), ids: plan.nodes.map((n) => n.d.dispute_id),
    pts: plan.nodes.map((n) => [n.x, n.y, n.side]) };
  }
}
console.log(JSON.stringify({ max: DDMap.MAX_NODES, out }));
"""


def _layouts():
    node = _shutil.which("node")
    if not node:
        _pytest.skip("Node.js is not installed")
    done = _subprocess.run([node, "-e", _LAYOUT_PROBE, str(WEB / "static" / "casemap.js")],
                           capture_output=True, text=True, timeout=30, check=True)
    return _json.loads(done.stdout)


def test_case_map_layout_handles_0_1_2_6_and_more_disputes():
    res = _layouts()
    out = res["out"]
    assert res["max"] == 8
    assert out["0+0"] == {"box": [560, 400], "more": 0, "kinds": [], "ids": [], "pts": []}
    assert out["c0+0"]["box"] == [560, 720]  # phone widths: a taller box
    assert out["c9+3"]["more"] == 4 and len(out["c9+3"]["ids"]) == 8
    assert out["1+0"]["kinds"] == ["defend"] and out["1+0"]["pts"][0][2] == "right"
    assert out["0+1"]["kinds"] == ["paypal"]  # a with-PayPal dispute still shows, dimmed
    assert [p[2] for p in out["2+0"]["pts"]] == ["right", "left"]  # two nodes face each other
    assert out["2+1"]["kinds"] == ["defend", "accept", "paypal"]  # waiting first, then with PayPal
    assert out["6+0"]["kinds"] == ["defend", "accept", "pending", "accept", "defend", "pending"]
    assert len(out["4+4"]["ids"]) == 8 and out["4+4"]["more"] == 0
    assert len(out["9+3"]["ids"]) == 8 and out["9+3"]["more"] == 4  # capped at 8, the rest counted
    assert "paypal" not in out["9+3"]["kinds"]  # every waiting dispute outranks a with-PayPal one
    for case in out.values():
        pts, (W, H) = case["pts"], case["box"]
        assert len({(round(x), round(y)) for x, y, _ in pts}) == len(pts)  # no two nodes overlap
        for x, y, side in pts:
            assert 40 < x < W - 40 and 40 < y < H - 40, (x, y)
        for i, (x1, y1, _) in enumerate(pts):  # nodes stay apart from each other
            for x2, y2, _ in pts[i + 1:]:
                assert ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5 > 40


def test_case_map_labels_link_to_cases_and_use_the_shared_clock():
    labels = between(CASEMAP, "function labels(plan", "function legend(plan")
    assert "href: `/case/${encodeURIComponent(d.dispute_id)}`" in labels
    assert "DD.countdown(d.seller_response_due_date)" in labels and '"data-due": d.seller_response_due_date' in labels
    assert "Date.now()" not in CASEMAP  # countdowns go through DD.clock(), frozen in demo mode
    assert 'paypal: "With PayPal"' in CASEMAP and 'Nothing waiting on you."' in CASEMAP
    assert "`+${plan.more} more`" in labels and 'href: "/queue"' in labels
    assert "fetch(" not in CASEMAP  # draws the rows the Overview already has


def test_case_map_falls_back_to_flat_svg_and_frees_the_renderer():
    create = between(CASEMAP, "function create(rows, ", "return { MAX_NODES")
    assert "DD3D.unavailableReason()" in create and "view = flat(plan)" in create
    gl = between(CASEMAP, "function webgl(plan, container, onLost", "function create(rows, ")
    for must in ("renderer.dispose()", "forceContextLoss()", "webglcontextlost", "io.disconnect()", "ro.disconnect()",
                 "document.hidden", "1000 / 30"):
        assert must in gl, must
    assert "disposeMap();" in between(OVERVIEW, "function render(rows", "// The hero's countdown")
    assert 'window.addEventListener("pagehide", disposeMap)' in OVERVIEW


def test_overview_loads_the_pinned_three_js_and_collapses_the_map_on_narrow_screens():
    assert "three.js/0.160.0/three.min.js" in OVERVIEW_HTML and 'integrity="sha512-' in OVERVIEW_HTML
    order = [OVERVIEW_HTML.index(f"/static/{name}") for name in ("common.js", "graph3d.js", "casemap.js", "overview.js")]
    assert order == sorted(order)
    phase = CSS.split("visual moments, phase 2")[1]
    narrow = phase.split("@media (max-width: 900px)")[1]
    assert ".hero-split { grid-template-columns: minmax(0, 1fr); }" in narrow


def test_wide_content_never_widens_the_page_on_phones():
    # The ticker track is max-content wide; the shell's grid item must be allowed to shrink below it.
    assert re.search(r"body\.shell > main \{[^}]*min-width: 0;", CSS)


# --- visual moments, phase 3: agent pipeline strip and favicon --------------------------------

PIPELINE = (WEB / "static" / "pipeline.js").read_text(encoding="utf-8")

_PIPE_PROBE = r"""
const DDPipe = require(process.argv[1]);
const W = "WAITING_FOR_SELLER_RESPONSE";
const rows = [
  { dispute_id: "A", status: W },             // decided, graph ok, awaiting approval
  { dispute_id: "B", status: W },             // decided, submission in flight -> not awaiting approval
  { dispute_id: "C", status: "UNDER_REVIEW" }, // decided, sent to PayPal
  { dispute_id: "D", status: W },             // graph down: error, no context
  { dispute_id: "E", status: W },             // not analysed yet
];
const ok = { status: "ok", graph_context: { dispute: {} } };
const records = {
  A: { decision: ok, submit: { submission: null } },
  B: { decision: ok, submit: { submission: { state: "sending" } } },
  C: { decision: ok, submit: { submission: { state: "succeeded" } } },
  D: { decision: { status: "error", graph_context: null }, submit: { submission: null } },
  E: { decision: null, submit: { submission: null } },
};
console.log(JSON.stringify({ c: DDPipe.counts(rows, records), empty: DDPipe.counts([], {}), steps: DDPipe.STEPS.map((s) => s.title) }));
"""


def test_pipeline_counts_each_step_from_stored_records():
    node = _shutil.which("node")
    if not node:
        _pytest.skip("Node.js is not installed")
    out = _json.loads(_subprocess.run([node, "-e", _PIPE_PROBE, str(WEB / "static" / "pipeline.js")],
                                      capture_output=True, text=True, timeout=30, check=True).stdout)
    assert out["steps"] == ["Detected", "Graph checked", "Decision", "Your approval", "PayPal"]
    assert out["c"] == {"detected": 5, "graph": 3, "decided": 3, "approval": 1, "paypal": 1}
    assert out["empty"] == {"detected": 0, "graph": 0, "decided": 0, "approval": 0, "paypal": 0}


def test_pipeline_reads_only_existing_get_routes_and_respects_motion():
    assert "fetch(`/api/decisions/${encodeURIComponent(d.dispute_id)}`, { cache: \"no-store\" })" in PIPELINE
    assert "method" not in PIPELINE and "POST" not in PIPELINE
    assert "pipe.node, cases" in OVERVIEW and "ticker(waiting, heroNext), pipe.node" in OVERVIEW
    assert '<script src="/static/pipeline.js"></script>' in OVERVIEW_HTML
    phase = CSS.split("visual moments, phase 3")[1]
    reduced = phase.split("@media (prefers-reduced-motion: reduce)")[1]
    assert ".pipeline.is-flowing .pipe-rail::after { animation: none; display: none; }" in reduced
    assert "overflow: hidden" in phase.split(".pipe-rail {")[1].split("}")[0]  # the light never widens the page
    assert '"aria-label": "Agent pipeline"' in PIPELINE and 'el("ol", { class: "pipe-steps" }' in PIPELINE


def test_every_page_has_the_folder_favicon():
    svg = (WEB / "static" / "favicon.svg").read_text(encoding="utf-8")
    assert "<script" not in svg and 'rel="icon"' in COMMON  # the static file is the no-JavaScript fallback
    assert "function paintFavicon()" in COMMON and 'color("--primary")' in COMMON
    for page in WEB.glob("*.html"):
        assert '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">' in page.read_text(encoding="utf-8"), page.name


# --- visual moments, phase 4: the verdict stamp ------------------------------------------------

STAMP = (WEB / "static" / "stamp.js").read_text(encoding="utf-8")


def test_stamp_appears_only_after_a_successful_approval_from_this_page():
    wiring = between(CASE, "// Verdict stamp:", "const band = el(")
    assert 'lastResult && lastResult.ok && sent && sent.state === "succeeded"' in wiring
    assert "DDStamp.create(sent)" in wiring
    assert CASE.count("DDStamp") == 1  # nowhere else: old submissions and demo data never get a stamp
    assert "lastResult = result;" in between(CASE, "function openConfirm(d, submit)", "function citeTitle(")  # set only by the existing flow
    assert '<script src="/static/stamp.js"></script>' in (WEB / "case.html").read_text(encoding="utf-8")


def test_stamp_is_visual_only_and_static_without_motion():
    assert "fetch(" not in STAMP and '"aria-hidden": "true"' in STAMP
    assert 'provide_evidence: "Defended", accept_claim: "Accepted"' in STAMP
    assert "!DD.reducedMotion() && !played.has(key)" in STAMP  # lands once, never under reduced motion
    phase = CSS.split("visual moments, phase 4")[1]
    assert ".verdict-stamp.is-landing { animation: none; }" in phase.split("@media (prefers-reduced-motion: reduce)")[1]
