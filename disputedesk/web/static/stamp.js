// The verdict stamp on the case page: shown only after an approval this page sent succeeded (the
// existing success state, lastResult.ok, plus the stored submission reading "succeeded"). Visual only:
// it repeats what the success message already says, so it is hidden from screen readers. It lands
// once with a short thump; later re-renders and reduced motion show it already in place.
"use strict";

const DDStamp = (() => {
  const WORDS = { provide_evidence: "Defended", accept_claim: "Accepted" };
  const played = new Set();  // submissions whose stamp has already landed on this page

  function create(submission) {
    const { el } = DD;
    const key = `${submission.dispute_id || ""}:${submission.finished_at || ""}`;
    const animate = !DD.reducedMotion() && !played.has(key);
    played.add(key);
    const when = submission.finished_at
      ? new Date(submission.finished_at).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "";
    return el("div", { class: `verdict-stamp${animate ? " is-landing" : ""}`, "aria-hidden": "true" },
      el("span", { class: "stamp-word", text: WORDS[submission.action] || "Sent" }),
      el("span", { class: "stamp-line", text: when ? `Sent to PayPal, ${when}` : "Sent to PayPal" }));
  }

  return { WORDS, create };
})();
