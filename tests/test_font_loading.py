"""Font loading on a cold cache: preconnect + preload, metric-matched fallbacks, a brand that cannot be clipped."""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "disputedesk" / "web"
CSS = (WEB / "static" / "app.css").read_text(encoding="utf-8")
CSS_NO_COMMENTS = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
PAGES = sorted(WEB.glob("*.html"))
FALLBACKS = ("DD Display Fallback", "DD Display Fallback Impact", "DD Display Fallback Arial")


def head(page):
    html = page.read_text(encoding="utf-8")
    return html[: html.index("</head>")]


def font_faces():
    faces = re.findall(r"@font-face\s*\{([^}]*)\}", CSS_NO_COMMENTS)
    return {re.search(r'font-family:\s*"([^"]+)"', f).group(1): f for f in faces}


def rule(selector):
    return " ".join(body for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", CSS_NO_COMMENTS) if sel.strip() == selector)


def test_every_page_head_preconnects_and_preloads_the_font_files():
    assert len(PAGES) >= 7
    for page in PAGES:
        h = head(page)
        assert '<link rel="preconnect" href="https://fonts.googleapis.com">' in h, page.name
        assert '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>' in h, page.name
        tags = re.findall(r'<link rel="preload"[^>]*>', h)
        hrefs = set()
        for tag in tags:
            assert 'as="font"' in tag and 'type="font/woff2"' in tag and re.search(r"\scrossorigin\b", tag), (page.name, tag)
            hrefs.add(re.search(r'href="([^"]+)"', tag).group(1))
        assert any("/bigshouldersdisplay/" in u for u in hrefs), page.name
        assert any("/publicsans/" in u for u in hrefs), page.name
        # exact, versioned files on the font host: no unpinned or local URL
        assert all(re.fullmatch(r"https://fonts\.gstatic\.com/s/[a-z]+/v\d+/[\w-]+\.woff2", u) for u in hrefs), hrefs
        assert h.index('rel="preload"') < h.index("fonts.googleapis.com/css2"), page.name


def test_every_page_preloads_the_same_files():
    assert len({tuple(sorted(re.findall(r'rel="preload"[^>]*href="([^"]+)"', head(p)))) for p in PAGES}) == 1


def test_every_font_request_has_a_font_display_value():
    for page in PAGES:
        for url in re.findall(r'href="(https://fonts\.googleapis\.com/css2\?[^"]+)"', page.read_text(encoding="utf-8")):
            assert re.search(r"[?&]display=(swap|optional|fallback|block)\b", url), (page.name, url)
    for name, face in font_faces().items():
        assert re.search(r"font-display:\s*(swap|optional|fallback|block)\s*;", face), name


def test_metric_matched_display_fallback_faces_exist():
    faces = font_faces()
    for name in FALLBACKS:
        face = faces[name]
        assert "local(" in face and "url(" not in face, name   # local fonts only, nothing downloaded or bundled
        for prop in ("size-adjust", "ascent-override", "descent-override", "line-gap-override"):
            assert re.search(prop + r":\s*\d+(\.\d+)?%", face), (name, prop)
        assert re.search(r"font-weight:\s*\d+\s+\d+", face), name   # a range: no synthetic bold on 800/900


def test_fallback_faces_lead_the_display_stack_ahead_of_generic_fallbacks():
    stack = [s.strip().strip('"') for s in re.search(r"--font-display:\s*([^;]+);", CSS).group(1).split(",")]
    assert stack[0] == "Big Shoulders Display"
    assert stack[1:1 + len(FALLBACKS)] == list(FALLBACKS)
    assert stack[-1] == "sans-serif"


def test_fallback_faces_carry_no_colour():
    for face in font_faces().values():
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", face)


def test_brand_is_nowrap_with_a_fixed_size_and_bounded_width():
    brand = rule(".sidebar .brand")
    for decl in ("white-space: nowrap", "min-width: 0", "max-width: 100%", "overflow: hidden", "flex: 0 0 auto"):
        assert decl in brand, decl
    sizes = re.findall(r"font-size:\s*([^;]+);", brand)
    assert sizes and all(re.fullmatch(r"\d+px", s.strip()) for s in sizes), sizes   # no vw or clamp
    assert "overflow-x: hidden" in rule(".sidebar")
    assert "grid-template-columns: 232px" in rule("body.shell")


def test_sidebar_labels_never_wrap_or_push_the_sidebar_wider():
    link = rule(".side-links a")
    for decl in ("min-width: 0", "white-space: nowrap", "overflow: hidden", "text-overflow: clip"):
        assert decl in link, decl
