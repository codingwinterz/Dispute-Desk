// Shared by every page: the nav, formatters, and the 1-second countdown ticker.
// No build step. Everything user-visible is set with textContent, never innerHTML.
"use strict";

const DD = (() => {
  const URGENT_MS = 72 * 3600 * 1000;

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children) if (c !== null && c !== undefined) node.append(c);
    return node;
  }

  // ---- app shell: sidebar + top bar --------------------------------------------------
  // Pages whose data exists are links; the rest are listed as coming soon, never as dead links.
  const PAGES = { queue: "Dispute queue", policies: "Dispute policies", case: "Case" };
  const COMING_SOON = ["Overview", "Activity", "Insights", "Graph", "System"];

  function renderNav(current, title) {
    const links = [["queue", "/", "Queue"], ["policies", "/policies", "Policies"]];
    const sidebar = el("aside", { class: "sidebar" },
      el("a", { class: "brand", href: "/" }, el("span", { class: "tab", "aria-hidden": "true" }), "DisputeDesk"),
      el("nav", { class: "side-links", "aria-label": "Main" }, ...links.map(([key, href, label]) =>
        el("a", { href, "aria-current": key === current ? "page" : null, text: label }))),
      el("div", { class: "side-soon" },
        el("p", { class: "soon-title", id: "soon-title", text: "Coming soon" }),
        el("ul", { "aria-labelledby": "soon-title" }, ...COMING_SOON.map((name) => el("li", { text: name })))));
    const live = el("span", { class: "live", id: "live", role: "status", "aria-live": "polite" },
      el("span", { class: "dot", "aria-hidden": "true" }),
      el("span", { class: "live-label", text: "Connecting" }),
      el("span", { class: "live-age num" }));
    const topbar = el("header", { class: "topbar" },
      el("span", { class: "topbar-title", text: title || PAGES[current] || "DisputeDesk" }),
      el("div", { class: "topbar-status" }, live,
        el("span", { class: "env", title: "Every call goes to api-m.sandbox.paypal.com; there are no live credentials.", text: "PayPal sandbox" })));
    document.body.classList.add("shell");
    document.body.prepend(sidebar, topbar);
  }

  // Live indicator: pages report each request to the local server. "Live" (pulsing) only while the
  // last request succeeded; "Offline" (red, still) after a failure. Shows how long ago it last answered.
  let lastOk = 0;
  function live(ok) {
    const node = document.getElementById("live");
    if (!node) return;
    if (ok) lastOk = Date.now();
    node.classList.toggle("is-live", ok);
    node.classList.toggle("is-offline", !ok);
    node.querySelector(".live-label").textContent = ok ? "Live" : "Offline";
    updateAge();
  }
  function updateAge() {
    const age = document.querySelector("#live .live-age");
    if (!age) return;
    if (!lastOk) { age.textContent = ""; return; }
    const s = Math.max(0, Math.round((Date.now() - lastOk) / 1000));
    age.textContent = s < 2 ? "updated just now" : s < 120 ? `updated ${s}s ago` : `updated ${Math.round(s / 60)} min ago`;
  }

  // PayPal enums -> plain words. Unknown values fall back to a readable form, never dropped.
  // Enum values as returned by the Customer Disputes API (labels are display-only):
  // https://developer.paypal.com/docs/api/customer-disputes/v1/
  const REASONS = {
    MERCHANDISE_OR_SERVICE_NOT_RECEIVED: "Item not received",
    MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED: "Not as described",
    UNAUTHORISED: "Unauthorized payment",
    CREDIT_NOT_PROCESSED: "Refund not processed",
    DUPLICATE_TRANSACTION: "Duplicate payment",
    INCORRECT_AMOUNT: "Incorrect amount",
    PAYMENT_BY_OTHER_MEANS: "Paid by other means",
    CANCELED_RECURRING_BILLING: "Canceled subscription",
    PROBLEM_WITH_REMITTANCE: "Remittance problem",
    OTHER: "Other",
  };
  const STATUSES = {
    OPEN: ["Open", "waiting"],
    WAITING_FOR_SELLER_RESPONSE: ["Waiting for your response", "waiting"],
    WAITING_FOR_BUYER_RESPONSE: ["Waiting for buyer", "review"],
    UNDER_REVIEW: ["PayPal is reviewing", "review"],
    RESOLVED: ["Resolved", "resolved"],
    OTHER: ["Other", ""],
  };
  const words = (v) => v ? v.charAt(0) + v.slice(1).toLowerCase().replaceAll("_", " ") : "Unknown";
  const reasonLabel = (r) => REASONS[r] || words(r);
  const statusLabel = (s) => (STATUSES[s] || [words(s), ""])[0];
  const statusClass = (s) => (STATUSES[s] || ["", ""])[1];
  const stageLabel = (s) => ({ INQUIRY: "Inquiry", CHARGEBACK: "Chargeback", PRE_ARBITRATION: "Pre-arbitration", ARBITRATION: "Arbitration" }[s] || words(s));

  function money(amount) {
    if (!amount || amount.value === undefined) return "Amount unknown";
    const n = Number(amount.value);
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency: amount.currency_code || "USD" }).format(n);
    } catch {
      return `${amount.value} ${amount.currency_code || ""}`.trim();
    }
  }

  // "2d 4h", "5h 12m", "42m 10s"; overdue says so.
  function countdown(dueIso, now = Date.now()) {
    if (!dueIso) return { text: "No due date", urgent: false };
    const ms = Date.parse(dueIso) - now;
    if (Number.isNaN(ms)) return { text: "No due date", urgent: false };
    const abs = Math.abs(ms);
    const d = Math.floor(abs / 86400000), h = Math.floor(abs / 3600000) % 24;
    const m = Math.floor(abs / 60000) % 60, s = Math.floor(abs / 1000) % 60;
    const span = d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m ${String(s).padStart(2, "0")}s`;
    return ms < 0 ? { text: `Overdue by ${span}`, urgent: true } : { text: span, urgent: ms < URGENT_MS };
  }

  function dueDate(dueIso) {
    if (!dueIso) return "";
    return new Date(dueIso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }

  // Any element with data-due shows a live countdown; one ticker updates them all.
  function tick() {
    updateAge();
    const now = Date.now();
    for (const node of document.querySelectorAll("[data-due]")) {
      const c = countdown(node.dataset.due, now);
      const main = node.querySelector(".cd-main") || node;
      if (main.textContent !== c.text) main.textContent = c.text;
      node.classList.toggle("urgent", c.urgent);
    }
  }
  setInterval(tick, 1000);

  function countdownNode(dueIso, withDate = true) {
    const c = countdown(dueIso);
    return el("span", { class: `countdown${c.urgent ? " urgent" : ""}`, "data-due": dueIso || "" },
      el("span", { class: "cd-main", text: c.text }),
      withDate && dueIso ? el("span", { class: "sub", text: `Due ${dueDate(dueIso)}` }) : null);
  }

  function sourceTag(source) {
    return source === "seeded"
      ? el("span", { class: "tag seeded", title: "Merchant-supplied sandbox test data, not from PayPal", text: "seeded" })
      : el("span", { class: "tag", title: "From the PayPal sandbox API", text: "PayPal" });
  }

  function decisionChip(d) {
    if (!d) return el("span", { class: "chip pending", text: "Analysis pending" });
    if (d.status !== "ok") return el("span", { class: "chip error", text: "Analysis failed" });
    return d.decision === "DEFEND"
      ? el("span", { class: "chip defend", text: "Defend" })
      : el("span", { class: "chip accept", text: "Accept" });
  }

  function riskNode(score) {
    if (score === null || score === undefined) return el("span", { class: "hint", text: "Not scored" });
    const bar = el("span", { class: "bar", "aria-hidden": "true" }, el("span", { style: `width:${Math.max(0, Math.min(100, score))}%` }));
    return el("span", { class: "risk", title: "Likelihood the buyer's claim is valid, based only on the evidence on file" },
      el("span", { text: String(score) }), bar);
  }

  return { el, renderNav, live, reasonLabel, statusLabel, statusClass, stageLabel, money, countdown, countdownNode,
           dueDate, sourceTag, decisionChip, riskNode, tick };
})();
