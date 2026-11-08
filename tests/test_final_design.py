"""Final design pass: shared page hero, type weights, cards, reveals, and the page-by-page redesigns."""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "disputedesk" / "web"
STATIC = WEB / "static"
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
COMMON = (STATIC / "common.js").read_text(encoding="utf-8")
QUEUE = (STATIC / "queue.js").read_text(encoding="utf-8")
QUEUE_HTML = (WEB / "queue.html").read_text(encoding="utf-8")
TOKENS = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-f]{6})", CSS[CSS.index(":root {"):CSS.index("}", CSS.index(":root {"))]))


def lum(h):
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a, b):
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def section(marker):
    return re.sub(r"/\*.*?\*/", "", CSS.split(marker, 1)[1].split("/* ---- ", 1)[0], flags=re.S)


PHASE1 = section("final design pass, phase 1")


# --- phase 1: shared hero, type, cards, buttons, reveals ---------------------------------------

def test_page_hero_has_eyebrow_headline_sentence_and_stat_chips():
    hero = QUEUE_HTML.split('<header class="page-hero">', 1)[1].split("</header>", 1)[0]
    for part in ('class="eyebrow"', '<h1 id="queue-title">', 'class="lede" id="summary"', 'class="stat-chips" id="queue-stats"'):
        assert part in hero, part
    for rule in (".page-hero {", ".page-hero::before {", ".eyebrow {", ".stat-chip {", ".stat-value {"):
        assert rule in PHASE1, rule
    assert "font-variant-caps: all-small-caps; letter-spacing: 0.16em" in PHASE1  # tracked small-caps eyebrow
    assert "function statChip({ label, value, note = null, tone = \"\" })" in COMMON and "function eyebrow(section)" in COMMON


def test_display_face_has_a_lighter_weight_for_secondary_headings():
    for page in WEB.glob("*.html"):
        assert "family=Big+Shoulders+Display:wght@600;800;900" in page.read_text(encoding="utf-8"), page.name
    assert ".stack-head h2, .day h2, .rec-head h3, .policy-title, .bar-label, .chart .stack-head h2 { font-weight: 600; letter-spacing: 0.04em; }" in PHASE1
    h1 = re.search(r"(?m)^h1 \{[^}]+\}", CSS).group(0)
    assert "font-weight: 800" in h1  # page titles stay heavy
    assert "font-weight: 800" in re.search(r"\.stat-value \{[^}]+\}", PHASE1).group(0)  # hero numbers stay heavy


def test_cards_share_depth_and_a_blue_edge_and_secondary_buttons_skip_the_dialog():
    assert ".panel, .chart, .rec-folder, .policy, .case-card { border-top-color: color-mix(in srgb, var(--primary) 55%, var(--border)); }" in PHASE1
    assert "main button.secondary {" in PHASE1 and "text-transform: uppercase" in PHASE1 and "letter-spacing: 0.1em" in PHASE1
    assert "button.secondary {" not in PHASE1.replace("main button.secondary {", "")  # never unscoped
    assert "--splash" not in PHASE1.split("#grid .ag-row")[0].replace("var(--splash)", "")  # no new tangerine


def test_reveals_are_staggered_once_and_never_hide_on_screen_content():
    body = COMMON.split("function revealStaggered(nodes)", 1)[1].split("\n  }\n", 1)[0]
    assert 'n.style.setProperty("--reveal-i", String(i))' in body and "reveal(nodes);" in body  # same once-only reveal
    assert ".is-revealed { transition-delay: calc(var(--reveal-i, 0) * 70ms); }" in PHASE1
    assert "DD.revealStaggered(" in (STATIC / "overview.js").read_text(encoding="utf-8")


# --- phase 1: queue ------------------------------------------------------------------------------

def test_queue_stats_come_from_group_disputes_and_the_shared_clock():
    stats = QUEUE.split("function updateStats()", 1)[1].split("\n  }\n", 1)[0]
    assert "DD.groupDisputes(rows)" in stats and "DD.countdown(r.seller_response_due_date)" in stats
    assert "c.urgent && !DD.isOverdue(c)" in stats  # due soon: under 72 hours, not overdue
    assert "DD.sumAmounts(waiting)" in stats and 'total || "None"' in stats  # a real empty state
    assert 'value: "None"' in stats and '"data-due": next' in stats  # next deadline, live countdown
    assert "Date.now()" not in QUEUE


