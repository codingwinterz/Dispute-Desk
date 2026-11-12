"""Demo-mode guided tour: loaded only in demo mode, read-only, text-only DOM, links to existing routes."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from disputedesk.dispute_store import DisputeStore
from disputedesk.webhooks import EventStore, create_app
from tests.fakes import sample_dispute
from tests.test_demo import demo_client, no_outside_access, snapshot_file  # noqa: F401  (fixtures)
from tests.test_palette import COLOUR_LITERAL

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "disputedesk" / "web"
TOUR = (WEB / "static" / "tour.js").read_text(encoding="utf-8")
COMMON = (WEB / "static" / "common.js").read_text(encoding="utf-8")
CSS = (WEB / "static" / "app.css").read_text(encoding="utf-8")
BANNED = re.compile(r"\b(decides?|decided|won|automatically resolves?|100% live|instant refund|fraud\w*|abuse\w*)\b",
                    re.IGNORECASE)


def go_links() -> list[str]:
    case = re.search(r'const CASE = "([^"]+)";', TOUR).group(1)
    block = TOUR.split("const STEPS = [", 1)[1].split("];", 1)[0]
    hrefs = re.findall(r'href: (?:"([^"]+)"|`\$\{CASE\}([^`]*)`|(CASE))', block)
    links = []
    for plain, suffix, bare in hrefs:
        links.append(plain or (case + suffix if suffix else case) if not bare else case)
    return links


# --- loading: demo mode only ----------------------------------------------------------------------

def test_only_common_js_loads_the_tour_and_only_in_its_demo_branch():
    for page in WEB.glob("*.html"):
        assert "tour.js" not in page.read_text(encoding="utf-8"), page.name
    assert COMMON.count("tour.js") == 1
    demo_branch = COMMON.split("if (!m.demo) return;", 1)[1].split("});", 1)[0]
    assert 'document.body.append(el("script", { src: "/static/tour.js" }));' in demo_branch
    # and the tour checks again before drawing anything
    assert "if (typeof DD === \"undefined\" || !DD.mode || !DD.mode.demo || DD.mode.scratch) return;" in TOUR


def test_tour_is_served_in_demo_mode(demo_client):  # noqa: F811
    client, router = demo_client
    assert client.get("/api/mode").json()["demo"] is True
    resp = client.get("/static/tour.js")
    assert resp.status_code == 200 and "Take the 60-second tour" in resp.text
    assert router.calls.call_count == 0


def test_live_and_scratch_modes_never_load_the_tour(tmp_path, monkeypatch):
    store = DisputeStore(tmp_path / "a.db")
    store.claim("PP-R-TST-1", "poll", sample_dispute("PP-R-TST-1"), full=True)
    live = TestClient(create_app(verifier=object(), store=EventStore(tmp_path / "logs"), dispute_store=store))
    assert live.get("/api/mode").json()["demo"] is False
    for path in ("/", "/queue", "/activity", "/insights", "/policies", "/merchant-records", "/case/PP-R-TST-1"):
        page = live.get(path)
        assert page.status_code == 200 and "tour.js" not in page.text, path

    scratch_db = tmp_path / "scratch.db"
    DisputeStore(scratch_db).claim("PP-R-TST-1", "poll", sample_dispute("PP-R-TST-1"), full=True)
    monkeypatch.setenv("DISPUTEDESK_DB", str(scratch_db))
    monkeypatch.setenv("DISPUTEDESK_GRAPH", "dd_scratch_test")
    with TestClient(create_app(verifier=object(), store=EventStore(tmp_path / "logs"), demo=False)) as scratch:
        mode = scratch.get("/api/mode").json()
        assert mode["scratch"] is True and mode["demo"] is False  # common.js's demo branch never runs
        assert "tour.js" not in scratch.get("/").text


# --- steps and links ------------------------------------------------------------------------------

def test_six_steps_each_with_a_go_link_to_an_existing_route(demo_client):  # noqa: F811
    client, _ = demo_client
    block = TOUR.split("const STEPS = [", 1)[1].split("];", 1)[0]
    assert block.count("title:") == 6
    links = go_links()
    assert links == ["/", "/queue", "/case/PP-R-YOV-10196864", "/case/PP-R-YOV-10196864#timeline-title",
                     "/policies", "/activity"]
    for link in links:
        assert client.get(link.split("#")[0]).status_code == 200, link
    case_js = (WEB / "static" / "case.js").read_text(encoding="utf-8")
    assert 'id: "timeline-title"' in case_js  # the anchor exists on the case page
    assert "https://github.com/codingwinterz/Dispute-Desk#what-is-real-and-what-is-simulated" in TOUR
    assert "What is real and what is simulated" in TOUR
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## What is real and what is simulated" in readme


def test_wording_is_accurate():
    assert not BANNED.findall(TOUR), BANNED.findall(TOUR)
    for word in ("recommends", "recorded", "simulated", "seeded", "Read-only", "nothing is sent"):
        assert word in TOUR, word
    line = next(ln for ln in (ROOT / "README.md").read_text(encoding="utf-8").splitlines() if "60-second tour" in ln)
    assert not BANNED.findall(line)


# --- safety and accessibility ------------------------------------------------------------------

def test_no_html_injection_colour_literals_or_network_requests():
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert bad not in TOUR
    assert not COLOUR_LITERAL.findall(TOUR)
    for net in ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "import("):
        assert net not in TOUR
    tour_css = CSS.split("/* Demo-mode guided tour", 1)[1]
    assert not COLOUR_LITERAL.findall(tour_css)


def test_controls_keyboard_storage_and_motion():
    for label in ('"Next"', '"Back"', '"Close"', '"Take the 60-second tour"'):
        assert label in TOUR
    assert 'e.key !== "Escape"' in TOUR
    assert 'type: "button"' in TOUR and '"aria-label": "Demo tour"' in TOUR and '"aria-live": "polite"' in TOUR
    assert "aria-modal" not in TOUR and "showModal" not in TOUR  # never blocks the page
    uses = list(re.finditer(r"window\.sessionStorage", TOUR))
    assert len(uses) == 2 and TOUR.count("sessionStorage") == 2
    for m in uses:
        assert "try {" in TOUR[max(0, m.start() - 80):m.start()], "sessionStorage outside try"
    assert "@media (prefers-reduced-motion: no-preference)" in CSS.split("/* Demo-mode guided tour", 1)[1]
    assert ".tour { position: fixed;" in CSS
