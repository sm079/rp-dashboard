// Pod Control dashboard client: renders server state pushed over SSE and
// sends start/stop/terminate requests.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const body = document.body;
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

  let S = null;              // latest server snapshot
  let chosenProfile = null;  // profile picked in the UI
  let pending = false;       // a request is in flight
  let lastBeat = 0;
  let transfer = null, transferAt = 0;
  let follow = true;
  const seenOutputs = new Set();
  let firstOutputs = true;

  const MODEL_NAMES = { krea2: "Krea 2", h3: "MiniMax H3" };

  const START_STEPS = [
    ["launch", "GPU"], ["ssh", "Boot"], ["health", "Host check"],
    ["setup", "Setup"], ["models", "Models"], ["connect", "Sync"],
  ];
  const DOWN_STEPS = [["stop", "Stop sync"], ["copy", "Save outputs"], ["terminate", "Terminate"]];

  const PHASE_COPY = {
    launch: ["Finding a GPU", (s) => s.job.detail === "waiting for a free GPU"
      ? `No ${shortGpu(s.gpu)} free yet. Retrying every 30 s.`
      : `Requesting a ${shortGpu(s.gpu)} on secure cloud.`],
    ssh: ["Booting the pod", () => "Waiting for the container to start and accept SSH."],
    health: ["Checking the host", () => "Timing SSH and disk writes to skip an overloaded machine."],
    setup: ["Setting up ComfyUI", () => "Pinning the release, moving user folders to RAM, binding to localhost."],
    models: ["Downloading models", (s) => {
      const m = s.job.models;
      return `${s.job.detail || "Model sets"}${m ? ` · repo ${m.i} of ${m.n}` : ""}. Straight from Hugging Face to the pod.`;
    }],
    connect: ["Connecting", () => "Opening the tunnel and uploading local workflows, settings and LoRAs."],
  };

  const ICONS = {
    play: '<svg viewBox="0 0 20 20"><path d="M6 4.5v11l9-5.5z" fill="currentColor" stroke="none"/></svg>',
    open: '<svg viewBox="0 0 20 20"><path d="M11 3.5h5.5V9M16.5 3.5 9 11M14 12.5v3a1 1 0 0 1-1 1H4.5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h3"/></svg>',
    pause: '<svg viewBox="0 0 20 20"><path d="M7 4.5v11M13 4.5v11"/></svg>',
    stop: '<svg viewBox="0 0 20 20"><rect x="4.5" y="4.5" width="11" height="11" rx="2.5"/></svg>',
    x: '<svg viewBox="0 0 20 20"><path d="M5 5l10 10M15 5 5 15"/></svg>',
    resume: '<svg viewBox="0 0 20 20"><path d="M3.5 10a6.5 6.5 0 1 0 2-4.7M3.5 3.5v3.3h3.3"/></svg>',
    check: '<svg viewBox="0 0 12 12"><path d="M2.5 6.2 5 8.5l4.5-5"/></svg>',
    warn: '<svg viewBox="0 0 24 24"><path d="M12 3 2.5 20h19z"/><path d="M12 10v4.5M12 17.4v.1"/></svg>',
    power: '<svg viewBox="0 0 24 24"><path d="M12 3v8M7 6.3a7.5 7.5 0 1 0 10 0"/></svg>',
    image: '<svg viewBox="0 0 40 40"><rect x="5" y="8" width="30" height="24" rx="4"/><circle cx="14" cy="16" r="3"/><path d="m5 28 9-8 7 6 5-4 9 7"/></svg>',
  };

  // Glyphs drawn in the orb's core (200x200 viewBox, centred on 100,100).
  const GLYPHS = {
    idle: '<path d="M100 80v19"/><path d="M87.5 86.5a19 19 0 1 0 25 0"/>',
    launching: '<circle class="dot" cx="84" cy="100" r="5.5"/><circle class="dot" cx="100" cy="100" r="5.5"/><circle class="dot" cx="116" cy="100" r="5.5"/>',
    live: '<path class="pulse-line" d="M72 101h13l6-15 10 30 8-22 5 7h14"/>',
    paused: '<path d="M92 85v30M108 85v30"/>',
    stopping: '<rect x="88" y="88" width="24" height="24" rx="5"/>',
  };

  // ---------- helpers ----------

  function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  function setText(node, text, animate = true) {
    if (node.textContent === text) return;
    node.textContent = text;
    if (animate && !reduceMotion) {
      node.classList.remove("swap");
      void node.offsetWidth;
      node.classList.add("swap");
    }
  }

  function human(n) {
    if (n == null) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return i === 0 ? `${n.toFixed(0)} ${units[i]}` : `${n.toFixed(n < 10 ? 2 : 1)} ${units[i]}`;
  }

  function hms(sec) {
    sec = Math.max(0, Math.floor(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }

  function ago(t) {
    const d = Math.max(0, Date.now() / 1000 - t);
    if (d < 10) return "just now";
    if (d < 60) return `${Math.floor(d)} s ago`;
    if (d < 3600) return `${Math.floor(d / 60)} min ago`;
    if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
    return `${Math.floor(d / 86400)} d ago`;
  }

  function shortGpu(name) {
    return (name || "GPU").replace(/^NVIDIA\s+/i, "").replace(/^GeForce\s+/i, "");
  }

  function profileLabel(models) {
    return models.map((m) => MODEL_NAMES[m] || m).join(" + ");
  }

  // ---------- requests ----------

  async function post(path, payload = {}) {
    pending = true;
    render();
    try {
      const r = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.ok) toast(j.error || `Request failed (${r.status})`, "danger");
      return j;
    } catch {
      toast("The dashboard server is not responding.", "danger");
      return {};
    } finally {
      pending = false;
      render();
    }
  }

  const act = {
    start: () => post("/api/start", { profile: chosenProfile }),
    stop: () => post("/api/stop"),
    cancel: () => {
      if (S.job && S.job.reusing) return act.stop();
      confirmDialog({
        title: "Cancel the launch?",
        body: "The pod being set up is terminated, so it stops billing.",
        ok: "Cancel launch", cancel: "Keep going",
        onOk: () => post("/api/stop"),
      });
    },
    terminate: (noSync = false) => confirmDialog({
      title: "Terminate the pod?",
      body: noSync
        ? "Files not yet copied home are lost. Everything on the pod is deleted and billing stops."
        : "Anything not yet copied home is saved first. Then everything on the pod is deleted and billing stops.",
      ok: noSync ? "Terminate without copying" : "Terminate",
      onOk: () => post("/api/terminate", { no_sync: noSync }),
    }),
    open: () => S && window.open(S.comfy_url, "_blank", "noopener"),
  };

  // ---------- rendering ----------

  function render() {
    if (!S) return;
    const s = S;
    if (body.dataset.state !== s.state) {
      const prev = body.dataset.state;
      body.dataset.state = s.state;
      onStateChange(prev, s.state);
    }
    renderHero(s);
    renderActions(s);
    renderProfiles(s);
    renderSteps(s);
    renderPod(s);
    renderSync(s);
    renderOutputs(s);
  }

  function heroCopy(s) {
    const job = s.job;
    switch (s.state) {
      case "launching": {
        if (job.stopping) return ["Cancelling", "Cancelling", "Stopping the launch; the pod is terminated next."];
        const [title, sub] = PHASE_COPY[job.phase] || ["Starting", () => "Preparing to launch."];
        return [job.reusing ? "Reconnecting" : "Launching", title, sub(s)];
      }
      case "live":
        if (job.stopping) return ["Live", "Stopping sync", "Closing the tunnel. The pod keeps running."];
        return s.comfy
          ? ["Live", "Ready", `ComfyUI is at localhost:${new URL(s.comfy_url).port}. Files sync both ways every 3 s.`]
          : ["Live", "Starting ComfyUI", "The tunnel is up. Waiting for ComfyUI to answer on the pod."];
      case "paused":
        if (s.external_sync) {
          return ["Pod running", "Syncing elsewhere", "A sync is running in a terminal window. Close it to control the pod from here."];
        }
        return ["Pod running · not synced", "Paused", "The pod is still billing. Resume sync to use ComfyUI, or terminate it."];
      case "stopping": {
        const phase = job && job.name === "down" ? job.phase : "stop";
        if (phase === "copy") return ["Shutting down", "Saving outputs", "Copying anything not yet saved to your sync folder."];
        if (phase === "terminate") return ["Shutting down", "Terminating", "Deleting the pod. Billing stops in a moment."];
        return ["Shutting down", "Stopping sync", "Closing the tunnel before terminating the pod."];
      }
      default:
        return ["Standing by", "Idle", "No pod running. Nothing is billing."];
    }
  }

  let glyphState = null;
  function renderHero(s) {
    const [eyebrow, title, sub] = heroCopy(s);
    setText($("eyebrow"), eyebrow, false);
    setText($("stateTitle"), title);
    setText($("stateSub"), sub);
    if (glyphState !== s.state) {
      glyphState = s.state;
      const g = $("orbGlyph");
      g.innerHTML = GLYPHS[s.state] || "";
      g.classList.remove("enter");
      void g.getBoundingClientRect();
      g.classList.add("enter");
    }
    // Inline error with its follow-up actions.
    let box = document.querySelector(".hero-error");
    if (s.error) {
      const key = s.error.message + "|" + s.error.action;
      if (!box || box.dataset.key !== key) {
        box && box.remove();
        box = el("div", "hero-error");
        box.dataset.key = key;
        box.innerHTML = `<div class="msg">${esc(s.error.message)}</div><div class="btns"></div>`;
        const btns = box.querySelector(".btns");
        if (s.error.action === "force_down") {
          const b = el("button", "btn danger xs", "Terminate without copying");
          b.onclick = () => act.terminate(true);
          btns.append(b);
        }
        const d = el("button", "btn ghost xs", "Dismiss");
        d.onclick = () => post("/api/dismiss");
        btns.append(d);
        $("stateSub").after(box);
      }
    } else if (box) {
      box.remove();
    }
  }

  function button(cls, label, icon, onClick, opts = {}) {
    const b = el("button", `btn ${cls}`);
    b.type = "button";
    b.innerHTML = (opts.spin ? '<span class="spinner"></span>' : icon ? ICONS[icon] : "") + `<span>${esc(label)}</span>`;
    b.disabled = !!opts.disabled;
    if (onClick) b.onclick = onClick;
    return b;
  }

  let actionsKey = "";
  function renderActions(s) {
    const job = s.job || {};
    const busy = pending;
    const list = [];
    switch (s.state) {
      case "idle":
        list.push(["primary beckon", "Launch pod", "play", act.start, { disabled: busy }]);
        break;
      case "launching":
        list.push(job.stopping
          ? ["ghost", "Cancelling", null, null, { disabled: true, spin: true }]
          : ["ghost", "Cancel", "x", act.cancel, { disabled: busy }]);
        break;
      case "live":
        list.push(s.comfy
          ? ["primary", "Open ComfyUI", "open", act.open, {}]
          : ["primary", "Waiting for ComfyUI", null, null, { disabled: true, spin: true }]);
        list.push(job.stopping
          ? ["ghost", "Stopping", null, null, { disabled: true, spin: true }]
          : ["ghost", "Stop sync", "pause", act.stop, { disabled: busy }]);
        list.push(["danger", "Terminate", "stop", () => act.terminate(), { disabled: busy || job.stopping }]);
        break;
      case "paused":
        list.push(["primary", "Resume sync", "resume", act.start, { disabled: busy || s.external_sync }]);
        list.push(["danger", "Terminate", "stop", () => act.terminate(), { disabled: busy }]);
        break;
      case "stopping":
        list.push(["danger", "Terminating", null, null, { disabled: true, spin: true }]);
        break;
    }
    const key = JSON.stringify(list.map(([c, l, i, , o]) => [c, l, i, o]));
    if (key === actionsKey) return;
    actionsKey = key;
    const box = $("actions");
    box.replaceChildren(...list.map(([c, l, i, f, o]) => button(c, l, i, f, o)));
  }

  let profilesKey = "";
  function renderProfiles(s) {
    if (!chosenProfile) chosenProfile = s.profile;
    const seg = $("profiles");
    const key = JSON.stringify(s.profiles);
    if (key !== profilesKey) {
      profilesKey = key;
      seg.replaceChildren(el("span", "seg-thumb"));
      for (const p of s.profiles) {
        const models = profileLabel(p.models);
        const sub = models.toLowerCase() === p.name.toLowerCase() ? "" : `<span class="seg-models">${esc(models)}</span>`;
        const b = el("button", "", `<span class="seg-name">${esc(p.name)}</span>${sub}`);
        b.type = "button";
        b.setAttribute("role", "radio");
        b.dataset.name = p.name;
        b.onclick = () => { chosenProfile = p.name; renderProfiles(S); };
        seg.append(b);
      }
    }
    seg.classList.toggle("locked", !["idle", "paused"].includes(s.state) || pending);
    let active = null;
    for (const b of seg.querySelectorAll("button")) {
      const on = b.dataset.name === chosenProfile;
      b.setAttribute("aria-checked", on);
      if (on) active = b;
    }
    const thumb = seg.querySelector(".seg-thumb");
    if (active) {
      thumb.style.width = `${active.offsetWidth}px`;
      thumb.style.transform = `translateX(${active.offsetLeft}px)`;
    }
  }

  let stepsKey = "";
  function renderSteps(s) {
    const job = s.job;
    let defs = START_STEPS, current = -1, live = false, skipTo = -1;
    const details = {};
    if (s.state === "stopping") {
      defs = DOWN_STEPS;
      const phase = job && job.name === "down" ? (job.phase || "copy") : "stop";
      current = DOWN_STEPS.findIndex(([k]) => k === phase);
    } else if (s.state === "launching") {
      current = START_STEPS.findIndex(([k]) => k === job.phase);
      if (current < 0) current = 0;
      if (job.reusing) skipTo = 3;
      if (job.phase === "models" && job.models) details.models = `${job.models.i}/${job.models.n}`;
      if (job.phase === "launch" && job.detail === "waiting for a free GPU") details.launch = "waiting";
    } else if (s.state === "live") {
      current = START_STEPS.length - 1;
      live = true;
      details.connect = s.comfy ? "live" : "tunnel up";
      if (job && job.reusing) skipTo = 3;
    } else if (s.state === "paused") {
      current = START_STEPS.length - 1;
      details.connect = "off";
    }
    const ol = $("steps");
    const key = defs.map(([k]) => k).join();
    if (key !== stepsKey) {
      stepsKey = key;
      ol.replaceChildren(...defs.map(([k, label]) => {
        const li = el("li", "step", `<span class="node">${ICONS.check}</span><span class="rail"><i></i></span><span class="lbl">${esc(label)}</span><span class="det"></span>`);
        li.dataset.key = k;
        return li;
      }));
    }
    [...ol.children].forEach((li, i) => {
      const skip = i <= skipTo;
      const paused = s.state === "paused" && i === current;
      li.className = "step" + (skip ? " skip" : i < current ? " done"
        : i === current ? (live ? " live done" : paused ? "" : " active") : "");
      const det = details[li.dataset.key] || (skip ? "reused" : "");
      li.querySelector(".det").textContent = det;
    });
  }

  function renderPod(s) {
    const p = s.pod;
    let tone = "idle", label = "None";
    if (p) {
      if (p.gone) { tone = "danger"; label = "Gone"; }
      else if (p.status === "RUNNING" && p.ready) { tone = "ok"; label = "Running"; }
      else if (p.status === "RUNNING" || !p.status) { tone = "busy"; label = "Starting"; }
      else { tone = "warn"; label = p.status.toLowerCase(); }
    }
    const pill = $("podPill");
    pill.dataset.tone = tone;
    setText(pill, label, false);
    $("rate").textContent = p && p.cost_per_hr ? `$${Number(p.cost_per_hr).toFixed(2)} / hr` : "— / hr";
    const gpu = $("gpu");
    gpu.textContent = p && p.gpu_name ? shortGpu(p.gpu_name) : `${shortGpu(s.gpu)}`;
    gpu.classList.toggle("dim", !(p && p.gpu_name));
    const host = $("host");
    const bits = [];
    if (p && p.ram_gb) bits.push(`${p.ram_gb} GB RAM`);
    if (p && p.vcpus) bits.push(`${p.vcpus} vCPU`);
    host.textContent = bits.length ? bits.join(" · ") : p && p.location ? p.location : "—";
    host.title = p && p.location ? p.location : "";
    host.classList.toggle("dim", !bits.length);
    $("podId").textContent = p ? p.id : "—";
    $("podId").classList.toggle("dim", !p);
    tick();
  }

  function podStart(p) {
    if (!p) return null;
    if (p.created) return p.created;
    if (p.started_at) {
      const t = Date.parse(String(p.started_at).replace(" ", "T"));
      if (!Number.isNaN(t)) return t / 1000;
    }
    return null;
  }

  // Once a second (and on render): cost meter, uptime, heartbeat age.
  function tick() {
    if (!S) return;
    const p = S.pod && !S.pod.gone ? S.pod : null;
    const start = podStart(p);
    const elapsed = start ? Date.now() / 1000 - start : 0;
    const rate = p && p.cost_per_hr ? Number(p.cost_per_hr) : 0;
    const cost = (rate * elapsed) / 3600;
    const [whole, frac] = cost.toFixed(4).split(".");
    $("cost").innerHTML = `${whole}.${frac.slice(0, 2)}<span class="frac">${frac.slice(2)}</span>`;
    $("uptime").textContent = start ? hms(elapsed) : "—";
    $("uptime").classList.toggle("dim", !start);

    const watching = S.state === "live";
    const dot = $("beatDot");
    dot.classList.toggle("on", watching);
    $("beatText").textContent = watching
      ? (lastBeat ? `Pod checked ${ago(lastBeat).replace("just now", "just now")}` : "Watching the pod")
      : S.external_sync ? "Synced by a terminal window" : "Not watching the pod";

    if (transfer && Date.now() - transferAt > 2500) settleTransfer();
  }

  function renderSync(s) {
    let tone = "idle", label = "Off";
    if (s.state === "live") {
      if (transfer) { tone = "busy"; label = transfer.direction === "up" ? "Uploading" : "Downloading"; }
      else { tone = "ok"; label = "In sync"; }
    } else if (s.state === "launching" && s.job.phase === "connect") { tone = "busy"; label = "Connecting"; }
    else if (s.external_sync) { tone = "warn"; label = "Terminal"; }
    const pill = $("syncPill");
    pill.dataset.tone = tone;
    setText(pill, label, false);
    if (!transfer && Date.now() - transferAt > 2500) {
      $("xferName").textContent = s.state === "live" ? "Everything is up to date" : "Sync is off";
      if (s.state !== "live") $("xferBytes").textContent = "—";
    }
    renderFolders(s.folders || {});
  }

  const FOLDER_ARROWS = { pull: "↓", push: "↑", both: "⇅" };
  const folderCounts = {};
  function renderFolders(folders) {
    const ul = $("folders");
    const names = Object.keys(folders);
    if (ul.children.length !== names.length) {
      ul.replaceChildren(...names.map((n) => {
        const li = el("li", "folder", `<span class="n">0</span><span class="d"></span><span class="k">${esc(n)}</span>`);
        li.dataset.name = n;
        return li;
      }));
    }
    for (const li of ul.children) {
      const f = folders[li.dataset.name];
      const n = li.querySelector(".n");
      li.querySelector(".d").textContent = FOLDER_ARROWS[f.direction] || "";
      li.title = { pull: "pod → this PC", push: "this PC → pod", both: "both ways" }[f.direction] || "";
      if (folderCounts[li.dataset.name] !== undefined && folderCounts[li.dataset.name] !== f.count) {
        n.classList.remove("bump");
        void n.offsetWidth;
        n.classList.add("bump");
      }
      folderCounts[li.dataset.name] = f.count;
      n.textContent = f.count;
    }
  }

  function onTransfer(ev) {
    transferAt = Date.now();
    const box = $("xfer");
    if (ev.final) {
      transfer = null;
      box.classList.remove("active");
      $("xferBar").style.width = "100%";
      $("xferPct").textContent = "100%";
      $("xferSpeed").textContent = "";
      $("xferBytes").textContent = `${ev.files} file${ev.files === 1 ? "" : "s"} · ${human(ev.total)} ${ev.direction === "up" ? "sent" : "saved"}`;
      $("xferName").textContent = `${ev.folder} done`;
      setTimeout(settleTransfer, 1800);
      renderSync(S);
      return;
    }
    transfer = ev;
    box.classList.add("active");
    box.classList.remove("settled");
    $("xferDir").classList.toggle("up", ev.direction === "up");
    const pct = Math.min(100, (ev.done / Math.max(ev.total, 1)) * 100);
    $("xferBar").style.width = `${pct}%`;
    $("xferPct").textContent = `${pct.toFixed(0)}%`;
    $("xferName").textContent = ev.name || ev.folder;
    $("xferName").title = ev.name || "";
    $("xferBytes").textContent = `${human(ev.done)} / ${human(ev.total)} · ${ev.files_done}/${ev.files} ${ev.folder}`;
    $("xferSpeed").textContent = `${human(ev.speed)}/s`;
    renderSync(S);
  }

  function settleTransfer() {
    if (transfer && Date.now() - transferAt < 2500) return;
    transfer = null;
    const box = $("xfer");
    box.classList.remove("active");
    box.classList.add("settled");
    $("xferPct").textContent = "";
    $("xferSpeed").textContent = "";
    if (S) renderSync(S);
    if (S && S.state !== "live") $("xferBytes").textContent = "—";
  }

  // ---------- outputs ----------

  function renderOutputs(s) {
    const strip = $("strip");
    const outs = s.outputs || [];
    $("outToday").textContent = s.today ? `${s.today} today` : "";
    if (!outs.length) {
      if (!strip.querySelector(".empty")) {
        strip.replaceChildren(el("div", "empty",
          `${ICONS.image}<div><b>No outputs yet</b>Renders are copied to <code>sync/outputs/&lt;date&gt;/</code> as soon as ComfyUI writes them.</div>`));
      }
      firstOutputs = false;
      return;
    }
    strip.querySelector(".empty")?.remove();
    const existing = new Map([...strip.children].map((t) => [t.dataset.path, t]));
    const wanted = outs.map((o) => o.path);
    for (const [path, t] of existing) if (!wanted.includes(path)) t.remove();
    outs.forEach((o, i) => {
      let t = existing.get(o.path);
      if (!t) {
        t = makeTile(o);
        if (!firstOutputs && !seenOutputs.has(o.path)) t.classList.add("new");
      }
      seenOutputs.add(o.path);
      if (strip.children[i] !== t) strip.insertBefore(t, strip.children[i] || null);
      t.querySelector(".ago").textContent = ago(o.mtime);
    });
    firstOutputs = false;
  }

  function makeTile(o) {
    const t = el("div", "tile");
    t.dataset.path = o.path;
    t.title = o.path;
    const src = "/media/" + o.path.split("/").map(encodeURIComponent).join("/");
    let media;
    if (o.kind === "video") {
      media = el("video");
      media.muted = true; media.loop = true; media.playsInline = true; media.preload = "metadata";
      media.src = src + "#t=0.1";
      media.addEventListener("loadeddata", () => media.classList.add("loaded"));
      t.addEventListener("mouseenter", () => media.play().catch(() => {}));
      t.addEventListener("mouseleave", () => media.pause());
      t.append(el("span", "badge", "video"));
    } else {
      media = el("img");
      media.loading = "lazy"; media.decoding = "async"; media.alt = o.name;
      media.addEventListener("load", () => media.classList.add("loaded"));
      media.src = src;
    }
    t.prepend(media);
    t.append(el("div", "cap", `<span class="nm">${esc(o.name)}</span><span class="ago"></span>`));
    t.onclick = () => window.open(src, "_blank", "noopener");
    return t;
  }

  // ---------- activity log ----------

  const log = $("log");
  const MAX_LINES = 400;
  function fmtTime(t) {
    return new Date(t * 1000).toLocaleTimeString([], { hour12: false });
  }
  function logLine(e, animate) {
    const last = log.lastElementChild;
    if (e.replace && last && last.classList.contains("replace")) {
      last.querySelector(".x").textContent = e.text;
      last.querySelector(".t").textContent = fmtTime(e.t);
    } else {
      const ln = el("div", `ln ${e.level}${e.replace ? " replace" : ""}${animate ? " new" : ""}`);
      ln.innerHTML = `<span class="t"></span><span class="x"></span>`;
      ln.querySelector(".t").textContent = fmtTime(e.t);
      ln.querySelector(".x").textContent = e.text;
      log.append(ln);
      while (log.children.length > MAX_LINES) log.firstElementChild.remove();
    }
    if (follow) log.scrollTop = log.scrollHeight;
  }
  log.addEventListener("scroll", () => {
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
    if (atBottom !== follow) setFollow(atBottom);
  });
  function setFollow(on) {
    follow = on;
    const b = $("follow");
    b.setAttribute("aria-pressed", on);
    b.textContent = on ? "Following" : "Paused · jump to latest";
    if (on) log.scrollTop = log.scrollHeight;
  }
  $("follow").onclick = () => setFollow(!follow);

  // ---------- sparkline of transfer speed ----------

  const spark = $("spark");
  const ctx = spark.getContext("2d");
  const SAMPLES = 120;  // 0.5 s apart: one minute
  const samples = new Array(SAMPLES).fill(0);
  let sparkPhase = 0;
  function sampleSpeed() {
    const active = transfer && Date.now() - transferAt < 1500;
    samples.push(active ? transfer.speed : 0);
    samples.shift();
  }
  function drawSpark() {
    const dpr = window.devicePixelRatio || 1;
    const w = spark.clientWidth, h = spark.clientHeight;
    if (!w || !h) return;
    if (spark.width !== Math.round(w * dpr) || spark.height !== Math.round(h * dpr)) {
      spark.width = Math.round(w * dpr);
      spark.height = Math.round(h * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const style = getComputedStyle(body);
    const hue = parseFloat(style.getPropertyValue("--h")) || 200;
    const sat = style.getPropertyValue("--s").trim() || "50%";
    const color = (l, a) => `hsl(${hue} ${sat} ${l}% / ${a})`;
    const max = Math.max(...samples, 1024 * 1024);
    const pad = 4, baseY = h - 2;
    // Smooth scroll between samples.
    const step = w / (SAMPLES - 2);
    const shift = (sparkPhase % 1) * step;
    const pts = samples.map((v, i) => [i * step - shift, baseY - (v / max) * (h - pad * 2)]);

    // Faint guide lines.
    ctx.strokeStyle = "rgba(255,255,255,0.05)";
    ctx.lineWidth = 1;
    for (const f of [0.33, 0.66]) {
      ctx.beginPath(); ctx.moveTo(0, Math.round(h * f) + 0.5); ctx.lineTo(w, Math.round(h * f) + 0.5); ctx.stroke();
    }
    const path = new Path2D();
    path.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) {
      const [x0, y0] = pts[i - 1], [x1, y1] = pts[i];
      const mx = (x0 + x1) / 2;
      path.bezierCurveTo(mx, y0, mx, y1, x1, y1);
    }
    const fill = new Path2D(path);
    fill.lineTo(pts[pts.length - 1][0], h);
    fill.lineTo(pts[0][0], h);
    fill.closePath();
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, color(60, 0.35));
    grad.addColorStop(1, color(60, 0));
    ctx.fillStyle = grad;
    ctx.fill(fill);
    ctx.strokeStyle = color(66, 0.9);
    ctx.lineWidth = 1.6;
    ctx.stroke(path);
    // Live head.
    const [hx, hy] = pts[pts.length - 1];
    const live = S && S.state === "live";
    const pulse = 0.5 + 0.5 * Math.sin(performance.now() / 380);
    ctx.fillStyle = color(70, live ? 0.18 + 0.2 * pulse : 0.08);
    ctx.beginPath(); ctx.arc(Math.min(hx, w - 4), hy, 4 + 3 * pulse, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = color(72, live ? 1 : 0.4);
    ctx.beginPath(); ctx.arc(Math.min(hx, w - 4), hy, 2.6, 0, Math.PI * 2); ctx.fill();
    // Peak label.
    const peak = Math.max(...samples);
    if (peak > 0) {
      ctx.fillStyle = "rgba(255,255,255,0.45)";
      ctx.font = `11px ${getComputedStyle(document.documentElement).getPropertyValue("--mono")}`;
      ctx.textAlign = "left";
      ctx.fillText(`peak ${human(peak)}/s`, 0, 11);
    }
  }
  let lastSample = performance.now();
  function frame(now) {
    const dt = now - lastSample;
    if (dt >= 500) { sampleSpeed(); lastSample = now; }
    sparkPhase = Math.floor(sparkPhase) + Math.min(dt / 500, 1);
    drawSpark();
    requestAnimationFrame(frame);
  }

  // ---------- heartbeat, state changes, toasts ----------

  function onHeartbeat() {
    lastBeat = Date.now() / 1000;
    const dot = $("beatDot");
    dot.classList.remove("flash");
    void dot.offsetWidth;
    dot.classList.add("flash");
    if (reduceMotion || document.hidden) return;
    const r = el("span", "ripple");
    $("ripples").append(r);
    r.addEventListener("animationend", () => r.remove());
  }

  let prevComfy = false;
  function onStateChange(prev, next) {
    if (!prev || prev === next) return;
    if (next === "live") toast("Pod is live. Files are syncing.", "ok");
    if (prev === "stopping" && next === "idle") toast("Pod terminated. Billing stopped.", "ok");
    if (prev === "live" && next === "paused") toast("Sync stopped. The pod is still running and billing.", "warn");
  }

  function toast(message, tone = "info", life = 5000) {
    const t = el("div", "toast");
    t.dataset.tone = tone;
    t.style.setProperty("--life", `${life}ms`);
    t.textContent = message;
    $("toasts").append(t);
    const kill = () => { t.classList.add("out"); setTimeout(() => t.remove(), 350); };
    const timer = setTimeout(kill, life);
    t.onclick = () => { clearTimeout(timer); kill(); };
    while ($("toasts").children.length > 4) $("toasts").firstElementChild.remove();
  }

  // ---------- modal ----------

  function confirmDialog({ title, body: text, ok = "Confirm", cancel = "Cancel", onOk }) {
    const modal = $("modal");
    $("modalTitle").textContent = title;
    $("modalBody").textContent = text;
    $("modalIcon").innerHTML = ICONS.warn;
    const okBtn = $("modalOk"), cancelBtn = $("modalCancel");
    okBtn.className = "btn danger solid";
    okBtn.textContent = ok;
    cancelBtn.textContent = cancel;
    modal.hidden = false;
    const close = () => { modal.hidden = true; document.removeEventListener("keydown", onKey); };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    okBtn.onclick = () => { close(); onOk(); };
    cancelBtn.onclick = close;
    modal.onclick = (e) => { if (e.target === modal) close(); };
    cancelBtn.focus();
  }

  // ---------- header controls ----------

  $("openFolder").onclick = () => post("/api/open-folder");
  $("quit").onclick = () => confirmDialog({
    title: "Quit the dashboard?",
    body: S && S.pod
      ? "Syncing stops. The pod keeps running and billing until you terminate it."
      : "You can open it again any time with dashboard.cmd.",
    ok: "Quit",
    onOk: () => post("/api/quit"),
  });

  // Pointer spotlight on cards.
  for (const card of document.querySelectorAll(".card")) {
    card.addEventListener("pointermove", (e) => {
      const r = card.getBoundingClientRect();
      card.style.setProperty("--mx", `${e.clientX - r.left}px`);
      card.style.setProperty("--my", `${e.clientY - r.top}px`);
    });
  }

  // ---------- live connection ----------

  function connect() {
    const conn = $("conn");
    const es = new EventSource("/api/events");
    es.onopen = () => { conn.className = "conn ok"; $("connText").textContent = "Connected"; };
    es.onerror = () => { conn.className = "conn bad"; $("connText").textContent = "Reconnecting"; };
    es.addEventListener("state", (e) => {
      const next = JSON.parse(e.data);
      if (next.error && (!S || !S.error || S.error.message !== next.error.message)) {
        toast(next.error.message, "danger", 8000);
      }
      if (next.comfy && !prevComfy && S) toast("ComfyUI is ready.", "ok");
      prevComfy = next.comfy;
      S = next;
      render();
    });
    es.addEventListener("logs", (e) => {
      log.replaceChildren();
      for (const entry of JSON.parse(e.data)) logLine(entry, false);
    });
    es.addEventListener("log", (e) => logLine(JSON.parse(e.data), true));
    es.addEventListener("transfer", (e) => onTransfer(JSON.parse(e.data)));
    es.addEventListener("heartbeat", onHeartbeat);
    es.addEventListener("bye", () => {
      es.close();
      $("offline").hidden = false;
      conn.className = "conn bad";
      $("connText").textContent = "Closed";
    });
  }

  addEventListener("resize", () => S && renderProfiles(S));
  setInterval(() => { tick(); if (S) renderOutputsAges(); }, 1000);
  function renderOutputsAges() {
    for (const t of $("strip").querySelectorAll(".tile")) {
      const o = (S.outputs || []).find((x) => x.path === t.dataset.path);
      if (o) t.querySelector(".ago").textContent = ago(o.mtime);
    }
  }

  connect();
  requestAnimationFrame(frame);
  requestAnimationFrame(() => body.classList.remove("booting"));
})();
