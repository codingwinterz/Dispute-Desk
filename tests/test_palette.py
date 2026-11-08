"""Palette pass, phase 1: one token block, contrast with the grain composited, colour roles kept in budget."""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "disputedesk" / "web"
CSS = (WEB / "static" / "app.css").read_text(encoding="utf-8")
COMMON = (WEB / "static" / "common.js").read_text(encoding="utf-8")
ROOT_BLOCK = CSS[CSS.index(":root {"):CSS.index("}", CSS.index(":root {"))]
AFTER_ROOT = CSS[CSS.index("}", CSS.index(":root {")) + 1:]
TOKENS = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-f]{6})", ROOT_BLOCK))
COLOUR_LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(\s*\d|\bhsla?\(|\b0x[0-9a-fA-F]{6}\b")


def lum(h):
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a, b):
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def over(base, top, alpha):
    mix = [round(int(base[i:i + 2], 16) * (1 - alpha) + int(top[i:i + 2], 16) * alpha) for i in (1, 3, 5)]
    return "#" + "".join(f"{c:02x}" for c in mix)


def backgrounds():
    """Every ground and surface text or marks can sit on, with the page grain composited on the grounds."""
    grain = float(re.search(r"opacity='([0-9.]+)'", re.search(r"--grain:\s*(url\([^;]+\));", CSS).group(1)).group(1))
    out = {name: TOKENS[name] for name in ("ground", "ground-deep", "surface", "surface-2")}
    for name in ("ground", "ground-deep"):
        out[f"{name}+light grain"] = over(TOKENS[name], "#ffffff", grain)
        out[f"{name}+dark grain"] = over(TOKENS[name], "#000000", grain)
    return out


def rules():
    """(selector, declarations) for every CSS rule, including rules inside @media blocks."""
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", AFTER_ROOT)]


def test_every_palette_role_is_a_token_in_the_one_root_block():
    for name in ("ground", "ground-deep", "surface", "surface-2", "border", "text", "muted", "placeholder", "control-edge",
                 "primary", "primary-fill", "primary-text", "on-primary", "seeded", "splash", "on-splash", "danger",
                 "focus", "shade", "sheen"):
        assert name in TOKENS, name
    # Other :root blocks (the phone breakpoint's --gutter) may change spacing, never colours.
    for block in re.findall(r":root \{([^}]*)\}", AFTER_ROOT):
        assert not COLOUR_LITERAL.search(block), block


def test_text_tokens_pass_4_5_on_every_ground_and_surface_with_grain():
    for text in ("text", "muted", "placeholder", "primary-text", "splash", "danger", "focus", "seeded"):
        for where, bg in backgrounds().items():
            assert ratio(TOKENS[text], bg) >= 4.5, (text, where, round(ratio(TOKENS[text], bg), 2))


def test_graphic_marks_and_control_edges_reach_3_5():
    for mark in ("primary", "control-edge", "focus", "seeded", "splash", "danger"):
        for where, bg in backgrounds().items():
            assert ratio(TOKENS[mark], bg) >= 3.5, (mark, where, round(ratio(TOKENS[mark], bg), 2))


def test_text_on_fills():
    assert TOKENS["on-primary"] == "#ffffff"  # white on solid blue chips
    assert ratio(TOKENS["on-primary"], TOKENS["primary-fill"]) >= 4.5
    assert ratio(TOKENS["on-splash"], TOKENS["splash"]) >= 4.5  # charcoal on tangerine (Approve, DUE SOON)
    assert lum(TOKENS["on-splash"]) < 0.02  # charcoal, not white
    assert ".chip.defend { box-shadow: 0 0 0 1px var(--primary)" in CSS  # its edge is the 3.5:1 blue


def test_red_and_tangerine_are_clearly_different_hues():
    import colorsys

    def hue(h):
        return colorsys.rgb_to_hsv(*(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)))[0] * 360
    gap = abs(hue(TOKENS["splash"]) - hue(TOKENS["danger"]))
    assert min(gap, 360 - gap) >= 30


