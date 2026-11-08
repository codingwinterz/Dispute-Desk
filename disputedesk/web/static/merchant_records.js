// Merchant records: shipments and checkout devices per disputed transaction, and earlier disputes.
// Reads GET /api/merchant-records only. Every record shows where it came from (PayPal or seeded).
// - Shipments: a tracking stepper built only from the stored status and dates.
// - Devices: chips for the device hash, IP and location.
// - Linked disputes: an earlier dispute that shares this transaction's device hash or IP (the same
//   rule the graph uses) is joined to it by a drawn connector, and tagged in the Earlier list.
"use strict";

(() => {
  const { el } = DD;
  DD.renderNav("records");

  const root = document.getElementById("records");
  const banner = document.getElementById("error");

  const field = (label, value) => (value === undefined || value === null || value === "" ? null
    : el("div", { class: "rec-field" }, el("dt", { text: label }), el("dd", { text: String(value) })));
  const pastAnchor = (id) => `past-${String(id).replace(/[^A-Za-z0-9_-]/g, "")}`;

  // Which earlier disputes share a device hash or IP with this transaction's checkout device.
  function linkedPast(telemetry, past) {
    if (!telemetry) return [];
    return past.filter((p) => p.telemetry && (
      (telemetry.device_hash && p.telemetry.device_hash === telemetry.device_hash) || (telemetry.ip && p.telemetry.ip === telemetry.ip)))
      .map((p) => ({ p, via: telemetry.device_hash && p.telemetry.device_hash === telemetry.device_hash ? "device" : "IP" }));
  }

  // ---- shipment: tracking stepper from what is stored ---------------------------------------------
  function stepper(s) {
    const status = String(s.status || "").toUpperCase();
    const delivered = status === "DELIVERED" || Boolean(s.delivered_at);
    const shipped = delivered || Boolean(s.shipped_at) || ["IN_TRANSIT", "SHIPPED", "OUT_FOR_DELIVERY"].includes(status);
    const steps = [
      { label: "Label created", done: Boolean(status) || shipped, when: null },  // a stored status means a label exists
      { label: "Shipped", done: shipped, when: s.shipped_at },
      { label: "Delivered", done: delivered, when: s.delivered_at },
    ];
    if (s.signed_by_buyer !== undefined) {
      steps.push({ label: s.signed_by_buyer ? "Signed by buyer" : "No signature", done: Boolean(s.signed_by_buyer), when: null, missing: !s.signed_by_buyer && delivered });
    }
    const reached = steps.filter((x) => x.done).length;
    return el("ol", { class: "stepper", style: `--steps:${steps.length};--reached:${reached}`, "aria-label": `Tracking: ${steps.filter((x) => x.done).map((x) => x.label).join(", ") || "no scans"}` },
      ...steps.map((x) => el("li", { class: `step${x.done ? " done" : ""}${x.missing ? " missing" : ""}` },
        el("span", { class: "step-dot", "aria-hidden": "true" }),
        el("span", { class: "step-label", text: x.label }),
        x.when ? el("span", { class: "step-when num", text: x.when }) : null)));
  }

  function shipmentCard(s) {
    const head = el("div", { class: "rec-head" }, el("h3", { text: "Shipment" }), s ? DD.sourceTag(s.source) : null);
    if (!s) return el("section", { class: "rec-block" }, head, el("p", { class: "hint", text: "No shipment on file for this transaction." }));
    return el("section", { class: "rec-block" }, head,
      el("p", { class: "ship-line" }, el("span", { class: "ship-carrier", text: s.carrier || "Carrier not stored" }),
        s.tracking_id ? el("span", { class: "num ship-tracking", text: s.tracking_id }) : null),
      stepper(s),
      el("dl", { class: "rec-fields" }, field("Ship to", s.destination), field("Last scan", s.last_scan)));
  }

  // ---- checkout device: chips, and the connector to linked earlier disputes ---------------------------
  function deviceCard(t, links) {
    const head = el("div", { class: "rec-head" }, el("h3", { text: "Checkout device" }), t ? DD.sourceTag(t.source) : null);
    if (!t) return el("section", { class: "rec-block" }, head, el("p", { class: "hint", text: "No device record on file for this transaction." }));
    const chip = (label, value, linked) => (value ? el("span", { class: `dev-chip${linked ? " linked" : ""}` },
      el("span", { class: "dev-chip-label", text: label }), el("span", { class: "num", text: value })) : null);
    const viaDevice = links.some((l) => l.via === "device"), viaIp = links.some((l) => l.via === "IP");
    const block = el("section", { class: "rec-block" }, head,
      el("div", { class: "dev-chips" }, chip("Device", t.device_hash, viaDevice), chip("IP", t.ip, viaIp), chip("Location", t.geo, false)));
    if (links.length) {
      block.append(el("div", { class: "link-tree" },
        el("p", { class: "link-title", text: `Shared with ${links.length} earlier ${links.length === 1 ? "dispute" : "disputes"}` }),
        el("ul", {}, ...links.map(({ p, via }) => el("li", {},
          el("a", { class: "num", href: `#${pastAnchor(p.dispute_id)}`, text: p.dispute_id }),
          el("span", { class: "hint", text: `same ${via}, ${DD.reasonLabel(p.reason)}` }))))));
    } else {
      block.append(el("p", { class: "hint link-none", text: "Not shared with any earlier dispute on file." }));
    }
    return block;
  }

  function render(body) {
    const txns = body.transactions || [], past = body.past_disputes || [];
    document.getElementById("records-eyebrow").replaceChildren(...DD.eyebrow("Merchant records"));
    if (!txns.length && !past.length) {
      document.getElementById("records-stats").replaceChildren();
      root.replaceChildren(el("div", { class: "state" },
        el("h2", { text: "No merchant records imported yet" }),
        el("p", { text: "Import them from a JSON file (the sandbox example ships with the repository):" }),
        el("p", {}, el("code", { text: "python scripts/import_merchant_records.py --file seed/merchant_data.json" }))));
      return;
    }
    const linksByTxn = new Map(txns.map((t) => [t, linkedPast(t.telemetry, past)]));
    const linkedTo = new Map();  // earlier dispute -> transactions it shares a device or IP with
    for (const [t, links] of linksByTxn) for (const { p } of links) linkedTo.set(p, [...(linkedTo.get(p) || []), t.dispute_id]);
    const shipments = txns.filter((t) => t.shipment), delivered = shipments.filter((t) => String(t.shipment.status).toUpperCase() === "DELIVERED");
    document.getElementById("records-stats").replaceChildren(
      DD.statChip({ label: "disputed transactions", value: String(txns.length) }),
      DD.statChip({ label: "shipments delivered", value: `${delivered.length} of ${shipments.length}` }),
      DD.statChip({ label: "earlier disputes", value: String(past.length) }),
      DD.statChip({ label: "linked by device or IP", value: String([...linksByTxn.values()].filter((l) => l.length).length) }));

    const meta = body.imported_at
      ? el("p", { class: "hint", text: `Imported from ${body.input_file} on ${new Date(body.imported_at).toLocaleString()}.` }) : null;
    const folders = txns.map((t) => el("article", { class: `rec-folder${linksByTxn.get(t).length ? " has-links" : ""}` },
      el("header", { class: "rec-title" },
        el("a", { class: "num", href: `/case/${encodeURIComponent(t.dispute_id)}`, text: t.dispute_id }),
        t.dispute ? el("span", { class: "hint", text: `${DD.reasonLabel(t.dispute.reason)}, ${DD.money(t.dispute.amount)}` }) : null,
        el("span", { class: "rec-txn" }, el("span", { class: "hint", text: "Transaction" }),
          el("span", { class: "num", text: t.transaction_id }), DD.sourceTag(t.transaction_source))),
      el("div", { class: "rec-grid" }, shipmentCard(t.shipment), deviceCard(t.telemetry, linksByTxn.get(t)))));
    const earlier = past.length ? el("section", { class: "rec-past" },
      el("div", { class: "stack-head" }, el("h2", {}, el("span", { text: "Earlier" }), el("span", { text: "disputes" }))),
      el("p", { class: "hint", text: "Past disputes from the store's history. A checkout device or IP shared with these shows up as a linked dispute on the case page." }),
      el("ul", { class: "rec-past-list" }, ...past.map((p) => el("li", { id: pastAnchor(p.dispute_id), class: linkedTo.has(p) ? "is-linked" : null },
        el("span", { class: "num rec-past-id", text: p.dispute_id }),
        el("span", { text: `${DD.reasonLabel(p.reason)}, ${p.status.toLowerCase()}` }),
        p.amount !== undefined ? el("span", { class: "num", text: DD.money({ value: p.amount.toFixed(2), currency_code: p.currency || "USD" }) }) : null,
        p.telemetry ? el("span", { class: "hint", text: `device ${p.telemetry.device_hash}, IP ${p.telemetry.ip}` }) : null,
        linkedTo.has(p) ? el("span", { class: "link-tag", text: `linked to ${linkedTo.get(p).join(", ")}` }) : null,
        DD.sourceTag(p.source))))) : null;
    root.replaceChildren(...[meta, ...folders, earlier].filter(Boolean));
    DD.revealStaggered([...folders, earlier].filter(Boolean));
  }

  (async () => {
    try {
      const resp = await fetch("/api/merchant-records", { cache: "no-store" });
      if (resp.status === 403) throw new Error("This page only works on the machine running DisputeDesk (open it at http://127.0.0.1:8000).");
      if (!resp.ok) throw new Error(`The server answered HTTP ${resp.status}. Reload to try again.`);
      const body = await resp.json();
      DD.live(true);
      await DD.modeReady;
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