def test_segmented_filter_replaces_the_status_dropdown_and_keeps_search_and_reason():
    assert 'id="status"' not in QUEUE_HTML and 'id="search"' in QUEUE_HTML and 'id="reason"' in QUEUE_HTML
    for status, label in (("WAITING_FOR_SELLER_RESPONSE", "Waiting on you"), ("UNDER_REVIEW", "With PayPal"), ("", "All")):
        assert f'data-status="{status}"' in QUEUE_HTML and f"> {label} <" not in QUEUE_HTML
        assert label in QUEUE_HTML
    assert 'data-status="" aria-pressed="true"' in QUEUE_HTML  # All by default: the grid opens as before
    assert "filters.status = button.dataset.status;" in QUEUE and 'b.setAttribute("aria-pressed"' in QUEUE
    assert "(!filters.status || node.data.status === filters.status)" in QUEUE  # the same external filter


def test_queue_rows_get_a_left_edge_by_recommendation_only():
    assert '"rec-defend": (p) => Boolean(p.data) && p.data.recommended === "DEFEND"' in QUEUE
    assert "#grid .ag-row.rec-defend { box-shadow: inset 4px 0 0 var(--primary); }" in PHASE1
    assert QUEUE.count("headerName:") == 8


def test_new_pairings_pass_contrast():
    pairs = [("on-primary", "primary-fill", 4.5),   # selected segment
             ("primary-text", "surface", 4.5),       # segment counts, eyebrow separator
             ("muted", "surface", 4.5),              # stat labels and notes
             ("text", "surface", 4.5),               # stat values
             ("danger", "surface", 4.5),             # overdue stat
             ("splash", "surface", 4.5),             # countdown in the next-deadline chip
             ("control-edge", "surface", 3.5),       # segmented control edge
             ("primary", "ground", 3.5)]             # eyebrow rule, row edges
    for fg, bg, minimum in pairs:
        assert ratio(TOKENS[fg], TOKENS[bg]) >= minimum, (fg, bg)


# --- phase 2: insights ---------------------------------------------------------------------------

INSIGHTS = (STATIC / "insights.js").read_text(encoding="utf-8")
INSIGHTS_HTML = (WEB / "insights.html").read_text(encoding="utf-8")
PHASE2 = section("final design pass, phase 2")


def test_insights_uses_the_shared_hero():
    assert '<header class="page-hero">' in INSIGHTS_HTML and 'id="insights-eyebrow"' in INSIGHTS_HTML
    assert 'DD.eyebrow("Insights")' in INSIGHTS and "DD.statChip(" in INSIGHTS and "DD.groupDisputes(rows)" in INSIGHTS


def test_deadline_runway_plots_each_waiting_dispute_on_the_shared_clock():
    rw = INSIGHTS.split("function runway(groups)", 1)[1].split("// ---- risk ruler", 1)[0]
    assert "const now = DD.clock();" in rw and "Date.now()" not in INSIGHTS
    assert "groups.waiting.filter((d) => d.seller_response_due_date)" in rw
    for part in ("d.dispute_id", "verdictChip(d)", "DD.countdown(d.seller_response_due_date)", '"data-due": d.seller_response_due_date'):
        assert part in rw, part  # ID, verdict and a live countdown on every marker
    assert 'el("ol", { class: "rw-markers" }' in rw and "href: caseHref(d)" in rw  # a list of links
    assert "nothing to plot" in rw  # a real empty state


def test_risk_ruler_plots_every_decided_dispute_with_its_caption():
    rl = INSIGHTS.split("function ruler(rows)", 1)[1].split("function hero(", 1)[0]
    assert "Higher means the buyer is more likely right." in rl
    assert "d.decision.risk_score" in rl and "shortId(d.dispute_id)" in rl
    assert "[0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]" in rl
    assert "No recommendations yet, so there are no risk scores to plot." in rl


def test_tiles_count_from_stored_rows_only():
    tl = INSIGHTS.split("function tiles(rows, groups)", 1)[1].split("// ---- deadline runway", 1)[0]
    assert "DD.sumAmounts(groups.waiting)" in tl and 'waitingTotal || "None"' in tl
    assert 'd.decision.decision === "DEFEND"' in tl and "split-bar" in tl
    assert "DD.reasonLabel(r)" in tl


def test_lanes_keep_labels_apart_and_narrow_screens_get_a_list():
    assert "function lanes(items, gap)" in INSIGHTS
    assert "lanes(dated.map(" in INSIGHTS and ", 26)" in INSIGHTS  # runway cards: 190px at ~760px wide
    assert ".rw-card {" in PHASE2 and "width: 190px" in PHASE2
    narrow = PHASE2.split("@media (max-width: 760px)", 1)[1]
    assert ".rw-axis, .rw-soon, .rw-now, .rw-pin { display: none; }" in narrow


