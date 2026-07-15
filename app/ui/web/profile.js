/* Character profile (5.5d) — the container that finally makes the image
   pipeline operable from the window. Reached from a Library card's "Open";
   holds the identity, promotion, catalog, on-demand-posing and footprint
   panels over one saved character. Every heavy op (avatar candidates,
   bootstrap, train, catalog, matte, on-demand) runs as a background JOB
   (window.Jobs) with progress + cancel — never a synchronous image_* bridge.
   Generated frames are shown via image_frame_thumbnail (data URIs; the CSP
   forbids reading disk paths directly). All backend access goes through
   window.pywebview.api. */

"use strict";

window.Profile = (function () {
  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  let cid = null;
  let data = null;          // aggregated status snapshot
  let busy = false;         // a heavy job is running (single GPU slot, §3)
  let confirmDelete = false;
  // sub-state that must survive a re-render
  const vetSelection = new Set();   // bootstrap candidate ids checked for training
  const pose = { expression: "", pose: "", outfit: "" };
  let lastRender = null;    // {label, path} of the most recent identity/pose frame
  let candidates = null;    // avatar-candidate frames awaiting a reference pick
  let identityScale = 0.45; // 3b plus-band default (0.3–0.6)
  let bootBatch = 64;       // bootstrap batch size (backend clamps [1, 256])
  let feedbackMsg = null;   // {text, isError} — survives a full re-render
  const thumbCache = new Map();     // path -> data URI | null

  // ------------------------------------------------------------ backend calls

  async function call(name, ...args) {
    try {
      const res = await window.pywebview.api[name](...args);
      return res || { ok: false, error: "empty response" };
    } catch (err) {
      return { ok: false, error: String(err) };
    }
  }

  function fmtBytes(n) {
    if (!Number.isFinite(n) || n < 0) return "—";
    if (n < 1024) return n + " B";
    const units = ["KB", "MB", "GB", "TB"];
    let v = n;
    for (const u of units) {
      v /= 1024;
      if (v < 1024 || u === "TB")
        return (v >= 100 ? Math.round(v) : v.toFixed(1)) + " " + u;
    }
  }

  // Lazily fetch + cache a data-URI thumbnail for a character-owned frame.
  // Cache key includes the size: the same frame is requested small in a grid
  // and large in the zoom overlay, and the two must not collide.
  async function loadThumb(path, img, px) {
    if (!path) return;
    const size = px || 384;
    const key = path + "@" + size;
    if (thumbCache.has(key)) {
      const uri = thumbCache.get(key);
      if (uri) { img.src = uri; img.hidden = false; }
      return;
    }
    const res = await call("image_frame_thumbnail", cid, path, size);
    const uri = res && res.ok ? res.thumbnail : null;
    thumbCache.set(key, uri);
    if (uri) { img.src = uri; img.hidden = false; }
  }

  function thumbTile(path, cls, px) {
    const tile = el("div", "pf-thumb" + (cls ? " " + cls : ""));
    const img = el("img");
    img.hidden = true;
    img.alt = "";
    tile.appendChild(img);
    loadThumb(path, img, px);
    return tile;
  }

  // Full-size preview overlay (5.5 acceptance: 256 px tiles were too small to
  // judge identity on). Click anywhere / Esc to dismiss.
  function zoomOverlay(path) {
    const overlay = el("div", "pf-zoom");
    const img = el("img");
    img.hidden = true;
    img.alt = "";
    overlay.appendChild(img);
    const close = () => {
      overlay.remove();
      document.removeEventListener("keydown", onKey);
    };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    overlay.addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    document.body.appendChild(overlay);
    loadThumb(path, img, 1024);
  }

  // A small magnifier riding a grid cell — opens the zoom overlay WITHOUT
  // triggering the cell's own click action (pick / toggle).
  function zoomButton(path) {
    const z = el("button", "pf-zoom-btn", "⤢");
    z.type = "button";
    z.title = "View full size";
    z.addEventListener("click", (e) => {
      e.stopPropagation();
      zoomOverlay(path);
    });
    return z;
  }

  // ------------------------------------------------------------- data load

  async function refresh() {
    if (!cid) return;
    const [record, reference, lora, boot, catalog, matte, cache, states] =
      await Promise.all([
        call("library_get", cid),
        call("image_reference_status", cid),
        call("image_lora_status", cid),
        call("image_bootstrap_status", cid),
        call("image_catalog_status", cid),
        call("image_matte_status", cid),
        call("image_cache_status", cid),
        call("image_catalog_states", cid),
      ]);
    data = { record, reference, lora, boot, catalog, matte, cache, states };
    // prune vet selection to still-in-grid candidates
    const proposed = new Set(
      (boot.ok ? boot.proposed || [] : []).map((c) => c.candidate_id));
    for (const id of [...vetSelection]) if (!proposed.has(id)) vetSelection.delete(id);
    // Already-vetted candidates arrive flagged and PRE-CHECKED: confirm
    // REPLACES the vetted set, so keeping them selected by default means a
    // later confirm can only shrink the set when the user UNchecks (5.5 F1 —
    // prior picks were silently dropped).
    for (const c of (boot.ok ? boot.proposed || [] : []))
      if (c.confirmed) vetSelection.add(c.candidate_id);
    // default the pose picker to the first of each dimension
    if (states.ok) {
      if (!pose.expression && states.expressions[0])
        pose.expression = states.expressions[0].id;
      if (!pose.pose && states.poses[0]) pose.pose = states.poses[0].id;
      if (!pose.outfit && states.outfits[0]) pose.outfit = states.outfits[0].id;
    }
  }

  // -------------------------------------------------------- job orchestration

  // Start a heavy op as a job: disable the rest of the profile, mount the
  // progress+cancel widget, and on completion refresh + re-render.
  function startJob(cfg) {
    if (busy) return;
    busy = true;
    render();
    window.Jobs.mount($("profile-job"), {
      kind: cfg.kind, targetId: cid, options: cfg.options || {},
      label: cfg.label,
      async onDone(st) {
        busy = false;
        if (cfg.onDone) { try { await cfg.onDone(st); } catch (_) {} }
        // Clear the widget after a short beat so the terminal line is seen,
        // then refresh the whole profile from disk.
        await refresh();
        render();
      },
    });
  }

  function feedback(text, isError) {
    // Persist in state: an action calls feedback() then finishes with a full
    // render(), which rebuilds the feedback node — the message must survive it.
    feedbackMsg = text ? { text, isError: !!isError } : null;
    applyFeedback();
  }

  function applyFeedback() {
    const node = $("profile-feedback");
    if (!node) return;
    node.className = "feedback" +
      (feedbackMsg ? (feedbackMsg.isError ? " error" : " ok") : "");
    node.textContent = feedbackMsg ? feedbackMsg.text : "";
  }

  // A non-job action (set/clear reference, confirm-vetted, clear, delete).
  async function act(fn) {
    if (busy) return;
    busy = true;
    render();
    try { await fn(); }
    finally { busy = false; await refresh(); render(); }
  }

  // --------------------------------------------------------------- panels

  function headerCard() {
    const card = $("profile-header");
    card.textContent = "";
    const rec = data.record;

    const top = el("div", "pf-head-row");
    const back = el("button", "ghost", "‹ Library");
    back.type = "button";
    back.addEventListener("click", () => window.AppNav.show("library"));
    top.appendChild(back);
    card.appendChild(top);

    if (!rec.ok) {
      card.appendChild(el("h1", null, "Unreadable character"));
      card.appendChild(el("div", "alert warn",
        `${rec.kind || "error"}: ${rec.error || "this record cannot be loaded"}` +
        " — it can still be deleted."));
      deleteRow(card);
      return;
    }

    const row = el("div", "pf-identity-row");
    if (data.reference.ok && data.reference.has_reference)
      row.appendChild(thumbTile(data.reference.reference, "big"));
    else {
      const ph = el("div", "pf-thumb big empty");
      ph.appendChild(el("span", null, "no reference"));
      row.appendChild(ph);
    }
    const info = el("div", "pf-info");
    const h = el("h1", null, rec.name);
    h.appendChild(el("span", "pf-age", ` · ${rec.age}`));
    info.appendChild(h);
    info.appendChild(el("div", "lib-meta", `id ${String(rec.id).slice(0, 12)}…`));

    const badges = el("div", "badges");
    if (data.lora.ok && data.lora.has_lora) badges.appendChild(el("span", "badge ok", "LoRA"));
    else if (data.reference.ok && data.reference.has_reference)
      badges.appendChild(el("span", "badge", "reference"));
    if (data.catalog.ok && data.catalog.has_catalog)
      badges.appendChild(el("span", "badge" + (data.catalog.stale ? " warn" : ""),
        `catalog ${data.catalog.frames}`));
    if (data.catalog.ok && data.catalog.stale) badges.appendChild(el("span", "badge warn", "stale"));
    if (data.cache.ok && data.cache.frames)
      badges.appendChild(el("span", "badge", `cache ${data.cache.frames}`));
    info.appendChild(badges);

    const fp = rec.footprint;
    if (fp)
      info.appendChild(el("div", "lib-footprint",
        `${fmtBytes(fp.total_bytes)} on disk — LoRA ${fmtBytes(fp.lora_bytes)}` +
        ` · catalog ${fmtBytes(fp.catalog_bytes)} · cache ${fmtBytes(fp.cache_bytes)}`));
    row.appendChild(info);
    card.appendChild(row);

    const actions = el("div", "pf-actions");
    const edit = el("button", "lib-btn", "Edit character");
    edit.disabled = busy;
    edit.addEventListener("click", () => {
      window.Creator.beginEdit(cid);
      window.AppNav.show("create");
    });
    actions.appendChild(edit);
    card.appendChild(actions);
    if (rec.issues && rec.issues.length) {
      const warn = el("div", "alert warn");
      warn.appendChild(el("div", null,
        "This record references options no longer loaded, or is missing part " +
        "of the render-identity minimum — edit to resolve:"));
      for (const line of rec.issues) warn.appendChild(el("div", "alert-line", line));
      card.appendChild(warn);
    }
  }

  function deleteRow(container) {
    const wrap = el("div", "pf-actions");
    if (confirmDelete) {
      const yes = el("button", "lib-btn danger", "Confirm delete");
      yes.disabled = busy;
      yes.addEventListener("click", () => act(async () => {
        const res = await call("library_delete", cid);
        if (res.ok) { window.AppNav.show("library"); window.Library.refresh(); }
        else feedback("Delete failed: " + (res.error || res.kind), true);
      }));
      const no = el("button", "lib-btn ghost", "Keep");
      no.disabled = busy;
      no.addEventListener("click", () => { confirmDelete = false; render(); });
      wrap.appendChild(yes);
      wrap.appendChild(no);
      wrap.appendChild(el("span", "lib-confirm-hint",
        "Deletes the record, reference, LoRA, catalog, and cache."));
    } else {
      const del = el("button", "lib-btn ghost", "Delete character…");
      del.disabled = busy;
      del.addEventListener("click", () => { confirmDelete = true; render(); });
      wrap.appendChild(del);
    }
    container.appendChild(wrap);
  }

  function panel(title, hint) {
    const card = el("section", "card pf-panel");
    card.appendChild(el("h2", null, title));
    if (hint) card.appendChild(el("p", "hint", hint));
    return card;
  }

  function jobButton(label, cfg, opts) {
    const b = el("button", "lib-btn" + (opts && opts.accent ? " accent" : ""), label);
    b.type = "button";
    b.disabled = busy || (opts && opts.disabled);
    if (opts && opts.title) b.title = opts.title;
    b.addEventListener("click", () => startJob(cfg));
    return b;
  }

  // -- identity panel: reference + IP-Adapter render + avatar candidates ------

  function identityPanel() {
    const p = panel("Identity",
      "Quick-create identity rides on a reference image steered by IP-Adapter " +
      "(§6). Generate avatar candidates and pick one, or render a steered look.");
    const ref = data.reference;
    const hasRef = ref.ok && ref.has_reference;

    p.appendChild(el("div", "pf-status",
      hasRef ? "Reference set." : "No identity reference yet."));

    const row = el("div", "pf-btn-row");
    // Avatar candidates (base job): the create-wizard reference step, also
    // here. A repeat click is a re-roll — fresh seeds, same record prompt
    // (edit the character to change the prompt; the live preview shows it).
    row.appendChild(jobButton(
      candidates && candidates.length
        ? "Re-roll candidates" : "Generate avatar candidates", {
      kind: "avatar", options: { count: 4 }, label: "Avatar candidates",
      onDone: (st) => {
        candidates = (window.Jobs.isSuccess(st) && st.result.candidates) || null;
      },
    }, { accent: !hasRef,
         title: candidates && candidates.length
           ? "Fresh seeds over the same record prompt"
           : "Render base candidates; pick one as the reference" }));

    if (hasRef) {
      const clearBtn = el("button", "lib-btn ghost", "Clear reference");
      clearBtn.disabled = busy;
      clearBtn.addEventListener("click", () => act(async () => {
        candidates = null;
        const res = await call("image_clear_reference", cid);
        feedback(res.ok ? "Reference cleared." :
          "Could not clear: " + (res.error || res.kind), !res.ok);
      }));
      row.appendChild(clearBtn);
    }
    p.appendChild(row);

    // Pick a candidate as the reference (populated after an avatar job).
    if (candidates && candidates.length) {
      const box = el("div", "pf-candidates");
      box.appendChild(el("div", "field-label",
        "Pick a candidate as the identity reference (⤢ views full size):"));
      const grid = el("div", "pf-grid wide");
      for (const c of candidates) {
        const cell = el("button", "pf-grid-cell");
        cell.type = "button";
        cell.disabled = busy;
        cell.appendChild(thumbTile(c.path, null, 512));
        cell.appendChild(zoomButton(c.path));
        cell.addEventListener("click", () => act(async () => {
          const res = await call("image_set_reference", cid, c.path);
          if (res.ok) { candidates = null; feedback("Reference set from candidate."); }
          else feedback("Could not set: " + (res.error || res.kind), true);
        }));
        grid.appendChild(cell);
      }
      box.appendChild(grid);
      p.appendChild(box);
    }

    // IP-Adapter steered render at the chosen scale. A single frame — quick on
    // hardware, engine-unavailable on the sandbox — so it runs as a direct
    // guarded call (never a raw synchronous HEAVY op; identity render is light).
    if (hasRef) {
      const scaleWrap = el("label", "pf-scale");
      scaleWrap.appendChild(el("span", null, `IP-Adapter scale`));
      const scale = el("input");
      scale.type = "range";
      scale.min = "0.3"; scale.max = "0.6"; scale.step = "0.05";
      scale.value = String(identityScale);
      scale.disabled = busy;
      const val = el("span", "pf-scale-val", identityScale.toFixed(2));
      scale.addEventListener("input", () => {
        identityScale = Number(scale.value);
        val.textContent = identityScale.toFixed(2);
      });
      scaleWrap.appendChild(scale);
      scaleWrap.appendChild(val);
      p.appendChild(scaleWrap);

      const genRow = el("div", "pf-btn-row");
      // The steered render loads the image model — route it through the job
      // runner (progress + cancel), never a synchronous bridge call.
      genRow.appendChild(jobButton("Render identity frame", {
        kind: "identity", options: { scale: identityScale },
        label: "Identity render",
        onDone: (st) => {
          if (window.Jobs.isSuccess(st))
            lastRender = { label: "Identity render", path: st.result.path };
        },
      }, { title: "One IP-Adapter-steered frame at the chosen scale" }));
      p.appendChild(genRow);
    }

    if (lastRender) {
      const prev = el("div", "pf-render");
      prev.appendChild(el("div", "field-label", lastRender.label));
      prev.appendChild(thumbTile(lastRender.path, "big"));
      p.appendChild(prev);
    }
    return p;
  }

  // -- promotion panel: bootstrap -> vetted grid -> confirm -> train ----------

  function promotionPanel() {
    const p = panel("Identity LoRA (promotion)",
      "Optional, explicit (§17): bootstrap a machine-vetted set from the " +
      "reference, approve the grid, then train. Bootstrap and train run as " +
      "jobs — bootstrap is ~15 min, training ~31 min on the target.");
    const lora = data.lora;
    const boot = data.boot;
    const hasRef = data.reference.ok && data.reference.has_reference;

    if (lora.ok && lora.has_lora) {
      p.appendChild(el("div", "pf-status", "Trained identity LoRA in place."));
      const prov = lora.provenance || {};
      p.appendChild(el("div", "lib-meta",
        `trigger ${lora.trigger || "?"} · ${prov.steps || "?"} steps · ` +
        `${fmtBytes(prov.lora_bytes || 0)}`));
      const row = el("div", "pf-btn-row");
      // Accumulates like the main flow; Discard (here too, when a bootstrap
      // exists) is the explicit fresh-start — without it, a fresh set would
      // need Clear LoRA first (session-5 review F5).
      row.appendChild(jobButton("Re-bootstrap", {
        kind: "bootstrap",
        options: { batch: bootBatch, total: bootBatch,
                   ...(boot.ok && boot.phase ? { more: true } : {}) },
        label: "Bootstrap",
      }, { disabled: !hasRef, title: hasRef ? "" : "Set a reference first" }));
      if (boot.ok && boot.phase) {
        const discard = el("button", "lib-btn ghost", "Discard candidates");
        discard.disabled = busy;
        discard.addEventListener("click", () => act(async () => {
          vetSelection.clear();
          const res = await call("image_clear_bootstrap", cid, "all");
          feedback(res.ok ? "Bootstrap candidates discarded." :
            "Could not discard: " + (res.error || res.kind), !res.ok);
        }));
        row.appendChild(discard);
      }
      row.appendChild(jobButton("Re-train LoRA", {
        kind: "train", options: {}, label: "Train LoRA",
      }));
      const clr = el("button", "lib-btn ghost", "Clear LoRA");
      clr.disabled = busy;
      clr.addEventListener("click", () => act(async () => {
        const res = await call("image_clear_lora", cid);
        feedback(res.ok ? "LoRA cleared — the character drops to IP-Adapter." :
          "Could not clear: " + (res.error || res.kind), !res.ok);
      }));
      row.appendChild(clr);
      p.appendChild(row);
      return p;
    }

    // No LoRA yet — the bootstrap→approve→train flow.
    if (!hasRef) {
      p.appendChild(el("div", "hint",
        "Set an identity reference above before bootstrapping."));
      return p;
    }
    const row = el("div", "pf-btn-row");
    const proposed = boot.ok ? boot.proposed || [] : [];
    // Accumulate whenever ANY bootstrap exists (5.5 acceptance fix: keying
    // off proposed.length sent more:false after a confirm emptied the grid,
    // silently WIPING the prior candidates). Discard is the explicit reset.
    const hasCandidates = !!(boot.ok && boot.phase);
    // Batch size — user-tunable so one run can net the whole training set
    // ("is there any reason the number cannot be changed?" — no).
    const batchWrap = el("label", "pf-batch");
    batchWrap.appendChild(el("span", null, "Batch"));
    const batchInput = el("input");
    batchInput.type = "number";
    batchInput.min = "1"; batchInput.max = "256"; batchInput.step = "1";
    batchInput.value = String(bootBatch);
    batchInput.disabled = busy;
    batchInput.addEventListener("change", () => {
      const v = Math.round(Number(batchInput.value));
      bootBatch = Number.isFinite(v) ? Math.min(256, Math.max(1, v)) : 64;
      batchInput.value = String(bootBatch);
    });
    batchWrap.appendChild(batchInput);
    row.appendChild(batchWrap);
    row.appendChild(jobButton(
      hasCandidates ? "Generate more candidates" : "Bootstrap candidates", {
        kind: "bootstrap",
        // total makes the progress bar determinate (done/total frames).
        options: { batch: bootBatch, total: bootBatch,
                   ...(hasCandidates ? { more: true } : {}) },
        label: "Bootstrap",
      }, { accent: !proposed.length,
           title: hasCandidates
             ? "Adds a fresh batch to the existing candidates"
             : "Generate and auto-filter a candidate batch" }));
    // A bootstrap exists → offer to re-cull it (no regeneration, §6) or discard.
    if (boot.ok && boot.phase) {
      const recull = el("button", "lib-btn", "Re-cull candidates");
      recull.disabled = busy;
      recull.title = "Re-run the auto-filter without regenerating (CPU only)";
      recull.addEventListener("click", () => act(async () => {
        const res = await call("image_bootstrap_recull", cid, null);
        feedback(res.ok ? "Re-culled the existing candidates." :
          "Could not re-cull: " + (res.error || res.kind), !res.ok);
      }));
      row.appendChild(recull);
      const discard = el("button", "lib-btn ghost", "Discard candidates");
      discard.disabled = busy;
      discard.addEventListener("click", () => act(async () => {
        vetSelection.clear();
        const res = await call("image_clear_bootstrap", cid, "all");
        feedback(res.ok ? "Bootstrap candidates discarded." :
          "Could not discard: " + (res.error || res.kind), !res.ok);
      }));
      row.appendChild(discard);
    }
    p.appendChild(row);

    if (boot.ok && boot.has_vetted) {
      p.appendChild(el("div", "pf-status",
        `${boot.vetted_count} vetted image${boot.vetted_count === 1 ? "" : "s"} ` +
        "confirmed — ready to train."));
      const trainRow = el("div", "pf-btn-row");
      trainRow.appendChild(jobButton("Train identity LoRA", {
        kind: "train", options: {}, label: "Train LoRA",
      }, { accent: true }));
      p.appendChild(trainRow);
    }

    // Cull summary — name the counts AND the rejecting gates (5.5
    // diagnosability: "rejected_quality: 53" hid a face_area miscalibration).
    if (boot.ok && boot.phase) {
      p.appendChild(el("div", "lib-meta", cullSummary(boot, proposed)));
    }

    if (proposed.length) {
      p.appendChild(vettedGrid(proposed, boot));
    } else if (boot.ok && boot.phase &&
               !Number((boot.counts || {}).confirmed || 0)) {
      // Only when nothing survived at all — a fully-confirmed batch is not a
      // failure (the summary line above already says "N confirmed").
      p.appendChild(el("div", "hint",
        "No candidates passed the auto-filter — the summary above names the " +
        "rejecting gates; generate more, or tune image_gen.bootstrap.* in " +
        "Settings if one gate is doing all the damage."));
    }
    return p;
  }

  const REASON_LABELS = {
    face_too_small: "face too small", face_too_large: "face too large",
    blurry: "blurry", low_confidence: "low face confidence",
    multi_face: "multiple faces", no_face: "no face",
    off_identity: "off-identity", content: "content-blocked",
    decode_error: "unreadable",
  };

  function cullSummary(boot, proposed) {
    const counts = boot.counts || {};
    const total = Object.values(counts).reduce(
      (a, b) => a + (Number(b) || 0), 0);
    const rejected = Object.entries(counts)
      .filter(([k]) => k.startsWith("rejected_"))
      .reduce((a, [, v]) => a + (Number(v) || 0), 0);
    // the grid list carries confirmed tiles too (flagged) — count them in
    // their own segment, not inside "proposed"
    const confirmed = proposed.filter((c) => c.confirmed).length;
    let line = `${total} candidate${total === 1 ? "" : "s"} · ` +
      `${proposed.length - confirmed} proposed`;
    if (confirmed) line += ` · ${confirmed} confirmed`;
    if (rejected) {
      const reasons = Object.entries(boot.reasons || {})
        .sort((a, b) => b[1] - a[1])
        .map(([k, v]) => `${REASON_LABELS[k] || k} ${v}`)
        .join(", ");
      line += ` · ${rejected} rejected` + (reasons ? ` — ${reasons}` : "");
    }
    return line;
  }

  function vettedGrid(proposed, boot) {
    const wrap = el("div", "pf-vetted");
    const head = el("div", "pf-vetted-head");
    head.appendChild(el("div", "field-label",
      `Machine-vetted grid — approve the images to train on (${proposed.length}):`));
    const all = el("button", "lib-btn ghost", "Select all");
    all.type = "button";
    all.disabled = busy;
    all.addEventListener("click", () => {
      proposed.forEach((c) => vetSelection.add(c.candidate_id));
      render();
    });
    head.appendChild(all);
    const none = el("button", "lib-btn ghost", "Clear");
    none.type = "button";
    none.disabled = busy;
    none.addEventListener("click", () => { vetSelection.clear(); render(); });
    head.appendChild(none);
    wrap.appendChild(head);

    const confirm = el("button", "lib-btn accent", "");
    confirm.type = "button";
    // A tile toggle updates the Confirm button in place (no full re-render):
    // without this the count/enabled state went stale on individual picks and
    // Confirm stayed disabled unless "Select all" (which re-renders) was used.
    function updateConfirm() {
      confirm.disabled = busy || vetSelection.size === 0;
      confirm.textContent = `Confirm ${vetSelection.size} for training`;
    }

    const grid = el("div", "pf-grid");
    for (const c of proposed) {
      const cell = el("div", "pf-grid-cell selectable" +
        (vetSelection.has(c.candidate_id) ? " on" : ""));
      cell.appendChild(thumbTile(c.path, null, 320));
      cell.appendChild(zoomButton(c.path));
      if (c.confirmed)  // already in the vetted set; unchecking removes it
        cell.appendChild(el("span", "pf-vetted-badge", "vetted"));
      if (typeof c.similarity === "number")
        cell.appendChild(el("span", "pf-sim", c.similarity.toFixed(2)));
      cell.addEventListener("click", () => {
        if (busy) return;
        if (vetSelection.has(c.candidate_id)) vetSelection.delete(c.candidate_id);
        else vetSelection.add(c.candidate_id);
        cell.classList.toggle("on");
        updateConfirm();
      });
      grid.appendChild(cell);
    }
    wrap.appendChild(grid);

    confirm.addEventListener("click", () => act(async () => {
      const res = await call("image_confirm_vetted", cid, [...vetSelection]);
      if (res.ok) {
        // The floor is a RECOMMENDATION (§6's ~15-30 band) — training runs
        // below it (5.5: the old wording read as a hard block).
        feedback(`Confirmed ${res.count} — now train the LoRA.` +
          (res.below_floor
            ? " (Below the recommended 15 — training still works; more" +
              " images strengthen identity.)"
            : ""));
      } else {
        feedback("Could not confirm: " + (res.error || res.kind), true);
      }
    }));
    updateConfirm();          // set the initial label + disabled state
    wrap.appendChild(confirm);
    return wrap;
  }

  // -- catalog panel: generate / matte / clear --------------------------------

  function catalogPanel() {
    const p = panel("Seed catalog",
      "The core matrix (expressions × poses × wardrobe) rendered through the " +
      "LoRA and matted for compositing (§7). Generation and matting are jobs.");
    const cat = data.catalog;
    const matte = data.matte;
    const hasLora = data.lora.ok && data.lora.has_lora;

    if (cat.ok && cat.has_catalog) {
      p.appendChild(el("div", "pf-status",
        `${cat.frames} frames${cat.stale ? " — STALE (record changed)" : ""}` +
        (matte.ok ? ` · ${matte.matted}/${matte.frames} matted` : "")));
    } else {
      p.appendChild(el("div", "pf-status",
        hasLora ? "No catalog yet." :
          "Train an identity LoRA first — the catalog renders through it."));
    }
    const row = el("div", "pf-btn-row");
    row.appendChild(jobButton(
      cat.ok && cat.has_catalog ? "Regenerate catalog" : "Generate catalog", {
        kind: "catalog", options: {}, label: "Catalog",
      }, { accent: !(cat.ok && cat.has_catalog),
           disabled: !hasLora,
           title: hasLora ? "" : "Needs a trained LoRA" }));
    if (cat.ok && cat.has_catalog) {
      row.appendChild(jobButton("Matte frames", {
        kind: "matte", options: {}, label: "Matte",
      }, { disabled: !(matte.ok && matte.ready),
           title: matte.ok && matte.ready ? "" :
             "Matting model not configured on this machine" }));
      const clr = el("button", "lib-btn ghost", "Clear catalog");
      clr.disabled = busy;
      clr.addEventListener("click", () => act(async () => {
        const res = await call("image_clear_catalog", cid);
        feedback(res.ok ? "Catalog cleared." :
          "Could not clear: " + (res.error || res.kind), !res.ok);
      }));
      row.appendChild(clr);
    }
    p.appendChild(row);
    return p;
  }

  // -- on-demand posing: {expression, pose, outfit} -> a cached frame ---------

  function posePanel() {
    const p = panel("On-demand posing",
      "Pick an expression, pose and outfit — a covered state serves instantly " +
      "from the catalog/cache; a novel one generates through the LoRA and " +
      "caches (§7). Generation is a job.");
    const states = data.states;
    const hasLora = data.lora.ok && data.lora.has_lora;
    if (!states.ok || (!states.expressions.length && !states.poses.length)) {
      p.appendChild(el("div", "hint", "No state space available."));
      return p;
    }
    const row = el("div", "pf-pose-row");
    row.appendChild(poseSelect("expression", "Expression", states.expressions));
    row.appendChild(poseSelect("pose", "Pose", states.poses));
    row.appendChild(poseSelect("outfit", "Outfit", states.outfits));
    p.appendChild(row);

    const actionRow = el("div", "pf-btn-row");
    const complete = pose.expression && pose.pose && pose.outfit;
    actionRow.appendChild(jobButton("Generate this pose", {
      kind: "on_demand",
      options: { state: { expression: pose.expression, pose: pose.pose,
                          outfit: pose.outfit } },
      label: "On-demand frame",
      onDone: (st) => {
        if (window.Jobs.isSuccess(st))
          lastRender = { label: "On-demand frame", path: st.result.abs_path ||
            st.result.path };
      },
    }, { accent: true, disabled: !hasLora || !complete,
         title: hasLora ? (complete ? "" : "Pick all three") :
           "Needs a trained LoRA" }));
    p.appendChild(actionRow);
    return p;
  }

  function poseSelect(dim, label, options) {
    const wrap = el("label", "pf-select");
    wrap.appendChild(el("span", null, label));
    const sel = el("select");
    sel.disabled = busy;
    for (const o of options) {
      const opt = el("option", null, o.label);
      opt.value = o.id;
      if (pose[dim] === o.id) opt.selected = true;
      sel.appendChild(opt);
    }
    sel.addEventListener("change", () => { pose[dim] = sel.value; });
    wrap.appendChild(sel);
    return wrap;
  }

  // -- footprint / cache / delete panel ---------------------------------------

  function storagePanel() {
    const p = panel("Storage & cleanup", null);
    const cache = data.cache;
    const cat = data.catalog;
    const info = el("div", "pf-storage");
    if (cache.ok)
      info.appendChild(el("div", null,
        `On-demand cache: ${cache.frames} frames · ${fmtBytes(cache.bytes || 0)}` +
        (cache.matted ? ` · ${cache.matted} matted` : "")));
    if (cat.ok && cat.has_catalog)
      info.appendChild(el("div", null, `Catalog: ${cat.frames} frames`));
    p.appendChild(info);

    const row = el("div", "pf-btn-row");
    if (cache.ok && cache.frames) {
      const clr = el("button", "lib-btn ghost", "Clear on-demand cache");
      clr.disabled = busy;
      clr.title = "Evicted frames regenerate on demand if needed again (§14)";
      clr.addEventListener("click", () => act(async () => {
        const res = await call("image_clear_cache", cid);
        feedback(res.ok ? "Cache cleared." :
          "Could not clear: " + (res.error || res.kind), !res.ok);
      }));
      row.appendChild(clr);
    }
    p.appendChild(row);
    deleteRow(p);
    return p;
  }

  // --------------------------------------------------------------- render

  function render() {
    if (!cid || !data) return;
    headerCard();
    const root = $("profile-root");
    root.textContent = "";
    if (!data.record.ok) return; // header already carries the error + delete
    const fb = el("p", "feedback");
    fb.id = "profile-feedback";
    fb.setAttribute("role", "status");
    root.appendChild(fb);
    applyFeedback();
    root.appendChild(identityPanel());
    root.appendChild(promotionPanel());
    root.appendChild(catalogPanel());
    root.appendChild(posePanel());
    root.appendChild(storagePanel());
  }

  // ---------------------------------------------------------------- open

  async function open(id) {
    cid = id;
    data = null;
    busy = false;
    confirmDelete = false;
    vetSelection.clear();
    lastRender = null;
    candidates = null;
    feedbackMsg = null;
    thumbCache.clear();
    pose.expression = pose.pose = pose.outfit = "";
    $("profile-header").textContent = "";
    $("profile-root").textContent = "";
    $("profile-job").textContent = "";
    $("profile-header").appendChild(el("p", "hint", "Loading…"));
    await refresh();
    render();
  }

  return { open };
})();
