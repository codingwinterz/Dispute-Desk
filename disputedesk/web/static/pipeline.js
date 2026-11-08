// The agent pipeline strip on the Overview: how many stored disputes have reached each step.
// Read-only. Counts come from GET /api/disputes plus each dispute's GET /api/decisions/{id} record
// (the same read-only route the Activity page uses):
//   Detected       every dispute in the local store
//   Graph checked  a decision was saved with merchant-graph context
//   Decision       the agent's recommendation is ready (decision status "ok")
//   Your approval  waiting on you, recommendation ready, nothing sent or in flight yet
//   PayPal         a response this app sent was accepted by PayPal (submission "succeeded")
"use strict";

const DDPipe = (() => {
  const WAITING = "WAITING_FOR_SELLER_RESPONSE";
  const IN_FLIGHT = new Set(["succeeded", "sending", "unknown"]);
  const MAX_RECORDS = 60;  // one small GET per dispute; the sandbox never gets near this

  const STEPS = [
    { key: "detected", title: "Detected", note: "disputes stored" },
    { key: "graph", title: "Graph checked", note: "matched in your records" },
    { key: "decided", title: "Decision", note: "recommendations ready" },
    { key: "approval", title: "Your approval", note: "waiting for you to approve" },
    { key: "paypal", title: "PayPal", note: "responses sent" },
  ];

  /** Pure: counts per step. `records` maps dispute_id -> the /api/decisions/{id} body. */
  function counts(rows, records) {
    const rec = (d) => records[d.dispute_id] || {};
    const decision = (d) => rec(d).decision || null;
    const submission = (d) => (rec(d).submit && rec(d).submit.submission) || null;
    const ok = (d) => Boolean(decision(d) && decision(d).status === "ok");
    return {
      detected: rows.length,
      graph: rows.filter((d) => decision(d) && decision(d).graph_context).length,
      decided: rows.filter(ok).length,
      approval: rows.filter((d) => d.status === WAITING && ok(d) && !(submission(d) && IN_FLIGHT.has(submission(d).state))).length,
      paypal: rows.filter((d) => submission(d) && submission(d).state === "succeeded").length,
    };
  }

  async function load(rows) {
    const list = rows.slice(0, MAX_RECORDS);
    const bodies = await Promise.all(list.map((d) =>
      fetch(`/api/decisions/${encodeURIComponent(d.dispute_id)}`, { cache: "no-store" })
        .then((r) => (r.ok ? r.json() : null)).catch(() => null)));
    if (bodies.some((b) => b === null)) throw new Error("incomplete");
    return Object.fromEntries(list.map((d, i) => [d.dispute_id, bodies[i]]));
  }

  /** The strip, with counts filled in once the records load. Returns {node, update(rows)}. */
  function create(rows, firstRender) {
    const { el } = DD;
    const nums = {};
    const list = el("ol", { class: "pipe-steps" }, ...STEPS.map((s, i) => {
      nums[s.key] = el("span", { class: "pipe-num num", text: s.key === "detected" ? String(rows.length) : "…" });
      return el("li", { class: `pipe-step is-${s.key}`, style: `--i:${i}` },
        el("div", { class: "pipe-head" }, nums[s.key], i < STEPS.length - 1 ? el("span", { class: "pipe-rail", "aria-hidden": "true" }) : null),
        el("span", { class: "pipe-title", text: s.title }), el("span", { class: "pipe-note cap", text: s.note }));
    }));
    const cap = el("p", { class: "pipe-cap cap", text: "The agent's pipeline, counted from the local store." });
    const node = el("section", { class: `pipeline${firstRender ? " is-flowing" : ""}`, "aria-label": "Agent pipeline", "aria-busy": "true" }, list, cap);
    let token = 0;

    async function update(nextRows) {
      const mine = ++token;
      try {
        const c = counts(nextRows, await load(nextRows));
        if (mine !== token) return;  // a newer refresh started
        for (const s of STEPS) nums[s.key].textContent = String(c[s.key]);
        node.classList.toggle("needs-you", c.approval > 0);
        cap.textContent = nextRows.length > MAX_RECORDS
          ? `The agent's pipeline for the first ${MAX_RECORDS} disputes, counted from the local store.`
          : "The agent's pipeline, counted from the local store.";
      } catch {
        if (mine !== token) return;
        cap.textContent = "Couldn't load every dispute's record, so some steps aren't counted yet. Retrying.";
      } finally {
        if (mine === token) node.removeAttribute("aria-busy");
      }
    }
    return { node, update };
  }

  return { STEPS, counts, create };
})();

if (typeof module !== "undefined" && module.exports) module.exports = DDPipe;  // unit tests run counts() in Node