def test_no_hard_coded_colours_outside_the_root_block():
    assert not COLOUR_LITERAL.findall(AFTER_ROOT), COLOUR_LITERAL.findall(AFTER_ROOT)[:5]
    for js in (WEB / "static").glob("*.js"):
        found = COLOUR_LITERAL.findall(js.read_text(encoding="utf-8"))
        assert not found, (js.name, found[:5])
    for page in WEB.glob("*.html"):
        found = COLOUR_LITERAL.findall(re.sub(r'href="https://fonts[^"]+"', "", page.read_text(encoding="utf-8")))
        assert not found, (page.name, found[:5])
    # The one exception: the static favicon file (SVG can't read CSS); common.js repaints it from the tokens.


def test_tangerine_stays_on_its_short_list():
    allowed = ("aria-current", ".hero-line .hl", ".ticker-item .hl", ".badge.waiting", "[data-due]", ".pipe-title::after",
               ".verdict-stamp", "svg .node.used", "svg .edge.used", ".graph-legend .swatch", ".shape.glow",
               ".approve", ".due-soon", ".map-ring", ".lg.is-waiting")
    for sel, body in rules():
        if "--splash" in body or "--on-splash" in body:
            assert any(a in sel for a in allowed), sel
    for name in ("casemap.js", "graph3d.js"):
        js = (WEB / "static" / name).read_text(encoding="utf-8")
        # casemap: the waiting ring. graph3d: the cited glow (palette entry, label outline, three halo stops).
        assert js.count('"--splash"') <= (1 if name == "casemap.js" else 5), name
    assert '"--splash"' not in COMMON  # never in shared helpers


def test_red_only_for_errors_offline_and_overdue():
    allowed = (".banner", ".chip.error", ".live.is-offline", ".overdue", ".hero-line .hl.urgent", ".error-text",
               ".result.error", ".event.failed", "li.failed", ".figure.urgent")
    for sel, body in rules():
        if "--danger" in body:
            assert any(a in sel for a in allowed), sel


def test_defend_solid_accept_outlined_with_a_2px_stroke():
    assert ".chip.defend { background: var(--primary-fill); color: var(--on-primary); }" in CSS
    assert ".chip.accept { border: 1.5px solid var(--primary); color: var(--primary-text);" in CSS
    accept = re.search(r"\.verdict-word\.is-accept \{([^}]*)\}", CSS).group(1)
    assert "color: transparent" in accept
    width = float(re.search(r"-webkit-text-stroke:\s*([0-9.]+)px", accept).group(1))
    assert width >= 2
    for name in ("overview.js", "case.js"):
        assert "verdict-word is-${" in (WEB / "static" / name).read_text(encoding="utf-8")


def test_waiting_and_with_paypal_markers_and_source_tags():
    assert ".badge.waiting::before { background: var(--splash); }" in CSS
    assert ".badge.review::before { background: transparent; border: 2px solid var(--primary);" in CSS
    assert ".tag.paypal { border-color: var(--primary);" in CSS and ".tag.seeded { border: 1px dashed" in CSS
    assert 'class: "tag paypal"' in COMMON


def test_due_soon_chip_under_72_hours_and_red_only_when_overdue():
    assert 'el("span", { class: "due-soon", hidden: !(c.urgent && !isOverdue(c)), text: "Due soon" })' in COMMON
    assert 'node.classList.toggle("overdue", isOverdue(c));' in COMMON  # the shared tick keeps it current
    assert "[data-due].overdue .cd-main { color: var(--danger); }" in CSS
    chip = re.search(r"\.due-soon \{([^}]*)\}", CSS).group(1)
    assert "color: var(--on-splash)" in chip and "background: var(--splash)" in chip
    overview = (WEB / "static" / "overview.js").read_text(encoding="utf-8")
    assert '`hl${c.urgent ? " urgent" : ""}`' not in overview  # the hero turns red only when overdue


