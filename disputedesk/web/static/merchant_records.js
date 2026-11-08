// Merchant records: shipments and checkout devices per disputed transaction, and earlier disputes.
// Reads GET /api/merchant-records only. Every record shows where it came from.
"use strict";

(() => {
  const { el } = DD;
  DD.renderNav("records");

  const root = document.getElementById("records");
  const banner = document.getElementById("error");

  const field = (label, value) => (value === undefined || value === null || value === "" ? null
    : el("div", { class: "rec-field" }, el("dt", { text: label }), el("dd", { text: String(value) })));

  function block(title, record, fields, missing) {
    return el("section", { class: "rec-block" },
      el("div", { class: "rec-head" }, el("h3", { text: title }), record ? DD.sourceTag(record.source) : null),
      record ? el("dl", { class: "rec-fields" }, ...fields(record)) : el("p", { class: "hint", text: missing }));
  }

  const shipmentFields = (s) => [
    field("Carrier", s.carrier), field("Tracking", s.tracking_id),
    field("Status", s.status.replaceAll("_", " ").toLowerCase()), field("Ship to", s.destination),
    field("Shipped", s.shipped_at), field("Delivered", s.delivered_at), field("Last scan", s.last_scan),
    field("Signed by buyer", s.signed_by_buyer === undefined ? null : s.signed_by_buyer ? "Yes" : "No"),
  ];
  const deviceFields = (t) => [field("Device hash", t.device_hash), field("IP address", t.ip), field("IP location", t.geo)];

  function render(body) {
    const txns = body.transactions || [], past = body.past_disputes || [];
    if (!txns.length && !past.length) {
      root.replaceChildren(el("div", { class: "state" },
        el("h2", { text: "No merchant records imported yet" }),
        el("p", { text: "Import them from a JSON file (the sandbox example ships with the repository):" }),
        el("p", {}, el("code", { text: "python scripts/import_merchant_records.py --file seed/merchant_data.json" }))));
      return;
    }
    const meta = body.imported_at
      ? el("p", { class: "hint", text: `Imported from ${body.input_file} on ${new Date(body.imported_at).toLocaleString()}.` }) : null;
    const folders = txns.map((t) => el("article", { class: "rec-folder" },
      el("header", { class: "rec-title" },
        el("a", { class: "num", href: `/case/${encodeURIComponent(t.dispute_id)}`, text: t.dispute_id }),
        t.dispute ? el("span", { class: "hint", text: `${DD.reasonLabel(t.dispute.reason)}, ${DD.money(t.dispute.amount)}` }) : null,
        el("span", { class: "rec-txn" }, el("span", { class: "hint", text: "Transaction" }),
          el("span", { class: "num", text: t.transaction_id }), DD.sourceTag(t.transaction_source))),
      el("div", { class: "rec-grid" },
        block("Shipment", t.shipment, shipmentFields, "No shipment on file for this transaction."),
        block("Checkout device", t.telemetry, deviceFields, "No device record on file for this transaction."))));
    const earlier = past.length ? el("section", { class: "rec-past" },
      el("h2", { text: "Earlier disputes on file" }),
      el("p", { class: "hint", text: "Past disputes from the store's history. A checkout device or IP shared with these shows up as a linked dispute on the case page." }),
      el("ul", { class: "rec-past-list" }, ...past.map((p) => el("li", {},
        el("span", { class: "num rec-past-id", text: p.dispute_id }),
        el("span", { text: `${DD.reasonLabel(p.reason)}, ${p.status.toLowerCase()}` }),
        p.amount !== undefined ? el("span", { class: "num", text: DD.money({ value: p.amount.toFixed(2), currency_code: p.currency || "USD" }) }) : null,
        p.telemetry ? el("span", { class: "hint", text: `device ${p.telemetry.device_hash}, IP ${p.telemetry.ip}` }) : null,
        DD.sourceTag(p.source))))) : null;
    root.replaceChildren(...[meta, ...folders, earlier].filter(Boolean));
  }

  (async () => {
    try {
      const resp = await fetch("/api/merchant-records", { cache: "no-store" });
      if (resp.status === 403) throw new Error("This page only works on the machine running DisputeDesk (open it at http://127.0.0.1:8000).");
      if (!resp.ok) throw new Error(`The server answered HTTP ${resp.status}. Reload to try again.`);
      const body = await resp.json();
      DD.live(true);
      if (body.note) document.getElementById("note").textContent = body.note;
      render(body);
    } catch (err) {
      DD.live(false);
      banner.textContent = err instanceof TypeError
        ? "Can't reach the DisputeDesk server. Is uvicorn still running? Reload to try again." : err.message;
      banner.hidden = false;
      root.replaceChildren(el("div", { class: "state" }, el("p", { text: "No records loaded." })));
    }
  })();
})();
