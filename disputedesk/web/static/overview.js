// Overview: what needs attention now. Reads GET /api/disputes only; every figure is counted from it.
// The hero and ticker count only disputes waiting for the seller's response (status
// WAITING_FOR_SELLER_RESPONSE). In demo mode every countdown is frozen at the snapshot's recorded time.
"use strict";

(() => {
  const { el } = DD;
  DD.renderNav("overview");

  const POLL_MS = 5000;
  const WAITING = "WAITING_FOR_SELLER_RESPONSE";
  const root = document.getElementById("overview");
  const banner = document.getElementById("error");
  let lastKey = "";
  let heroNext = null;   // the waiting dispute the hero counts down to

  const isOpen = (d) => d.status !== "RESOLVED";
  const dueMs = (d) => Date.parse(d.seller_response_due_date) || Infinity;
  const byDue = (a, b) => dueMs(a) - dueMs(b);

  function amountTotal(rows) {
    // Summed per currency; never converted. Empty when no amounts are stored.
    const byCur = {};
    for (const r of rows) {
      if (!r.amount || r.amount.value === undefined) continue;
      const cur = r.amount.currency_code || "USD";
      byCur[cur] = (byCur[cur] || 0) + Number(r.amount.value);
    }
    return Object.entries(byCur).map(([cur, v]) => DD.money({ value: v.toFixed(2), currency_code: cur })).join(" + ");
  }

  // Each character in its own span; digits in fixed-width cells so the ticking line never jitters.
  function chars(text, cls) {
    return el("span", { class: cls }, ...[...text].map((c) => (/\d/.test(c) ? el("span", { class: "dg", text: c }) : c)));
  }

  // A line of words that rise into place on first load (static under reduced motion).
  function line(parts, startIndex) {
    return el("span", { class: "hero-line" }, ...parts.map((p, i) => {
      const word = el("span", { class: "word", style: `--i:${startIndex + i}` }, p);
      return i < parts.length - 1 ? [word, " "] : [word];
    }).flat());
  }

  function countdownLine(next) {
    const c = DD.countdown(next.seller_response_due_date);
    if (c.text.startsWith("Overdue")) {
      return line([el("span", { class: "hl urgent" }, "OVERDUE"), chars(c.text.replace("Overdue by ", "").toUpperCase() + ".", "hl urgent")], 2);
    }
    return line([chars(c.text.toUpperCase(), `hl${c.urgent ? " urgent" : ""}`), "LEFT."], 2);
  }

  function hero(waiting, next, firstRender) {
    const n = waiting.length;
    const board = el("section", { class: `hero-board${firstRender ? " rise" : ""}`, "aria-label": "Disputes waiting for your response" },
      line([el("span", { class: "hl" }, chars(String(n), "")), n === 1 ? "DISPUTE." : "DISPUTES."], 0),
      next ? countdownLine(next) : line(["NOTHING", "DUE."], 2),
      caption());
    return board;
  }

  function caption() {
    const cap = el("span", { class: "cap" }, "Waiting for your response", el("span", { class: "sep", text: "." }));
    if (DD.mode.demo) {
      cap.append(`recorded sandbox data, as of ${DD.recordedLabel()}`);
    } else {
      cap.append("PayPal sandbox, ", el("span", { "data-live-age": "", text: DD.ageText() || "updating" }));
    }
    return cap;
  }

  function ticker(waiting, next) {
    const decided = waiting.filter((d) => d.decision && d.decision.status === "ok");
    const defend = decided.filter((d) => d.decision.decision === "DEFEND").length;
    const items = [[el("span", { class: "hl", text: String(waiting.length) }), " waiting"]];
    const total = amountTotal(waiting);
    if (total) items.push([el("span", { class: "hl", text: total }), " in dispute"]);
    if (next) {
      items.push(["Next due ", el("span", { class: "hl", text: DD.dueDate(next.seller_response_due_date) }),
                  DD.mode.demo ? `, as recorded ${DD.recordedLabel()}` : ""]);
    }
    items.push(decided.length
      ? [el("span", { class: "hl", text: String(defend) }), " defend / ", el("span", { class: "hl", text: String(decided.length - defend) }), " accept"]
      : ["Awaiting analysis"]);
    const group = (hidden) => el("div", { class: "ticker-group", "aria-hidden": hidden ? "true" : null },
      ...items.map((parts) => el("span", { class: "ticker-item" }, ...parts.map((p) => (typeof p === "string" ? p : p.cloneNode(true))))));
    // Two identical groups make the loop seamless; the copy is hidden from screen readers.
    return el("section", { class: "ticker", "aria-label": "Figures for disputes waiting for your response" },
      el("div", { class: "ticker-track" }, group(false), group(true)));
  }

  // A case card: the verdict as a huge word and the risk score as a large number. On hover or keyboard
  // focus the summary layer gives way to the agent's main reason. No recommendation: no verdict, no swap.
  function caseCard(d) {
    const href = `/case/${encodeURIComponent(d.dispute_id)}`;
    const decided = d.decision && d.decision.status === "ok";
    const summary = el("div", { class: "card-layer card-front" },
      decided ? el("div", { class: "card-verdict" },
        el("span", { class: "verdict-word", text: d.decision.decision }),
        el("span", { class: "card-risk", title: "Likelihood, 0 to 100, that the buyer's claim is valid, from the evidence on file" },
          el("span", { class: "risk-number num", text: String(d.decision.risk_score) }),
          el("span", { class: "cap", text: "risk" }))) : null,
      el("div", { class: "card-summary" },
        el("span", { text: DD.reasonLabel(d.reason) }),
        el("span", { class: "num", text: DD.money(d.amount) }),
        el("span", { class: `badge ${DD.statusClass(d.status)}`, text: DD.statusLabel(d.status) })),
      el("div", { class: "card-due" }, DD.countdownNode(d.seller_response_due_date)));
    const card = el("a", { class: `case-card${decided ? " has-reason" : ""}`, href },
      el("h3", { class: "card-id num", text: d.dispute_id }),
      DD.caseCaption(d),
      el("div", { class: "card-stage" }, summary));
    if (decided) {
      const back = el("div", { class: "card-layer card-back" },
        el("span", { class: "cap", text: "the agent's main reason" }),
        el("p", { class: "card-reason", text: "Loading the main reason." }));
      card.querySelector(".card-stage").append(back);
      DD.topReason(d).then((text) => {
        if (text) back.querySelector(".card-reason").textContent = text;
        else { back.remove(); card.classList.remove("has-reason"); }  // no stored reason: nothing to swap to
      });
    }
    return card;
  }

  function render(rows, firstRender) {
    if (!rows.length) {
      heroNext = null;
      root.replaceChildren(el("div", { class: "state" },
        el("h1", { text: "No disputes yet" }),
        el("p", { text: "When a buyer opens a dispute in the PayPal sandbox, the poller picks it up within 10 seconds and it appears here." })));
      return;
    }
    const waiting = rows.filter((d) => d.status === WAITING).sort(byDue);
    const open = rows.filter(isOpen).sort(byDue);
    heroNext = waiting.find((d) => Number.isFinite(dueMs(d))) || null;

    const cases = open.length
      ? el("section", { class: "folders" },
          el("div", { class: "stack-head" },
            el("h2", {}, el("span", { text: "Open" }), el("span", { text: "cases" })),
            el("span", { class: "cap", text: "Soonest deadline first. Hover or focus a card for the agent's main reason." })),
          el("div", { class: "case-cards" }, ...open.map(caseCard)))
      : el("section", { class: "folders" }, el("p", { class: "lede", text: `All ${rows.length} disputes in the local store are resolved.` }));

    root.replaceChildren(el("h1", { class: "sr-only", text: "Overview" }), hero(waiting, heroNext, firstRender), ticker(waiting, heroNext), cases);
    DD.reveal([cases]);
  }

  // The hero's countdown line ticks every second (not in demo mode, where it is frozen at the recording).
  setInterval(() => {
    if (!heroNext || DD.mode.demo || document.hidden) return;
    const board = root.querySelector(".hero-board");
    const old = board && board.children[1];
    if (old) old.replaceWith(countdownLine(heroNext));
  }, 1000);

  // Gentle parallax on the two hero lines: pointer position -> CSS variables, at most once per frame.
  // Off under reduced motion, on touch-only devices, and while the tab is hidden.
  if (!DD.reducedMotion() && window.matchMedia("(hover: hover) and (pointer: fine)").matches) {
    let frame = 0, px = 0, py = 0;
    window.addEventListener("pointermove", (e) => {
      px = (e.clientX / window.innerWidth - 0.5) * 2;
      py = (e.clientY / window.innerHeight - 0.5) * 2;
      if (frame || document.hidden) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        const board = root.querySelector(".hero-board");
        if (board) { board.style.setProperty("--px", px.toFixed(3)); board.style.setProperty("--py", py.toFixed(3)); }
      });
    }, { passive: true });
  }

  async function refresh() {
    try {
      const resp = await fetch("/api/disputes", { cache: "no-store" });
      if (resp.status === 403) throw new Error("This page only works on the machine running DisputeDesk (open it at http://127.0.0.1:8000).");
      if (!resp.ok) throw new Error(`The server answered HTTP ${resp.status}. Retrying every 5 seconds.`);
      const body = await resp.json();
      DD.live(true);
      await DD.modeReady;  // demo mode decides which clock the countdowns use
      banner.hidden = true;
      const key = JSON.stringify(body);
      if (key === lastKey) return;
      const firstRender = !lastKey;
      lastKey = key;
      render(body.disputes || [], firstRender);
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
