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
    for ground in (tokens["ink"], tokens["ink-0"]):
        for grained in (over(ground, "#ffffff", opacity), over(ground, "#000000", opacity)):
            for text in ("text", "muted", "accent", "urgent", "placeholder"):
                assert ratio(tokens[text], grained) >= 4.5, (text, grained)


def test_hero_and_ticker_count_only_disputes_waiting_for_the_seller():
    assert 'const WAITING = "WAITING_FOR_SELLER_RESPONSE";' in OVERVIEW
    assert "rows.filter((d) => d.status === WAITING)" in OVERVIEW
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
    reduced = CSS.split("@media (prefers-reduced-motion: reduce)")[-1]
    for rule in (".rise .word", ".ticker-track", ".reveal-pending", ".hero-line"):
        assert rule in reduced, rule
    assert "html.is-hidden *" in CSS and "animation-play-state: paused !important" in CSS
    assert ".ticker:hover .ticker-track" in CSS
    assert "translate3d(-50%, 0, 0)" in CSS  # transform-only loop


def test_reveal_never_hides_content_without_motion():
    body = COMMON.split("function reveal(nodes)", 1)[1].split("\n  }\n", 1)[0]
    assert "reducedMotion()" in body and "IntersectionObserver" in body
    assert "getBoundingClientRect().top < window.innerHeight" in body  # on-screen content is never hidden
