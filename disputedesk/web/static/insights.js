// Insights: totals and splits counted from GET /api/disputes. No estimates, no projections.
// Charts are single-series horizontal bars in one neutral hue, every bar labeled with its value.
"use strict";

(() => {
  const { el } = DD;
  DD.renderNav("insights");

  const POLL_MS = 10000;
  const root = document.getElementById("insights");
  const banner = document.getElementById("error");
  let lastKey = "";

  const amountOf = (d) => (d.amount && d.amount.value !== undefined ? Number(d.amount.value) : 0);
  const currencies = (rows) => [...new Set(rows.map((d) => (d.amount && d.amount.currency_code) || "USD"))];

  function money(total, rows) {
    const cur = currencies(rows);
    // Only add amounts in one currency; mixed currencies are shown as counts only.
    return cur.length <= 1 ? DD.money({ value: total.toFixed(2), currency_code: cur[0] || "USD" }) : "mixed currencies";
  }

  /** One chart: [{label, count, amountRows}] as horizontal bars scaled to the largest count. */
  function bars(title, note, items, { showAmount = true } = {}) {
    const max = Math.max(1, ...items.map((i) => i.count));
    return el("section", { class: "chart" },
      el("h2", { text: title }),
      note ? el("p", { class: "hint", text: note }) : null,
      el("ul", { class: "bars", role: "list" }, ...items.map((i) => {
        const total = i.rows.reduce((s, d) => s + amountOf(d), 0);
        const value = `${i.count} ${i.count === 1 ? "dispute" : "disputes"}${showAmount && i.count ? `, ${money(total, i.rows)}` : ""}`;
        return el("li", { class: "bar-row", title: `${i.label}: ${value}` },
          el("span", { class: "bar-label", text: i.label }),
          el("span", { class: "bar-track", "aria-hidden": "true" },
            el("span", { class: "bar-fill", style: `width:${(i.count / max) * 100}%` })),
          el("span", { class: "bar-value num", text: value }));
      })));
  }

  function render(rows) {
    if (!rows.length) {
      root.replaceChildren(el("div", { class: "state" },
        el("p", { text: "No disputes yet, so there is nothing to count. Insights fill in as disputes arrive." })));
      return;
    }
    document.getElementById("scope").textContent =
      `Counted from the ${rows.length} ${rows.length === 1 ? "dispute" : "disputes"} in the local store. All dispute records come from the PayPal sandbox.`;
    const open = rows.filter((d) => d.status !== "RESOLVED");
    const ok = rows.filter((d) => d.decision && d.decision.status === "ok");

    const rec = bars("Recommendations", "What the agent recommended for every dispute.", [
      { label: "Defend", count: ok.filter((d) => d.decision.decision === "DEFEND").length, rows: ok.filter((d) => d.decision.decision === "DEFEND") },
      { label: "Accept", count: ok.filter((d) => d.decision.decision === "ACCEPT").length, rows: ok.filter((d) => d.decision.decision === "ACCEPT") },
      { label: "Pending or failed", count: rows.length - ok.length, rows: rows.filter((d) => !ok.includes(d)) },
    ]);

    const reasons = [...new Set(rows.map((d) => d.reason))].sort();
    const byReason = bars("By reason", "Why buyers opened their disputes.",
      reasons.map((r) => ({ label: DD.reasonLabel(r), count: rows.filter((d) => d.reason === r).length, rows: rows.filter((d) => d.reason === r) })));

    const bucket = (lo, hi) => ok.filter((d) => d.decision.risk_score >= lo && d.decision.risk_score <= hi);
    const risk = bars("Risk scores",
      "How likely each buyer's claim is to be valid, from the evidence on file. Higher means the buyer is more likely right.", [
        { label: "Low (0 to 29)", count: bucket(0, 29).length, rows: bucket(0, 29) },
        { label: "Medium (30 to 69)", count: bucket(30, 69).length, rows: bucket(30, 69) },
        { label: "High (70 to 100)", count: bucket(70, 100).length, rows: bucket(70, 100) },
      ]);

    const now = Date.now(), H = 3600 * 1000;
    const due = (d) => Date.parse(d.seller_response_due_date);
    const within = (lo, hi) => open.filter((d) => due(d) - now >= lo && due(d) - now < hi);
    const noDate = open.filter((d) => Number.isNaN(due(d)));
    const urgency = bars("Open disputes by deadline", "Time left to respond to each open dispute.", [
      { label: "Overdue", count: within(-Infinity, 0).length, rows: within(-Infinity, 0) },
      { label: "Within 72 hours", count: within(0, 72 * H).length, rows: within(0, 72 * H) },
      { label: "3 to 7 days", count: within(72 * H, 168 * H).length, rows: within(72 * H, 168 * H) },
      { label: "More than 7 days", count: within(168 * H, Infinity).length, rows: within(168 * H, Infinity) },
      { label: "No deadline (PayPal is reviewing)", count: noDate.length, rows: noDate },
    ]);

    root.replaceChildren(rec, risk, byReason, urgency);
  }

  async function refresh() {
    try {
      const resp = await fetch("/api/disputes", { cache: "no-store" });
      if (resp.status === 403) throw new Error("This page only works on the machine running DisputeDesk (open it at http://127.0.0.1:8000).");
      if (!resp.ok) throw new Error(`The server answered HTTP ${resp.status}. Retrying every 10 seconds.`);
      const body = await resp.json();
      DD.live(true);
      banner.hidden = true;
      const key = JSON.stringify(body);
      if (key === lastKey) return;
      lastKey = key;
      render(body.disputes || []);
    } catch (err) {
      DD.live(false);
      banner.textContent = err instanceof TypeError
        ? "Can't reach the DisputeDesk server. Is uvicorn still running? Retrying every 10 seconds."
        : err.message;
      banner.hidden = false;
      if (!lastKey) root.replaceChildren(el("div", { class: "state" }, el("p", { text: "No data loaded yet." })));
    }
  }

  refresh();
  setInterval(refresh, POLL_MS);
})();
