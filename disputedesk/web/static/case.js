// Case page: one dispute, its recommendation and the graph path behind it. GET /api/decisions/{id}.
"use strict";

(() => {
  const { el } = DD;
  DD.renderNav("queue", "Case");

  const POLL_MS = 5000;
  const SVG = "http://www.w3.org/2000/svg";
  const root = document.getElementById("case");
  const disputeId = decodeURIComponent(location.pathname.replace(/^\/case\//, "").replace(/\/$/, ""));
  let lastKey = "";
  let editorDirty = false;   // unsaved draft edits: the 5-second refresh must not replace them
  let dialogOpen = false;    // the confirm dialog is open: don't re-render underneath it
  let lastResult = null;     // outcome of the last approval on this page, shown after re-render
  let graph3d = null;        // live three.js view; disposed before every re-render and on leaving
  let pendingGraph = null;   // a 3D view to mount once its container is in the page
  const MAX_DRAFT = 2000;    // matches the server's cap (disputedesk/drafts.py)

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
        DD.caseCaption(d),
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

  function decisionPanels(d, rec, draft, submit) {
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
    const citedRules = [...new Set(rec.reasoning.flatMap((p) => p.cites).filter((c) => c.startsWith("P")))];
    const ruleText = Object.fromEntries((rec.rules || []).map((r) => [r.id, r.text]));
    const rulesBlock = citedRules.length ? el("div", { class: "rules-used" },
      el("h3", { text: "Policy rules it relied on" }),
      el("ul", {}, ...citedRules.map((id) => el("li", { id: `rule-${id}` },
        el("strong", { text: `${id} ` }),
        ruleText[id] || "Rule text wasn't recorded for this older recommendation. Run it again to record the rules it uses.")))) : null;
    return [
      // Tier 1, editorial: the verdict as a huge word, the risk score as a large number, the main reason.
      el("section", { class: "panel verdict-panel", "aria-label": "Recommendation" },
        el("div", { class: "verdict-hero" },
          el("span", { class: "verdict-word", text: rec.decision }),
          el("span", { class: "verdict-risk", title: "Likelihood, 0 to 100, that the buyer's claim is valid (you would lose if you defended), based only on the evidence on file" },
            el("span", { class: "risk-number num", text: String(rec.risk_score) }),
            el("span", { class: "cap", text: "risk score, 0 to 100" }))),
        rec.reasoning && rec.reasoning[0] ? el("p", { class: "main-reason" },
          el("span", { class: "main-reason-label", text: "Main reason" }), rec.reasoning[0].point) : null,
        el("span", { class: "cap verdict-meta", text: `analyzed ${new Date(rec.created_at).toLocaleString()} by ${rec.model}` })),
      el("section", { class: "panel" }, el("h2", { text: "Why" }), reasons, rulesBlock),
      // An accept sends the fixed note shown in the approval panel, so the evidence draft only
      // matters for a defence.
      rec.decision === "DEFEND" ? draftPanel(d, rec, draft, submit) : null,
      approvePanel(d, submit),
    ];
  }

  // ---- draft editor ----------------------------------------------------------------------

  function draftPanel(d, rec, draft, submit) {
    const original = draft ? draft.text : rec.evidence_summary;
    const locked = submit && submit.submission && ["sending", "succeeded", "unknown"].includes(submit.submission.state);
    if (locked) {
      return el("section", { class: "panel" },
        el("h2", { text: "Response sent to PayPal" }),
        el("p", { class: "hint", text: submit.submission.state === "succeeded"
          ? "This is the text PayPal received. It can't be changed now."
          : "This text was being sent when the outcome became unknown. It can't be changed now." }),
        el("p", { class: "draft", text: original }));
    }
    const text = el("textarea", { id: "draft-text", class: "draft-editor", rows: "12", maxlength: String(MAX_DRAFT),
                                  "aria-describedby": "draft-origin draft-count", spellcheck: "true" });
    text.value = original;
    const count = el("span", { class: "hint num", id: "draft-count" });
    const status = el("p", { class: "hint", id: "draft-status", "aria-live": "polite" });
    const save = el("button", { class: "primary", type: "button", disabled: DD.mode.demo,
                                text: DD.mode.demo ? DD.DEMO_LABEL : "Save draft" });
    const restore = el("button", { class: "secondary", type: "button", text: "Use the agent's draft" });
    const fix = el("button", { class: "secondary", type: "button", hidden: true, text: "Add the note back" });
    let sandboxNote = "";

    const origin = draft && draft.edited
      ? `Your edited version, saved ${new Date(draft.updated_at).toLocaleString()}. This is the text that will be sent to PayPal when you approve.`
      : "The agent's draft. Edit it and save; the saved text is what will be sent to PayPal when you approve.";
    const newer = draft && draft.edited && Date.parse(rec.created_at) > Date.parse(draft.updated_at)
      ? el("p", { class: "notice", text: `The agent wrote a newer draft on ${new Date(rec.created_at).toLocaleString()}. Your saved version is unchanged; use the button below to switch to the agent's.` })
      : null;

    const updateCount = () => {
      count.textContent = `${text.value.length} / ${MAX_DRAFT} characters`;
      count.classList.toggle("error-text", text.value.length > MAX_DRAFT);
    };
    const markDirty = () => {
      editorDirty = text.value !== original;
      status.textContent = editorDirty ? "Unsaved changes." : "";
      status.classList.remove("error-text");
      fix.hidden = true;
      updateCount();
    };
    text.addEventListener("input", markDirty);
    restore.hidden = !(draft && draft.edited);
    restore.addEventListener("click", () => { text.value = rec.evidence_summary; markDirty(); text.focus(); });
    fix.addEventListener("click", () => { text.value = `${sandboxNote} ${text.value.replace(sandboxNote, "").trim()}`; markDirty(); text.focus(); });

    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        const resp = await fetch(`/api/drafts/${encodeURIComponent(d.dispute_id)}`, {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: text.value }) });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          status.textContent = `Not saved. ${body.message || body.error || `The server answered HTTP ${resp.status}.`}`;
          status.classList.add("error-text");
          if (body.error === "missing_sandbox_note" && body.note) { sandboxNote = body.note; fix.hidden = false; }
          return;
        }
        editorDirty = false;
        lastKey = "";  // re-render from the server so the page shows the saved version
        await load();
        const after = document.getElementById("draft-status");
        if (after) after.textContent = `Saved ${new Date(body.draft.updated_at).toLocaleTimeString()}.`;
      } catch {
        status.textContent = "Not saved. Can't reach the DisputeDesk server; your text is still here.";
        status.classList.add("error-text");
      } finally {
        save.disabled = false;
      }
    });
    updateCount();

    return el("section", { class: "panel" },
      el("h2", {}, el("label", { for: "draft-text", text: "Draft response to PayPal" })),
      el("p", { class: "hint", id: "draft-origin", text: origin }),
      newer,
      text,
      el("div", { class: "editor-row" }, count),
      el("div", { class: "approve-row" }, save, restore, fix, status));
  }

  // ---- approval (the only path that writes to PayPal) ------------------------------------

  function verificationNote(submit) {
    if (submit.unverified) {
      return el("p", { class: "notice unverified" },
        el("strong", { text: "UNVERIFIED. " }),
        "PayPal's docs don't show an evidence submission with notes and no file. This app hasn't had one accepted yet, so the first approval will show whether PayPal takes it.");
    }
    if (submit.verified) {
      return el("p", { class: "hint", text: `Notes-only evidence was accepted by PayPal on ${new Date(submit.verified.verified_at).toLocaleString()} (dispute ${submit.verified.dispute_id}).` });
    }
    return null;
  }

  function submissionStatus(submit) {
    const sub = submit.submission;
    const lines = [];
    if (lastResult && lastResult.ok) {
      lines.push(el("p", { class: "result ok", role: "status", text: lastResult.message }));
    } else if (lastResult) {
      lines.push(errorBox(lastResult.message, lastResult.paypal));
    }
    if (sub && sub.state === "succeeded" && !(lastResult && lastResult.ok)) {
      lines.push(el("p", { class: "result ok", text: `Sent to PayPal on ${new Date(sub.finished_at).toLocaleString()} (HTTP ${sub.http_status}).` }));
    }
    if (sub && sub.state === "rejected" && !lastResult) {
      lines.push(errorBox(`PayPal refused the last attempt (HTTP ${sub.http_status}). Nothing changed on PayPal; you can fix the problem and try again.`, sub.paypal_error));
    }
    return lines;
  }

  function errorBox(message, paypal) {
    const details = paypal ? [paypal.name, paypal.message, ...(paypal.issues || []), paypal.debug_id ? `debug_id ${paypal.debug_id}` : null].filter(Boolean) : [];
    return el("div", { class: "result error", role: "alert" },
      el("p", { text: message }),
      details.length ? el("p", { class: "hint", text: `PayPal said: ${details.join(". ")}` }) : null);
  }

  function approvePanel(d, submit) {
    if (!submit || !submit.action) return null;
    const demo = DD.mode.demo;
    const reasons = demo ? ["This is recorded sandbox data, so nothing can be sent to PayPal from here. The dry run still shows the exact request an approval would send."] : (submit.reasons || []);
    const approve = el("button", { class: "primary", type: "button", disabled: demo || !submit.can_submit,
                                   "aria-describedby": "approve-reasons", text: demo ? DD.DEMO_LABEL : submit.label });
    if (!demo) approve.addEventListener("click", () => openConfirm(d, submit));
    const dryButton = el("button", { class: "secondary", type: "button", text: "Show the exact request (dry run)" });
    const dryOut = el("div", { class: "dry-run", hidden: true, "aria-live": "polite" });
    dryButton.addEventListener("click", async () => {
      dryButton.disabled = true;
      try {
        const resp = await fetch(`/api/submissions/${encodeURIComponent(d.dispute_id)}/dry-run`, { cache: "no-store" });
        const body = await resp.json();
        if (!resp.ok) throw new Error(body.message || body.error || `HTTP ${resp.status}`);
        const r = body.request;
        dryOut.replaceChildren(
          el("p", { class: "hint", text: "Built from the last stored state of this dispute. Nothing was sent; approving re-checks with PayPal first. The token is shown redacted." }),
          r ? el("pre", { class: "request", text: [`${r.method} ${r.url}`, ...Object.entries(r.headers).map(([k, v]) => `${k}: ${v}`), "", r.body].join("\n") })
            : el("p", { class: "hint", text: "There is no request to build until the agent has a recommendation." }));
      } catch (err) {
        dryOut.replaceChildren(el("p", { class: "error-text", text: `Couldn't build the dry run: ${err.message}` }));
      } finally {
        dryOut.hidden = false;
        dryButton.disabled = false;
      }
    });
    return el("section", { class: "panel" },
      el("h2", { text: "Approve and send" }),
      el("p", { class: "hint", text: submit.action === "provide_evidence"
        ? "Sends your saved draft to PayPal as evidence (type OTHER, notes only). PayPal then reviews the case; you can't change the evidence afterwards."
        : "Accepts the claim with the note below. The dispute closes in the buyer's favour and PayPal refunds the buyer from your sandbox balance." }),
      submit.action === "accept_claim" ? el("p", { class: "draft", text: submit.text }) : null,
      submit.action === "provide_evidence" ? verificationNote(submit) : null,
      ...submissionStatus(submit),
      el("div", { class: "approve-row" }, approve, dryButton),
      reasons.length
        ? el("ul", { class: "reasons-off", id: "approve-reasons" }, ...reasons.map((r) => el("li", { text: r })))
        : el("p", { class: "hint", id: "approve-reasons", text: "Nothing is sent until you confirm in the next step." }),
      dryOut);
  }

  function openConfirm(d, submit) {
    const amount = DD.money(d.amount);
    const cancel = el("button", { class: "secondary", type: "button", text: "Cancel", autofocus: true });
    const confirm = el("button", { class: "primary", type: "button", text: submit.label });
    const status = el("p", { class: "hint", "aria-live": "polite" });
    const dialog = el("dialog", { class: "confirm", "aria-labelledby": "confirm-title" },
      el("h2", { id: "confirm-title", text: `${submit.label}?` }),
      el("dl", { class: "confirm-facts" },
        el("dt", { text: "Action" }), el("dd", { text: submit.action === "provide_evidence" ? "Provide evidence (type OTHER, notes only)" : "Accept claim" }),
        el("dt", { text: "Dispute" }), el("dd", { class: "num", text: d.dispute_id }),
        el("dt", { text: "Amount" }), el("dd", { class: "num", text: amount })),
      el("p", { class: "label", text: submit.action === "provide_evidence" ? "Notes PayPal will receive, exactly:" : "Note PayPal will receive, exactly:" }),
      el("pre", { class: "confirm-text", text: submit.text }),
      submit.action === "provide_evidence" ? verificationNote(submit) : null,
      el("p", { class: "hint", text: submit.action === "provide_evidence"
        ? "This sends the evidence to the PayPal sandbox now. It can't be withdrawn."
        : `This accepts the claim in the PayPal sandbox now and refunds ${amount} to the buyer. It can't be undone.` }),
      el("div", { class: "approve-row" }, confirm, cancel, status));
    const close = () => { dialog.close(); };
    dialog.addEventListener("close", () => { dialogOpen = false; dialog.remove(); });
    cancel.addEventListener("click", close);
    confirm.addEventListener("click", async () => {
      confirm.disabled = true;
      cancel.disabled = true;
      status.textContent = "Sending to PayPal…";
      let result;
      try {
        const resp = await fetch(`/api/submissions/${encodeURIComponent(d.dispute_id)}`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm: true, action: submit.action, text: submit.text }) });
        const body = await resp.json().catch(() => ({}));
        result = resp.ok
          ? { ok: true, message: `Sent at ${new Date().toLocaleTimeString()}. PayPal now shows: ${DD.statusLabel(body.dispute && body.dispute.status)}.` }
          : { ok: false, message: body.message || body.error || `The server answered HTTP ${resp.status}.`, paypal: body.paypal };
      } catch {
        result = { ok: false, message: "Can't reach the DisputeDesk server, so the outcome isn't known here. Reload the page to see the dispute's current state." };
      }
      lastResult = result;
      close();
      lastKey = "";
      await load();
    });
    document.body.append(dialog);
    dialogOpen = true;
    dialog.showModal();
  }

  function citeTitle(rec, id) {
    const f = (rec.facts || []).find((x) => x.id === id);
    if (f) return f.text;
    const r = (rec.rules || []).find((x) => x.id === id);
    if (r) return r.text;
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
    const flat = el("div", { class: "graph-wrap" }, chart);

    // 3D view of the same nodes (graph3d.js). The flat SVG stays one click away, and is the only
    // view when WebGL is missing, the library didn't load, or reduced motion is requested.
    const why3dOff = typeof DD3D === "undefined" ? "Showing the flat diagram because the 3D view didn't load." : DD3D.unavailableReason();
    let view = flat, legend3d = null, toggle = null, note = null;
    if (why3dOff) {
      note = el("p", { class: "hint", text: why3dOff });
    } else {
      const labels = {
        customer: { title: "Buyer", sub: cust ? cust.payer_id : "Not on file" },
        transaction: { title: "Transaction", sub: tx ? tx.id : "Not on file" },
        dispute: { title: "Dispute", sub: DD.reasonLabel(d.reason) },
        telemetry: { title: "Checkout device", sub: tel ? tel.device_hash : "No record on file" },
        shipment: { title: "Shipment", sub: ship ? `${ship.carrier} ${ship.status.replaceAll("_", " ").toLowerCase()}` : "No record on file" },
      };
      linked.forEach((o) => { labels[`linked:${o.id}`] = { title: "Linked dispute", sub: o.id }; });
      const stage = el("div", { class: "graph3d", "aria-label": label });
      flat.hidden = true;
      view = el("div", {}, stage, flat);
      pendingGraph = { stage, ctx, used, labels, label };
      toggle = el("button", { class: "secondary small", type: "button", "aria-pressed": "false", text: "Show flat diagram" });
      toggle.addEventListener("click", () => {
        const showFlat = flat.hidden;
        flat.hidden = !showFlat;
        stage.hidden = showFlat;
        toggle.setAttribute("aria-pressed", String(showFlat));
        toggle.textContent = showFlat ? "Show 3D view" : "Show flat diagram";
      });
      legend3d = el("div", { class: "graph-legend" },
        el("span", {}, el("span", { class: "shape sphere", "aria-hidden": "true" }), "PayPal record"),
        el("span", {}, el("span", { class: "shape crystal", "aria-hidden": "true" }), "Seeded test data"),
        el("span", {}, el("span", { class: "shape glow", "aria-hidden": "true" }), "Cited in the recommendation"),
        el("span", { text: "Drag, or focus it and use the arrow keys, to rotate." }),
        linked.length > 6 ? el("span", { text: `${linked.length - 6} more linked disputes are in the flat diagram and the fact list.` }) : null);
    }

    const facts = el("ul", { class: "fact-list" }, ...(rec.facts || []).map((f) => {
      const cited = rec.reasoning.some((p) => p.cites.includes(f.id));
      return el("li", { id: `fact-${f.id}`, class: cited ? "cited" : null },
        el("span", { class: "fid", text: f.id }), el("span", { text: f.text }), DD.sourceTag(f.source));
    }));

    return [
      el("section", { class: "panel" },
        el("div", { class: "panel-head" }, el("h2", { text: "Evidence path" }), toggle),
        note,
        view,
        legend3d,
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

  // ---- per-case timeline (stored timestamps only) ------------------------------------------

  function caseTimeline(d, rec, draft, submit) {
    const sub = submit && submit.submission;
    const steps = [
      [d.detected_at, `Detected through the ${d.detected_via === "webhook" ? "PayPal webhook" : "dispute poller"}`, ""],
      [rec && rec.created_at, rec ? (rec.status === "ok" ? "Analyzed by the agent" : "Analysis failed") : "", rec && rec.status !== "ok" ? "failed" : ""],
      [draft && draft.edited ? draft.updated_at : null, "Draft edited and saved", ""],
      [sub && sub.started_at, "Approved and sent", ""],
      [sub && sub.finished_at, sub ? { succeeded: `PayPal accepted it (HTTP ${sub.http_status})`, rejected: `PayPal refused it (HTTP ${sub.http_status})`, unknown: "Outcome unknown" }[sub.state] || "" : "",
       sub ? (sub.state === "succeeded" ? "done" : sub.state === "sending" ? "" : "failed") : ""],
    ].filter(([at, label]) => at && label).sort((a, b) => Date.parse(a[0]) - Date.parse(b[0]));
    return el("section", { class: "panel case-timeline", "aria-labelledby": "timeline-title" },
      el("h2", { id: "timeline-title", text: "Timeline" }),
      el("ol", {}, ...steps.map(([at, label, kind]) => el("li", { class: kind || null },
        el("span", { class: "tl-dot", "aria-hidden": "true" }),
        el("span", { class: "tl-label", text: label }),
        el("time", { class: "tl-time num", datetime: at, text: new Date(at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) })))));
  }

  function disposeGraph() {
    if (graph3d) { graph3d.dispose(); graph3d = null; }
  }

  function mountPendingGraph() {
    const p = pendingGraph;
    pendingGraph = null;
    if (!p || !p.stage.isConnected) return;
    try {
      graph3d = DD3D.mount(p.stage, p.ctx, p.used, p.labels);
      const canvas = p.stage.querySelector("canvas");
      if (canvas) canvas.setAttribute("aria-label", `${p.label} Cited nodes glow. Drag or use the arrow keys to rotate.`);
      window.__ddGraph3d = graph3d;  // lets a developer read frame stats from the console
    } catch (err) {
      // Any 3D failure falls back to the flat diagram, never a broken panel.
      p.stage.replaceChildren(el("p", { class: "hint", text: "The 3D view couldn't start here, so the flat diagram is shown below." }));
      const flat = p.stage.nextElementSibling;
      if (flat) flat.hidden = false;
    }
  }

  async function load() {
    let body;
    try {
      const resp = await fetch(`/api/decisions/${encodeURIComponent(disputeId)}`, { cache: "no-store" });
      if (resp.status === 404) {
        DD.live(true);  // the server answered; the dispute just is not there
        disposeGraph();
        root.replaceChildren(stateView("Dispute not found",
          `There is no dispute ${disputeId} in the local store. Disputes appear here after the poller first sees them in the PayPal sandbox.`));
        return;
      }
      if (resp.status === 403) throw new Error("This page only works on the machine running DisputeDesk (open it at http://127.0.0.1:8000).");
      if (!resp.ok) throw new Error(`The server answered HTTP ${resp.status}. Retrying every 5 seconds.`);
      body = await resp.json();
      DD.live(true);
    } catch (err) {
      DD.live(false);
      disposeGraph();
      root.replaceChildren(stateView("Can't load this case", err instanceof TypeError
        ? "Can't reach the DisputeDesk server. Is uvicorn still running? Retrying every 5 seconds." : err.message));
      return;
    }
    await DD.modeReady;
    const key = JSON.stringify(body);
    if (key === lastKey) return; // nothing changed; keep scroll position and any open anchor
    if (editorDirty || dialogOpen) return;  // never replace unsaved edits or an open confirm dialog
    lastKey = key;
    const { dispute, decision, draft, submit } = body;
    // Hierarchy: 1) decision and risk (full width, with the timeline), 2) reasons beside the evidence
    // path, 3) draft and approval. Same panels as before; only their placement changes.
    const panels = decisionPanels(dispute, decision, draft, submit).filter(Boolean);
    const band = el("div", { class: "verdict-band" }, panels[0], caseTimeline(dispute, decision, draft, submit));
    const left = el("div", { class: "case-col" }, ...panels.slice(1));
    const right = el("div", { class: "case-col" }, ...((decision && decision.status === "ok" && graphPanel(decision)) || [
      el("section", { class: "panel" }, el("h2", { text: "Evidence path" }),
        el("p", { class: "hint", text: "The graph path appears once the agent has a recommendation for this dispute." }))]));
    disposeGraph();
    const callHead = el("div", { class: "stack-head case-call-head" },
      el("h2", {}, el("span", { text: "The agent's" }), el("span", { text: "call" })),
      el("span", { class: "cap", text: decision && decision.status === "ok"
        ? "What the agent recommends, how strong the buyer's claim looks, and why."
        : "The agent's recommendation appears here once the analysis has run." }));
    root.replaceChildren(header(dispute), callHead, band, el("div", { class: "case-grid" }, left, right));
    mountPendingGraph();
  }

  window.addEventListener("beforeunload", (e) => { if (editorDirty) e.preventDefault(); });
  window.addEventListener("pagehide", disposeGraph);  // free the GPU context when leaving the page

  if (!disputeId) {
    root.replaceChildren(stateView("No dispute selected", "Open a dispute from the queue to see its case."));
  } else {
    load();
    setInterval(load, POLL_MS);
  }
})();
