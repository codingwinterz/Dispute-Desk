// Overview: what needs attention now. Reads GET /api/disputes only; every figure is counted from it.
// The hero and ticker count only disputes waiting for the seller's response (status
// WAITING_FOR_SELLER_RESPONSE). In demo mode every countdown is frozen at the snapshot's recorded time.
"use strict";

(() => {
  const { el } = DD;
  DD.renderNav("overview");

  const POLL_MS = 5000;
  const root = document.getElementById("overview");
  const banner = document.getElementById("error");
  let lastKey = "";
  let heroNext = null;   // the waiting dispute the hero counts down to
  let map = null;        // the case map beside the hero (DDMap); freed before every re-render
  const disposeMap = () => { if (map) { map.dispose(); map = null; } };
  window.addEventListener("pagehide", disposeMap);
  // The map's layout follows its own width (narrow column or phone): swap it when that crosses over.
  let lastRows = [];
  let pipe = null;       // the agent pipeline strip under the ticker (DDPipe)
  const mapWidth = () => { const s = root.querySelector(".hero-split"); return s ? s.clientWidth : root.clientWidth; };
  const wantCompact = () => {
    const w = mapWidth();
    return (window.matchMedia("(max-width: 900px)").matches ? w : w / 2.1) < DDMap.COMPACT_BELOW;
  };
  new ResizeObserver(() => {
    if (!map || map.plan.compact === wantCompact()) return;
    const old = map;
    map = DDMap.create(lastRows, { compact: !old.plan.compact });
    old.node.replaceWith(map.node);
    old.dispose();
    map.start();
  }).observe(root);

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
    return line([chars(c.text.toUpperCase(), "hl"), "LEFT."], 2);  // red is for overdue only
  }

  function hero(waiting, next, firstRender) {
    const n = waiting.length;
    const board = el("section", { class: `hero-board${firstRender ? " rise" : ""}`, "aria-label": "Disputes waiting for your response" },
      line([el("span", { class: "hl" }, chars(String(n), "")), n === 1 ? "DISPUTE." : "DISPUTES."], 0),
      next ? countdownLine(next) : line(["NOTHING", "DUE."], 2),
      caption(next));
    return board;
  }

  function caption(next) {
    const cap = el("span", { class: "cap" }, "Waiting on you", el("span", { class: "sep", text: "." }));
    if (next) {
      const c = DD.countdown(next.seller_response_due_date);
      if (c.urgent && !DD.isOverdue(c)) cap.prepend(DD.dueSoonChip(c), " ");
    }
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
    const items = [[el("span", { class: "hl", text: String(waiting.length) }), " waiting on you"]];
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
    // A Pause/Play button stops the motion from the keyboard (hover also pauses it).
    const section = el("section", { class: "ticker", "aria-label": "Figures for disputes waiting for your response" },
      el("div", { class: "ticker-track" }, group(false), group(true)));
    const toggle = el("button", { class: "ticker-toggle", type: "button", "aria-pressed": "false", text: "Pause" });
    toggle.addEventListener("click", () => {
      const paused = section.classList.toggle("is-paused");
      toggle.setAttribute("aria-pressed", String(paused));
      toggle.textContent = paused ? "Play" : "Pause";
    });
    section.append(toggle);
    return section;
  }

  // A case card: the verdict as a huge word and the risk score as a large number. On hover or keyboard
  // focus the summary layer gives way to the agent's main reason. No recommendation: no verdict, no swap.
  function caseCard(d) {
    const href = `/case/${encodeURIComponent(d.dispute_id)}`;
    const decided = d.decision && d.decision.status === "ok";
    const summary = el("div", { class: "card-layer card-front" },
      decided ? el("div", { class: "card-verdict" },
        el("span", { class: `verdict-word is-${d.decision.decision.toLowerCase()}`, text: d.decision.decision }),
        el("span", { class: "card-risk", title: "Likelihood, 0 to 100, that the buyer's claim is valid, from the evidence on file" },
          el("span", { class: "risk-number num", text: String(d.decision.risk_score) }),
          el("span", { class: "cap", text: "risk" }))) : null,
      el("div", { class: "card-summary" },
        el("span", { text: DD.reasonLabel(d.reason) }),
        el("span", { class: "num", text: DD.money(d.amount) }),
        DD.statusBadge(d.status)),
      el("div", { class: "card-due" }, DD.deadlineNode(d)));
    const due = d.seller_response_due_date ? `due ${DD.dueDate(d.seller_response_due_date).split(",")[0]}`
      : d.status === "UNDER_REVIEW" ? "with PayPal" : "no due date";
    const label = decided
      ? `${d.dispute_id}: ${d.decision.decision.toLowerCase()}, risk ${d.decision.risk_score}, ${due}`
      : `${d.dispute_id}: no recommendation yet, ${due}`;
    const card = el("a", { class: `case-card${decided ? " has-reason" : ""}`, href, "aria-label": `${label}.` },
      el("h3", { class: "card-id num", text: d.dispute_id }),
      DD.caseCaption(d),
      el("div", { class: "card-stage" }, summary));
    if (decided) {
      const back = el("div", { class: "card-layer card-back" },
        el("span", { class: "cap", text: "the agent's main reason" }),
        el("p", { class: "card-reason", text: "Loading the main reason." }));
      card.querySelector(".card-stage").append(back);
      DD.topReason(d).then((text) => {
        if (text) {
          back.querySelector(".card-reason").textContent = text;
          card.setAttribute("aria-label", `${label}. Main reason: ${text}`);
        }
        else { back.remove(); card.classList.remove("has-reason"); }  // no stored reason: nothing to swap to
      });
    }
    return card;
  }

  function render(rows, firstRender) {
    disposeMap();
    lastRows = rows;
    if (!rows.length) {
      heroNext = null;
      pipe = null;
      root.replaceChildren(el("div", { class: "state" },
        el("h1", { text: "No disputes yet" }),
        el("p", { text: "When a buyer opens a dispute in the PayPal sandbox, the poller picks it up within 10 seconds and it appears here." })));
      return;
    }
    const groups = DD.groupDisputes(rows);  // the same definition the Queue uses
    const waiting = groups.waiting;
    // Cards: waiting on you first, then with PayPal, then with the buyer, then anything else unresolved.
    const grouped = [...groups.waiting, ...groups.withPayPal, ...groups.withBuyer];
    const open = [...grouped, ...rows.filter((d) => isOpen(d) && !grouped.includes(d)).sort(byDue)];
    heroNext = waiting.find((d) => Number.isFinite(dueMs(d))) || null;

    const cases = open.length
      ? el("section", { class: "folders" },
          el("div", { class: "stack-head" },
            el("h2", {}, el("span", { text: "Open" }), el("span", { text: "cases" })),
            el("span", { class: "cap", text: [
              `${groups.waiting.length} waiting on you`,
              groups.withPayPal.length ? `${groups.withPayPal.length} with PayPal` : "",
              groups.withBuyer.length ? `${groups.withBuyer.length} waiting on the buyer` : "",
            ].filter(Boolean).join(", ") + ". Hover or focus a card for the agent's main reason." })),
          el("div", { class: "case-cards" }, ...open.map(caseCard)))
      : el("section", { class: "folders" }, el("p", { class: "lede", text: `All ${rows.length} disputes in the local store are resolved.` }));

    // The hero: stacked type on the left, the case map on the right (below it on narrow screens).
    map = DDMap.create(rows, { compact: wantCompact() });
    const split = el("div", { class: "hero-split" }, hero(waiting, heroNext, firstRender), map.node);
    pipe = DDPipe.create(rows, firstRender);
    root.replaceChildren(el("h1", { class: "sr-only", text: "Overview" }), split, ticker(waiting, heroNext), pipe.node, cases);
    map.start();
    pipe.update(rows);
    DD.reveal([cases]);
  }

  // The hero's countdown line ticks every second (not in demo mode, where it is frozen at the recording).
  setInterval(() => {
    if (!heroNext || DD.mode.demo || document.hidden) return;
    const board = root.querySelector(".hero-board");
    const old = board && board.children[1];
    if (old) old.replaceWith(countdownLine(heroNext));
  }, 1000);

  // ---- pointer effects, on the shared DDPointer loop (off on touch, under reduced motion, in hidden tabs) ----

  // Multi-layer hero parallax: one eased pointer position -> CSS variables on the hero container; each
  // layer (glow pool, two type lines, caption) moves at its own rate, set in app.css.
  const par = { x: 0, y: 0 };
  DDPointer.subscribe((dt, p) => {
    const tx = p.inside ? (p.x / p.vw - 0.5) * 2 : 0, ty = p.inside ? (p.y / p.vh - 0.5) * 2 : 0;
    par.x = DDPointer.ease(par.x, tx, dt, 160);
    par.y = DDPointer.ease(par.y, ty, dt, 160);
    const hero = root.querySelector(".hero-split");
    if (hero) { hero.style.setProperty("--px", par.x.toFixed(3)); hero.style.setProperty("--py", par.y.toFixed(3)); }
    return !(DDPointer.settled(par.x, tx, 0.002) && DDPointer.settled(par.y, ty, 0.002));
  }, () => {
    const hero = root.querySelector(".hero-split");
    if (hero) { hero.style.removeProperty("--px"); hero.style.removeProperty("--py"); }
  });

  // Tilt and glare on the case cards: up to MAX_TILT degrees toward the pointer, with a soft blue glare
  // that follows it. Pointer events only pick the active card; the loop reads its size once per
  // activation (and after scrolling), never inside a pointer handler.
  const MAX_TILT = 7;
  const cards = new Map();   // card -> { rx, ry, gx, gy, g, box }
  let activeCard = null, boxDirty = true;
  root.addEventListener("pointerover", (e) => {
    const card = e.target.closest && e.target.closest(".case-card");
    if (card === activeCard) return;
    activeCard = card;
    boxDirty = true;
    if (card && !cards.has(card)) cards.set(card, { rx: 0, ry: 0, gx: 50, gy: 50, g: 0, box: null });
    DDPointer.kick();
  });
  root.addEventListener("pointerleave", () => { activeCard = null; DDPointer.kick(); });
  window.addEventListener("scroll", () => { boxDirty = true; }, { passive: true });
  DDPointer.subscribe((dt, p) => {
    let busy = false;
    for (const [card, st] of cards) {
      if (!card.isConnected) { cards.delete(card); continue; }
      const active = card === activeCard && p.inside;
      if (active && (boxDirty || !st.box)) {
        // Untransformed size, and the centre (which a centred tilt does not move).
        const r = card.getBoundingClientRect();
        st.box = { cx: r.left + r.width / 2, cy: r.top + r.height / 2, hw: card.offsetWidth / 2, hh: card.offsetHeight / 2 };
        boxDirty = false;
      }
      let nx = 0, ny = 0;
      if (active && st.box) {
        nx = Math.max(-1, Math.min(1, (p.x - st.box.cx) / st.box.hw));
        ny = Math.max(-1, Math.min(1, (p.y - st.box.cy) / st.box.hh));
      }
      const trx = -ny * MAX_TILT, try_ = nx * MAX_TILT, tg = active ? 1 : 0;
      st.rx = DDPointer.ease(st.rx, trx, dt, 110);
      st.ry = DDPointer.ease(st.ry, try_, dt, 110);
      st.gx = DDPointer.ease(st.gx, (nx + 1) * 50, dt, 90);
      st.gy = DDPointer.ease(st.gy, (ny + 1) * 50, dt, 90);
      st.g = DDPointer.ease(st.g, tg, dt, 160);
      const resting = !active && Math.abs(st.rx) < 0.02 && Math.abs(st.ry) < 0.02 && st.g < 0.005;
      if (resting) {
        card.style.removeProperty("transform");
        card.style.removeProperty("--glare-on");
        cards.delete(card);
        continue;
      }
      card.style.transform = `perspective(900px) rotateX(${st.rx.toFixed(2)}deg) rotateY(${st.ry.toFixed(2)}deg)`;
      card.style.setProperty("--gx", `${st.gx.toFixed(1)}%`);
      card.style.setProperty("--gy", `${st.gy.toFixed(1)}%`);
      card.style.setProperty("--glare-on", st.g.toFixed(3));
      if (!(DDPointer.settled(st.rx, trx, 0.02) && DDPointer.settled(st.ry, try_, 0.02) && DDPointer.settled(st.g, tg, 0.004)
            && DDPointer.settled(st.gx, (nx + 1) * 50, 0.2) && DDPointer.settled(st.gy, (ny + 1) * 50, 0.2))) busy = true;
    }
    return busy;
  }, () => {
    for (const card of cards.keys()) { card.style.removeProperty("transform"); card.style.removeProperty("--glare-on"); }
    cards.clear();
  });

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
  // Submissions don't always change /api/disputes at once: recount the pipeline every 30 seconds.
  setInterval(() => { if (pipe && !document.hidden) pipe.update(lastRows); }, 30000);
})();
