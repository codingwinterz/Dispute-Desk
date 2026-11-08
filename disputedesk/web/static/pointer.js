// Pointer effects, shared by every page: ONE requestAnimationFrame loop drives all of them.
// - Pointer events only record numbers (clientX/Y): no layout reads, no style writes.
// - Each effect subscribes a step(dt, pointer) function that eases toward its target and returns true
//   while it is still moving. The loop stops when every effect has settled, so an idle page costs nothing.
// - Off on touch-only devices, under prefers-reduced-motion (static final state), and in hidden tabs.
// - Never applied to AG Grid rows or the approval dialog: effects attach only to the page background and
//   to elements that opt in (the Overview hero and case cards).
"use strict";

const DDPointer = (() => {
  const fineQuery = window.matchMedia("(hover: hover) and (pointer: fine)");
  const motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  const enabled = () => fineQuery.matches && !motionQuery.matches;

  const pointer = { x: window.innerWidth / 2, y: window.innerHeight / 2, inside: false, vw: window.innerWidth, vh: window.innerHeight };
  const steps = new Set();
  const resets = new Set();
  let raf = 0, last = 0;

  function frame(now) {
    raf = 0;
    if (document.hidden || !enabled()) return;
    const dt = Math.min(64, now - last || 16);
    last = now;
    let busy = false;
    for (const step of steps) if (step(dt, pointer)) busy = true;
    if (busy) raf = requestAnimationFrame(frame);
  }
  function kick() {
    if (!raf && enabled() && !document.hidden) { last = performance.now(); raf = requestAnimationFrame(frame); }
  }

  /** Exponential smoothing that behaves the same at any frame rate. tau in ms. */
  const ease = (current, target, dt, tau = 120) => current + (target - current) * (1 - Math.exp(-dt / tau));
  const settled = (a, b, eps = 0.05) => Math.abs(a - b) < eps;

  function subscribe(step, reset) {
    steps.add(step);
    if (reset) resets.add(reset);
    kick();
    return () => { steps.delete(step); if (reset) { resets.delete(reset); reset(); } };
  }

  window.addEventListener("pointermove", (e) => {
    if (e.pointerType !== "mouse" && e.pointerType !== "pen") return;
    pointer.x = e.clientX; pointer.y = e.clientY; pointer.inside = true;
    kick();
  }, { passive: true });
  document.documentElement.addEventListener("pointerleave", () => { pointer.inside = false; kick(); });
  window.addEventListener("blur", () => { pointer.inside = false; kick(); });
  window.addEventListener("resize", () => { pointer.vw = window.innerWidth; pointer.vh = window.innerHeight; kick(); }, { passive: true });
  document.addEventListener("visibilitychange", () => { if (document.hidden) { cancelAnimationFrame(raf); raf = 0; } else kick(); });
  const onModeChange = () => { if (enabled()) kick(); else { cancelAnimationFrame(raf); raf = 0; for (const r of resets) r(); } };
  fineQuery.addEventListener("change", onModeChange);
  motionQuery.addEventListener("change", onModeChange);

  // ---- the page-background spotlight (soft blue, never tangerine) ----------------------------------
  // A fixed, pre-painted disc moved with transform only (no repaint per frame). It blends with
  // mix-blend-mode: lighten, so where it meets the hero glow the brighter light wins instead of adding.
  const spot = document.createElement("div");
  spot.className = "spotlight";
  spot.setAttribute("aria-hidden", "true");
  document.body.append(spot);
  const s = { x: pointer.x, y: pointer.y, o: 0 };
  subscribe((dt, p) => {
    s.x = ease(s.x, p.x, dt, 140);
    s.y = ease(s.y, p.y, dt, 140);
    s.o = ease(s.o, p.inside ? 1 : 0, dt, 220);
    spot.style.transform = `translate3d(${s.x.toFixed(1)}px, ${s.y.toFixed(1)}px, 0)`;
    spot.style.opacity = s.o.toFixed(3);
    return !(settled(s.x, p.x, 0.3) && settled(s.y, p.y, 0.3) && settled(s.o, p.inside ? 1 : 0, 0.004));
  }, () => { s.o = 0; spot.style.opacity = "0"; });

  // ---- floating depth shapes behind sparse page heroes (decorative, no data) ------------------------
  // Soft case-folder and evidence-card outlines at different depths and opacities, with a gentle scroll
  // parallax on this same loop. aria-hidden; static under reduced motion and on touch devices.
  const SVGNS = "http://www.w3.org/2000/svg";
  const SHAPES = [   // kind, left %, top %, width px, scroll depth, opacity, rotation
    ["folder", 60, -4, 150, 0.10, 0.9, -8], ["card", 77, 30, 118, 0.22, 0.7, 6], ["folder", 89, -12, 92, 0.32, 0.5, 12],
    ["card", 69, 60, 80, 0.16, 0.45, -14], ["card", 50, 6, 66, 0.28, 0.35, 4],
  ];
  function shapeSvg(kind) {
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", kind === "folder" ? "0 0 120 90" : "0 0 90 116");
    const add = (tag, attrs) => { const n = document.createElementNS(SVGNS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); svg.append(n); };
    if (kind === "folder") {
      add("path", { d: "M4 18 h40 l10 -12 h58 a4 4 0 0 1 4 4 v72 a4 4 0 0 1 -4 4 h-104 a4 4 0 0 1 -4 -4 z" });
      add("path", { d: "M4 30 h112" });
    } else {
      add("rect", { x: 3, y: 3, width: 84, height: 110, rx: 6 });
      for (const y of [26, 40, 54]) add("path", { d: `M16 ${y} h${y === 54 ? 34 : 58}` });
    }
    return svg;
  }
  const depth = { items: [], y: window.scrollY, off: [] };
  for (const hero of document.querySelectorAll(".page-hero")) {
    const field = document.createElement("div");
    field.className = "depth-shapes";
    field.setAttribute("aria-hidden", "true");
    for (const [kind, x, y, w, d, o, r] of SHAPES) {
      const wrap = document.createElement("span");
      wrap.className = `depth-shape is-${kind}`;
      wrap.style.cssText = `left:${x}%;top:${y}%;width:${w}px;opacity:${o};--rot:${r}deg`;
      wrap.append(shapeSvg(kind));
      field.append(wrap);
      depth.items.push({ node: wrap, d, r });
    }
    hero.prepend(field);
  }
  depth.off = depth.items.map(() => 0);
  if (depth.items.length) {
    window.addEventListener("scroll", () => { depth.y = window.scrollY; kick(); }, { passive: true });  // a number only
    subscribe((dt) => {
      let busy = false;
      depth.items.forEach((it, i) => {
        const target = -depth.y * it.d;
        depth.off[i] = ease(depth.off[i], target, dt, 160);
        it.node.style.transform = `translate3d(0, ${depth.off[i].toFixed(1)}px, 0) rotate(${it.r}deg)`;
        if (!settled(depth.off[i], target, 0.2)) busy = true;
      });
      return busy;
    }, () => { depth.items.forEach((it) => it.node.style.removeProperty("transform")); depth.off = depth.items.map(() => 0); });
  }

  return { enabled, subscribe, ease, settled, kick };
})();
