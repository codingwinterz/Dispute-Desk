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

  return { enabled, subscribe, ease, settled, kick };
})();