def test_insights_colours_stay_in_budget_and_pass_contrast():
    assert "--splash" not in PHASE2  # countdowns get their tangerine from the shared [data-due] rule
    overdue = re.search(r"\.rw-marker\.overdue \.rw-pin[^{]*\{([^}]*)\}", PHASE2).group(1)
    assert "var(--danger)" in overdue  # red only for overdue, and the card says "Overdue by"
    for fg, bg, minimum in [("text", "surface-2", 4.5), ("muted", "surface-2", 4.5), ("primary-text", "surface-2", 4.5),
                            ("muted", "surface", 4.5), ("primary-text", "surface", 4.5), ("control-edge", "surface", 3.5),
                            ("primary", "surface", 3.5), ("danger", "surface", 3.5)]:
        assert ratio(TOKENS[fg], TOKENS[bg]) >= minimum, (fg, bg)


# --- phase 3: merchant records and activity ---------------------------------------------------------

RECORDS = (STATIC / "merchant_records.js").read_text(encoding="utf-8")
RECORDS_HTML = (WEB / "merchant_records.html").read_text(encoding="utf-8")
ACTIVITY = (STATIC / "activity.js").read_text(encoding="utf-8")
ACTIVITY_HTML = (WEB / "activity.html").read_text(encoding="utf-8")
PHASE3 = section("final design pass, phase 3")


def test_records_and_activity_use_the_shared_hero_and_keep_the_production_note():
    for html, page in ((RECORDS_HTML, "records"), (ACTIVITY_HTML, "activity")):
        assert '<header class="page-hero">' in html and f'id="{page}-eyebrow"' in html and f'id="{page}-stats"' in html
    assert 'id="note">In production your store supplies this; in the sandbox it is simulated.' in RECORDS_HTML
    assert RECORDS.count("DD.sourceTag(") >= 4  # shipment, device, transaction and earlier-dispute tags stay


def test_tracking_stepper_uses_only_stored_status_and_dates():
    st = RECORDS.split("function stepper(s)", 1)[1].split("function shipmentCard(", 1)[0]
    assert 'status === "DELIVERED" || Boolean(s.delivered_at)' in st
    assert "Boolean(s.shipped_at)" in st and "s.signed_by_buyer !== undefined" in st
    assert "when: s.shipped_at" in st and "when: s.delivered_at" in st
    assert "Date.now()" not in RECORDS and "new Date()" not in st  # nothing estimated


def test_devices_show_chips_and_linked_disputes_follow_the_graph_rule():
    link = RECORDS.split("function linkedPast(telemetry, past)", 1)[1].split("// ---- shipment", 1)[0]
    assert "p.telemetry.device_hash === telemetry.device_hash" in link and "p.telemetry.ip === telemetry.ip" in link
    for chip in ('chip("Device", t.device_hash', 'chip("IP", t.ip', 'chip("Location", t.geo'):
        assert chip in RECORDS
    assert ".link-tree::before {" in PHASE3 and ".link-tree li::before {" in PHASE3  # the drawn connector
    assert 'text: `linked to ${linkedTo.get(p).join(", ")}`' in RECORDS  # tagged in the Earlier list


def test_activity_filters_summaries_nodes_and_milestones():
    assert 'const groupOf = (kind) => (kind === "approved" || kind === "sent" ? "milestone" : kind);' in ACTIVITY
    assert '"aria-pressed": String(filter === key)' in ACTIVITY and "again.focus()" in ACTIVITY
    assert "function summary(events)" in ACTIVITY and 'class: "day-summary"' in ACTIVITY
    assert ".event.milestone .event-dot {" in PHASE3 and ".event.milestone .event-body {" in PHASE3
    for kind in ("analyzed", "edited", "milestone", "failed"):
        assert f".kind-{kind} .chip-node" in PHASE3  # the chips double as the legend


def test_phase3_colours_stay_in_budget_and_pass_contrast():
    assert "--splash" not in PHASE3  # the approval milestone is blue, not tangerine
    for fg, bg, minimum in [("text", "surface", 4.5), ("primary-text", "surface", 4.5), ("muted", "surface", 4.5),
                            ("on-primary", "primary-fill", 4.5), ("muted", "ground", 4.5), ("text", "ground", 4.5),
                            ("control-edge", "surface", 3.5), ("primary", "ground", 3.5), ("primary", "surface", 3.5),
                            ("danger", "surface", 3.5)]:
        assert ratio(TOKENS[fg], TOKENS[bg]) >= minimum, (fg, bg)


