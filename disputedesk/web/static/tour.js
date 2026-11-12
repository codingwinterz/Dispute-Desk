// Demo-mode guided tour: six read-only steps with links to existing pages. Loaded by common.js only in
// demo mode, and it checks again before drawing. No network request; nodes are built with DD.el and
// textContent only. The current step is kept for this browser tab when storage is allowed; otherwise the
// tour still works.
"use strict";

(() => {
  if (typeof DD === "undefined" || !DD.mode || !DD.mode.demo || DD.mode.scratch) return;
  if (!document.body.classList.contains("shell") || document.querySelector(".tour")) return;
  const el = DD.el;
  const KEY = "disputedesk-tour";
  const CASE = "/case/PP-R-YOV-10196864";
  const README = "https://github.com/codingwinterz/Dispute-Desk#what-is-real-and-what-is-simulated";

  const STEPS = [
    { title: "Overview", href: "/",
      text: "This is recorded PayPal sandbox data. Demo mode is read-only: nothing is sent to PayPal or to the AI model." },
    { title: "Queue", href: "/queue",
      text: "Every dispute, sorted by urgency: the soonest seller response deadline first, with a countdown to it." },
    { title: "A case", href: CASE,
      text: "The agent recommends Defend or Accept with a risk score and reasons that cite numbered facts. "
        + "Records tagged “seeded” are simulated merchant data." },
    { title: "New evidence", href: `${CASE}#timeline-title`,
      text: "A recorded, simulated carrier-scan event arrived after the first analysis. "
        + "The case shows the recommendation before and after it." },
    { title: "Policies", href: "/policies",
      text: "The merchant's rules P1 to P4, in the exact words the agent reads. Read-only in demo mode." },
    { title: "Activity", href: "/activity",
      text: "Every action is logged: detected, analyzed, approved and PayPal's answer.",
      extra: { text: "What is real and what is simulated", href: README } },
  ];

  function load() {
    try {
      const saved = JSON.parse(window.sessionStorage.getItem(KEY) || "null");
      if (saved && Number.isInteger(saved.step) && saved.step >= 0 && saved.step < STEPS.length) {
        return { step: saved.step, open: saved.open === true };
      }
    } catch { /* storage blocked or unreadable: start fresh */ }
    return { step: 0, open: false };
  }
  function save() {
    try { window.sessionStorage.setItem(KEY, JSON.stringify(state)); } catch { /* not stored; fine */ }
  }

  const state = load();
  const panel = el("section", { class: "tour", "aria-label": "Demo tour" });
  const live = el("p", { class: "tour-count cap", "aria-live": "polite" });

  function button(label, cls, onClick) {
    const b = el("button", { type: "button", class: cls, text: label });
    b.addEventListener("click", onClick);
    return b;
  }

  function startButton() {
    return button("Take the 60-second tour", "secondary small tour-start", () => open(0));
  }

  function open(step) {
    state.open = true;
    if (Number.isInteger(step)) state.step = step;
    save();
    render();
    const title = panel.querySelector(".tour-title");
    if (title) title.focus();
  }

  function close() {
    state.open = false;
    save();
    render();
    const start = panel.querySelector(".tour-start");
    if (start) start.focus();
  }

  function go(delta) {
    state.step = Math.min(STEPS.length - 1, Math.max(0, state.step + delta));
    save();
    render();
    const title = panel.querySelector(".tour-title");
    if (title) title.focus();
  }

  function render() {
    if (!state.open) {
      panel.classList.remove("is-open");
      panel.replaceChildren(startButton());
      return;
    }
    const s = STEPS[state.step];
    const here = location.pathname === s.href.split("#")[0];
    live.textContent = `Step ${state.step + 1} of ${STEPS.length}`;
    const goLink = el("a", { class: "tour-go", href: s.href, text: here ? `Go to ${s.title} (this page)` : `Go to ${s.title}` });
    goLink.addEventListener("click", () => { state.open = true; save(); });
    const back = button("Back", "secondary small", () => go(-1));
    const next = button("Next", "secondary small", () => go(1));
    back.disabled = state.step === 0;
    next.disabled = state.step === STEPS.length - 1;
    panel.classList.add("is-open");
    panel.replaceChildren(
      el("div", { class: "tour-head" }, live, button("Close", "secondary small tour-close", close)),
      el("h2", { class: "tour-title", tabindex: "-1", text: s.title }),
      el("p", { class: "tour-text", text: s.text }),
      s.extra ? el("p", { class: "tour-text" }, el("a", { href: s.extra.href, rel: "noopener", text: s.extra.text })) : null,
      el("div", { class: "tour-nav" }, back, goLink, next));
  }

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !state.open) return;
    const active = document.activeElement;
    if (!active || active === document.body || panel.contains(active)) close();
  });

  // The Overview also gets a start button at the top of its content.
  if (location.pathname === "/") {
    const main = document.querySelector("main");
    if (main) main.prepend(el("p", { class: "tour-overview" }, startButton()));
  }
  render();
  document.body.append(panel);
})();
