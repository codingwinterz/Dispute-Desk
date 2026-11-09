// Activity: a timeline built from the existing read-only API. GET /api/disputes, then
// GET /api/decisions/{id} per dispute (its decision, saved draft and submission carry the timestamps).
// Event nodes are coloured by type, filter chips (which double as the legend) narrow the list, each day
// opens with a summary line, and the approval and PayPal's answer are highlighted as milestones.
"use strict";

(() => {
  const { el } = DD;
  DD.renderNav("activity");

  const POLL_MS = 15000;
  const MAX_DISPUTES = 50;  // most recently detected; keeps the page to a bounded number of requests
  const root = document.getElementById("activity");
  const banner = document.getElementById("error");
  let lastKey = "", lastItems = [];
  let filter = "all";

  // What you approved, in the words the case timeline uses too (display text only).
  const APPROVED = { provide_evidence: "You approved sending evidence to PayPal", accept_claim: "You approved accepting the claim" };
  // Event types: the filter chips and the per-day summaries use these words.
  const KINDS = [
    { key: "detected", label: "Detected", one: "detected", many: "detected" },
    { key: "analyzed", label: "Analyzed", one: "analysis", many: "analyses" },
    { key: "edited", label: "Draft edited", one: "draft edit", many: "draft edits" },
    { key: "milestone", label: "Approved and sent", one: "approval step", many: "approval steps" },
    { key: "failed", label: "Failed", one: "failure", many: "failures" },
  ];
  // approved and sent are both approval milestones; everything else maps to itself
  const groupOf = (kind) => (kind === "approved" || kind === "sent" ? "milestone" : kind);

  function eventsFor(item) {
    const { dispute: d, decision: rec, draft, submit } = item;
    const out = [];
    const push = (at, kind, text, extra = {}) => { if (at) out.push({ at, kind, text, dispute: d, ...extra }); };
    push(d.detected_at, "detected", `Detected through the ${d.detected_via === "webhook" ? "PayPal webhook" : "dispute poller"}.`,
         { source: "paypal" });
    if (rec) {
      push(rec.created_at, rec.status === "ok" ? "analyzed" : "failed",
           rec.status === "ok"
             ? `Agent recommended ${rec.decision === "DEFEND" ? "defending" : "accepting"} (risk ${rec.risk_score}).`
             : `Analysis failed (${rec.error || "unknown error"}).`,
           { decision: rec.status === "ok" ? rec : null });
    }
    if (draft && draft.edited) push(draft.updated_at, "edited", "You saved an edited draft.");
    const sub = submit && submit.submission;
    if (sub) {
      push(sub.started_at, "approved", `${APPROVED[sub.action] || `You approved ${String(sub.action).replaceAll("_", " ")}`}.`);
      const result = {
        succeeded: [`PayPal accepted it (HTTP ${sub.http_status}).`, "sent"],
        rejected: [`PayPal refused it (HTTP ${sub.http_status}).`, "failed"],
        unknown: ["The outcome is unknown (network error or PayPal server error).", "failed"],
      }[sub.state];
      if (result) push(sub.finished_at, result[1], result[0]);
    }
    return out;
  }

  function dayLabel(iso) {
    // The shared clock: the real time live, the snapshot's recorded time in demo mode.
    const now = DD.clock();
    const d = new Date(iso), today = new Date(now);
    const y = new Date(now); y.setDate(today.getDate() - 1);
    if (d.toDateString() === today.toDateString()) return "Today";
    if (d.toDateString() === y.toDateString()) return "Yesterday";
    return d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
  }

  // "6 events: 4 analyses, 2 approval steps" from the day's own events.
  function summary(events) {
    const parts = KINDS.map((k) => {
      const n = events.filter((e) => groupOf(e.kind) === k.key).length;
      return n ? `${n} ${n === 1 ? k.one : k.many}` : "";
    }).filter(Boolean);
    return `${events.length} ${events.length === 1 ? "event" : "events"}: ${parts.join(", ")}.`;
  }

  function filterChips(events) {
    const present = KINDS.filter((k) => events.some((e) => groupOf(e.kind) === k.key));
    const chip = (key, label, count) => {
      const b = el("button", { type: "button", class: `filter-chip kind-${key}`, "aria-pressed": String(filter === key) },
        key === "all" ? null : el("span", { class: "chip-node", "aria-hidden": "true" }), label, el("span", { class: "chip-count num", text: String(count) }));
      b.addEventListener("click", () => {
        filter = key;
        render(lastItems);
        const again = root.querySelector(`.filter-chip.kind-${key}`);  // keep keyboard focus on the chip
        if (again) again.focus();
      });
      return b;
    };
    return el("div", { class: "filter-chips", role: "group", "aria-label": "Show events" },
      chip("all", "All", events.length), ...present.map((k) => chip(k.key, k.label, events.filter((e) => groupOf(e.kind) === k.key).length)));
  }

  function hero(events, items) {
    document.getElementById("activity-eyebrow").replaceChildren(...DD.eyebrow("Activity"));
    const sent = events.filter((e) => e.kind === "sent").length;
    document.getElementById("activity-stats").replaceChildren(
      DD.statChip({ label: "events on file", value: String(events.length) }),
      DD.statChip({ label: "disputes", value: String(items.length) }),
      DD.statChip({ label: "recommendations", value: String(events.filter((e) => e.kind === "analyzed").length) }),
      DD.statChip({ label: "accepted by PayPal", value: String(sent) }));
  }

  function render(items) {
    lastItems = items;
    const all = items.flatMap(eventsFor).sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
    document.getElementById("activity-eyebrow").replaceChildren(...DD.eyebrow("Activity"));
    if (!all.length) {
      document.getElementById("activity-stats").replaceChildren();
      root.replaceChildren(el("div", { class: "state" },
        el("p", { text: "Nothing has happened yet. Disputes appear here when the poller first sees them in the PayPal sandbox." })));
      return;
    }
    hero(all, items);
    if (filter !== "all" && !all.some((e) => groupOf(e.kind) === filter)) filter = "all";
    const events = filter === "all" ? all : all.filter((e) => groupOf(e.kind) === filter);
    const groups = [];
    for (const e of events) {
      const label = dayLabel(e.at);
      if (!groups.length || groups[groups.length - 1].label !== label) groups.push({ label, events: [] });
      groups[groups.length - 1].events.push(e);
    }
    const days = groups.map((g) => el("section", { class: "day" },
      el("h2", { text: g.label }),
      el("p", { class: "day-summary", text: summary(g.events) }),
      el("ol", { class: "timeline" }, ...g.events.map((e) => el("li", { class: `event ${e.kind}${groupOf(e.kind) === "milestone" ? " milestone" : ""}` },
        el("span", { class: "event-dot", "aria-hidden": "true" }),
        el("time", { class: "event-time num", datetime: e.at, text: new Date(e.at).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }) }),
        el("div", { class: "event-body" },
          el("a", { class: "event-case num", href: `/case/${encodeURIComponent(e.dispute.dispute_id)}`, text: e.dispute.dispute_id }),
          el("span", { text: e.text }),
          e.decision ? DD.decisionChip(e.decision) : null,
          e.source ? DD.sourceTag(e.source) : null))))));
    root.replaceChildren(filterChips(all), ...days);
    DD.revealStaggered(days);
  }

  async function refresh() {
    try {
      const resp = await fetch("/api/disputes", { cache: "no-store" });
      if (resp.status === 403) throw new Error("This page only works on the machine running DisputeDesk (open it at http://127.0.0.1:8000).");
      if (!resp.ok) throw new Error(`The server answered HTTP ${resp.status}. Retrying every 15 seconds.`);
      const disputes = ((await resp.json()).disputes || [])
        .sort((a, b) => Date.parse(b.detected_at) - Date.parse(a.detected_at)).slice(0, MAX_DISPUTES);
      const items = (await Promise.all(disputes.map((d) =>
        fetch(`/api/decisions/${encodeURIComponent(d.dispute_id)}`, { cache: "no-store" }).then((r) => (r.ok ? r.json() : null)))))
        .filter(Boolean);
      DD.live(true);
      await DD.modeReady;
      banner.hidden = true;
      const key = JSON.stringify(items);
      if (key === lastKey) return;
      lastKey = key;
      render(items);
    } catch (err) {
      DD.live(false);
      banner.textContent = err instanceof TypeError
        ? "Can't reach the DisputeDesk server. Is uvicorn still running? Retrying every 15 seconds."
        : err.message;
      banner.hidden = false;
      if (!lastKey) root.replaceChildren(el("div", { class: "state" }, el("p", { text: "No activity loaded yet." })));
    }
  }

  refresh();
  setInterval(refresh, POLL_MS);
})();
