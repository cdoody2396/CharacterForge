/* Stage-5 builders: personas / scenes / events / scenarios, rendered from the
   per-kind option catalog (builder_describe). Scenes generate a background
   (scene_generate_background) and a matted character frame composites over it
   (image_composite) with a background on/off toggle. All free text is checked
   live against Layer 1 (check_text) and re-gated on save; a scenario requires
   an approved consent frame (a Layer-3 gate — advertised from code). */

"use strict";

window.Builders = (function () {
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function debounce(fn, ms) {
    let t = null;
    return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  }

  let currentKind = "persona";
  const describeByKind = {};    // kind -> builder_describe payload
  let editing = null;           // builder id being edited, or null for new
  let started = false;
  const form = { name: "", selections: {}, tags: {}, free_text: {}, consent: null };

  // ---------------------------------------------------------- live L1 check
  function makeChecker(statusEl, context) {
    let seq = 0;
    return debounce(async (text) => {
      const token = ++seq;
      text = (text || "").trim();
      if (!text) { statusEl.className = "field-status"; statusEl.textContent = ""; return; }
      try {
        const res = await window.pywebview.api.check_text(text, context);
        if (token !== seq) return;
        if (res.allowed) {
          statusEl.className = "field-status ok";
          statusEl.textContent = "Passes the content filter.";
        } else {
          statusEl.className = "field-status blocked";
          statusEl.textContent = "Blocked — " + res.category +
            (res.matched ? ` (matched: “${res.matched}”)` : "");
        }
      } catch (err) {
        if (token !== seq) return;
        statusEl.className = "field-status blocked";
        statusEl.textContent = "Filter check failed: " + err;
      }
    }, 350);
  }

  // ----------------------------------------------------------- form controls
  function fieldWrap(dataField, labelText, hint) {
    const wrap = el("div", "field");
    wrap.dataset.field = dataField;
    wrap.appendChild(el("div", "field-label", labelText));
    if (hint) wrap.appendChild(el("div", "field-hint", hint));
    return wrap;
  }
  function optionButton(o, on) {
    const b = el("button", "opt", o.label);
    b.type = "button";
    if (on) b.classList.add("on");
    return b;
  }
  function singleControl(group) {
    const wrap = fieldWrap("selections." + group.id, group.label);
    if (group.options.length > 10) {
      const select = el("select");
      const none = el("option", null, "—"); none.value = ""; select.appendChild(none);
      for (const o of group.options) {
        const opt = el("option", null, o.label); opt.value = o.id; select.appendChild(opt);
      }
      select.value = form.selections[group.id] || "";
      select.addEventListener("change", () => {
        if (select.value) form.selections[group.id] = select.value;
        else delete form.selections[group.id];
      });
      wrap.appendChild(select);
    } else {
      const row = el("div", "chips");
      for (const o of group.options) {
        const btn = optionButton(o, form.selections[group.id] === o.id);
        btn.addEventListener("click", () => {
          const wasOn = form.selections[group.id] === o.id;
          if (wasOn) delete form.selections[group.id];
          else form.selections[group.id] = o.id;
          for (const sib of row.children) sib.classList.toggle("on", sib === btn && !wasOn);
        });
        row.appendChild(btn);
      }
      wrap.appendChild(row);
    }
    return wrap;
  }
  function multiControl(group) {
    const wrap = fieldWrap("tags." + group.id, group.label);
    const row = el("div", "chips");
    for (const o of group.options) {
      const cur = form.tags[group.id] || [];
      const btn = optionButton(o, cur.includes(o.id));
      btn.addEventListener("click", () => {
        const list = form.tags[group.id] || (form.tags[group.id] = []);
        const at = list.indexOf(o.id);
        if (at >= 0) { list.splice(at, 1); btn.classList.remove("on"); if (!list.length) delete form.tags[group.id]; }
        else { list.push(o.id); btn.classList.add("on"); }
      });
      row.appendChild(btn);
    }
    wrap.appendChild(row);
    return wrap;
  }
  function freeTextControl(f, textMax) {
    const wrap = fieldWrap("free_text." + f.key, f.label, f.hint);
    const area = el("textarea");
    area.rows = f.rows || 4;
    area.maxLength = textMax || 20000;
    area.value = form.free_text[f.key] || "";
    const status = el("div", "field-status");
    const check = makeChecker(status, "freetext");
    area.addEventListener("input", () => { form.free_text[f.key] = area.value; check(area.value); });
    wrap.appendChild(area);
    wrap.appendChild(status);
    return wrap;
  }
  function consentControl(frames) {
    const wrap = fieldWrap("consent", "Consent frame (required)",
      "Every scenario carries an affirmative-consent frame — a structural gate. All are adult and consensual.");
    const row = el("div", "chips");
    for (const c of frames) {
      const btn = optionButton(c, form.consent === c.id);
      btn.addEventListener("click", () => {
        form.consent = c.id;
        for (const sib of row.children) sib.classList.toggle("on", sib === btn);
      });
      row.appendChild(btn);
    }
    wrap.appendChild(row);
    return wrap;
  }

  // ------------------------------------------------------------------- render
  function alerts(payload) {
    const box = $("bld-alerts");
    box.textContent = "";
    if (payload && payload.errors && payload.errors.length) {
      const warn = el("div", "alert warn");
      warn.appendChild(el("div", null, "Some builder option files were skipped (bad format):"));
      for (const e of payload.errors) warn.appendChild(el("div", "alert-line", `${e.file}: ${e.error}`));
      box.appendChild(warn);
    }
  }

  function renderTabs() {
    for (const btn of $("bld-kind-tabs").children)
      btn.classList.toggle("on", btn.dataset.kind === currentKind);
  }

  function kindBadge(kind) {
    const b = el("span", "badge kind-" + kind, kind);
    return b;
  }

  function renderList(builders) {
    const root = $("bld-list");
    root.textContent = "";
    const rows = builders.filter((b) => b.ok && b.kind === currentKind)
      .concat(builders.filter((b) => !b.ok));  // broken rows always show
    if (!rows.length) {
      root.appendChild(el("p", "hint", `No ${currentKind}s yet — click New to author one.`));
      return;
    }
    const list = el("div", "bld-cards");
    for (const b of rows) {
      const card = el("div", "bld-card" + (b.ok ? "" : " broken"));
      const head = el("div", "bld-card-head");
      head.appendChild(el("div", "bld-name", b.ok ? b.name : "(unreadable)"));
      if (b.ok) head.appendChild(kindBadge(b.kind));
      if (b.ok && b.kind === "scenario" && b.consent)
        head.appendChild(el("span", "badge consent", "consent: " + b.consent));
      if (b.ok && b.kind === "scene")
        head.appendChild(el("span", "badge", (b.backgrounds || 0) + " bg"));
      card.appendChild(head);
      if (!b.ok) card.appendChild(el("div", "field-hint", b.error || b.load_kind));
      const actions = el("div", "bld-actions");
      if (b.ok) {
        const edit = el("button", "lib-btn", "Edit");
        edit.addEventListener("click", () => editBuilder(b.id));
        actions.appendChild(edit);
      }
      const del = el("button", "lib-btn ghost", "Delete");
      del.addEventListener("click", () => deleteBuilder(b.id, b.name || b.id));
      actions.appendChild(del);
      card.appendChild(actions);
      list.appendChild(card);
    }
    root.appendChild(list);
  }

  function renderEditor() {
    const editor = $("bld-editor");
    editor.hidden = false;
    editor.textContent = "";
    const payload = describeByKind[currentKind] || {};
    editor.appendChild(el("h2", null,
      (editing ? "Edit " : "New ") + currentKind));

    // name
    const nameWrap = fieldWrap("name", "Name");
    const nameInput = el("input"); nameInput.type = "text";
    nameInput.maxLength = payload.name_max_len || 120;
    nameInput.placeholder = currentKind + " name";
    nameInput.value = form.name;
    const nameStatus = el("div", "field-status");
    const nameCheck = makeChecker(nameStatus, "name");
    nameInput.addEventListener("input", () => { form.name = nameInput.value; nameCheck(nameInput.value); });
    nameWrap.appendChild(nameInput); nameWrap.appendChild(nameStatus);
    editor.appendChild(nameWrap);

    // consent (scenario only, required)
    if (currentKind === "scenario" && payload.consent_frames)
      editor.appendChild(consentControl(payload.consent_frames));

    // option groups
    const fields = el("div", "fields");
    for (const g of (payload.groups || []))
      fields.appendChild(g.multi ? multiControl(g) : singleControl(g));
    editor.appendChild(fields);

    // free text
    for (const f of (payload.free_text_fields || []))
      editor.appendChild(freeTextControl(f, payload.text_max_len));

    // save bar
    const bar = el("div", "savebar-row");
    const save = el("button", "accent", editing ? "Save changes" : "Create " + currentKind);
    save.addEventListener("click", doSave);
    const cancel = el("button", "ghost", "Cancel");
    cancel.addEventListener("click", closeEditor);
    bar.appendChild(save); bar.appendChild(cancel);
    if (editing) {
      const del = el("button", "ghost danger", "Delete");
      del.addEventListener("click", () => deleteBuilder(editing, form.name));
      bar.appendChild(del);
    }
    const fb = el("span", "feedback"); fb.id = "bld-editor-feedback";
    bar.appendChild(fb);
    editor.appendChild(bar);

    // scene background panel (editing an existing scene only)
    if (currentKind === "scene" && editing) editor.appendChild(backgroundPanel(editing));
    editor.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // --------------------------------------------------------- scene backgrounds
  function backgroundPanel(sceneId) {
    const box = el("div", "bg-panel");
    box.appendChild(el("h3", null, "Backgrounds"));
    const hint = el("p", "hint",
      "Generate a background for this scene (needs the image model — shows a clear " +
      "message when it isn't loaded). Generated frames are screened by the Layer-2 filter.");
    box.appendChild(hint);
    const row = el("div", "bld-actions");
    const gen = el("button", "lib-btn accent", "Generate background");
    const clear = el("button", "lib-btn ghost", "Clear all");
    const status = el("span", "feedback");
    const list = el("div", "bg-thumbs");
    async function refreshBg() {
      list.textContent = "";
      let st;
      try { st = await window.pywebview.api.scene_background_status(sceneId); }
      catch (err) { status.textContent = "Status error: " + err; return; }
      if (!st.ok) { status.textContent = st.error || st.kind; return; }
      status.textContent = `${st.count} background(s). Layer-2 classifier ${st.classifier_ready ? "ready" : "not configured"}.`;
      for (const f of st.frames) {
        const chip = el("div", "bg-thumb" + (f.exists ? "" : " missing"), f.frame_id.slice(0, 12));
        list.appendChild(chip);
      }
    }
    gen.addEventListener("click", async () => {
      gen.disabled = true; status.textContent = "Generating…";
      let out;
      try { out = await window.pywebview.api.scene_generate_background(sceneId); }
      catch (err) { out = { ok: false, error: String(err) }; }
      gen.disabled = false;
      status.textContent = out.ok
        ? "Background generated."
        : "Did not run: " + (out.error || out.kind);
      refreshBg();
      populateCompositeScenes();
    });
    clear.addEventListener("click", async () => {
      let out;
      try { out = await window.pywebview.api.scene_clear_background(sceneId); }
      catch (err) { out = { ok: false, error: String(err) }; }
      status.textContent = out.ok ? "Cleared." : (out.error || out.kind);
      refreshBg(); populateCompositeScenes();
    });
    row.appendChild(gen); row.appendChild(clear); row.appendChild(status);
    box.appendChild(row); box.appendChild(list);
    refreshBg();
    return box;
  }

  // ---------------------------------------------------------------- list ops
  async function loadDescribe(kind) {
    if (describeByKind[kind]) return describeByKind[kind];
    const p = await window.pywebview.api.builder_describe(kind);
    describeByKind[kind] = p;
    return p;
  }

  async function refresh() {
    let listRes;
    try { listRes = await window.pywebview.api.builder_list(); }
    catch (err) { $("bld-status").textContent = "List error: " + err; return; }
    if (!listRes.ok) { $("bld-status").textContent = "List error."; return; }
    const n = listRes.builders.filter((b) => b.ok && b.kind === currentKind).length;
    $("bld-status").textContent = `${n} ${currentKind}${n === 1 ? "" : "s"}.`;
    renderList(listRes.builders);
  }

  function resetForm() {
    form.name = ""; form.selections = {}; form.tags = {}; form.free_text = {}; form.consent = null;
  }

  async function newBuilder() {
    editing = null; resetForm();
    await loadDescribe(currentKind);
    alerts(describeByKind[currentKind]);
    renderEditor();
  }

  async function editBuilder(id) {
    let res;
    try { res = await window.pywebview.api.builder_get(id); }
    catch (err) { res = { ok: false, error: String(err) }; }
    if (!res.ok) { $("bld-status").textContent = "Could not load: " + (res.error || res.kind); return; }
    currentKind = res.kind; renderTabs();
    await loadDescribe(currentKind);
    editing = res.id; resetForm();
    form.name = res.name || "";
    form.selections = Object.assign({}, res.selections || {});
    for (const [k, v] of Object.entries(res.tags || {})) form.tags[k] = (v || []).slice();
    form.free_text = Object.assign({}, res.free_text || {});
    form.consent = res.consent || null;
    alerts(describeByKind[currentKind]);
    renderEditor();
    if (res.issues && res.issues.length) {
      const warn = el("div", "alert warn");
      warn.appendChild(el("div", null, "This record references options no longer loaded (kept, not re-pickable):"));
      for (const line of res.issues) warn.appendChild(el("div", "alert-line", line));
      $("bld-alerts").appendChild(warn);
    }
  }

  async function deleteBuilder(id, label) {
    let out;
    try { out = await window.pywebview.api.builder_delete(id); }
    catch (err) { out = { ok: false, error: String(err) }; }
    if (out.ok) {
      $("bld-status").textContent = `Deleted “${label}”.`;
      if (editing === id) closeEditor();
      refresh(); populateCompositeScenes();
    } else {
      $("bld-status").textContent = "Delete failed: " + (out.error || out.kind);
    }
  }

  function buildPayload() {
    const payload = { kind: currentKind, name: form.name.trim(),
                      selections: {}, tags: {}, free_text: {} };
    const groups = (describeByKind[currentKind] || {}).groups || [];
    const ids = new Set(groups.map((g) => g.id));
    for (const [gid, v] of Object.entries(form.selections)) if (ids.has(gid) && v) payload.selections[gid] = v;
    for (const [gid, list] of Object.entries(form.tags)) if (ids.has(gid) && list.length) payload.tags[gid] = list.slice();
    for (const f of ((describeByKind[currentKind] || {}).free_text_fields || [])) {
      const t = (form.free_text[f.key] || "").trim();
      if (t) payload.free_text[f.key] = t;
    }
    if (currentKind === "scenario") payload.consent = form.consent;
    return payload;
  }

  let saving = false;
  async function doSave() {
    if (saving) return;
    const fb = $("bld-editor-feedback");
    const payload = buildPayload();
    if (!payload.name) { fb.className = "feedback error"; fb.textContent = "A name is required."; return; }
    if (currentKind === "scenario" && !payload.consent) {
      fb.className = "feedback error"; fb.textContent = "A scenario needs a consent frame."; return;
    }
    saving = true; fb.className = "feedback"; fb.textContent = editing ? "Saving…" : "Creating…";
    let res;
    try {
      res = editing
        ? await window.pywebview.api.builder_update(editing, payload)
        : await window.pywebview.api.builder_create(payload);
    } catch (err) { res = { ok: false, error: String(err) }; }
    finally { saving = false; }
    if (res.ok) {
      fb.className = "feedback ok";
      fb.textContent = `Saved “${res.name}”.`;
      const wasCreate = !editing;
      if (wasCreate) editing = res.id;   // stay on the just-created record
      refresh(); populateCompositeScenes();
      // Re-render the editor for EVERY kind after a create, not only scenes:
      // otherwise the button keeps reading "Create <kind>" while doSave now
      // dispatches builder_update — a second click would silently OVERWRITE the
      // just-created record instead of making a new one. Re-rendering flips the
      // button to "Save changes", adds Delete, and (for scenes) reveals the
      // background panel.
      if (wasCreate || currentKind === "scene") renderEditor();
    } else {
      fb.className = "feedback error";
      fb.textContent = res.error || res.kind;
    }
  }

  function closeEditor() {
    editing = null; resetForm();
    $("bld-editor").hidden = true;
    $("bld-alerts").textContent = "";
  }

  async function reloadOptions() {
    try {
      const p = await window.pywebview.api.builder_reload_options(currentKind);
      describeByKind[currentKind] = p;
      $("bld-status").textContent = "Options reloaded.";
      if (!$("bld-editor").hidden) renderEditor();
    } catch (err) { $("bld-status").textContent = "Reload failed: " + err; }
  }

  // ---------------------------------------------------------- compositing UI
  const comp = { characterId: "", frameRef: "", sceneId: "", anchor: "bottom_center", scale: 0.85, edgeChoke: 0 };

  async function populateCompositeScenes() {
    const sel = $("comp-scene");
    if (!sel) return;
    let res;
    try { res = await window.pywebview.api.builder_list(); } catch (_) { return; }
    const keep = comp.sceneId;
    sel.textContent = "";
    const none = el("option", null, "No background (transparent)"); none.value = ""; sel.appendChild(none);
    if (res.ok) for (const b of res.builders.filter((x) => x.ok && x.kind === "scene")) {
      const o = el("option", null, `${b.name} (${b.backgrounds || 0} bg)`); o.value = b.id; sel.appendChild(o);
    }
    sel.value = keep;
  }

  async function populateCompositeFrames() {
    const sel = $("comp-frame");
    sel.textContent = "";
    comp.frameRef = "";
    if (!comp.characterId) { sel.appendChild(el("option", null, "— pick a character —")); return; }
    let res;
    try { res = await window.pywebview.api.image_matted_frames(comp.characterId); }
    catch (err) { sel.appendChild(el("option", null, "error: " + err)); return; }
    if (!res.ok || !res.frames.length) {
      sel.appendChild(el("option", null, res.ok ? "— no matted frames —" : (res.error || res.kind)));
      return;
    }
    for (const f of res.frames) {
      const label = f.frame_id.slice(0, 14) + " (" + f.source + ")";
      const o = el("option", null, label); o.value = f.matted_path; sel.appendChild(o);
    }
    comp.frameRef = res.frames[0].matted_path;
    sel.value = comp.frameRef;
  }

  function renderCompositeControls() {
    const root = $("composite-controls");
    root.textContent = "";
    const grid = el("div", "comp-grid");

    const charWrap = fieldWrap("comp-char", "Character");
    const charSel = el("select"); charSel.id = "comp-char";
    charSel.appendChild(el("option", null, "— loading —"));
    charSel.addEventListener("change", () => { comp.characterId = charSel.value; populateCompositeFrames(); });
    charWrap.appendChild(charSel);

    const frameWrap = fieldWrap("comp-frame", "Matted frame");
    const frameSel = el("select"); frameSel.id = "comp-frame";
    frameSel.addEventListener("change", () => { comp.frameRef = frameSel.value; });
    frameWrap.appendChild(frameSel);

    const sceneWrap = fieldWrap("comp-scene", "Background (scene)");
    const sceneSel = el("select"); sceneSel.id = "comp-scene";
    sceneSel.addEventListener("change", () => { comp.sceneId = sceneSel.value; });
    sceneWrap.appendChild(sceneSel);

    grid.appendChild(charWrap); grid.appendChild(frameWrap); grid.appendChild(sceneWrap);
    root.appendChild(grid);

    // placement controls
    const place = el("div", "comp-grid");
    const anchorWrap = fieldWrap("comp-anchor", "Anchor");
    const anchorSel = el("select");
    for (const a of ["bottom_center", "center", "bottom_left", "bottom_right", "top_center"]) {
      const o = el("option", null, a.replace("_", " ")); o.value = a; anchorSel.appendChild(o);
    }
    anchorSel.value = comp.anchor;
    anchorSel.addEventListener("change", () => { comp.anchor = anchorSel.value; });
    anchorWrap.appendChild(anchorSel);

    const scaleWrap = fieldWrap("comp-scale", "Scale");
    const scaleRow = el("div", "slider-row");
    const scale = el("input"); scale.type = "range"; scale.min = 0.2; scale.max = 1.0; scale.step = 0.05; scale.value = comp.scale;
    const scaleVal = el("span", "slider-val", String(comp.scale));
    scale.addEventListener("input", () => { comp.scale = Number(scale.value); scaleVal.textContent = scale.value; });
    scaleRow.appendChild(scale); scaleRow.appendChild(scaleVal); scaleWrap.appendChild(scaleRow);

    const chokeWrap = fieldWrap("comp-choke", "Edge choke (halo)");
    const chokeRow = el("div", "slider-row");
    const choke = el("input"); choke.type = "range"; choke.min = 0; choke.max = 8; choke.step = 1; choke.value = comp.edgeChoke;
    const chokeVal = el("span", "slider-val", String(comp.edgeChoke));
    choke.addEventListener("input", () => { comp.edgeChoke = Number(choke.value); chokeVal.textContent = choke.value; });
    chokeRow.appendChild(choke); chokeRow.appendChild(chokeVal); chokeWrap.appendChild(chokeRow);

    place.appendChild(anchorWrap); place.appendChild(scaleWrap); place.appendChild(chokeWrap);
    root.appendChild(place);

    const bar = el("div", "bld-actions");
    const render = el("button", "accent", "Render preview");
    render.addEventListener("click", doComposite);
    const status = el("span", "feedback"); status.id = "comp-status";
    bar.appendChild(render); bar.appendChild(status);
    root.appendChild(bar);

    // populate the selects
    (async () => {
      let libs;
      try { libs = await window.pywebview.api.library_list(); } catch (_) { libs = { ok: false }; }
      charSel.textContent = "";
      charSel.appendChild(el("option", null, "— pick a character —"));
      if (libs.ok) for (const c of libs.characters.filter((x) => x.ok)) {
        const o = el("option", null, c.name); o.value = c.id; charSel.appendChild(o);
      }
    })();
    populateCompositeFrames();
    populateCompositeScenes();
  }

  async function doComposite() {
    const status = $("comp-status");
    const preview = $("composite-preview");
    if (!comp.characterId || !comp.frameRef) {
      status.className = "feedback error"; status.textContent = "Pick a character and a matted frame."; return;
    }
    status.className = "feedback"; status.textContent = "Compositing…";
    const overrides = { anchor: comp.anchor, scale: comp.scale, edge_choke: comp.edgeChoke };
    let res;
    try {
      res = await window.pywebview.api.image_composite(
        comp.characterId, comp.frameRef, comp.sceneId || null, null, overrides);
    } catch (err) { res = { ok: false, error: String(err) }; }
    if (!res.ok) {
      status.className = "feedback error"; status.textContent = res.error || res.kind;
      return;
    }
    status.className = "feedback ok";
    status.textContent = res.background
      ? `Composited over the scene (${res.width}×${res.height}).`
      : `Character alone — transparent (${res.width}×${res.height}).`;
    preview.textContent = "";
    const img = el("img", "composite-img" + (res.background ? "" : " checker"));
    img.src = res.preview;
    preview.appendChild(img);
  }

  // ---------------------------------------------------------------- init
  function bind() {
    for (const btn of $("bld-kind-tabs").children) {
      btn.addEventListener("click", async () => {
        currentKind = btn.dataset.kind; renderTabs();
        closeEditor();
        await loadDescribe(currentKind);
        alerts(describeByKind[currentKind]);
        refresh();
      });
    }
    $("bld-new").addEventListener("click", newBuilder);
    $("bld-reload").addEventListener("click", reloadOptions);
  }

  let startPromise = null;
  function ensureStarted() {
    if (started) { refresh(); return Promise.resolve(); }
    if (startPromise) return startPromise;
    startPromise = (async () => {
      try {
        bind();
        renderTabs();
        await loadDescribe(currentKind);
        alerts(describeByKind[currentKind]);
        renderCompositeControls();
        await refresh();
        started = true;
      } catch (err) {
        $("bld-status").textContent = "Builders unavailable: " + err;
      } finally { startPromise = null; }
    })();
    return startPromise;
  }

  return { ensureStarted, refresh };
})();
