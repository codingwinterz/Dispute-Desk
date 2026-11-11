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
  // Only working pages are listed.
  const PAGES = { overview: "Overview", queue: "Dispute queue", activity: "Activity", insights: "Insights", records: "Merchant records", policies: "Dispute policies", case: "Case" };

  function renderNav(current, title) {
    const links = [["overview", "/", "Overview"], ["queue", "/queue", "Queue"], ["activity", "/activity", "Activity"],
                   ["insights", "/insights", "Insights"], ["records", "/merchant-records", "Merchant records"],
                   ["policies", "/policies", "Policies"]];
    const sidebar = el("aside", { class: "sidebar" },
      el("a", { class: "brand", href: "/" }, el("span", { class: "tab", "aria-hidden": "true" }), "DisputeDesk"),
      el("nav", { class: "side-links", "aria-label": "Main" }, ...links.map(([key, href, label]) =>
        el("a", { href, "aria-current": key === current ? "page" : null, text: label }))));
    const live = el("span", { class: "live", id: "live", role: "status", "aria-live": "polite" },
      el("span", { class: "dot", "aria-hidden": "true" }),
      el("span", { class: "live-label", text: "Connecting" }),
      el("span", { class: "live-age num" }));
    const topbar = el("header", { class: "topbar" },
      el("span", { class: "topbar-title", text: title || PAGES[current] || "DisputeDesk" }),
      el("div", { class: "topbar-status" }, live,
        // Poller status from GET /api/poller; text set by renderPollChip(). Not aria-live: it ticks every second.
        el("span", { class: "poll-chip num", id: "poll-chip", text: "Polling status loading" }),
        el("span", { class: "env", title: "Every call goes to api-m.sandbox.paypal.com; there are no live credentials.", text: "PayPal sandbox" })));
    document.body.classList.add("shell");
    // Keyboard users can jump past the sidebar to the page content.
    const main = document.querySelector("main");
    if (main) { main.id = main.id || "content"; main.tabIndex = -1; }
    const skip = el("a", { class: "skip-link", href: `#${main ? main.id : "content"}`, text: "Skip to content" });
    document.body.prepend(skip, sidebar, topbar);
    startPollStatus();
    modeReady.then((m) => {
      if (m.scratch) {
        // Scratch mode: a rehearsal copy of the store and graph. Real PayPal sandbox, not the real local store.
        topbar.querySelector(".topbar-status").prepend(el("span", { class: "scratch-badge",
          title: `Scratch copy: database ${m.scratch_db}, graph ${m.scratch_graph}. The real local store and graph are not touched.`, text: "Scratch data" }));
        document.body.classList.add("is-scratch");
      }
      if (!m.demo && !m.scratch) {
        mountTopbarKillSwitch(topbar.querySelector(".topbar-status"));
      }
      if (!m.demo) return;
      const recorded = m.recorded_at ? new Date(m.recorded_at).toLocaleString() : "an earlier session";
      topbar.querySelector(".topbar-status").prepend(el("span", { class: "demo-badge", title: `Recorded sandbox data from ${recorded}. Nothing is sent to PayPal.`, text: "Demo data" }));
      document.body.classList.add("is-demo");
    });
  }

  // Demo mode (GET /api/mode): pages await this before drawing write controls, so a button never
  // flashes enabled before the mode is known. Any failure is treated as normal (non-demo) mode;
  // the server refuses writes in demo mode regardless.
  const DEMO_LABEL = "Demo mode: recorded sandbox data";
  const modeReady = fetch("/api/mode", { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : { demo: false }))
    .catch(() => ({ demo: false }));
  const mode = { demo: false };
  modeReady.then((m) => { mode.demo = Boolean(m.demo); mode.recorded_at = m.recorded_at; mode.scratch = Boolean(m.scratch); });

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
  function ageText() {
    if (!lastOk) return "";
    const s = Math.max(0, Math.round((Date.now() - lastOk) / 1000));
    return s < 2 ? "updated just now" : s < 120 ? `updated ${s}s ago` : `updated ${Math.round(s / 60)} min ago`;
  }
  function updateAge() {
    const text = ageText();
    for (const node of document.querySelectorAll("#live .live-age, [data-live-age]")) {
      if (node.textContent !== text) node.textContent = text;
    }
  }

  // ---- poller status chip (GET /api/poller, read-only) --------------------------------------
  // One fetch every POLL_STATUS_MS; the chip's "Last 3s ago" ticks locally between fetches, counted
  // from the server's own seconds_since_last_poll so the two clocks are never compared.
  const POLL_STATUS_MS = 5000;
  let pollStatus = null;      // last /api/poller body, or { state: "unknown" } after a failed fetch
  let pollFetchedAt = 0;      // Date.now() when pollStatus arrived
  const secs = (n) => `${+Number(n).toFixed(1)}s`;
  const agoText = (s) => (s < 120 ? `${Math.round(s)}s ago` : `${Math.round(s / 60)} min ago`);
  /** Chip wording and tone for a /api/poller body, `elapsedMs` after it was fetched. Pure, for tests. */
  function pollChipText(s, elapsedMs = 0, now = clock()) {
    const gone = Math.max(0, elapsedMs) / 1000;
    if (!s || s.state === "unknown") return { text: "Polling status unknown", tone: "", title: "The local server did not answer GET /api/poller." };
    if (s.state === "recorded") {
      const when = new Date(now).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
      return { text: "Recorded data", tone: "recorded", title: `Recorded sandbox data as of ${when}. Polling is disabled; nothing is fetched from PayPal.` };
    }
    if (s.state === "off") return { text: "Polling off", tone: "", title: s.message || "" };
    if (s.state === "error") {
      const wait = s.next_poll_in_seconds;
      const next = wait === null || wait === undefined ? "" : ` Retry in ${Math.ceil(Math.max(0, wait - gone))}s.`;
      const n = s.consecutive_errors;
      return { text: `Polling failing: ${n} error${n === 1 ? "" : "s"} in a row.${next}`, tone: "error",
               title: `Last error: ${s.last_error || "unknown"}. The poller backs off and retries on its own.` };
    }
    if (s.state === "starting" || s.seconds_since_last_poll === null || s.seconds_since_last_poll === undefined) {
      return { text: `Polling every ${secs(s.interval_seconds)}. First poll running.`, tone: "", title: "" };
    }
    const count = s.disputes_returned;
    const fresh = (s.new_ids || []).length;
    const disputes = count === null || count === undefined ? "" : ` ${count} dispute${count === 1 ? "" : "s"}${fresh ? `, ${fresh} new` : ""}.`;
    return { text: `Polling every ${secs(s.interval_seconds)}. Last ${agoText(s.seconds_since_last_poll + gone)}.${disputes}`, tone: "",
             title: fresh ? `New on the last poll: ${s.new_ids.join(", ")}` : "Read-only GET /v1/customer/disputes against the PayPal sandbox." };
  }
  function renderPollChip() {
    const node = document.getElementById("poll-chip");
    if (!node || !pollStatus) return;
    const c = pollChipText(pollStatus, Date.now() - pollFetchedAt);
    if (node.textContent !== c.text) node.textContent = c.text;
    if (node.title !== c.title) node.title = c.title;
    node.classList.toggle("is-failing", c.tone === "error");
    node.classList.toggle("error-text", c.tone === "error");  // the shared red text class: errors only
    node.classList.toggle("is-recorded", c.tone === "recorded");
  }
  async function fetchPollStatus() {
    try {
      const r = await fetch("/api/poller", { cache: "no-store" });
      pollStatus = r.ok ? await r.json() : { state: "unknown" };
    } catch {
      pollStatus = { state: "unknown" };
    }
    pollFetchedAt = Date.now();
    renderPollChip();
    return pollStatus;
  }
  // Only pages with the top bar fetch; demo mode answers once ("recorded") and is not asked again.
  function startPollStatus() {
    fetchPollStatus().then((s) => {
      if (s.state === "recorded") return;
      setInterval(() => { if (!document.hidden) fetchPollStatus(); }, POLL_STATUS_MS);
    });
  }

  // When the app first saw a dispute, relative to PayPal's create_time. "" when either time is missing,
  // the order is reversed, or the gap is over 10 minutes (the sandbox can list a new dispute late, and
  // a dispute already open when the app started was not detected live). Pure, for tests.
  const DETECTION_MAX_S = 600;
  function detectionText(createIso, detectedIso, via) {
    const gap = (Date.parse(detectedIso) - Date.parse(createIso)) / 1000;
    if (!Number.isFinite(gap) || gap < 0 || gap > DETECTION_MAX_S) return "";
    const s = Math.round(gap);
    const span = s < 120 ? `${s} second${s === 1 ? "" : "s"}` : `${Math.floor(s / 60)} min ${s % 60} s`;
    return `Detected ${span} after PayPal created it, via ${via === "webhook" ? "webhook" : "poll"}`;
  }

  // ---- the agent's main reason (read-only, cached) -----------------------------------------
  // From the existing GET /api/decisions/{id}; fetched once per dispute and decision, and only for
  // disputes with a recommendation. Resolves to "" when there is no stored reason.
  const reasonCache = new Map();  // dispute_id -> { key, promise }
  function topReason(row) {
    const d = row && row.decision;
    if (!d || d.status !== "ok") return Promise.resolve("");
    const cached = reasonCache.get(row.dispute_id);
    if (cached && cached.key === d.created_at) return cached.promise;
    const promise = fetch(`/api/decisions/${encodeURIComponent(row.dispute_id)}`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => {
        const first = body && body.decision && body.decision.reasoning && body.decision.reasoning[0];
        return first ? first.point : "";
      })
      .catch(() => "");
    reasonCache.set(row.dispute_id, { key: d.created_at, promise });
    return promise;
  }

  // Caption metadata: "PP-R-QQW . due Nov 15" plus the record's source tag.
  function caseCaption(row) {
    const shortId = String(row.dispute_id).split("-").slice(0, 3).join("-");
    const due = row.seller_response_due_date ? `due ${dueDate(row.seller_response_due_date).split(",")[0]}`
      : row.status === "UNDER_REVIEW" ? "with PayPal" : "no due date";
    return el("span", { class: "cap" }, shortId, el("span", { class: "sep", text: "." }), due,
      el("span", { class: "sep", text: "." }), sourceTag(row.source));
  }

  // ---- palette tokens --------------------------------------------------------------------
  // Colours live only in app.css :root. Scripts (three.js, canvas labels, the favicon) read them here.
  // A probe element resolves each token through the browser, so color-mix() and any other CSS colour
  // syntax arrive as plain numbers that three.js and canvas understand.
  const colorCache = new Map();
  let probe = null;
  function resolveColor(name) {
    if (colorCache.has(name)) return colorCache.get(name);
    const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    if (!raw) {
      console.error(`DisputeDesk: colour token ${name} is not defined in the :root block of app.css.`);
      return { r: 128, g: 128, b: 128, a: 1 };  // visible grey, not a crash
    }
    if (!probe) {
      probe = document.createElement("span");
      probe.setAttribute("aria-hidden", "true");
      probe.style.cssText = "position:absolute;width:0;height:0;overflow:hidden;pointer-events:none;";
      document.documentElement.append(probe);
    }
    probe.style.color = "";
    probe.style.color = `var(${name})`;
    const parsed = parseColor(getComputedStyle(probe).color);
    if (!parsed) {
      console.error(`DisputeDesk: colour token ${name} (${raw}) could not be resolved to a colour.`);
      return { r: 128, g: 128, b: 128, a: 1 };
    }
    colorCache.set(name, parsed);
    return parsed;
  }
  // Computed colours come back as rgb()/rgba(), or as color(srgb r g b / a) for color-mix() results.
  function parseColor(text) {
    let m = text.match(/^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/);
    if (m) return { r: +m[1], g: +m[2], b: +m[3], a: alpha(m[4]) };
    m = text.match(/^color\(srgb\s+([\d.e-]+)\s+([\d.e-]+)\s+([\d.e-]+)(?:\s*\/\s*([\d.]+%?))?\s*\)$/);
    if (m) return { r: Math.round(+m[1] * 255), g: Math.round(+m[2] * 255), b: Math.round(+m[3] * 255), a: alpha(m[4]) };
    return null;
  }
  const alpha = (v) => (v === undefined ? 1 : v.endsWith("%") ? parseFloat(v) / 100 : +v);
  const hex2 = (n) => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, "0");
  /** "#rrggbb" for a token such as "--primary". */
  function color(name) {
    const c = resolveColor(name);
    return `#${hex2(c.r)}${hex2(c.g)}${hex2(c.b)}`;
  }
  /** "rgba(r, g, b, a)" for a token, with its own alpha multiplied by `a`. */
  function rgba(name, a = 1) {
    const c = resolveColor(name);
    return `rgba(${c.r}, ${c.g}, ${c.b}, ${+(c.a * a).toFixed(3)})`;
  }
  /** 0xRRGGBB for three.js. */
  function colorNum(name) {
    const c = resolveColor(name);
    return (Math.round(c.r) << 16) | (Math.round(c.g) << 8) | Math.round(c.b);
  }

  // The favicon follows the palette: the folder mark drawn from the tokens (static file as a fallback).
  function paintFavicon() {
    const link = document.querySelector('link[rel="icon"]');
    if (!link) return;
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="${color("--ground-deep")}"/>`
      + `<path fill="${color("--primary")}" d="M5 12.4h9.6L17.4 7H27v18.6H5z"/></svg>`;
    link.href = `data:image/svg+xml,${encodeURIComponent(svg)}`;
  }
  paintFavicon();

  // ---- motion helpers --------------------------------------------------------------------
  const reducedMotion = () => Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

  // Pause every CSS animation while the tab is hidden.
  document.addEventListener("visibilitychange", () => document.documentElement.classList.toggle("is-hidden", document.hidden));

  // Scroll reveal, once per element. Content is visible by default: only elements below the fold are
  // hidden, and only when motion is allowed and IntersectionObserver exists, so nothing can stay blank.
  function reveal(nodes) {
    if (reducedMotion() || !("IntersectionObserver" in window)) return;
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        e.target.classList.add("is-revealed");
        e.target.classList.remove("reveal-pending");
        io.unobserve(e.target);
      }
    }, { rootMargin: "0px 0px -8% 0px" });
    for (const n of nodes) {
      if (!n || n.getBoundingClientRect().top < window.innerHeight) continue;  // already on screen
      n.classList.add("reveal-pending");
      io.observe(n);
    }
  }

  // The same reveal, staggered: each node waits a little longer than the one before (CSS reads --reveal-i).
  function revealStaggered(nodes) {
    nodes.filter(Boolean).forEach((n, i) => n.style.setProperty("--reveal-i", String(i)));
    reveal(nodes);
  }

  // ---- editorial page hero ---------------------------------------------------------------------
  // Shared by every page: a small-caps eyebrow, the display headline, one sentence from real data and
  // stat chips. Pages own their headline and sentence; these helpers build the eyebrow and the chips.
  function eyebrow(section) {
    const source = mode.demo ? `recorded sandbox data, ${recordedLabel().split(",")[0]}` : "live from the PayPal sandbox";
    return eyebrowWith(section, source);
  }
  // The same eyebrow with a page's own source wording (e.g. Merchant records, which are simulated).
  function eyebrowWith(section, source) {
    return [el("span", {}, section, el("span", { class: "sep", text: "." }), source)];  // one item beside the rule
  }
  /** One stat chip. value: string or node; note: optional string or node; tone: "" | "waiting" | "overdue". */
  function statChip({ label, value, note = null, tone = "" }) {
    return el("li", { class: `stat-chip${tone ? ` ${tone}` : ""}` },
      el("span", { class: "stat-value num" }, tone === "waiting" ? el("span", { class: "badge waiting stat-dot", "aria-hidden": "true" }) : null, value),
      el("span", { class: "stat-label", text: label }),
      note ? el("span", { class: "stat-note" }, note) : null);
  }
  // Amounts summed per currency, never converted. "" when nothing is stored.
  function sumAmounts(rows) {
    const byCur = {};
    for (const r of rows) {
      if (!r.amount || r.amount.value === undefined) continue;
      const cur = r.amount.currency_code || "USD";
      byCur[cur] = (byCur[cur] || 0) + Number(r.amount.value);
    }
    return Object.entries(byCur).map(([cur, v]) => money({ value: v.toFixed(2), currency_code: cur })).join(" + ");
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
    WAITING_FOR_SELLER_RESPONSE: ["Waiting on you", "waiting"],
    WAITING_FOR_BUYER_RESPONSE: ["Waiting on the buyer", "review"],
    UNDER_REVIEW: ["With PayPal", "review"],
    RESOLVED: ["Resolved", "resolved"],
    OTHER: ["Other", ""],
  };
  const words = (v) => v ? v.charAt(0) + v.slice(1).toLowerCase().replaceAll("_", " ") : "Unknown";
  const reasonLabel = (r) => REASONS[r] || words(r);
  const statusLabel = (s) => (STATUSES[s] || [words(s), ""])[0];
  const statusClass = (s) => (STATUSES[s] || ["", ""])[1];
  // The longer meaning behind each short label, shown as a tooltip on every status badge.
  const STATUS_HELP = {
    WAITING_FOR_SELLER_RESPONSE: "PayPal is waiting for your response before the deadline.",
    UNDER_REVIEW: "Under review by PayPal. Nothing for you to answer for now.",
    WAITING_FOR_BUYER_RESPONSE: "PayPal is waiting for the buyer's response.",
    RESOLVED: "PayPal has closed this dispute.",
  };
  function statusBadge(status) {
    return el("span", { class: `badge ${statusClass(status)}`, title: STATUS_HELP[status] || null, text: statusLabel(status) });
  }
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

  // One clock for every countdown. In demo mode the data is a recording, so countdowns are frozen at
  // the snapshot's recorded time (and labelled with it) instead of running against today's date.
  function clock() {
    return mode.demo && mode.recorded_at ? Date.parse(mode.recorded_at) : Date.now();
  }

  function recordedLabel() {
    return mode.demo && mode.recorded_at
      ? new Date(mode.recorded_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
      : "";
  }

  // "2d 4h", "5h 12m", "42m 10s"; overdue says so.
  function countdown(dueIso, now = clock()) {
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
    renderPollChip();
    if (document.hidden) return;  // nothing to update while the tab is in the background
    const now = clock();
    for (const node of document.querySelectorAll("[data-due]")) {
      const c = countdown(node.dataset.due, now);
      const main = node.querySelector(".cd-main") || node;
      if (main.textContent !== c.text) main.textContent = c.text;
      node.classList.toggle("urgent", c.urgent);
      node.classList.toggle("overdue", isOverdue(c));
      const soon = node.querySelector(".due-soon");
      if (soon) soon.hidden = !(c.urgent && !isOverdue(c));
    }
  }
  setInterval(tick, 1000);

  // One definition of who a dispute is waiting on, shared by Overview and Queue so their counts match.
  //   waiting on you  = WAITING_FOR_SELLER_RESPONSE
  //   with PayPal     = UNDER_REVIEW
  //   with the buyer  = WAITING_FOR_BUYER_RESPONSE
  const byDueDate = (a, b) => (Date.parse(a.seller_response_due_date) || Infinity) - (Date.parse(b.seller_response_due_date) || Infinity);
  function groupDisputes(rows) {
    const pick = (status) => rows.filter((r) => r.status === status).sort(byDueDate);
    return { waiting: pick("WAITING_FOR_SELLER_RESPONSE"), withPayPal: pick("UNDER_REVIEW"), withBuyer: pick("WAITING_FOR_BUYER_RESPONSE") };
  }

  // A dispute's deadline cell: the live countdown, or "With PayPal" while it is under review by PayPal and
  // there is nothing for the merchant to answer.
  function deadlineNode(row, withDate = true) {
    if (row.status === "UNDER_REVIEW" && !row.seller_response_due_date) {
      return el("span", { class: "countdown with-paypal" }, el("span", { class: "cd-main", text: "With PayPal" }),
        withDate ? el("span", { class: "sub", text: "Nothing to answer" }) : null);
    }
    return countdownNode(row.seller_response_due_date, withDate);
  }

  const isOverdue = (c) => c.text.startsWith("Overdue");
  // Under 72 hours and not overdue: a filled tangerine "Due soon" chip beside the countdown.
  const dueSoonChip = (c) => el("span", { class: "due-soon", hidden: !(c.urgent && !isOverdue(c)), text: "Due soon" });

  function countdownNode(dueIso, withDate = true) {
    const c = countdown(dueIso);
    return el("span", { class: `countdown${c.urgent ? " urgent" : ""}${isOverdue(c) ? " overdue" : ""}`, "data-due": dueIso || "" },
      el("span", { class: "cd-row" }, el("span", { class: "cd-main", text: c.text }), dueSoonChip(c)),
      withDate && dueIso ? el("span", { class: "sub", title: mode.demo ? `Due ${dueDate(dueIso)}; countdown as recorded ${recordedLabel()}` : null,
        text: mode.demo ? `Due ${dueDate(dueIso).split(",")[0]}, as of ${recordedLabel().split(",")[0]}` : `Due ${dueDate(dueIso)}` }) : null);
  }

  function sourceTag(source) {
    return source === "seeded"
      ? el("span", { class: "tag seeded", title: "Merchant-supplied sandbox test data, not from PayPal", text: "seeded" })
      : el("span", { class: "tag paypal", title: "From the PayPal sandbox API", text: "PayPal" });
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

  // ---- Autopilot preview & kill switch (shadow-only; hidden in demo and scratch mode) --------
  function mountTopbarKillSwitch(statusBar) {
    if (!statusBar || mode.demo || mode.scratch) return;
    const btn = el("button", {
      type: "button",
      id: "autopilot-kill",
      class: "secondary small autopilot-kill",
      hidden: true,
      title: "Immediately switch off Autopilot preview and cancel any armed countdowns",
      text: "Stop Autopilot",
    });
    statusBar.prepend(btn);

    async function sync() {
      if (mode.demo || mode.scratch) { btn.hidden = true; return; }
      try {
        const resp = await fetch("/api/autopilot", { cache: "no-store" });
        if (!resp.ok) { btn.hidden = true; return; }
        const st = await resp.json();
        const active = Boolean(st.enabled || (st.pending && st.pending.length));
        btn.hidden = !active;
      } catch {
        // Keep last state on transient failure.
      }
    }

    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await fetch("/api/autopilot/kill", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        btn.hidden = true;
        window.dispatchEvent(new CustomEvent("dd:autopilot-changed"));
      } finally {
        btn.disabled = false;
      }
    });

    window.addEventListener("dd:autopilot-changed", sync);
    sync();
    setInterval(() => { if (!document.hidden) sync(); }, 5000);
  }

  function mountAutopilot(container, opts = {}) {
    if (!container) return null;
    const { disputeId = null, showToggle = true, onUpdate = null } = opts;
    let disposed = false;
    let state = null;
    let syncedAt = 0;
    let pollTimer = 0;
    let tickTimer = 0;
    let busy = false;
    let statusMsg = "";

    const remainingFor = (job) => {
      const elapsed = Math.max(0, (Date.now() - syncedAt) / 1000);
      return Math.max(0, (Number(job.remaining_seconds) || 0) - elapsed);
    };

    async function fetchState() {
      if (disposed || mode.demo || mode.scratch) return;
      try {
        const resp = await fetch("/api/autopilot", { cache: "no-store" });
        if (!resp.ok) {
          container.hidden = true;
          container.replaceChildren();
          return;
        }
        const next = await resp.json();
        const prevPending = state && state.pending ? state.pending.length : 0;
        const nextPending = next.pending ? next.pending.length : 0;
        state = next;
        syncedAt = Date.now();
        draw();
        if (prevPending > 0 && nextPending === 0 && onUpdate) onUpdate();
      } catch {
        // Transient connectivity loss; keep existing render.
      }
    }

    async function postAction(url, method, bodyObj, okText) {
      if (busy || mode.demo || mode.scratch) return;
      busy = true;
      statusMsg = "";
      draw();
      try {
        const resp = await fetch(url, {
          method,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(bodyObj),
        });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          statusMsg = body.message || body.error || `HTTP ${resp.status}`;
        } else {
          state = body.autopilot || body;
          syncedAt = Date.now();
          if (okText) statusMsg = okText;
        }
      } catch {
        statusMsg = "Can't reach the DisputeDesk server.";
      } finally {
        busy = false;
        draw();
        window.dispatchEvent(new CustomEvent("dd:autopilot-changed"));
        if (onUpdate) onUpdate();
      }
    }

    function updateTicks() {
      if (!state || mode.demo || mode.scratch) return;
      const pending = (state.pending || []).filter((j) => !disputeId || j.dispute_id === disputeId);
      for (const job of pending) {
        const rem = remainingFor(job);
        const total = Math.max(1, Number(job.window_seconds) || 30);
        const secs = Math.ceil(rem);
        const pct = Math.max(0, Math.min(100, (rem / total) * 100));
        const card = container.querySelector(`[data-autopilot-id="${CSS.escape(job.dispute_id)}"]`);
        if (card) {
          const timerEl = card.querySelector(".ap-timer");
          const barEl = card.querySelector(".ap-bar-fill");
          if (timerEl) timerEl.textContent = rem > 0 ? `Would accept in ${secs} s (preview only)` : "Building preview dry run…";
          if (barEl) barEl.style.width = `${pct.toFixed(1)}%`;
        }
      }
    }

    function draw() {
      if (disposed || mode.demo || mode.scratch || !state) {
        container.hidden = true;
        container.replaceChildren();
        return;
      }
      const pending = (state.pending || []).filter((j) => !disputeId || j.dispute_id === disputeId);
      if (!showToggle && !pending.length && !statusMsg) {
        container.hidden = true;
        container.replaceChildren();
        return;
      }
      container.hidden = false;
      const children = [];
      if (showToggle) {
        const toggleId = `autopilot-toggle-${disputeId || "global"}`;
        const input = el("input", {
          type: "checkbox",
          id: toggleId,
          checked: Boolean(state.enabled),
          disabled: busy,
        });
        input.addEventListener("change", () => postAction("/api/autopilot/settings", "PUT", { enabled: input.checked }, ""));
        const killBtn = (state.enabled || pending.length)
          ? el("button", { class: "secondary small", type: "button", disabled: busy, text: "Stop Autopilot" })
          : null;
        if (killBtn) killBtn.addEventListener("click", () => postAction("/api/autopilot/kill", "POST", {}, "Autopilot preview stopped."));
        const quotaPct = Math.max(0, Math.min(100, (state.daily_used / Math.max(1, state.daily_limit)) * 100));
        const pillText = state.enabled ? "PREVIEW ON" : state.tripped_at ? "TRIPPED" : "OFF";
        children.push(
          el("div", { class: "autopilot-head" },
            el("div", { class: "autopilot-toggle-wrap" },
              el("label", { class: "switch", for: toggleId }, input, el("span", { class: "track", "aria-hidden": "true" })),
              el("div", {},
                el("div", { class: "autopilot-title-row" },
                  el("label", { class: "autopilot-title", for: toggleId, text: "Autopilot preview (never sends)" }),
                  el("span", { class: `autopilot-pill${state.enabled ? " is-on" : ""}`, text: pillText }),
                  killBtn),
                el("p", {
                  class: "hint",
                  text: "Shadow-only preview for chargeback P2 disputes under $15 USD with no linked disputes and no buyer links or documents. Builds a dry run after 30 s and never sends to PayPal.",
                }))),
            el("div", { class: "autopilot-quota" },
              el("span", { class: "cap", text: "Preview cap" }),
              el("span", { class: "ap-quota-num num", text: `${state.daily_used} / ${state.daily_limit} today` }),
              el("div", { class: "ap-quota-bar", "aria-hidden": "true" },
                el("span", { class: "ap-quota-fill", style: `width:${quotaPct.toFixed(0)}%` })))));
        if (state.tripped_at && !state.enabled) {
          children.push(el("p", {
            class: "notice",
            text: `Breaker tripped (${state.trip_reason || "stopped"}). Switch Autopilot preview back on when ready.`,
          }));
        }
      }

      if (pending.length) {
        const cards = pending.map((job) => {
          const rem = remainingFor(job);
          const total = Math.max(1, Number(job.window_seconds) || 30);
          const pct = Math.max(0, Math.min(100, (rem / total) * 100));
          const cancelBtn = el("button", { class: "secondary", type: "button", disabled: busy, text: "Cancel" });
          cancelBtn.addEventListener("click", () =>
            postAction(`/api/autopilot/${encodeURIComponent(job.dispute_id)}/cancel`, "POST", {}, `Cancelled preview for ${job.dispute_id}.`));
          const amt = job.amount_value !== null && job.amount_value !== undefined
            ? `$${Number(job.amount_value).toFixed(2)} ${job.currency || "USD"}`
            : "P2 claim";
          return el("div", { class: "autopilot-countdown", "data-autopilot-id": job.dispute_id, role: "status", "aria-live": "polite" },
            el("div", { class: "ap-cd-main" },
              el("div", { class: "ap-cd-meta" },
                el("span", { class: "autopilot-pill is-on", text: "Preview window" }),
                el("a", { class: "num ap-case-link", href: `/case/${encodeURIComponent(job.dispute_id)}`, text: job.dispute_id }),
                el("span", { class: "num ap-amt", text: amt }),
                el("span", { class: "ap-timer num", text: rem > 0 ? `Would accept in ${Math.ceil(rem)} s (preview only)` : "Building preview dry run…" })),
              cancelBtn),
            el("div", { class: "ap-bar", "aria-hidden": "true" },
              el("span", { class: "ap-bar-fill", style: `width:${pct.toFixed(1)}%` })));
        });
        children.push(el("div", { class: "autopilot-list" }, ...cards));
      }

      if (statusMsg) {
        children.push(el("p", { class: "hint ap-status", role: "status", text: statusMsg }));
      }
      container.replaceChildren(...children);
    }

    const onChanged = () => fetchState();
    window.addEventListener("dd:autopilot-changed", onChanged);
    modeReady.then((m) => {
      if (disposed || m.demo || m.scratch) {
        container.hidden = true;
        container.replaceChildren();
        return;
      }
      fetchState();
      pollTimer = setInterval(() => {
        if (!document.hidden) fetchState();
      }, 2000);
      tickTimer = setInterval(updateTicks, 250);
    });

    return {
      refresh: fetchState,
      destroy: () => {
        disposed = true;
        window.removeEventListener("dd:autopilot-changed", onChanged);
        clearInterval(pollTimer);
        clearInterval(tickTimer);
      },
    };
  }

  return { el, color, rgba, colorNum, dueSoonChip, isOverdue, revealStaggered, eyebrow, eyebrowWith, statChip, sumAmounts, renderNav, live, mode, modeReady, DEMO_LABEL, clock, recordedLabel, ageText, reducedMotion, reveal, topReason, caseCaption, groupDisputes, deadlineNode, statusBadge, STATUS_HELP, reasonLabel, statusLabel, statusClass, stageLabel, money, countdown, countdownNode,
           dueDate, sourceTag, decisionChip, riskNode, tick, pollChipText, detectionText, mountAutopilot };
})();
