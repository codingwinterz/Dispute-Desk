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

  // Split-flap: each character of the countdown in a fixed-width flap tile. Tiles flip on first load and,
  // after that, only where the character changed (static under reduced motion). The line's accessible
  // name is the real text.
  function flaps(text, cls, prev) {
    return el("span", { class: `${cls} flaps`, "aria-hidden": "true" }, ...[...text].map((ch, i) => (ch === " " ? " "
      : el("span", { class: `flap${prev === null || prev[i] !== ch ? " flip" : ""}`, style: `--fi:${i}`, text: ch }))));
  }

  function countdownLine(next, prev = null) {
    const c = DD.countdown(next.seller_response_due_date);
    const overdue = c.text.startsWith("Overdue");
    const token = (overdue ? c.text.replace("Overdue by ", "") : c.text).toUpperCase();
    const node = overdue
      ? line([el("span", { class: "hl urgent", "aria-hidden": "true" }, "OVERDUE"), flaps(token, "hl urgent", prev), el("span", { class: "hl urgent", "aria-hidden": "true" }, ".")], 2)
      : line([flaps(token, "hl", prev), el("span", { "aria-hidden": "true" }, "LEFT.")], 2);  // red is for overdue only
    node.dataset.flap = token;
    node.setAttribute("role", "img");
    node.setAttribute("aria-label", overdue ? c.text : `${c.text} left`);
    return node;
  }

  // Magnet lines: faint segments behind the hero type that turn toward the pointer (decorative).
  const MAGNET_COLS = 9, MAGNET_ROWS = 4;
  function magnetField() {
    const field = el("div", { class: "magnet-field", "aria-hidden": "true" });
    for (let r = 0; r < MAGNET_ROWS; r++) {
      for (let c = 0; c < MAGNET_COLS; c++) {
        field.append(el("span", { class: "magnet", style: `left:${((c + 0.5) / MAGNET_COLS * 100).toFixed(2)}%;top:${((r + 0.5) / MAGNET_ROWS * 100).toFixed(2)}%` }));
      }
    }
    return field;
  }

  function hero(waiting, next, firstRender) {
    const n = waiting.length;
    const board = el("section", { class: `hero-board${firstRender ? " rise" : ""}`, "aria-label": "Disputes waiting for your response" },
      line([el("span", { class: "hl" }, chars(String(n), "")), n === 1 ? "DISPUTE." : "DISPUTES."], 0),
      next ? countdownLine(next) : line(["NOTHING", "DUE."], 2),
      caption(next),
      magnetField());   // last, so children[1] stays the countdown line
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
      el("span", { class: "card-glare", "aria-hidden": "true" }, el("span", { class: "card-glare-disc" })),
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
    const split = el("div", { class: "hero-split" }, el("div", { class: "hero-glow", "aria-hidden": "true" }), hero(waiting, heroNext, firstRender), map.node);
    pipe = DDPipe.create(rows, firstRender);
    root.replaceChildren(el("h1", { class: "sr-only", text: "Overview" }), split, ticker(waiting, heroNext), pipe.node, cases);
    map.start();
    pipe.update(rows);
    DD.revealStaggered([cases.querySelector(".stack-head"), ...cases.querySelectorAll(".case-card")]);
  }

  // The hero's countdown line ticks every second (not in demo mode, where it is frozen at the recording).
  setInterval(() => {
    if (!heroNext || DD.mode.demo || document.hidden) return;
    const board = root.querySelector(".hero-board");
    const old = board && board.children[1];
    if (!old) return;
    const fresh = countdownLine(heroNext, old.dataset.flap || "");
    if (fresh.dataset.flap === old.dataset.flap) return;   // nothing changed: no DOM work, no flips
    fresh.classList.add("settled");                          // no word-rise on replacement, only flips
    old.replaceWith(fresh);
    DDPointer.kick();                                        // one frame re-applies its parallax offset
  }, 1000);

  // ---- pointer effects, on the shared DDPointer loop (off on touch, under reduced motion, in hidden tabs) ----

  // Multi-layer hero parallax: one eased pointer position moves each layer at its own rate. Transforms
  // are written straight onto the four layers (never an inherited CSS variable, which would restyle the
  // whole hero and case map every frame). [selector, x px, y px] at full pointer deflection:
  const PARALLAX = [[".hero-glow", -26, -16], [".hero-board > .hero-line:nth-child(1)", 8, 8],
                    [".hero-board > .hero-line:nth-child(2)", 16, 16], [".hero-board > .cap", 24, 14]];
  const par = { x: 0, y: 0 };
  DDPointer.subscribe((dt, p) => {
    const tx = p.inside ? (p.x / p.vw - 0.5) * 2 : 0, ty = p.inside ? (p.y / p.vh - 0.5) * 2 : 0;
    par.x = DDPointer.ease(par.x, tx, dt, 160);
    par.y = DDPointer.ease(par.y, ty, dt, 160);
    for (const [sel, dx, dy] of PARALLAX) {
      const node = root.querySelector(sel);
      if (node) node.style.transform = `translate3d(${(par.x * dx).toFixed(2)}px, ${(par.y * dy).toFixed(2)}px, 0)`;
    }
    return !(DDPointer.settled(par.x, tx, 0.002) && DDPointer.settled(par.y, ty, 0.002));
  }, () => {
    for (const [sel] of PARALLAX) { const node = root.querySelector(sel); if (node) node.style.removeProperty("transform"); }
  });

  // Magnet lines turn toward the pointer; at rest (and under reduced motion or on touch) they lie still.
  const MAGNET_REST = -22;   // degrees
  const magnets = { field: null, lines: [], centres: [], angles: [], dirty: true };
  window.addEventListener("scroll", () => { magnets.dirty = true; }, { passive: true });
  window.addEventListener("resize", () => { magnets.dirty = true; }, { passive: true });
  DDPointer.subscribe((dt, p) => {
    const field = root.querySelector(".magnet-field");
    if (!field) return false;
    if (field !== magnets.field) {
      magnets.field = field; magnets.lines = [...field.children]; magnets.angles = magnets.lines.map(() => MAGNET_REST); magnets.dirty = true;
    }
    if (magnets.dirty) {   // read layout here, in the loop, never in a pointer handler
      const r = field.getBoundingClientRect();
      magnets.centres = magnets.lines.map((_, i) => ({ x: r.left + ((i % MAGNET_COLS) + 0.5) / MAGNET_COLS * r.width,
                                                       y: r.top + (Math.floor(i / MAGNET_COLS) + 0.5) / MAGNET_ROWS * r.height }));
      magnets.dirty = false;
    }
    let busy = false;
    magnets.lines.forEach((line, i) => {
      const c = magnets.centres[i];
      const target = p.inside ? (Math.atan2(p.y - c.y, p.x - c.x) * 180) / Math.PI : MAGNET_REST;
      const cur = magnets.angles[i];
      const delta = ((target - cur + 540) % 360) - 180;   // shortest way round
      const next = DDPointer.ease(cur, cur + delta, dt, 140);
      magnets.angles[i] = next;
      line.style.transform = `rotate(${next.toFixed(1)}deg)`;
      if (Math.abs(delta) > 0.3) busy = true;
    });
    return busy;
  }, () => { magnets.lines.forEach((l) => l.style.removeProperty("transform")); magnets.angles = magnets.lines.map(() => MAGNET_REST); });

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
      st.gx = DDPointer.ease(st.gx, (nx + 1) * 50, dt, 90);   // percent across the card
      st.gy = DDPointer.ease(st.gy, (ny + 1) * 50, dt, 90);
      st.g = DDPointer.ease(st.g, tg, dt, 160);
      const resting = !active && Math.abs(st.rx) < 0.02 && Math.abs(st.ry) < 0.02 && st.g < 0.005;
      if (resting) {
        card.style.removeProperty("transform");
        if (st.disc) st.disc.style.opacity = "0";
        cards.delete(card);
        continue;
      }
      card.style.transform = `perspective(900px) rotateX(${st.rx.toFixed(2)}deg) rotateY(${st.ry.toFixed(2)}deg)`;
      // The glare disc moves by transform and fades by opacity: nothing the card's children inherit.
      st.disc = st.disc || card.querySelector(".card-glare-disc");
      if (st.disc && st.box) {
        st.disc.style.transform = `translate3d(${(st.gx / 100 * st.box.hw * 2).toFixed(1)}px, ${(st.gy / 100 * st.box.hh * 2).toFixed(1)}px, 0)`;
        st.disc.style.opacity = st.g.toFixed(3);
      }
      if (!(DDPointer.settled(st.rx, trx, 0.02) && DDPointer.settled(st.ry, try_, 0.02) && DDPointer.settled(st.g, tg, 0.004)
            && DDPointer.settled(st.gx, (nx + 1) * 50, 0.2) && DDPointer.settled(st.gy, (ny + 1) * 50, 0.2))) busy = true;
    }
    return busy;
  }, () => {
    for (const [card, st] of cards) { card.style.removeProperty("transform"); if (st.disc) st.disc.style.opacity = "0"; }
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
