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