# --- phase 4: policies, split-flap, magnet lines, annotated citations ------------------------------

POLICIES = (STATIC / "policies.js").read_text(encoding="utf-8")
OVERVIEW = (STATIC / "overview.js").read_text(encoding="utf-8")
CASE = (STATIC / "case.js").read_text(encoding="utf-8")
PHASE4 = section("final design pass, phase 4")


def test_policies_count_citations_from_stored_decisions_read_only():
    load = POLICIES.split("async function loadCitations()", 1)[1].split("function citationLine(", 1)[0]
    assert 'fetch("/api/disputes", { cache: "no-store" })' in load and "fetch(`/api/decisions/${encodeURIComponent(d.dispute_id)}`" in load
    assert "method" not in load and 'rec.status !== "ok"' in load and "(rec.reasoning || []).flatMap((p) => p.cites || [])" in load
    assert "Not cited in any stored decision yet." in POLICIES  # a real empty state
    # Save and the demo-disabled behaviour are unchanged
    assert 'send("PUT", "/api/policies", current())' in POLICIES
    assert "button.disabled = true;\n        button.textContent = DD.DEMO_LABEL;" in POLICIES
    assert ".switch .track::after { transition: transform 0.28s" in PHASE4  # animated switch


def test_split_flap_flips_only_on_load_and_on_change_with_a_real_label():
    flaps = OVERVIEW.split("function flaps(text, cls, prev)", 1)[1].split("function countdownLine(", 1)[0]
    assert 'prev === null || prev[i] !== ch ? " flip" : ""' in flaps  # all on load, then only changed tiles
    line = OVERVIEW.split("function countdownLine(next, prev = null)", 1)[1].split("// Magnet lines", 1)[0]
    assert 'node.setAttribute("aria-label", overdue ? c.text : `${c.text} left`)' in line
    assert "DD.countdown(next.seller_response_due_date)" in line  # DD.clock(): frozen in demo mode
    assert "if (fresh.dataset.flap === old.dataset.flap) return;" in OVERVIEW  # no DOM work when nothing changed
    assert ".flap {" in PHASE4 and "width: 0.62em" in PHASE4  # fixed-width cells
    reduced = PHASE4.split("@media (prefers-reduced-motion: reduce)")[1]
    assert ".flap.flip { animation: none; }" in reduced


def test_magnet_lines_turn_on_the_shared_loop_in_blue_tints_without_layout_reads_in_handlers():
    sub = OVERVIEW.split("// Magnet lines turn toward the pointer", 1)[1].split("// Tilt and glare", 1)[0]
    assert "DDPointer.subscribe(" in sub and "Math.atan2(p.y - c.y, p.x - c.x)" in sub and "DDPointer.ease(" in sub
    assert "getBoundingClientRect" in sub.split("if (magnets.dirty)", 1)[1].split("}", 1)[0]  # read in the loop
    for handler in re.findall(r'addEventListener\("(?:scroll|resize)", \(\) => \{([^}]*)\}', sub):
        assert "getBoundingClientRect" not in handler
    magnet = re.search(r"\.magnet \{([^}]*)\}", PHASE4).group(1)
    assert "var(--primary) 22%, var(--ground)" in magnet and "--splash" not in PHASE4
    assert '"aria-hidden": "true"' in OVERVIEW.split("function magnetField()", 1)[1][:200]


def test_text_on_top_of_a_magnet_line_still_passes():
    def mix(a, b, t):
        return "#" + "".join(f"{round(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t):02x}" for i in (1, 3, 5))
    line = mix(TOKENS["ground"], TOKENS["primary"], 0.22)
    for text in ("text", "muted", "splash", "danger", "primary-text"):  # everything the hero board can show
        assert ratio(TOKENS[text], line) >= 4.5, text


def test_annotated_citations_underline_only_cited_facts_and_draw_once():
    facts = CASE.split('const facts = el("ul", { class: "fact-list" }', 1)[1].split("return [", 1)[0]
    assert "cited ? annotated(f.text, i) : el(\"span\", { text: f.text })" in facts and "drawAnnotations(facts);" in facts
    draw = CASE.split("function drawAnnotations(list)", 1)[1].split("function citeTitle(", 1)[0]
    assert "io.unobserve(e.target);" in draw and "DD.reducedMotion()" in draw and "drawnOnce" in draw
    assert 'path.setAttribute("pathLength", "1");' in CASE and '"aria-hidden", "true"' in CASE.split("function annotated(", 1)[1][:900]
    assert "stroke: var(--primary-text)" in PHASE4  # blue, not tangerine
