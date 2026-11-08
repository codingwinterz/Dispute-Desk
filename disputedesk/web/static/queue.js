// Queue page: AG Grid Community over GET /api/disputes, refreshed every 5 seconds.
"use strict";

(() => {
  const { el } = DD;
  DD.renderNav("queue");

  const POLL_MS = 5000;
  const filters = { reason: "", status: "" };
  let rows = [];
  let loaded = false;

  const flat = (d) => ({
    ...d,
    reason_label: DD.reasonLabel(d.reason),
    status_label: DD.statusLabel(d.status),
    amount_value: d.amount ? Number(d.amount.value) : null,
    amount_text: DD.money(d.amount),
    recommended: !d.decision ? "pending" : d.decision.status !== "ok" ? "error" : d.decision.decision,
    risk: d.decision && d.decision.status === "ok" ? d.decision.risk_score : null,
  });

  const columnDefs = [
    {
      headerName: "Respond within", field: "seller_response_due_date", sort: "asc", minWidth: 170, flex: 1.1,
      // The value is the due date itself (it never changes), so the 1-second ticker in common.js
      // updates the text without making AG Grid flash the cell.
      cellRenderer: (p) => DD.countdownNode(p.value),
      comparator: (a, b) => (Date.parse(a) || Infinity) - (Date.parse(b) || Infinity),
      getQuickFilterText: () => "",
    },
    {
      headerName: "Dispute", field: "dispute_id", minWidth: 190, flex: 1.2,
      cellRenderer: (p) => el("a", { href: `/case/${encodeURIComponent(p.value)}`, text: p.value }),
    },
    { headerName: "Reason", field: "reason_label", minWidth: 150, flex: 1 },
    {
      headerName: "Amount", field: "amount_value", minWidth: 110, flex: 0.7, type: "rightAligned",
      valueFormatter: (p) => p.data.amount_text, getQuickFilterText: (p) => p.data.amount_text,
      cellClass: "num",
    },
    {
      headerName: "Status", field: "status", minWidth: 200, flex: 1.2, enableCellChangeFlash: true,
      cellRenderer: (p) => el("span", { class: `badge ${DD.statusClass(p.value)}`, text: DD.statusLabel(p.value) }),
      getQuickFilterText: (p) => p.data.status_label,
    },
    {
      headerName: "Recommended", field: "recommended", minWidth: 150, flex: 0.9, enableCellChangeFlash: true,
      cellRenderer: (p) => DD.decisionChip(p.data.decision),
    },
    {
      headerName: "Risk", field: "risk", minWidth: 120, flex: 0.7, enableCellChangeFlash: true,
      headerTooltip: "Likelihood, 0 to 100, that the buyer's claim is valid (you would lose if you defended), based only on the evidence on file",
      cellRenderer: (p) => DD.riskNode(p.value),
    },
    {
      headerName: "Source", field: "source", minWidth: 100, flex: 0.6, sortable: false,
      cellRenderer: (p) => DD.sourceTag(p.value),
    },
  ];

  const emptyCopy = "No disputes yet. When a buyer opens one in the PayPal sandbox, the poller picks it up within 10 seconds and it appears here.";

  // Main reason on hover/focus: an overlay outside AG Grid's rows (columns and rendering untouched).
  // Reads the existing GET /api/decisions/{id} once per dispute and decision; nothing is fetched for
  // rows without a recommendation, and nothing is shown when there is no stored reason.
  const gridEl = document.getElementById("grid");
  const stage = el("div", { class: "grid-stage" });
  gridEl.parentNode.insertBefore(stage, gridEl);
  const reasonLine = el("div", { class: "row-reason", role: "status", "aria-live": "polite", hidden: true });
  stage.append(gridEl, reasonLine);
  const reasons = new Map();  // dispute_id -> { key, text } ; key = decision created_at
  let reasonFor = null, hoverTimer = 0;

  async function topReason(row) {
    const d = row.decision;
    if (!d || d.status !== "ok") return "";
    const cached = reasons.get(row.dispute_id);
    if (cached && cached.key === d.created_at) return cached.text;
    try {
      const resp = await fetch(`/api/decisions/${encodeURIComponent(row.dispute_id)}`, { cache: "no-store" });
      const body = resp.ok ? await resp.json() : null;
      const first = body && body.decision && body.decision.reasoning && body.decision.reasoning[0];
      const text = first ? first.point : "";
      reasons.set(row.dispute_id, { key: d.created_at, text });
      return text;
    } catch {
      return "";
    }
  }

  function hideReason() { reasonFor = null; reasonLine.hidden = true; }

  async function showReason(row, rowEl) {
    if (!row || !rowEl) return hideReason();
    reasonFor = row.dispute_id;
    const text = await topReason(row);
    if (reasonFor !== row.dispute_id) return;   // the pointer moved on while fetching
    if (!text) return hideReason();
    const top = rowEl.getBoundingClientRect().bottom - stage.getBoundingClientRect().top;
    reasonLine.replaceChildren(el("span", { class: "row-reason-label", text: "Main reason" }), el("span", { class: "row-reason-text", text }));
    reasonLine.style.top = `${top}px`;
    reasonLine.hidden = false;
  }

  // The 5-second refresh re-renders rows: keep the line on the same dispute and move it with the row,
  // or hide it if that row is no longer shown (filtered out, or gone).
  function keepReason() {
    if (!reasonFor || reasonLine.hidden) return;
    const node = grid.getRowNode(reasonFor);
    const rowEl = node && node.displayed && node.rowIndex !== null ? rowElementFor(node.rowIndex) : null;
    if (!rowEl) return hideReason();
    reasonLine.style.top = `${rowEl.getBoundingClientRect().bottom - stage.getBoundingClientRect().top}px`;
  }

  const rowElementFor = (rowIndex) =>
    stage.querySelector(`.ag-center-cols-container .ag-row[row-index="${rowIndex}"]`);

  const grid = agGrid.createGrid(gridEl, {
    columnDefs,
    rowData: null, // shows the loading overlay until the first response
    getRowId: (p) => p.data.dispute_id,
    defaultColDef: { sortable: true, resizable: true, suppressMovable: true },
    rowHeight: 60,
    domLayout: "autoHeight", // grid grows with its rows instead of a fixed height
    animateRows: false,
    suppressCellFocus: false,
    overlayLoadingTemplate: '<span class="grid-empty">Loading disputes…</span>',
    overlayNoRowsTemplate: `<span class="grid-empty">${emptyCopy}</span>`,
    isExternalFilterPresent: () => Boolean(filters.reason || filters.status),
    doesExternalFilterPass: (node) =>
      (!filters.reason || node.data.reason === filters.reason) &&
      (!filters.status || node.data.status === filters.status),
    onModelUpdated: () => { updateCount(); keepReason(); },
    onCellMouseOver: (e) => {
      clearTimeout(hoverTimer);
      const rowEl = e.event && e.event.target && e.event.target.closest(".ag-row");
      hoverTimer = setTimeout(() => showReason(e.data, rowEl), 120);
    },
    onCellFocused: (e) => {
      if (e.rowIndex === null || e.rowIndex === undefined) return hideReason();
      const node = grid.getDisplayedRowAtIndex(e.rowIndex);
      showReason(node && node.data, rowElementFor(e.rowIndex));
    },
    onBodyScroll: () => hideReason(),
  });

  function updateCount() {
    if (!loaded) return;
    const shown = grid.getDisplayedRowCount();
    document.getElementById("count").textContent =
      shown === rows.length ? `${rows.length} ${rows.length === 1 ? "dispute" : "disputes"}` : `Showing ${shown} of ${rows.length}`;
    if (rows.length && !shown) grid.showNoRowsOverlay();
  }

  function fillSelect(id, values, label) {
    const select = document.getElementById(id);
    const known = new Set([...select.options].map((o) => o.value));
    for (const v of values) {
      if (!known.has(v)) select.append(el("option", { value: v, text: label(v) }));
    }
  }

  function updateSummary() {
    const open = rows.filter((r) => r.status !== "RESOLVED");
    const next = open.map((r) => r.seller_response_due_date).filter(Boolean).sort()[0];
    const summary = document.getElementById("summary");
    if (!rows.length) {
      summary.textContent = "Nothing to answer right now.";
      return;
    }
    const pending = rows.filter((r) => !r.decision).length;
    summary.replaceChildren(
      `${open.length} open ${open.length === 1 ? "dispute" : "disputes"}. `,
      next ? "Next response due in " : "",
      next ? el("span", { class: "num", "data-due": next }, el("span", { class: "cd-main", text: DD.countdown(next).text })) : "",
      next ? ". " : "",
      pending ? `${pending} still being analyzed.` : "Every dispute has a recommendation.",
    );
  }

  async function refresh() {
    const banner = document.getElementById("error");
    try {
      const resp = await fetch("/api/disputes", { cache: "no-store" });
      if (resp.status === 403) throw new Error("This page only works on the machine running DisputeDesk (open it at http://127.0.0.1:8000).");
      if (!resp.ok) throw new Error(`The server answered HTTP ${resp.status}. Retrying every 5 seconds.`);
      const body = await resp.json();
      rows = (body.disputes || []).map(flat);
      loaded = true;
      DD.live(true);
      banner.hidden = true;
      // With getRowId, AG Grid diffs the rows and flashes only the cells whose values changed.
      grid.setGridOption("rowData", rows);
      fillSelect("reason", [...new Set(rows.map((r) => r.reason).filter(Boolean))].sort(), DD.reasonLabel);
      fillSelect("status", [...new Set(rows.map((r) => r.status).filter(Boolean))].sort(), DD.statusLabel);
      updateSummary();
      updateCount();
    } catch (err) {
      DD.live(false);
      banner.textContent = err instanceof TypeError
        ? "Can't reach the DisputeDesk server. Is uvicorn still running? Retrying every 5 seconds."
        : err.message;
      banner.hidden = false;
      if (!loaded) grid.setGridOption("rowData", []);
    }
  }

  stage.addEventListener("mouseleave", () => { clearTimeout(hoverTimer); hideReason(); });

  document.getElementById("search").addEventListener("input", (e) => grid.setGridOption("quickFilterText", e.target.value));
  for (const key of ["reason", "status"]) {
    document.getElementById(key).addEventListener("change", (e) => {
      filters[key] = e.target.value;
      grid.onFilterChanged();
    });
  }

  refresh();
  setInterval(refresh, POLL_MS);
})();