def test_scripts_read_colours_through_a_probe_element():
    body = COMMON.split("function resolveColor(name)", 1)[1].split("function color(name)", 1)[0]
    assert 'probe.style.color = `var(${name})`' in body and "getComputedStyle(probe).color" in body
    assert "console.error(`DisputeDesk: colour token ${name} is not defined" in body
    assert "color(srgb" in body  # color-mix() results parse too
    assert "DD.colorNum(" in (WEB / "static" / "graph3d.js").read_text(encoding="utf-8")
    assert "DD.colorNum(" in (WEB / "static" / "casemap.js").read_text(encoding="utf-8")


def test_approve_action_is_the_one_tangerine_button():
    case = (WEB / "static" / "case.js").read_text(encoding="utf-8")
    assert case.count('class: "primary approve"') == 2  # the Approve button and its confirm button
    assert "button.primary.approve { background: var(--splash); color: var(--on-splash); }" in CSS


# --- phase 2: depth lighting ------------------------------------------------------------------

def _pct(name):
    return float(re.search(rf"--{name}:\s*([0-9.]+)%", ROOT_BLOCK).group(1)) / 100


def _chmax(a, b):
    """Channel-wise maximum: what mix-blend-mode: lighten paints for an opaque light over a backdrop."""
    return "#" + "".join(f"{max(int(a[i:i + 2], 16), int(b[i:i + 2], 16)):02x}" for i in (1, 3, 5))


def _blends_lighten(selector):
    rule = re.search(re.escape(selector) + r" \{([^}]*)\}", AFTER_ROOT).group(1)
    return "mix-blend-mode: lighten" in rule, rule


def _lit_backgrounds():
    """The brightest point of every lit area. The ground carries the lift and the grain. Lights drawn as
    opaque blue-tinted ground with mix-blend-mode: lighten (hero pools, cursor spotlight) take the
    channel-wise maximum with whatever is under them, so overlapping lights never add; if a light is not
    drawn that way, it is stacked additively instead (the stricter model)."""
    grain = float(re.search(r"opacity='([0-9.]+)'", re.search(r"--grain:\s*(url\([^;]+\));", CSS).group(1)).group(1))
    blue, lift = TOKENS["primary"], _pct("lift-ground")
    lifted = over(TOKENS["ground"], blue, lift)
    grounds = {"ground lift": lifted, "ground lift + light grain": over(lifted, "#ffffff", grain),
               "ground lift + dark grain": over(lifted, "#000000", grain)}
    out = dict(grounds)
    lights = {"hero glow": (".hero-split::before", "glow-hero"), "soft hero glow": (".hero-split::before", "glow-hero-soft"),
              "cursor spotlight": (".spotlight", "glow-spotlight")}
    for name, (selector, token) in lights.items():
        lighten, rule = _blends_lighten(selector)
        assert f"var(--{token})" in rule, (selector, token)
        for gname, g in grounds.items():
            if lighten and f"var(--{token}), var(--ground))" in rule:   # opaque tinted ground, blended lighten
                out[f"{gname} under {name}"] = _chmax(g, over(TOKENS["ground"], blue, _pct(token)))
            else:                                                       # otherwise: add it on top
                out[f"{gname} + {name}"] = over(g, blue, _pct(token))
    # the two hero pools and the spotlight can meet: under lighten the brightest wins; the strongest is above
    out["verdict panel glow"] = over(TOKENS["surface"], blue, _pct("glow-verdict"))
    out["card glare (under the text)"] = over(TOKENS["surface"], blue, _pct("glare"))
    return out


