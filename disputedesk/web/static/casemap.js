// Case map for the Overview hero: the merchant in the middle, each dispute on a ring around it.
// Read-only and visual only. Every node is a dispute row from GET /api/disputes:
//   waiting on you -> a full node, manila for Defend, light for Accept, hollow while awaiting analysis
//   with PayPal    -> a smaller dimmed node labelled "With PayPal"
// At most MAX_NODES are drawn (waiting first); the rest are counted in a "+N more" link to the queue.
// The labels are real links laid over the drawing, so they are clickable, focusable and readable by
// screen readers, and their countdowns are kept current by the shared DD tick (DD.clock(): frozen at
// the recording in demo mode). The drawing itself is decorative: three.js when WebGL is available and
// motion is allowed (DD3D.unavailableReason() decides), otherwise a flat SVG of the same layout.
"use strict";

const DDMap = (() => {
  const MAX_NODES = 8;
  // Layout boxes, in units the drawing and the labels share. Compact (phone-width maps) is taller,
  // so stacked labels on the same side of the ring keep clear of each other.
  const BOXES = {
    wide: { W: 560, H: 400, CX: 280, CY: 200, RX: 110, RY: 150 },
    compact: { W: 560, H: 720, CX: 280, CY: 360, RX: 70, RY: 280 },
  };
  const COMPACT_BELOW = 500;              // map widths (px) that get the compact layout

  /**
   * Pure layout: which disputes become nodes, where they sit and which side their label goes.
   * `groups` is DD.groupDisputes(rows). Returns {box, nodes, more, waiting, withPayPal}.
   */
  function layout(groups, max = MAX_NODES, compact = false) {
    const box = compact ? BOXES.compact : BOXES.wide;
    const { CX, CY, RX, RY } = box;
    const all = [
      ...groups.waiting.map((d) => ({ d, kind: kindOf(d) })),
      ...groups.withPayPal.map((d) => ({ d, kind: "paypal" })),
    ];
    const shown = all.slice(0, max);
    const n = shown.length;
    // Odd counts start at the top; even counts start half a step round, so no two labels share a row.
    const start = -Math.PI / 2 + (n % 2 === 0 && n > 0 ? Math.PI / n : 0);
    const nodes = shown.map((item, i) => {
      const a = start + (i * 2 * Math.PI) / n;
      const x = CX + RX * Math.cos(a), y = CY + RY * Math.sin(a);
      // Labels sit beside their node, away from the centre; the top node (odd counts) labels to the right.
      const side = Math.cos(a) > -0.01 ? "right" : "left";
      return { ...item, x: +x.toFixed(2), y: +y.toFixed(2), side };
    });
    return { box, compact, nodes, more: all.length - shown.length, waiting: groups.waiting.length, withPayPal: groups.withPayPal.length };
  }

  function kindOf(d) {
    const dec = d.decision;
    if (!dec || dec.status !== "ok") return "pending";
    return dec.decision === "DEFEND" ? "defend" : "accept";
  }

  const KIND_WORD = { defend: "Defend", accept: "Accept", pending: "Awaiting analysis", paypal: "With PayPal" };

  // ---- labels (HTML links over the drawing) ---------------------------------------------------

  function labels(plan, onHover) {
    const { el } = DD;
    const { W, H, CY } = plan.box;
    const pct = (v, of) => `${((v / of) * 100).toFixed(3)}%`;
    const layer = el("div", { class: `map-labels${plan.nodes.length ? " has-nodes" : ""}`, style: `--map-w:${W}` });
    const links = [];
    plan.nodes.forEach((node, i) => {
      const d = node.d;
      const sr = (text) => el("span", { class: "sr-only", text });  // spoken separators, hidden on screen
      const meta = el("span", { class: "mn-meta" }, sr(", "), el("span", { class: "mn-kind", text: KIND_WORD[node.kind] }));
      if (node.kind !== "paypal" && d.seller_response_due_date) {
        const c = DD.countdown(d.seller_response_due_date);  // DD.clock(): the recording's time in demo mode
        const overdue = DD.isOverdue(c);
        // "time left:" reads correctly for both "7d 4h" and "Overdue by 2h" as the shared tick updates it.
        meta.append(sr(", time left: "), el("span", { class: `mn-due${c.urgent ? " urgent" : ""}${overdue ? " overdue" : ""}`, "data-due": d.seller_response_due_date, title: "Time left to respond" },
          el("span", { class: "cd-main", text: c.text }), DD.dueSoonChip(c)));
      }
      const link = el("a", { class: `map-node is-${node.kind} side-${node.side}`, href: `/case/${encodeURIComponent(d.dispute_id)}`,
                             style: `left:${pct(node.x, W)};top:${pct(node.y, H)}` },
        el("span", { class: "mn-hit", "aria-hidden": "true" }),
        el("span", { class: "mn-text" }, idNode(d.dispute_id), meta));
      link.addEventListener("pointerenter", () => onHover(i));
      link.addEventListener("pointerleave", () => onHover(-1));
      link.addEventListener("focus", () => onHover(i));
      link.addEventListener("blur", () => onHover(-1));
      layer.append(link);
      links.push(link);
    });
    // The hover label: dispute ID, verdict and countdown for the hovered or focused node. It repeats the
    // link's own text, so it is hidden from screen readers.
    const chip = el("div", { class: "map-hover", "aria-hidden": "true", hidden: true });
    layer.append(chip);
    layer.append(el("span", { class: "map-merchant-label", style: `left:50%;top:${pct(CY + 44, H)}` },
      plan.nodes.length ? "Your store" : "Your store. Nothing waiting on you."));
    if (plan.more > 0) {
      layer.append(el("a", { class: "map-more", href: "/queue", style: `left:50%;top:${pct(H - 14, H)}` }, `+${plan.more} more`));
    }
    return { layer, links, chip };
  }

  // Fill and place the hover label for node i (or hide it for -1). Countdown from DD.countdown, i.e.
  // DD.clock(): the recording's time in demo mode; the shared tick keeps it current through data-due.
  function showChip(plan, ui, i) {
    const { chip } = ui;
    if (i < 0 || !plan.nodes[i]) { chip.hidden = true; return; }
    const node = plan.nodes[i], d = node.d, { el } = DD, { W, H } = plan.box;
    const parts = [el("span", { class: "mh-id num", text: d.dispute_id }), el("span", { class: `mh-kind is-${node.kind}`, text: KIND_WORD[node.kind] })];
    if (node.kind !== "paypal" && d.seller_response_due_date) {
      const c = DD.countdown(d.seller_response_due_date);
      parts.push(el("span", { class: `mh-due${DD.isOverdue(c) ? " overdue" : ""}`, "data-due": d.seller_response_due_date },
        el("span", { class: "cd-main", text: c.text }), DD.dueSoonChip(c)));
    }
    chip.replaceChildren(...parts);
    chip.style.left = `${((node.x / W) * 100).toFixed(3)}%`;
    chip.style.top = `${((node.y / H) * 100).toFixed(3)}%`;
    const link = ui.links[i];
    chip.style.setProperty("--dx", link.style.getPropertyValue("--dx") || "0");
    chip.style.setProperty("--dy", link.style.getPropertyValue("--dy") || "0");
    chip.hidden = false;
  }

  // The full ID, split after its last hyphen so narrow maps can wrap it onto two lines (CSS decides).
  function idNode(id) {
    const cut = id.lastIndexOf("-") + 1;
    return DD.el("span", { class: "mn-id num" }, id.slice(0, cut), DD.el("span", { class: "id-tail", text: id.slice(cut) }));
  }

  function legend(plan) {
    const { el } = DD;
    const kinds = [...new Set(plan.nodes.map((n) => n.kind))];
    const order = ["defend", "accept", "pending", "paypal"].filter((k) => kinds.includes(k));
    if (!order.length) return null;
    const waiting = plan.nodes.some((n) => n.kind !== "paypal");
    return el("p", { class: "map-legend cap" }, ...order.map((k) =>
      el("span", { class: `lg is-${k}` }, el("span", { class: "lg-dot", "aria-hidden": "true" }), KIND_WORD[k])),
      waiting ? el("span", { class: "lg is-waiting" }, el("span", { class: "lg-dot", "aria-hidden": "true" }), "Waiting on you") : null);
  }

  // ---- flat SVG (reduced motion, no WebGL) -----------------------------------------------------

  const SVGNS = "http://www.w3.org/2000/svg";
  function s(tag, attrs) {
    const node = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    return node;
  }

  function flat(plan) {
    const { W, H, CX, CY, RX, RY } = plan.box;
    const svg = s("svg", { class: "map-flat", viewBox: `0 0 ${W} ${H}`, "aria-hidden": "true", focusable: "false" });
    svg.append(s("ellipse", { class: "map-orbit", cx: CX, cy: CY, rx: RX, ry: RY }));
    for (const node of plan.nodes) {
      svg.append(s("line", { class: `map-edge is-${node.kind}`, x1: CX, y1: CY, x2: node.x, y2: node.y }));
    }
    // The merchant: a folder mark.
    svg.append(s("path", { class: "map-merchant", d: `M${CX - 26} ${CY - 16} h18 l6 -7 h28 v39 h-52 z` }));
    const dots = plan.nodes.map((node) => {
      const r = node.kind === "paypal" ? 8 : 13;
      if (node.kind !== "paypal") svg.append(s("circle", { class: "map-ring", cx: node.x, cy: node.y, r: r + 6 }));  // waiting on you
      const dot = s("circle", { class: `map-dot is-${node.kind}`, cx: node.x, cy: node.y, r });
      svg.append(dot);
      return dot;
    });
    return { node: svg, highlight: (i) => dots.forEach((dot, j) => dot.classList.toggle("is-hot", j === i)), setHover() {}, dispose() {} };
  }

  // ---- three.js (motion allowed, WebGL available) -----------------------------------------------

  // Colours come from the CSS tokens (DD.colorNum), read when the map is drawn.
  function palette() {
    return {
      defend: DD.colorNum("--primary"), accept: DD.colorNum("--primary-text"), pending: DD.colorNum("--muted"),
      paypal: DD.colorNum("--primary"), edge: DD.colorNum("--border"), merchant: DD.colorNum("--primary"),
      ring: DD.colorNum("--splash"), light: DD.colorNum("--sheen"),
    };
  }

  function glowTexture() {
    const c = document.createElement("canvas");
    c.width = c.height = 64;
    const g = c.getContext("2d");
    const grad = g.createRadialGradient(32, 32, 1, 32, 32, 32);
    grad.addColorStop(0, DD.rgba("--primary-text", 1));
    grad.addColorStop(0.4, DD.rgba("--primary", 0.35));
    grad.addColorStop(1, DD.rgba("--primary", 0));
    g.fillStyle = grad;
    g.fillRect(0, 0, 64, 64);
    return new THREE.CanvasTexture(c);
  }

  const RAY_MS = 60;        // raycast at most this often, and only after the pointer moved
  const TILT_Y = 0.30, TILT_X = 0.20;   // radians: how far the map turns toward the pointer

  function webgl(plan, container, onLost, ui, onRayHover) {
    const { W, H, CX, CY, RX, RY } = plan.box;
    const COLORS = palette();
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "low-power" });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    const canvas = renderer.domElement;
    canvas.className = "map-canvas";
    canvas.setAttribute("aria-hidden", "true");
    // A lost GPU context (driver reset, too many contexts) would leave a blank box: fall back to flat.
    canvas.addEventListener("webglcontextlost", (e) => { e.preventDefault(); onLost(); }, { once: true });

    // Orthographic camera in layout units, so every node sits exactly under its HTML label.
    const camera = new THREE.OrthographicCamera(-W / 2, W / 2, H / 2, -H / 2, -500, 500);
    camera.position.set(0, 0, 200);
    const scene = new THREE.Scene();
    scene.add(new THREE.AmbientLight(COLORS.light, 0.6));
    const key = new THREE.DirectionalLight(COLORS.light, 1.2);
    key.position.set(-120, 160, 220);
    scene.add(key);
    const at = (x, y, z = 0) => new THREE.Vector3(x - CX, CY - y, z);
    const glow = glowTexture();
    // Everything turns together toward the pointer; the camera stays put so labels can be projected.
    const world = new THREE.Group();
    scene.add(world);

    // Merchant: a slowly turning blue crystal inside a tilted orbit ring.
    const merchant = new THREE.Mesh(new THREE.OctahedronGeometry(24, 0),
      new THREE.MeshStandardMaterial({ color: COLORS.merchant, roughness: 0.35, metalness: 0.2, emissive: COLORS.merchant, emissiveIntensity: 0.25, flatShading: true }));
    world.add(merchant);
    const ring = new THREE.Mesh(new THREE.TorusGeometry(38, 1.1, 8, 72),
      new THREE.MeshBasicMaterial({ color: COLORS.accept, transparent: true, opacity: 0.45 }));
    ring.rotation.x = 1.15;
    world.add(ring);
    const orbit = new THREE.Line(new THREE.BufferGeometry().setFromPoints(
      Array.from({ length: 97 }, (_, i) => at(CX + RX * Math.cos((i / 96) * 2 * Math.PI), CY + RY * Math.sin((i / 96) * 2 * Math.PI)))),
      new THREE.LineBasicMaterial({ color: COLORS.edge, transparent: true, opacity: 0.55 }));
    world.add(orbit);

    // Depth: disputes waiting on you stand forward, with-PayPal ones sit back, so turning shows parallax.
    const meshes = [], pulses = [], nodes = [], picks = [];
    plan.nodes.forEach((node, i) => {
      const dim = node.kind === "paypal";
      const baseZ = dim ? -28 : 28;
      const p = at(node.x, node.y, baseZ);
      const group = new THREE.Group();
      group.position.copy(p);
      world.add(group);
      const edgeMat = dim
        ? new THREE.LineDashedMaterial({ color: COLORS.edge, dashSize: 5, gapSize: 5, transparent: true, opacity: 0.8 })
        : new THREE.LineBasicMaterial({ color: node.kind === "defend" ? COLORS.defend : COLORS.pending, transparent: true, opacity: 0.45 });
      const edge = new THREE.Line(new THREE.BufferGeometry().setFromPoints([at(CX, CY), p]), edgeMat);
      if (dim) edge.computeLineDistances();
      world.add(edge);
      // Defend: solid blue. Accept: outlined (wireframe) light blue. Pending: grey wireframe. With PayPal: dimmed blue.
      const mat = node.kind === "pending" || node.kind === "accept"
        ? new THREE.MeshBasicMaterial({ color: COLORS[node.kind], wireframe: true })
        : new THREE.MeshStandardMaterial({ color: COLORS[node.kind], roughness: 0.4, metalness: 0.1, transparent: dim, opacity: dim ? 0.45 : 1 });
      const outlined = node.kind === "pending" || node.kind === "accept";  // a sparse wireframe reads as an outline
      const mesh = new THREE.Mesh(new THREE.SphereGeometry(dim ? 8 : 13, outlined ? 10 : 28, outlined ? 6 : 18), mat);
      group.add(mesh);
      meshes.push(mesh);
      if (!dim) {  // waiting on you: a tangerine ring around the node
        const halo = new THREE.Mesh(new THREE.TorusGeometry(19, 1.4, 8, 48), new THREE.MeshBasicMaterial({ color: COLORS.ring }));
        group.add(halo);
      }
      // Hover glow (blue, never tangerine) and an invisible, larger pick target for the raycaster.
      const shine = new THREE.Sprite(new THREE.SpriteMaterial({ map: glow, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true, opacity: 0 }));
      shine.scale.set(64, 64, 1);
      group.add(shine);
      const pick = new THREE.Mesh(new THREE.SphereGeometry(dim ? 16 : 22, 8, 6), new THREE.MeshBasicMaterial({ visible: false }));
      pick.userData.index = i;
      group.add(pick);
      picks.push(pick);
      nodes.push({ group, mesh, shine, baseZ, h: 0, x: node.x, y: node.y });
      if (!dim) {  // waiting on you: a spark travels from the dispute to the merchant
        const spark = new THREE.Sprite(new THREE.SpriteMaterial({ map: glow, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true }));
        spark.scale.set(14, 14, 1);
        world.add(spark);
        pulses.push({ spark, from: p, to: at(CX, CY), phase: i * 0.37 });
      }
    });

    // ---- pointer: on the shared DDPointer loop (numbers only; the render loop below draws them) ----
    const raycaster = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    const tilt = { x: 0, y: 0 };
    let linkHot = -1, rayHot = -1, lastRay = 0, lastPX = NaN, lastPY = NaN, rect = null, rectDirty = true;
    const markDirty = () => { rectDirty = true; };
    window.addEventListener("scroll", markDirty, { passive: true });
    window.addEventListener("resize", markDirty, { passive: true });
    const hotIndex = () => (linkHot >= 0 ? linkHot : rayHot);
    const unsubscribe = DDPointer.subscribe((dt, p) => {
      if (disposed) return false;
      if (rectDirty) { const r = container.getBoundingClientRect(); rect = { l: r.left, t: r.top, w: r.width, h: r.height }; rectDirty = false; }
      const cx = rect.l + rect.w / 2, cy = rect.t + rect.h / 2;
      const tx = p.inside ? Math.max(-1, Math.min(1, (p.x - cx) / (p.vw / 2))) : 0;
      const ty = p.inside ? Math.max(-1, Math.min(1, (p.y - cy) / (p.vh / 2))) : 0;
      tilt.y = DDPointer.ease(tilt.y, tx * TILT_Y, dt, 180);
      tilt.x = DDPointer.ease(tilt.x, ty * TILT_X, dt, 180);
      // Raycast: throttled, and only when the pointer moved over the map.
      const now = performance.now();
      const over = p.inside && p.x >= rect.l && p.x <= rect.l + rect.w && p.y >= rect.t && p.y <= rect.t + rect.h;
      if (!over) { if (rayHot !== -1) { rayHot = -1; onRayHover(hotIndex()); } }
      else if (now - lastRay >= RAY_MS && (p.x !== lastPX || p.y !== lastPY)) {
        lastRay = now; lastPX = p.x; lastPY = p.y;
        ndc.set(((p.x - rect.l) / rect.w) * 2 - 1, -(((p.y - rect.t) / rect.h) * 2 - 1));
        raycaster.setFromCamera(ndc, camera);
        const hit = raycaster.intersectObjects(picks, false)[0];
        const idx = hit ? hit.object.userData.index : -1;
        if (idx !== rayHot) { rayHot = idx; onRayHover(hotIndex()); }
      }
      // Hovered node lifts toward the viewer, grows and glows; the others settle back.
      let moving = false;
      nodes.forEach((n, i) => {
        const target = i === hotIndex() ? 1 : 0;
        n.h = DDPointer.ease(n.h, target, dt, 120);
        if (!DDPointer.settled(n.h, target, 0.004)) moving = true;
      });
      return moving || !(DDPointer.settled(tilt.y, tx * TILT_Y, 0.0005) && DDPointer.settled(tilt.x, ty * TILT_X, 0.0005));
    }, () => { tilt.x = tilt.y = 0; rayHot = -1; nodes.forEach((n) => { n.h = linkHot === nodes.indexOf(n) ? 1 : 0; }); });

    // Keep each HTML label on its node: project the node into layout units, offset the link by the change.
    const v = new THREE.Vector3();
    function placeLabels() {
      nodes.forEach((n, i) => {
        n.group.getWorldPosition(v).project(camera);
        const dx = ((v.x + 1) / 2) * W - n.x, dy = ((1 - v.y) / 2) * H - n.y;
        const link = ui.links[i];
        link.style.setProperty("--dx", dx.toFixed(2));
        link.style.setProperty("--dy", dy.toFixed(2));
        if (i === hotIndex() && !ui.chip.hidden) { ui.chip.style.setProperty("--dx", dx.toFixed(2)); ui.chip.style.setProperty("--dy", dy.toFixed(2)); }
      });
    }

    let raf = 0, last = 0, t = 0, inView = true, disposed = false;
    const FRAME_MS = 1000 / 30;            // at most 30 frames a second
    function frame(now) {
      raf = 0;
      if (disposed || document.hidden || !inView) return;
      if (now - last >= FRAME_MS) {
        t += Math.min(100, now - (last || now)) / 1000;
        last = now;
        merchant.rotation.y = t * 0.6;
        merchant.rotation.x = 0.35 + Math.sin(t * 0.4) * 0.12;
        ring.rotation.z = t * 0.25;
        for (const pl of pulses) {
          const k = ((t * 0.32 + pl.phase) % 1);
          pl.spark.position.lerpVectors(pl.from, pl.to, k);
          pl.spark.material.opacity = Math.sin(k * Math.PI);
        }
        world.rotation.set(tilt.x, tilt.y, 0);
        for (const n of nodes) {
          n.group.position.z = n.baseZ + n.h * 22;
          n.mesh.scale.setScalar(1 + n.h * 0.3);
          n.shine.material.opacity = n.h * 0.9;
        }
        renderer.render(scene, camera);
        placeLabels();
      }
      raf = requestAnimationFrame(frame);
    }
    const run = () => { if (!raf && !disposed && !document.hidden && inView) raf = requestAnimationFrame(frame); };
    const stop = () => { cancelAnimationFrame(raf); raf = 0; };
    const onVisibility = () => (document.hidden ? stop() : run());
    document.addEventListener("visibilitychange", onVisibility);
    const io = new IntersectionObserver((entries) => { inView = entries[0].isIntersecting; inView ? run() : stop(); });
    io.observe(container);
    const resize = () => {
      const w = container.clientWidth || W;
      renderer.setSize(w, Math.round((w * H) / W), false);
      canvas.style.width = "100%";
      canvas.style.height = "auto";
      renderer.render(scene, camera);
      rectDirty = true;
    };
    const ro = new ResizeObserver(resize);
    ro.observe(container);

    return {
      node: canvas,
      start() { resize(); run(); },
      highlight(i) { linkHot = i; DDPointer.kick(); },
      running: () => Boolean(raf),
      dispose() {
        if (disposed) return;
        disposed = true;
        stop();
        unsubscribe();
        window.removeEventListener("scroll", markDirty);
        window.removeEventListener("resize", markDirty);
        io.disconnect();
        ro.disconnect();
        document.removeEventListener("visibilitychange", onVisibility);
        scene.traverse((obj) => {
          if (obj.geometry) obj.geometry.dispose();
          const mats = obj.material ? (Array.isArray(obj.material) ? obj.material : [obj.material]) : [];
          for (const m of mats) m.dispose();
        });
        glow.dispose();
        renderer.dispose();
        renderer.forceContextLoss();
        canvas.remove();
      },
    };
  }

  /**
   * Build the map for `rows` (the /api/disputes rows); `compact` picks the tall phone-width layout. Returns {node, dispose, mode}. The caller puts
   * `node` in the page, then calls start(); dispose() frees the 3D renderer.
   */
  function create(rows, { compact = false } = {}) {
    const { el } = DD;
    const plan = layout(DD.groupDisputes(rows), MAX_NODES, compact);
    const stage = el("div", { class: "map-stage", style: `aspect-ratio:${plan.box.W} / ${plan.box.H}` });
    const off = typeof DD3D === "undefined" ? "3D unavailable" : DD3D.unavailableReason();
    let view, linkHot = -1, rayHot = -1;
    const ui = labels(plan, (i) => { linkHot = i; view.highlight(i); showChip(plan, ui, linkHot >= 0 ? linkHot : rayHot); });
    const onRayHover = (i) => { rayHot = i; showChip(plan, ui, linkHot >= 0 ? linkHot : i); };
    if (off) view = flat(plan);
    else {
      try {
        view = webgl(plan, stage, () => {
          const lost = view;
          view = flat(plan);
          lost.node.replaceWith(view.node);
          lost.dispose();
          section.classList.replace("is-3d", "is-flat");
          ui.links.forEach((l) => { l.style.removeProperty("--dx"); l.style.removeProperty("--dy"); });
        }, ui, onRayHover);
      } catch { view = flat(plan); }
    }
    const mode = view.node.tagName.toLowerCase() === "canvas" ? "3d" : "flat";
    stage.append(view.node, ui.layer);
    const parts = [`${plan.waiting} waiting on you`];
    if (plan.withPayPal) parts.push(`${plan.withPayPal} with PayPal`);
    const section = el("section", { class: `case-map is-${mode}${compact ? " is-compact" : ""}`, "aria-label": `Case map: ${parts.join(", ")}` },
      stage, legend(plan));
    return {
      node: section,
      mode,
      plan,
      start: () => { if (view.start) view.start(); },
      dispose: () => view.dispose(),
      running: () => Boolean(view.running && view.running()),
    };
  }

  return { MAX_NODES, BOXES, COMPACT_BELOW, layout, create };
})();

if (typeof module !== "undefined" && module.exports) module.exports = DDMap;  // unit tests run the layout in Node
