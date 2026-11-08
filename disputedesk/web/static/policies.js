// Policies page: edit merchant policy settings. GET/PUT /api/policies, live rule text from
// POST /api/policies/preview (the server builds the text, so the preview is exactly what the agent reads).
"use strict";

(() => {
  const { el } = DD;
  DD.renderNav("policies");

  const form = document.getElementById("policies");
  const banner = document.getElementById("error");

  // Plain-language controls; the rule ID ties each one to what the agent cites.
  const RULES = [
    { id: "P1", toggle: "defend_delivered_not_received",
      title: "Defend “item not received” claims when delivery is confirmed",
      body: () => ["When the carrier shows the parcel delivered to the shipping address, recommend defending."] },
    { id: "P2", toggle: "accept_under_enabled",
      title: "Accept small disputes",
      body: (s) => ["Recommend accepting disputes under ",
        input("accept_under_amount", s.accept_under_amount, { type: "number", min: "0", max: "100000", step: "0.01", "aria-label": "Amount threshold in US dollars", prefix: "$" }),
        " when nothing links them to other disputes. Contesting costs more than the amount."] },
    { id: "P3", toggle: "defend_linked_enabled",
      title: "Defend when the checkout device or IP appears in other disputes",
      body: (s) => ["Recommend defending when the device hash or IP is shared with at least ",
        input("defend_linked_min", s.defend_linked_min, { type: "number", min: "1", max: "20", step: "1", "aria-label": "Number of other disputes" }),
        " other disputes. Overrides “Accept small disputes”."] },
    { id: "P4", toggle: "accept_no_scan_not_received",
      title: "Accept “item not received” claims with no carrier scan",
      body: () => ["When the label was created but the carrier never scanned the parcel, recommend accepting: there is no proof it shipped."] },
  ];

  let saved = null;      // settings as last loaded or saved
  let previewTimer = 0;

  function input(name, value, attrs) {
    const { prefix, ...rest } = attrs;
    const field = el("input", { name, value: String(value), class: "inline-input num", inputmode: "decimal", ...rest });
    return prefix ? el("span", { class: "prefixed" }, el("span", { "aria-hidden": "true", text: prefix }), field) : field;
  }

  function current() {
    // Read the controls directly: FormData skips disabled inputs, and a rule switched off disables its
    // number field, which would otherwise send 0 and silently change the saved threshold.
    const f = form.elements;
    const s = {};
    for (const r of RULES) s[r.toggle] = f[r.toggle].checked;
    s.accept_under_amount = Number(f.accept_under_amount.value);
    s.defend_linked_min = Number(f.defend_linked_min.value);
    return s;
  }

  // Field-by-field: comparing JSON strings would depend on key order, which differs between the
  // server's response and current(), and made every page look like it had unsaved changes.
  const dirty = () => {
    if (!saved) return false;
    const now = current();
    return Object.keys(saved).some((k) => now[k] !== saved[k]);
  };

  function render(body) {
    const s = body.settings;
    const rows = RULES.map((r) => {
      const toggle = el("input", { type: "checkbox", name: r.toggle, id: `t-${r.id}`, checked: s[r.toggle] });
      return el("fieldset", { class: "policy", "data-rule": r.id },
        el("legend", { class: "sr-only", text: r.title }),
        el("div", { class: "policy-head" },
          el("label", { class: "switch", for: `t-${r.id}` }, toggle, el("span", { class: "track", "aria-hidden": "true" })),
          el("label", { class: "policy-title", for: `t-${r.id}`, text: r.title }),
          el("span", { class: "rule-id", text: r.id })),
        el("p", { class: "policy-body" }, ...r.body(s)),
        el("div", { class: "agent-reads" },
          el("span", { class: "hint", text: "The agent reads" }),
          el("blockquote", { id: `rule-${r.id}` })));
    });
    const status = el("p", { class: "hint", id: "save-status", "aria-live": "polite" });
    form.replaceChildren(...rows,
      el("div", { class: "save-row" },
        el("button", { class: "primary", type: "submit", id: "save", text: "Save policies" }),
        status),
      el("p", { class: "hint", text: "Saved policies apply to the next analysis. Recommendations already made keep the rules they were made with; to apply new rules to one, run it again:" }),
      el("p", { class: "hint" }, el("code", { text: "python scripts/analyze.py --dispute-id <dispute ID>" })));
    showRules(body.rules);
    setStatus(body.updated_at ? `Last saved ${new Date(body.updated_at).toLocaleString()}.` : "Using the default policies. Nothing saved yet.");
    syncDisabled();
  }

  function showRules(rules) {
    const byId = Object.fromEntries(rules.map((r) => [r.id, r.text]));
    for (const r of RULES) {
      const q = document.getElementById(`rule-${r.id}`);
      q.textContent = byId[r.id] ? `${r.id}: ${byId[r.id]}` : "Off. The agent won't see this rule.";
      q.classList.toggle("off", !byId[r.id]);
    }
  }

  function syncDisabled() {
    for (const r of RULES) {
      const on = form.elements[r.toggle].checked;
      form.querySelector(`[data-rule="${r.id}"]`).classList.toggle("is-off", !on);
      for (const f of form.querySelectorAll(`[data-rule="${r.id}"] .inline-input`)) f.disabled = !on;
    }
  }

  function setStatus(text, isError = false) {
    const node = document.getElementById("save-status");
    node.textContent = text;
    node.classList.toggle("error-text", isError);
  }

  async function send(method, url, body) {
    const resp = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const why = data.problems ? data.problems.join("; ") : data.message || data.error || `HTTP ${resp.status}`;
      throw new Error(why);
    }
    return data;
  }

  function onChange() {
    syncDisabled();
    if (dirty()) setStatus("Unsaved changes.");
    clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      try {
        showRules((await send("POST", "/api/policies/preview", current())).rules);
      } catch (err) {
        setStatus(`Check the values: ${err.message}`, true);
      }
    }, 250);
  }

  form.addEventListener("input", onChange);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const button = document.getElementById("save");
    button.disabled = true;
    try {
      const body = await send("PUT", "/api/policies", current());
      saved = body.settings;
      showRules(body.rules);
      setStatus(`Saved ${new Date(body.updated_at).toLocaleTimeString()}. The next analysis uses these rules.`);
    } catch (err) {
      setStatus(`Not saved: ${err.message}`, true);
    } finally {
      button.disabled = false;
    }
  });
  window.addEventListener("beforeunload", (e) => { if (dirty()) e.preventDefault(); });

  (async () => {
    try {
      const resp = await fetch("/api/policies", { cache: "no-store" });
      if (resp.status === 403) throw new Error("This page only works on the machine running DisputeDesk (open it at http://127.0.0.1:8000).");
      if (!resp.ok) throw new Error(`The server answered HTTP ${resp.status}. Reload to try again.`);
      const body = await resp.json();
      DD.live(true);
      await DD.modeReady;
      saved = body.settings;
      render(body);
      if (DD.mode.demo) {
        const button = document.getElementById("save");
        button.disabled = true;
        button.textContent = DD.DEMO_LABEL;
        setStatus("Recorded sandbox data: you can try the switches and see the rule text change, but nothing is saved.");
      }
    } catch (err) {
      DD.live(false);
      document.getElementById("loading").textContent = err instanceof TypeError
        ? "Can't reach the DisputeDesk server. Is uvicorn still running? Reload to try again." : err.message;
    }
  })();
})();