def test_text_and_marks_still_pass_on_top_of_the_glows():
    for where, bg in _lit_backgrounds().items():
        for text in ("text", "muted", "placeholder", "primary-text", "splash", "danger", "focus", "seeded"):
            assert ratio(TOKENS[text], bg) >= 4.5, (text, where, round(ratio(TOKENS[text], bg), 2))
        for mark in ("primary", "control-edge", "focus", "seeded", "splash", "danger"):
            assert ratio(TOKENS[mark], bg) >= 3.5, (mark, where, round(ratio(TOKENS[mark], bg), 2))


def test_lighting_uses_blue_and_neutral_tokens_only_and_never_moves():
    phase = CSS.split("palette pass, phase 2: depth lighting")[1]
    lighting = [phase, re.search(r"(?m)^body \{[^}]+\}", CSS).group(0),
                re.search(r"\.verdict-band > \.panel:first-child \{[^}]+\}", CSS).group(0)]
    lighting += re.findall(r"--(?:shadow-\d|lift-ground|glow-[a-z-]+):[^;]+;", ROOT_BLOCK)
    for chunk in lighting:
        assert "--splash" not in chunk and "--danger" not in chunk, chunk[:80]
        assert "animation" not in chunk and "transition" not in chunk.replace("transition: none", ""), chunk[:80]
        assert not COLOUR_LITERAL.search(chunk), chunk[:80]
    for token in ("var(--glow-hero)", "var(--glow-hero-soft)"):
        assert token in phase
    assert "var(--lift-ground)" in lighting[1] and "var(--glow-verdict)" in lighting[2]


def test_lighting_leaves_grid_rows_and_the_approve_dialog_alone():
    phase = CSS.split("palette pass, phase 2: depth lighting")[1]
    assert ".ag-row" not in phase and "dialog" not in phase and ".confirm" not in phase


# --- phase 3: cursor spotlight, card tilt and glare -----------------------------------------------

POINTER = (WEB / "static" / "pointer.js").read_text(encoding="utf-8")
OVERVIEW = (WEB / "static" / "overview.js").read_text(encoding="utf-8")


def test_one_shared_animation_loop_with_no_layout_reads_in_pointer_handlers():
    assert POINTER.count("requestAnimationFrame(frame)") == 2 and "requestAnimationFrame" not in OVERVIEW
    move = POINTER.split('window.addEventListener("pointermove"', 1)[1].split("});", 1)[0]
    for read in ("getBoundingClientRect", "offsetWidth", "offsetHeight", "clientWidth", "getComputedStyle", "style."):
        assert read not in move, read
    over_handler = OVERVIEW.split('root.addEventListener("pointerover"', 1)[1].split("});", 1)[0]
    assert "getBoundingClientRect" not in over_handler and "offset" not in over_handler
    assert "(1 - Math.exp(-dt / tau))" in POINTER  # eased, frame-rate independent


def test_pointer_effects_are_off_on_touch_reduced_motion_and_hidden_tabs():
    assert '"(hover: hover) and (pointer: fine)"' in POINTER and '"(prefers-reduced-motion: reduce)"' in POINTER
    assert "document.hidden" in POINTER and "visibilitychange" in POINTER
    phase = CSS.split("palette pass, phase 3")[1]
    assert "@media (prefers-reduced-motion: reduce), (hover: none), (pointer: coarse)" in phase
    assert ".spotlight, .case-card::before { display: none; }" in phase


def test_tilt_is_at_most_7_degrees_and_effects_stay_off_grid_rows_and_the_dialog():
    assert "const MAX_TILT = 7;" in OVERVIEW and ".case-card" in OVERVIEW
    for js in (POINTER, OVERVIEW.split("pointer effects, on the shared DDPointer loop", 1)[1]):
        code = re.sub(r"//.*", "", js)  # the code, not the comments that explain the rule
        assert ".ag-" not in code and "dialog" not in code
    phase = CSS.split("palette pass, phase 3")[1]
    assert ".ag-row" not in phase and "dialog" not in phase


