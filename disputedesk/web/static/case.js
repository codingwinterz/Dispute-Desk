// Case page: one dispute, its recommendation and the graph path behind it. GET /api/decisions/{id}.
"use strict";

(() => {
  const { el } = DD;
  DD.renderNav("queue");

  const POLL_MS = 5000;
  const SVG = "http://www.w3.org/2000/svg";
  const root = document.getElementById("case");
  const disputeId = decodeURIComponent(location.pathname.replace(/^\/case\//, "").replace(/\/$/, ""));
  let lastKey = "";

  function svg(tag, attrs = {}, ...children) {
    const node = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) node.setAttribute(k, v);
    for (const c of children) if (c !== null && c !== undefined) node.append(c);
    return node;
  }

  // ---- states ------------------------------------------------------------------

  function stateView(title, ...lines) {
    return el("div", { class: "state" }, el("h1", { text: title }), ...lines.map((l) => typeof l === "string" ? el("p", { text: l }) : l));
  }

  function commandLine(prefix, command, suffix = "") {
    return el("p", {}, prefix, el("code", { text: command }), suffix);
  }

  // ---- header --------------------------------------------------------------------

  function header(d) {
    document.title = `${d.dispute_id} DisputeDesk case`;
    return el("div", { class: "case-head" },
      el("div", {},
        el("h1", { class: "num", text: d.dispute_id }),
        el("div", { class: "facts-line" },
          el("span", { text: DD.reasonLabel(d.reason) }),
          el("span", { class: "num", text: DD.money(d.amount) }),
          el("span", { text: `${DD.stageLabel(d.stage)} stage` }),
          el("span", { class: `badge ${DD.statusClass(d.status)}`, text: DD.statusLabel(d.status) }),
          DD.sourceTag(d.source))),
      el("div", { class: "deadline" },
        el("div", { class: "label", text: "Respond within" }),
        DD.countdownNode(d.seller_response_due_date)));
  }

  // ---- decision panel --------------------------------------------------------------

  function decisionPanels(d, rec) {
    if (!rec) {
      return [el("section", { class: "panel" },
        el("h2", { text: "Recommendation" }),
        el("p", { class: "hint", text: "The agent hasn't analyzed this dispute yet. It runs automatically once PayPal's full dispute details are stored, and this page updates when it finishes." }),
        commandLine("To run it now: ", `python scripts/analyze.py --dispute-id ${d.dispute_id}`))];
    }
    if (rec.status !== "ok") {
      return [el("section", { class: "panel" },
        el("h2", { text: "Recommendation" }),
        el("div", { class: "verdict" }, DD.decisionChip(rec)),
        el("p", { class: "hint", text: `The last analysis stopped with ${rec.error || "an unknown error"} (model ${rec.model}, ${rec.attempts} attempt${rec.attempts === 1 ? "" : "s"}). The next startup sweep retries it.` }),
        commandLine("To retry now: ", `python scripts/analyze.py --dispute-id ${d.dispute_id}`))];
    }
    const reasons = el("ol", { class: "reasons" }, ...rec.reasoning.map((p) =>
      el("li", {}, p.point, ...p.cites.map((c) => el("a", { class: "cite", href: `#fact-${c}`, text: c, title: citeTitle(rec, c) })))));
    const approve = el("button", { class: "primary", type: "button", disabled: true, "aria-describedby": "approve-hint", text: "Approve and send to PayPal" });
    return [
      el("section", { class: "panel" },
        el("h2", { text: "Recommendation" }),
        el("div", { class: "verdict" },
          DD.decisionChip(rec),
          el("span", { class: "score", title: "Likelihood, 0 to 100, that the dispute is invalid, based only on the evidence on file" },
            el("strong", { text: String(rec.risk_score) }), "risk score"),
          el("span", { class: "meta", text: `Analyzed ${new Date(rec.created_at).toLocaleString()} by ${rec.model}` }))),
      el("section", { class: "panel" }, el("h2", { text: "Why" }), reasons),
      el("section", { class: "panel" },
        el("h2", { text: "Draft response to PayPal" }),
        el("p", { class: "draft", text: rec.evidence_summary }),
        el("div", { class: "approve-row" },
          approve,
          el("p", { class: "hint", id: "approve-hint", text: "Sending responses to PayPal isn't switched on yet. Review the draft here; nothing is submitted from this page." }))),
    ];
  }

  function citeTitle(rec, id) {
    const f = (rec.facts || []).find((x) => x.id === id);
    if (f) return f.text;
    return id.startsWith("P") ? `Merchant policy rule ${id}` : id;
  }

  // ---- graph path (SVG) --------------------------------------------------------------

  function usedRefs(rec) {
    const cited = new Set(rec.reasoning.flatMap((p) => p.cites));
    return new Set((rec.facts || []).filter((f) => cited.has(f.id) && f.ref).map((f) => f.ref));
  }

  function graphPanel(rec) {
    const ctx = rec.graph_context;
    if (!ctx) return null;
    const used = usedRefs(rec);
    const W = 680, NW = 170, NH = 62, cols = [10, 255, 500]; // 75px gaps leave room for edge labels
    const linked = ctx.linked_disputes || [];
    const H = 220 + (linked.length ? 40 + linked.length * 76 : 0);

    const node = (key, x, y, title, sub, source, isUsed) =>
      svg("g", { class: `node${isUsed ? " used" : ""}`, transform: `translate(${x},${y})` },
        svg("title", {}, `${title}. ${sub}. Source: ${source === "seeded" ? "seeded test data" : "PayPal"}${isUsed ? ". Cited by the agent." : ""}`),
        svg("rect", { width: NW, height: NH, rx: 6 }),
        svg("text", { class: "title", x: 12, y: 22 }, title),
        svg("text", { class: "sub", x: 12, y: 40 }, sub.length > 24 ? sub.slice(0, 23) + "…" : sub),
        svg("text", { class: `src${source === "seeded" ? " seeded" : ""}`, x: NW - 10, y: 22, "text-anchor": "end" }, source === "seeded" ? "seeded" : "PayPal"));
    const edge = (x1, y1, x2, y2, isUsed, label) => {
      const g = svg("g", {}, svg("path", { class: `edge${isUsed ? " used" : ""}`, d: `M${x1},${y1} C${(x1 + x2) / 2},${y1} ${(x1 + x2) / 2},${y2} ${x2},${y2}` }));
      if (label) g.append(svg("text", { class: "edge-label", x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 6, "text-anchor": "middle" }, label));
      return g;
    };

    const spineUsed = used.size > 0; // the dispute, its transaction and buyer always frame the path
    const tx = ctx.transaction, cust = ctx.customer, ship = ctx.shipment, tel = ctx.telemetry, d = ctx.dispute;
    const y1 = 24, y2 = 140;
    const parts = [];
    // edges first so nodes sit on top
    parts.push(edge(cols[0] + NW, y1 + NH / 2, cols[1], y1 + NH / 2, spineUsed, "initiated"));
    parts.push(edge(cols[1] + NW, y1 + NH / 2, cols[2], y1 + NH / 2, spineUsed, "disputed"));
    parts.push(edge(cols[1] + NW / 2 + 30, y1 + NH, cols[2] + NW / 2, y2, used.has("shipment"), "fulfilled by"));
    parts.push(edge(cols[1] + NW / 2 - 30, y1 + NH, cols[0] + NW / 2, y2, used.has("telemetry") || [...used].some((r) => r.startsWith("linked:")), "logged from"));
    linked.forEach((o, i) => {
      const y = y2 + NH + 40 + i * 76;
      parts.push(edge(cols[0] + NW / 2, y2 + NH, cols[1], y + NH / 2, used.has(`linked:${o.id}`), i === 0 ? `shares ${o.via === "ip" ? "IP" : "device hash"}` : null));
    });
    parts.push(node("customer", cols[0], y1, "Buyer", cust ? cust.payer_id : "Not on file", cust ? cust.source : "paypal", spineUsed));
    parts.push(node("transaction", cols[1], y1, "Transaction", tx ? tx.id : "Not on file", tx ? tx.source : "paypal", spineUsed));
    parts.push(node("dispute", cols[2], y1, "Dispute", DD.reasonLabel(d.reason), d.source, true));
    parts.push(node("telemetry", cols[0], y2, "Checkout device", tel ? `${tel.device_hash}, ${tel.ip}` : "No record on file", tel ? tel.source : "seeded", used.has("telemetry")));
    parts.push(node("shipment", cols[2], y2, "Shipment", ship ? `${ship.carrier} ${ship.status.replaceAll("_", " ").toLowerCase()}` : "No record on file", ship ? ship.source : "seeded", used.has("shipment")));
    linked.forEach((o, i) => {
      const y = y2 + NH + 40 + i * 76;
      parts.push(node(`linked:${o.id}`, cols[1], y, "Linked dispute", `${o.id}, ${DD.reasonLabel(o.reason).toLowerCase()}`, o.source, used.has(`linked:${o.id}`)));
    });

    const label = `Graph path for ${d.id}: buyer, transaction, dispute, checkout device, shipment` +
      (linked.length ? `, and ${linked.length} other dispute${linked.length === 1 ? "" : "s"} sharing this device or IP` : "") + ".";
    const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": label }, ...parts);

    const facts = el("ul", { class: "fact-list" }, ...(rec.facts || []).map((f) => {
      const cited = rec.reasoning.some((p) => p.cites.includes(f.id));
      return el("li", { id: `fact-${f.id}`, class: cited ? "cited" : null },
        el("span", { class: "fid", text: f.id }), el("span", { text: f.text }), DD.sourceTag(f.source));
    }));

    return [
      el("section", { class: "panel" },
        el("h2", { text: "Evidence path" }),
        el("div", { class: "graph-wrap" }, chart),
        el("div", { class: "graph-legend" },
          el("span", {}, el("span", { class: "swatch", "aria-hidden": "true" }), "Used in the recommendation"),
          el("span", {}, el("span", { class: "swatch plain", "aria-hidden": "true" }), "On file, not cited"),
          el("span", {}, DD.sourceTag("seeded"), " merchant-supplied test data"))),
      el("section", { class: "panel" },
        el("h2", { text: "Facts the agent was given" }),
        el("p", { class: "hint", text: "Highlighted facts are the ones cited in the recommendation. The agent may use nothing else." }),
        facts),
    ];
  }

  // ---- render loop -------------------------------------------------------------------

  async function load() {
    let body;
    try {
      const resp = await fetch(`/api/decisions/${encodeURIComponent(disputeId)}`, { cache: "no-store" });
      if (resp.status === 404) {
        root.replaceChildren(stateView("Dispute not found",
          `There is no dispute ${disputeId} in the local store. Disputes appear here after the poller first sees them in the PayPal sandbox.`));
        return;
      }
      if (resp.status === 403) throw new Error("This page only works on the machine running DisputeDesk (open it at http://127.0.0.1:8000).");
      if (!resp.ok) throw new Error(`The server answered HTTP ${resp.status}. Retrying every 5 seconds.`);
      body = await resp.json();
    } catch (err) {
      root.replaceChildren(stateView("Can't load this case", err instanceof TypeError
        ? "Can't reach the DisputeDesk server. Is uvicorn still running? Retrying every 5 seconds." : err.message));
      return;
    }
    const key = JSON.stringify(body);
    if (key === lastKey) return; // nothing changed; keep scroll position and any open anchor
    lastKey = key;
    const { dispute, decision } = body;
    const left = el("div", {}, ...decisionPanels(dispute, decision));
    const right = el("div", {}, ...((decision && decision.status === "ok" && graphPanel(decision)) || [
      el("section", { class: "panel" }, el("h2", { text: "Evidence path" }),
        el("p", { class: "hint", text: "The graph path appears once the agent has a recommendation for this dispute." }))]));
    root.replaceChildren(header(dispute), el("div", { class: "case-grid" }, left, right));
  }

  if (!disputeId) {
    root.replaceChildren(stateView("No dispute selected", "Open a dispute from the queue to see its case."));
  } else {
    load();
    setInterval(load, POLL_MS);
  }
})();
