// 3D evidence graph for the case page (three.js 0.160.0 UMD from cdnjs, global THREE). Visual only:
// it draws the same graph_context the flat SVG draws, and highlights the nodes and edges behind the
// facts the agent cited. Falls back to the SVG when WebGL is missing or reduced motion is requested.
"use strict";

const DD3D = (() => {
  const COLORS = {
    accent: 0xe2b85c,
    paypal: 0x8ea3bf,      // solid sphere: a PayPal record
    seeded: 0xb9a57a,      // wireframe crystal: merchant-supplied test data
    muted: 0x4a5668,
    edge: 0x3a4657,
  };
  const MAX_LINKED = 6;    // the decision never needs more; extra links are counted in the legend

  function reducedMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function webgl() {
    if (typeof THREE === "undefined") return false;
    try {
      const c = document.createElement("canvas");
      return Boolean(c.getContext("webgl2") || c.getContext("webgl"));
    } catch {
      return false;
    }
  }

  /** Why 3D is off, or null when it can run. */
  function unavailableReason() {
    if (reducedMotion()) return "Showing the flat diagram because your system asks for reduced motion.";
    if (typeof THREE === "undefined") return "Showing the flat diagram because the 3D library didn't load (is this machine offline?).";
    if (!webgl()) return "Showing the flat diagram because this browser can't draw 3D graphics (WebGL is off or unavailable).";
    return null;
  }

  function labelTexture(title, sub, tag, cited) {
    const W = 512, H = 168, c = document.createElement("canvas");
    c.width = W; c.height = H;
    const g = c.getContext("2d");
    g.fillStyle = "rgba(19, 25, 34, 0.86)";
    g.strokeStyle = cited ? "rgba(226, 184, 92, 0.95)" : "rgba(42, 52, 66, 1)";
    g.lineWidth = cited ? 5 : 3;
    const r = 18;
    g.beginPath();
    g.moveTo(r, 2); g.lineTo(W - r, 2); g.quadraticCurveTo(W - 2, 2, W - 2, r); g.lineTo(W - 2, H - r);
    g.quadraticCurveTo(W - 2, H - 2, W - r, H - 2); g.lineTo(r, H - 2); g.quadraticCurveTo(2, H - 2, 2, H - r);
    g.lineTo(2, r); g.quadraticCurveTo(2, 2, r, 2); g.closePath(); g.fill(); g.stroke();
    g.font = "600 44px 'Public Sans', system-ui, sans-serif";
    g.fillStyle = "#e6e9ee";
    g.fillText(title, 26, 64);
    g.font = "400 34px 'Public Sans', system-ui, sans-serif";
    g.fillStyle = "#95a1b3";
    g.fillText(sub.length > 26 ? sub.slice(0, 25) + "…" : sub, 26, 124);
    g.font = "700 30px 'Public Sans', system-ui, sans-serif";
    g.textAlign = "right";
    g.fillStyle = tag === "seeded" ? "#e2b85c" : "#95a1b3";
    g.fillText(tag === "seeded" ? "seeded" : "PayPal", W - 26, 62);
    const tex = new THREE.CanvasTexture(c);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = 4;
    return tex;
  }

  function haloTexture() {
    const c = document.createElement("canvas");
    c.width = c.height = 128;
    const g = c.getContext("2d");
    const grad = g.createRadialGradient(64, 64, 4, 64, 64, 64);
    grad.addColorStop(0, "rgba(226, 184, 92, 0.85)");
    grad.addColorStop(0.35, "rgba(226, 184, 92, 0.28)");
    grad.addColorStop(1, "rgba(226, 184, 92, 0)");
    g.fillStyle = grad;
    g.fillRect(0, 0, 128, 128);
    return new THREE.CanvasTexture(c);
  }

  /**
   * Mount the graph in `container`. `ctx` is the decision's graph_context, `used` the set of graph refs
   * cited by the agent, `labels` maps ref -> {title, sub}. Returns {dispose, stats, linkedHidden}.
   */
  function mount(container, ctx, used, labels) {
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "low-power" });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    const canvas = renderer.domElement;
    canvas.className = "graph3d-canvas";
    canvas.tabIndex = 0;
    canvas.setAttribute("role", "img");
    container.append(canvas);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 100);
    camera.position.set(0, 0.4, 12.6);  // framed so the linked disputes clear the bottom edge
    camera.lookAt(0, -0.7, 0);
    scene.add(new THREE.AmbientLight(0xffffff, 0.55));
    const key = new THREE.DirectionalLight(0xffffff, 1.1);
    key.position.set(3, 5, 6);
    scene.add(key);

    const world = new THREE.Group();
    world.rotation.x = 0.12;
    scene.add(world);
    const halo = haloTexture();

    // Positions follow the flat diagram: buyer -> transaction -> dispute across the top, the
    // checkout device and shipment below, linked disputes fanned out under the device.
    const linkedAll = ctx.linked_disputes || [];
    const linked = linkedAll.slice(0, MAX_LINKED);
    const nodes = {};
    // Same rule as the flat diagram: buyer -> transaction -> dispute frames every cited path.
    const spine = used.size > 0;
    const SPINE = new Set(["customer", "transaction", "dispute"]);
    const place = (ref, x, y, z, source) => {
      const isUsed = used.has(ref) || (spine && SPINE.has(ref));
      const seeded = source === "seeded";
      const color = isUsed ? COLORS.accent : seeded ? COLORS.seeded : COLORS.paypal;
      const geom = seeded ? new THREE.OctahedronGeometry(0.42, 0) : new THREE.SphereGeometry(0.36, 32, 20);
      const mat = new THREE.MeshStandardMaterial({
        color, roughness: 0.45, metalness: 0.15,
        emissive: isUsed ? COLORS.accent : 0x000000, emissiveIntensity: isUsed ? 0.55 : 0,
        transparent: seeded, opacity: seeded ? 0.35 : 1,
      });
      const mesh = new THREE.Mesh(geom, mat);
      mesh.position.set(x, y, z);
      world.add(mesh);
      if (seeded) {  // the crystal's edges make seeded records read as "outline only"
        const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geom),
          new THREE.LineBasicMaterial({ color: isUsed ? COLORS.accent : COLORS.seeded }));
        edges.position.copy(mesh.position);
        world.add(edges);
      }
      if (isUsed) {
        const glow = new THREE.Sprite(new THREE.SpriteMaterial({ map: halo, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true }));
        glow.scale.set(1.9, 1.9, 1);
        glow.position.copy(mesh.position);
        world.add(glow);
      }
      const l = labels[ref] || { title: ref, sub: "" };
      const label = new THREE.Sprite(new THREE.SpriteMaterial({ map: labelTexture(l.title, l.sub, source, isUsed), depthWrite: false, transparent: true }));
      label.scale.set(1.85, 0.607, 1);  // keeps the canvas's 512:168 aspect
      label.position.set(x, y + 0.78, z);
      world.add(label);
      nodes[ref] = mesh.position.clone();
    };

    const cust = ctx.customer, tx = ctx.transaction, ship = ctx.shipment, tel = ctx.telemetry, d = ctx.dispute;
    place("customer", -3.4, 1.4, 0, cust ? cust.source : "paypal");
    place("transaction", 0, 1.4, 0, tx ? tx.source : "paypal");
    place("dispute", 3.4, 1.4, 0, d.source);
    place("telemetry", -2.4, -1.1, 0.9, tel ? tel.source : "seeded");
    place("shipment", 2.8, -1.3, -0.6, ship ? ship.source : "seeded");
    linked.forEach((o, i) => {
      const spread = (i - (linked.length - 1) / 2) * 2.5;
      place(`linked:${o.id}`, -1.0 + spread, -3.6, 0.9 - i * 0.2, o.source);
    });

    const edge = (a, b, isUsed) => {
      if (!nodes[a] || !nodes[b]) return;
      if (isUsed) {  // cited path: a glowing tube, thick enough to read at a glance
        const tube = new THREE.Mesh(new THREE.TubeGeometry(new THREE.LineCurve3(nodes[a], nodes[b]), 1, 0.045, 8, false),
          new THREE.MeshStandardMaterial({ color: COLORS.accent, emissive: COLORS.accent, emissiveIntensity: 0.7 }));
        world.add(tube);
      } else {
        const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints([nodes[a], nodes[b]]),
          new THREE.LineBasicMaterial({ color: COLORS.edge, transparent: true, opacity: 0.85 }));
        world.add(line);
      }
    };
    edge("customer", "transaction", spine);
    edge("transaction", "dispute", spine);
    edge("transaction", "shipment", used.has("shipment"));
    edge("transaction", "telemetry", used.has("telemetry") || [...used].some((r) => r.startsWith("linked:")));
    linked.forEach((o) => edge("telemetry", `linked:${o.id}`, used.has(`linked:${o.id}`)));

    // ---- interaction, render loop, lifecycle ------------------------------------------------
    let autoRotate = true, dragging = false, lastX = 0, lastY = 0, raf = 0, last = performance.now();
    let frames = 0, frameMsTotal = 0, worstMs = 0, disposed = false;
    const stopAuto = () => { autoRotate = false; };

    function resize() {
      const w = container.clientWidth || 600, h = Math.max(320, Math.round(w * 0.62));
      renderer.setSize(w, h, false);
      canvas.style.width = "100%";
      canvas.style.height = `${h}px`;
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      requestRender();
    }
    function frame(now) {
      raf = 0;
      if (disposed) return;
      const dt = Math.min(64, now - last);
      last = now;
      if (autoRotate) world.rotation.y += dt * 0.00012;  // about one turn per 52 seconds
      const t0 = performance.now();
      renderer.render(scene, camera);
      const ms = performance.now() - t0;
      frames += 1; frameMsTotal += ms; worstMs = Math.max(worstMs, ms);
      if (autoRotate && !document.hidden) raf = requestAnimationFrame(frame);
    }
    function requestRender() {
      if (!raf && !disposed && !document.hidden) { last = performance.now(); raf = requestAnimationFrame(frame); }
    }
    const onDown = (e) => { stopAuto(); dragging = true; lastX = e.clientX; lastY = e.clientY; canvas.setPointerCapture(e.pointerId); };
    const onMove = (e) => {
      if (!dragging) return;
      world.rotation.y += (e.clientX - lastX) * 0.008;
      world.rotation.x = Math.max(-0.6, Math.min(0.8, world.rotation.x + (e.clientY - lastY) * 0.006));
      lastX = e.clientX; lastY = e.clientY;
      requestRender();
    };
    const onUp = () => { dragging = false; };
    const onKey = (e) => {
      const step = { ArrowLeft: [0, -0.12], ArrowRight: [0, 0.12], ArrowUp: [-0.08, 0], ArrowDown: [0.08, 0] }[e.key];
      if (!step) return;
      e.preventDefault();
      stopAuto();
      world.rotation.x = Math.max(-0.6, Math.min(0.8, world.rotation.x + step[0]));
      world.rotation.y += step[1];
      requestRender();
    };
    const onVisibility = () => { if (document.hidden) { cancelAnimationFrame(raf); raf = 0; } else requestRender(); };
    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointermove", onMove);
    canvas.addEventListener("pointerup", onUp);
    canvas.addEventListener("pointercancel", onUp);
    canvas.addEventListener("keydown", onKey);
    document.addEventListener("visibilitychange", onVisibility);
    const ro = new ResizeObserver(resize);
    ro.observe(container);
    resize();

    function dispose() {
      if (disposed) return;
      disposed = true;
      cancelAnimationFrame(raf);
      ro.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
      scene.traverse((obj) => {
        if (obj.geometry) obj.geometry.dispose();
        const mats = obj.material ? (Array.isArray(obj.material) ? obj.material : [obj.material]) : [];
        for (const m of mats) { if (m.map) m.map.dispose(); m.dispose(); }
      });
      halo.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      canvas.remove();
    }

    return {
      dispose,
      linkedHidden: linkedAll.length - linked.length,
      stats: () => ({ frames, avgRenderMs: frames ? +(frameMsTotal / frames).toFixed(3) : 0, worstRenderMs: +worstMs.toFixed(3),
                      autoRotate, running: Boolean(raf), disposed, drawCalls: renderer.info.render.calls,
                      geometries: renderer.info.memory.geometries, textures: renderer.info.memory.textures }),
    };
  }

  return { unavailableReason, mount };
})();
