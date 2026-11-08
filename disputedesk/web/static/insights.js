// Insights: counted from GET /api/disputes only. No estimates, no projections.
// - Big-number tiles: amount in dispute, the agent's Defend/Accept split, why buyers disputed.
// - Deadline runway: one marker per dispute waiting on you, placed by its due date on DD.clock()
//   (frozen at the recording in demo mode), with its ID, verdict and live countdown.
// - Risk ruler: 0 to 100, each decided dispute plotted at its score and labelled.
// Both charts are ordered lists of links, so screen readers get the same data in a sensible order.
"use strict";

(() => {
  const { el } = DD;
  DD.renderNav("insights");

  const POLL_MS = 10000;
  const DAY = 86400 * 1000, H72 = 72 * 3600 * 1000;
  const root = document.getElementById("insights");
  const banner = document.getElementById("error");
  let lastKey = "";

  const decided = (d) => Boolean(d.decision && d.decision.status === "ok");
  const verdictOf = (d) => (decided(d) ? d.decision.decision : null);
  const shortId = (id) => String(id).split("-").slice(0, 3).join("-");
  const caseHref = (d) => `/case/${encodeURIComponent(d.dispute_id)}`;

  function head(first, second, note) {
    return el("div", { class: "stack-head" },
      el("h2", {}, el("span", { text: first }), second ? el("span", { text: second }) : null),
      note ? el("span", { class: "cap", text: note }) : null);
  }

  function verdictChip(d) {
    const v = verdictOf(d);
    return v ? el("span", { class: `chip ${v.toLowerCase()}`, text: v === "DEFEND" ? "Defend" : "Accept" })
      : el("span", { class: "chip pending", text: "No recommendation yet" });
  }

  // Greedy lanes: a marker goes in the first lane whose last marker is at least `gap` percent to the left.
  function lanes(items, gap) {
    const ends = [];
    return items.map((it) => {
      let lane = ends.findIndex((x) => it.x - x >= gap);
      if (lane < 0) { lane = ends.length; ends.push(-Infinity); }
      ends[lane] = it.x;
      return { ...it, lane };
    });
  }
  const edgeClass = (x) => (x <= 25 ? " at-start" : x >= 75 ? " at-end" : "");  // labels near an edge grow inwards

  // ---- big-number tiles --------------------------------------------------------------------------
  function tiles(rows, groups) {
    const waitingTotal = DD.sumAmounts(groups.waiting), allTotal = DD.sumAmounts(rows);
    const amount = el("article", { class: "tile" },
      el("span", { class: "tile-label", text: "In dispute, waiting on you" }),
      el("span", { class: "tile-number num", text: waitingTotal || "None" }),
      el("span", { class: "tile-note", text: groups.waiting.length
        ? `Across ${groups.waiting.length} ${groups.waiting.length === 1 ? "dispute" : "disputes"}. ${allTotal} across all ${rows.length} stored.`
        : `Nothing waiting on you. ${allTotal ? `${allTotal} across all ${rows.length} stored.` : ""}` }));

    const ok = rows.filter(decided);
    const defend = ok.filter((d) => d.decision.decision === "DEFEND").length, accept = ok.length - defend;
    const pending = rows.length - ok.length;
    const split = el("article", { class: "tile" },
      el("span", { class: "tile-label", text: "The agent's call" }),
      el("span", { class: "tile-split" },
        el("span", { class: "tile-number num", text: String(defend) }), el("span", { class: "tile-unit", text: "defend" }),
        el("span", { class: "tile-number num", text: String(accept) }), el("span", { class: "tile-unit", text: "accept" })),
      ok.length ? el("span", { class: "split-bar", role: "img", "aria-label": `${defend} defend, ${accept} accept` },
        el("span", { class: "split-defend", style: `flex:${defend}` }), el("span", { class: "split-accept", style: `flex:${accept}` })) : null,
      el("span", { class: "tile-note", text: ok.length
        ? `Every recommendation on file${pending ? `; ${pending} still being analyzed` : ""}.` : "No recommendations yet." }));

    const reasons = [...new Set(rows.map((d) => d.reason))].sort();
    const why = el("article", { class: "tile" },
      el("span", { class: "tile-label", text: "Why buyers disputed" }),
      el("ul", { class: "tile-list" }, ...reasons.map((r) => {
        const n = rows.filter((d) => d.reason === r).length;
        return el("li", {}, el("span", { class: "tile-count num", text: String(n) }), el("span", { text: DD.reasonLabel(r) }));
      })));
    return el("section", { class: "tiles", "aria-label": "Totals" }, amount, split, why);
  }

  // ---- deadline runway -------------------------------------------------------------------------------
  function runway(groups) {
    const now = DD.clock();  // the recording's time in demo mode
    const dated = groups.waiting.filter((d) => d.seller_response_due_date).sort((a, b) => Date.parse(a.seller_response_due_date) - Date.parse(b.seller_response_due_date));
    const asOf = DD.mode.demo ? ` As of the recording, ${DD.recordedLabel()}.` : "";
    const others = [
      groups.withPayPal.length ? `${groups.withPayPal.length} With PayPal` : "",
      groups.withBuyer.length ? `${groups.withBuyer.length} waiting on the buyer` : "",
    ].filter(Boolean).join(", ");
    const section = el("section", { class: "chart" },
      head("Deadline", "runway", `Every dispute waiting on you, placed by its due date.${asOf}`));
    if (!dated.length) {
      section.append(el("p", { class: "empty-note", text: `Nothing waiting on you has a deadline, so there is nothing to plot.${others ? ` (${others}: no deadline to answer.)` : ""}` }));
      return section;
    }
    const last = Math.max(...dated.map((d) => Date.parse(d.seller_response_due_date)));
    const start = Math.min(now, ...dated.map((d) => Date.parse(d.seller_response_due_date)));
    const end = Math.ceil((last + DAY / 2) / DAY) * DAY;  // round up past the last deadline
    const pos = (t) => ((t - start) / (end - start)) * 100;
    const days = Math.round((end - start) / DAY);
    const step = days > 21 ? 7 : days > 10 ? 2 : 1;
    const ticks = [];
    for (let t = Math.ceil(start / DAY) * DAY; t <= end; t += step * DAY) ticks.push(t);

    const placed = lanes(dated.map((d) => ({ d, x: pos(Date.parse(d.seller_response_due_date)) })), 26);
    const laneCount = Math.max(1, ...placed.map((p) => p.lane + 1));
    const soonEnd = Math.min(100, pos(now + H72));
    const chart = el("div", { class: "runway", style: `--lanes:${laneCount}` },
      el("div", { class: "rw-soon", style: `left:${pos(now).toFixed(2)}%;width:${Math.max(0, soonEnd - pos(now)).toFixed(2)}%`, "aria-hidden": "true" },
        el("span", { text: "Next 72 hours" })),
      el("div", { class: "rw-axis", "aria-hidden": "true" },
        ...ticks.map((t) => el("span", { class: "rw-tick", style: `left:${pos(t).toFixed(2)}%` },
          el("span", { text: new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" }) })))),
      el("span", { class: "rw-now", style: `left:${pos(now).toFixed(2)}%`, "aria-hidden": "true" }, el("span", { text: DD.mode.demo ? "Recorded" : "Now" })),
      el("ol", { class: "rw-markers" }, ...placed.map(({ d, x, lane }) => {
        const c = DD.countdown(d.seller_response_due_date);
        const overdue = DD.isOverdue(c);
        return el("li", { class: `rw-marker${overdue ? " overdue" : ""}${edgeClass(x)}`, style: `left:${x.toFixed(2)}%;--lane:${lane}` },
          el("span", { class: "rw-pin", "aria-hidden": "true" }),
          el("a", { class: "rw-card", href: caseHref(d) },
            el("span", { class: "rw-id num", text: d.dispute_id }),
            el("span", { class: "rw-meta" }, verdictChip(d),
              el("span", { class: "rw-due", "data-due": d.seller_response_due_date },
                el("span", { class: "sr-only", text: "time left: " }), el("span", { class: "cd-main", text: c.text }), DD.dueSoonChip(c))),
            el("span", { class: "rw-date", text: `Due ${DD.dueDate(d.seller_response_due_date)}` })));
      })));
    section.append(chart);
    if (others) section.append(el("p", { class: "empty-note", text: `Not on the runway: ${others}, with no deadline for you to answer.` }));
    return section;
  }

  // ---- risk ruler -------------------------------------------------------------------------------------
  function ruler(rows) {
    const ok = rows.filter(decided).sort((a, b) => a.decision.risk_score - b.decision.risk_score);
    const section = el("section", { class: "chart" },
      head("Risk", "ruler", "How likely each buyer's claim is to be valid, from the evidence on file. Higher means the buyer is more likely right."));
    if (!ok.length) {
      section.append(el("p", { class: "empty-note", text: "No recommendations yet, so there are no risk scores to plot." }));
      return section;
    }
    const placed = lanes(ok.map((d) => ({ d, x: Math.max(0, Math.min(100, d.decision.risk_score)) })), 14);
    const laneCount = Math.max(1, ...placed.map((p) => p.lane + 1));
    section.append(el("div", { class: "ruler", style: `--lanes:${laneCount}` },
      el("div", { class: "rl-bands", "aria-hidden": "true" },
        el("span", { class: "rl-band low", style: "left:0;width:30%" }, el("span", { text: "Low, 0 to 29" })),
        el("span", { class: "rl-band mid", style: "left:30%;width:40%" }, el("span", { text: "Medium, 30 to 69" })),
        el("span", { class: "rl-band high", style: "left:70%;width:30%" }, el("span", { text: "High, 70 to 100" }))),
      el("div", { class: "rl-axis", "aria-hidden": "true" },
        ...[0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100].map((v) => el("span", { class: "rl-tick", style: `left:${v}%` }, el("span", { text: String(v) })))),
      el("ol", { class: "rl-markers" }, ...placed.map(({ d, x, lane }) =>
        el("li", { class: `rl-marker is-${d.decision.decision.toLowerCase()}${edgeClass(x)}`, style: `left:${x}%;--lane:${lane}` },
          el("span", { class: "rl-dot", "aria-hidden": "true" }),
          el("a", { class: "rl-label", href: caseHref(d), title: d.dispute_id },
            el("span", { class: "rl-id num", text: shortId(d.dispute_id) }),
            el("span", { class: "rl-score num" }, el("span", { class: "sr-only", text: ", risk " }), String(d.decision.risk_score)),
            el("span", { class: "sr-only", text: `, ${d.decision.decision.toLowerCase()}` })))))));
    return section;
  }

  function hero(rows, groups) {
    document.getElementById("insights-eyebrow").replaceChildren(...DD.eyebrow("Insights"));
    document.getElementById("scope").textContent =
      `Counted from the ${rows.length} ${rows.length === 1 ? "dispute" : "disputes"} in the local store. All dispute records come from the PayPal sandbox.`;
    const chips = [
      DD.statChip({ label: "disputes stored", value: String(rows.length) }),
      DD.statChip({ label: "waiting on you", value: String(groups.waiting.length), tone: groups.waiting.length ? "waiting" : "" }),
      DD.statChip({ label: "With PayPal", value: String(groups.withPayPal.length) }),
    ];
    if (groups.withBuyer.length) chips.push(DD.statChip({ label: "waiting on the buyer", value: String(groups.withBuyer.length) }));
    document.getElementById("insights-stats").replaceChildren(...chips);
  }

  function render(rows) {
    if (!rows.length) {
      document.getElementById("insights-stats").replaceChildren();
      root.replaceChildren(el("div", { class: "state" },
        el("p", { text: "No disputes yet, so there is nothing to count. Insights fill in as disputes arrive." })));
      return;
    }
    const groups = DD.groupDisputes(rows);
    hero(rows, groups);
    const parts = [tiles(rows, groups), runway(groups), ruler(rows)];
    root.replaceChildren(...parts);
    DD.revealStaggered(parts);
  }

  async function refresh() {
    try {
      const resp = await fetch("/api/disputes", { cache: "no-store" });
      if (resp.status === 403) throw new Error("This page only works on the machine running DisputeDesk (open it at http://127.0.0.1:8000).");
      if (!resp.ok) throw new Error(`The server answered HTTP ${resp.status}. Retrying every 10 seconds.`);
      const body = await resp.json();
      DD.live(true);
      await DD.modeReady;  // demo mode decides which clock the runway uses
      banner.hidden = true;
      // Live mode: re-place the runway once a minute as time moves on (demo time is frozen).
      const key = JSON.stringify(body) + (DD.mode.demo ? "" : Math.floor(DD.clock() / 60000));
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
