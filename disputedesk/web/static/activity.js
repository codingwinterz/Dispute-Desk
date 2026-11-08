// Activity: a timeline built from the existing read-only API. GET /api/disputes, then
// GET /api/decisions/{id} per dispute (its decision, saved draft and submission carry the timestamps).
"use strict";

(() => {
  const { el } = DD;
  DD.renderNav("activity");

  const POLL_MS = 15000;
  const MAX_DISPUTES = 50;  // most recently detected; keeps the page to a bounded number of requests
  const root = document.getElementById("activity");
  const banner = document.getElementById("error");
  let lastKey = "";

  const ACTION = { provide_evidence: "evidence", accept_claim: "accept the claim" };

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
      push(sub.started_at, "approved", `You approved sending ${ACTION[sub.action] || sub.action} to PayPal.`);
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
    const d = new Date(iso), today = new Date();
    const y = new Date(); y.setDate(today.getDate() - 1);
    if (d.toDateString() === today.toDateString()) return "Today";
    if (d.toDateString() === y.toDateString()) return "Yesterday";
    return d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
  }

  function render(items) {
    const events = items.flatMap(eventsFor).sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
    if (!events.length) {
      root.replaceChildren(el("div", { class: "state" },
        el("p", { text: "Nothing has happened yet. Disputes appear here when the poller first sees them in the PayPal sandbox." })));
      return;
    }
    const groups = [];
    for (const e of events) {
      const label = dayLabel(e.at);
      if (!groups.length || groups[groups.length - 1].label !== label) groups.push({ label, events: [] });
      groups[groups.length - 1].events.push(e);
    }
    const days = groups.map((g) => el("section", { class: "day" },
      el("h2", { text: g.label }),
      el("ol", { class: "timeline" }, ...g.events.map((e) => el("li", { class: `event ${e.kind}` },
        el("span", { class: "event-dot", "aria-hidden": "true" }),
        el("time", { class: "event-time num", datetime: e.at, text: new Date(e.at).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }) }),
        el("div", { class: "event-body" },
          el("a", { class: "event-case num", href: `/case/${encodeURIComponent(e.dispute.dispute_id)}`, text: e.dispute.dispute_id }),
          el("span", { text: e.text }),
          e.decision ? DD.decisionChip(e.decision) : null,
          e.source ? DD.sourceTag(e.source) : null))))));
    root.replaceChildren(...days);
    DD.reveal(days);
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