def test_spotlight_is_blue_and_every_page_loads_the_shared_pointer_module():
    rule = re.search(r"\.spotlight \{([^}]*)\}", CSS).group(1)
    assert "var(--primary) var(--glow-spotlight)" in rule and "--splash" not in rule
    assert "translate3d" in POINTER and "mix-blend-mode: lighten" in rule
    for page in WEB.glob("*.html"):
        html = page.read_text(encoding="utf-8")
        assert html.index("/static/common.js") < html.index("/static/pointer.js"), page.name


# --- phase 4: hero layers, case map camera, node hover --------------------------------------------

CASEMAP = (WEB / "static" / "casemap.js").read_text(encoding="utf-8")


def test_hero_parallax_has_layers_moving_at_different_rates():
    phase = CSS.split("palette pass, phase 4")[1]
    rates = re.findall(r"calc\(var\(--px, 0\) \* (-?\d+)px\)", phase)
    depths = re.findall(r"\.hero-line:nth-child\(\d\) \{ --depth: (\d+)px; \}", phase)
    assert sorted(set(map(int, rates + depths))) == sorted(map(int, rates + depths))  # every layer differs
    assert len(rates + depths) >= 4 and any(int(r) < 0 for r in rates)  # the glow drifts against the pointer
    assert 'hero.style.setProperty("--px"' in OVERVIEW and "transition: transform 0.25s" not in CSS
    reduced = phase.split("@media (prefers-reduced-motion: reduce)")[1]
    assert ".hero-board .cap, .hero-split::before { transform: none !important; }" in reduced


def test_map_camera_follows_the_pointer_on_the_shared_loop_and_is_released():
    gl = CASEMAP.split("function webgl(plan, container, onLost", 1)[1].split("function create(rows, ", 1)[0]
    assert "DDPointer.subscribe(" in gl and "unsubscribe();" in gl
    assert "world.rotation.set(tilt.x, tilt.y, 0)" in gl and "DDPointer.ease(tilt.y" in gl
    assert "requestAnimationFrame(frame)" in gl  # the map's own render loop draws; pointer maths stays on DDPointer
    assert 'addEventListener("pointermove"' not in CASEMAP  # pointer position only comes from DDPointer


def test_raycast_is_throttled_and_only_after_the_pointer_moves():
    assert "const RAY_MS = 60;" in CASEMAP
    assert "now - lastRay >= RAY_MS && (p.x !== lastPX || p.y !== lastPY)" in CASEMAP
    assert "raycaster.setFromCamera(ndc, camera)" in CASEMAP and "intersectObjects(picks, false)" in CASEMAP
    handlers = re.findall(r'addEventListener\("(?:scroll|resize)", markDirty', CASEMAP)
    assert len(handlers) == 2  # scroll/resize only mark the rect dirty; it is read inside the loop


def test_hover_lifts_and_glows_blue_and_shows_id_verdict_and_countdown():
    assert "n.group.position.z = n.baseZ + n.h * 22" in CASEMAP and "n.shine.material.opacity = n.h * 0.9" in CASEMAP
    chip = CASEMAP.split("function showChip(plan, ui, i)", 1)[1].split("\n  }\n", 1)[0]
    assert "d.dispute_id" in chip and "KIND_WORD[node.kind]" in chip
    assert "DD.countdown(d.seller_response_due_date)" in chip and '"data-due"' in chip  # DD.clock() in demo mode
    assert '"aria-hidden": "true"' in CASEMAP.split('class: "map-hover"', 1)[1][:40]
    assert CASEMAP.count('"--splash"') == 1  # the waiting ring only; the hover glow is blue


def test_flat_fallback_is_kept_and_shows_the_hover_label_too():
    create = CASEMAP.split("function create(rows, ", 1)[1]
    assert "if (off) view = flat(plan);" in create and "showChip(plan, ui," in create
    assert "setHover() {}" in CASEMAP.split("function flat(plan)", 1)[1].split("function palette()", 1)[0]
