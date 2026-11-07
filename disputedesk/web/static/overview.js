// Overview: what needs attention now. Reads GET /api/disputes only; every figure is counted from it.
"use strict";

(() => {
  const { el } = DD;
  DD.renderNav("overview");

  const POLL_MS = 5000;
  const URGENT_MS = 72 * 3600 * 1000;
  const root = document.getElementById("overview");
  const banner = document.getElementById("error");
  let lastKey = "";

  const isOpen = (d) => d.status !== "RESOLVED";
  const dueMs = (d) => Date.parse(d.seller_response_due_date) || Infinity;

  function amountTotals(rows) {
    // Summed per currency; never converted.
    const byCur = {};
    for (const r of rows) {
      if (!r.amount || r.amount.value === undefined) continue;
      const cur = r.amount.currency_code || "USD";
      byCur[cur] = (byCur[cur] || 0) + Number(r.amount.value);
    }
    const parts = Object.entries(byCur).map(([cur, v]) => DD.money({ value: v.toFixed(2), currency_code: cur }));
    return parts.length ? parts.join(" + ") : "None";
  }

  function figure(value, label, note, urgent = false) {
    return el("div", { class: `figure${urgent ? " urgent" : ""}` },
      el("span", { class: "figure-value num", text: value }),
      el("span", { class: "figure-label", text: label }),
      note ? el("span", { class: "figure-note", text: note }) : null);
  }

  function folderRow(d) {
    const href = `/case/${encodeURIComponent(d.dispute_id)}`;
    return el("a", { class: "folder-row", href },
      el("span", { class: "folder-due" }, DD.countdownNode(d.seller_response_due_date)),
      el("span", { class: "folder-main" },
        el("span", { class: "folder-id num", text: d.dispute_id }),
        el("span", { class: "folder-meta" },
          el("span", { text: DD.reasonLabel(d.reason) }),
          el("span", { class: "num", text: DD.money(d.amount) }),
          el("span", { class: `badge ${DD.statusClass(d.status)}`, text: DD.statusLabel(d.status) }))),
      el("span", { class: "folder-side" },
        DD.decisionChip(d.decision),
        d.decision && d.decision.status === "ok" ? DD.riskNode(d.decision.risk_score) : null,
        DD.sourceTag(d.source)));
  }

  function render(rows) {
    const open = rows.filter(isOpen).sort((a, b) => dueMs(a) - dueMs(b));
    if (!rows.length) {
      root.replaceChildren(el("div", { class: "state" },
        el("h1", { text: "No disputes yet" }),
        el("p", { text: "When a buyer opens a dispute in the PayPal sandbox, the poller picks it up within 10 seconds and it appears here." })));
      return;
    }
    const now = Date.now();
    const urgent = open.filter((d) => dueMs(d) - now < URGENT_MS);
    const pending = rows.filter((d) => !d.decision || d.decision.status !== "ok");
    const next = open[0];

    const hero = next
      ? el("section", { class: "hero-deadline" },
          el("p", { class: "hero-label", text: "Next response due" }),
          el("div", { class: "hero-row" },
            el("span", { class: "hero-count" }, DD.countdownNode(next.seller_response_due_date)),
            el("div", { class: "hero-case" },
              el("a", { class: "hero-id num", href: `/case/${encodeURIComponent(next.dispute_id)}`, text: next.dispute_id }),
              el("span", { class: "folder-meta" },
                el("span", { text: DD.reasonLabel(next.reason) }),
                el("span", { class: "num", text: DD.money(next.amount) }),
                DD.sourceTag(next.source)),
              el("span", { class: "hero-chip" }, "Recommended: ", DD.decisionChip(next.decision)))))
      : el("section", { class: "hero-deadline" },
          el("p", { class: "hero-label", text: "Nothing to answer" }),
          el("p", { class: "lede", text: `All ${rows.length} disputes in the local store are resolved.` }));

    const strip = el("section", { class: "figure-strip", "aria-label": "Counts from the local store" },
      figure(String(open.length), open.length === 1 ? "open dispute" : "open disputes", `${rows.length} in the local store`),
      figure(amountTotals(open), "in open disputes", "Summed per currency"),
      figure(String(urgent.length), "due within 72 hours", urgent.length ? "Answer these first" : "None this close", urgent.length > 0),
      figure(String(pending.length), "without a recommendation",
             pending.length ? "Analysis pending or failed" : "Every dispute is analyzed"));

    const list = open.length
      ? el("section", { class: "folders" },
          el("h2", { text: "Respond in this order" }),
          el("p", { class: "hint", text: "Open disputes, soonest deadline first." }),
          el("div", { class: "folder-stack" }, ...open.map(folderRow)))
      : null;

    root.replaceChildren(
      el("h1", { class: "sr-only", text: "Overview" }),
      hero, strip, list);
  }

  async function refresh() {
    try {
      const resp = await fetch("/api/disputes", { cache: "no-store" });
      if (resp.status === 403) throw new Error("This page only works on the machine running DisputeDesk (open it at http://127.0.0.1:8000).");
      if (!resp.ok) throw new Error(`The server answered HTTP ${resp.status}. Retrying every 5 seconds.`);
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
        ? "Can't reach the DisputeDesk server. Is uvicorn still running? Retrying every 5 seconds."
        : err.message;
      banner.hidden = false;
      if (!lastKey) root.replaceChildren(el("div", { class: "state" }, el("p", { text: "No data loaded yet." })));
    }
  }

  refresh();
  setInterval(refresh, POLL_MS);
})();
