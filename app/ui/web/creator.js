/* Stage-2 creator: quick + detailed create paths, rendered entirely from the
   option catalog (creator_catalog), writing records via create_character.
   The form is data-driven — groups, sections, anatomy regions, and the
   quick-path membership all come from the option data files, so a drop-in
   file surfaces here with no code change (§15).

   All free text is checked live against Layer 1 (check_text) for feedback,
   and re-gated in the backend on save — the live check is UX, not the
   safety boundary. */

"use strict";

window.Creator = (function () {
  function $(id) { return document.getElementById(id); }

  let catalog = null;      // creator_catalog() payload
  let mode = "quick";
  let loading = false;

  // Everything the user has entered, kept outside the DOM so switching
  // modes (or reloading options) re-renders without losing work.
  const state = {
    name: "",
    age: null,
    selections: {},        // group id -> option id
    tags: {},              // group id -> [option ids]
    sliders: {},           // group id -> number
    free_text: {},         // field key -> text
  };

  // ------------------------------------------------------------- helpers

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function debounce(fn, ms) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  }

  // ------------------------------------------------------ catalog shaping

  function ageGroup() {
    return catalog.groups.find((g) => g.field === "age") || null;
  }

  // Groups the current mode renders as controls; the age group feeds the
  // header input instead.
  function formGroups() {
    const groups = catalog.groups.filter((g) => g.field !== "age");
    return mode === "quick" ? groups.filter((g) => g.quick) : groups;
  }

  // ------------------------------------------------------- field controls

  function fieldWrap(dataField, labelText, hint) {
    const wrap = el("div", "field");
    wrap.dataset.field = dataField;
    wrap.appendChild(el("div", "field-label", labelText));
    if (hint) wrap.appendChild(el("div", "field-hint", hint));
    return wrap;
  }

  function optionButton(option, isOn) {
    const btn = el("button", "opt", option.label);
    btn.type = "button";
    if (option.color) {
      const dot = el("span", "dot");
      dot.style.backgroundColor = option.color;
      btn.prepend(dot);
    }
    if (isOn) btn.classList.add("on");
    return btn;
  }

  function singleControl(group, label) {
    const wrap = fieldWrap("selections." + group.id, label);
    const hasColors = group.options.some((o) => o.color);
    if (group.options.length > 8 && !hasColors) {
      // long colorless lists read better as a dropdown
      const select = el("select");
      const none = el("option", null, "—");
      none.value = "";
      select.appendChild(none);
      for (const o of group.options) {
        const opt = el("option", null, o.label);
        opt.value = o.id;
        select.appendChild(opt);
      }
      select.value = state.selections[group.id] || "";
      select.addEventListener("change", () => {
        if (select.value) state.selections[group.id] = select.value;
        else delete state.selections[group.id];
      });
      wrap.appendChild(select);
    } else {
      // pill row acting as a segmented single-select; re-click clears
      const row = el("div", "chips");
      for (const o of group.options) {
        const btn = optionButton(o, state.selections[group.id] === o.id);
        btn.addEventListener("click", () => {
          const wasOn = state.selections[group.id] === o.id;
          if (wasOn) delete state.selections[group.id];
          else state.selections[group.id] = o.id;
          for (const sib of row.children)
            sib.classList.toggle("on", sib === btn && !wasOn);
        });
        row.appendChild(btn);
      }
      wrap.appendChild(row);
    }
    return wrap;
  }

  function multiControl(group, label) {
    const wrap = fieldWrap("tags." + group.id, label);
    const row = el("div", "chips");
    for (const o of group.options) {
      const current = state.tags[group.id] || [];
      const btn = optionButton(o, current.includes(o.id));
      btn.addEventListener("click", () => {
        const list = state.tags[group.id] || (state.tags[group.id] = []);
        const at = list.indexOf(o.id);
        if (at >= 0) {
          list.splice(at, 1);
          btn.classList.remove("on");
          if (!list.length) delete state.tags[group.id];
        } else {
          list.push(o.id);
          btn.classList.add("on");
        }
      });
      row.appendChild(btn);
    }
    wrap.appendChild(row);
    return wrap;
  }

  function numericControl(group, label) {
    const wrap = fieldWrap("sliders." + group.id, label);
    const min = group.min ?? 0;
    const max = group.max ?? 100;
    if (!(group.id in state.sliders))
      state.sliders[group.id] = group.default ?? min;
    const row = el("div", "slider-row");
    const input = el("input");
    input.type = group.kind === "slider" ? "range" : "number";
    input.min = min;
    input.max = max;
    input.step = group.step ?? 1;
    input.value = state.sliders[group.id];
    const value = el("span", "slider-val");
    const show = () => {
      value.textContent = input.value + (group.unit ? " " + group.unit : "");
    };
    show();
    input.addEventListener("input", () => {
      // number inputs report "" while cleared/invalid, and Number("") is 0 —
      // keep the last valid value instead of silently recording a zero
      const v = Number(input.value);
      if (input.value !== "" && !Number.isNaN(v)) {
        state.sliders[group.id] = v;
        show();
      }
    });
    row.appendChild(input);
    row.appendChild(value);
    wrap.appendChild(row);
    return wrap;
  }

  function control(group, labelOverride) {
    const label = labelOverride || group.label;
    if (group.kind === "slider" || group.kind === "number")
      return numericControl(group, label);
    if (group.multi) return multiControl(group, label);
    return singleControl(group, label);
  }

  // Live Layer-1 feedback for a text field. Each field gets its own
  // debounced checker so parallel typing doesn't cross wires; a sequence
  // token drops responses that resolve after a newer check started.
  function makeChecker(statusEl, context) {
    let seq = 0;
    return debounce(async (text) => {
      const token = ++seq;
      text = (text || "").trim();
      if (!text) {
        statusEl.className = "field-status";
        statusEl.textContent = "";
        return;
      }
      try {
        const res = await window.pywebview.api.check_text(text, context);
        if (token !== seq) return; // superseded while in flight
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

  function freeTextControl(fieldDef) {
    const wrap = fieldWrap("free_text." + fieldDef.key, fieldDef.label, fieldDef.hint);
    const area = el("textarea");
    area.rows = fieldDef.rows || 4;
    area.maxLength = catalog.text_max_len || 20000;
    area.value = state.free_text[fieldDef.key] || "";
    const status = el("div", "field-status");
    const check = makeChecker(status, "freetext");
    area.addEventListener("input", () => {
      state.free_text[fieldDef.key] = area.value;
      check(area.value);
    });
    wrap.appendChild(area);
    wrap.appendChild(status);
    return wrap;
  }

  // -------------------------------------------------------------- header

  function identityCard() {
    const card = el("section", "card");
    card.appendChild(el("h2", null, "Who"));
    const grid = el("div", "grid2");

    const nameWrap = fieldWrap("name", "Name");
    const nameInput = el("input");
    nameInput.type = "text";
    nameInput.maxLength = catalog.name_max_len || 120;
    nameInput.placeholder = "Character name";
    nameInput.value = state.name;
    const nameStatus = el("div", "field-status");
    const nameCheck = makeChecker(nameStatus, "name");
    nameInput.addEventListener("input", () => {
      state.name = nameInput.value;
      nameCheck(nameInput.value);
    });
    nameWrap.appendChild(nameInput);
    nameWrap.appendChild(nameStatus);

    const age = ageGroup();
    const min = age?.min ?? catalog.min_age;
    const max = age?.max ?? catalog.max_age;
    const ageWrap = fieldWrap("age", `Age (${min}+)`);
    const ageInput = el("input");
    ageInput.type = "number";
    ageInput.min = min;
    ageInput.max = max;
    ageInput.step = 1;
    if (state.age === null) state.age = age?.default ?? min;
    ageInput.value = state.age;
    ageInput.addEventListener("input", () => { state.age = ageInput.value; });
    ageWrap.appendChild(ageInput);

    grid.appendChild(nameWrap);
    grid.appendChild(ageWrap);
    card.appendChild(grid);
    return card;
  }

  // -------------------------------------------------------------- render

  function renderAlerts() {
    const box = $("creator-alerts");
    box.textContent = "";
    if (catalog && catalog.errors && catalog.errors.length) {
      const warn = el("div", "alert warn");
      warn.appendChild(el("div", null,
        "Some option data files were skipped (bad format) — their choices are unavailable:"));
      for (const e of catalog.errors)
        warn.appendChild(el("div", "alert-line", `${e.file}: ${e.error}`));
      box.appendChild(warn);
    }
  }

  function render() {
    const root = $("creator-form");
    // keep progressive-disclosure state: remember which anatomy regions are
    // open so a mode switch / reload doesn't collapse the user's place
    const openRegions = new Set(
      [...root.querySelectorAll("details.region[open] > summary")]
        .map((s) => s.textContent));
    root.textContent = "";
    renderAlerts();
    root.appendChild(identityCard());

    const groups = formGroups();
    const plain = groups.filter((g) => !g.region);
    const anatomy = groups.filter((g) => g.region);

    // ordered section cards; a group's `section` places it, anatomy groups
    // collapse under one section by body region (§12 progressive disclosure)
    const sections = new Map(); // title -> {order, fields, card}
    function sectionFields(title, order) {
      let s = sections.get(title);
      if (!s) {
        const card = el("section", "card");
        card.appendChild(el("h2", null, title));
        const fields = el("div", "fields");
        card.appendChild(fields);
        s = { order, fields, card };
        sections.set(title, s);
      } else if (order < s.order) {
        s.order = order;
      }
      return s.fields;
    }

    for (const g of plain)
      sectionFields(g.section || "Options", g.order).appendChild(control(g));

    if (anatomy.length) {
      const fields = sectionFields("Anatomy", Math.min(...anatomy.map((g) => g.order)));
      fields.appendChild(el("p", "hint",
        "Categorical by design — the pipeline honors categories reliably, " +
        "not precise dimensions. Expand a region for its options."));
      const regions = new Map();
      for (const g of anatomy) {
        if (!regions.has(g.region)) regions.set(g.region, []);
        regions.get(g.region).push(g);
      }
      for (const [region, regionGroups] of regions) {
        const details = el("details", "region");
        if (openRegions.has(region)) details.open = true;
        details.appendChild(el("summary", null, region));
        const inner = el("div", "fields");
        for (const g of regionGroups)
          inner.appendChild(control(g, g.attribute || g.label));
        details.appendChild(inner);
        fields.appendChild(details);
      }
    }

    if (mode === "detailed") {
      for (const f of catalog.free_text_fields)
        sectionFields(f.section || "Notes", 9000).appendChild(freeTextControl(f));
    }

    [...sections.values()]
      .sort((a, b) => a.order - b.order)
      .forEach((s) => root.appendChild(s.card));
  }

  // ---------------------------------------------------------------- save

  // Only what the current mode shows is saved: quick stays the minimal
  // record even if detailed fields were touched earlier in the session.
  function buildPayload() {
    const visible = new Set(formGroups().map((g) => g.id));
    const selections = {};
    const tags = {};
    const sliders = {};
    for (const [gid, v] of Object.entries(state.selections))
      if (visible.has(gid) && v) selections[gid] = v;
    for (const [gid, list] of Object.entries(state.tags))
      if (visible.has(gid) && list.length) tags[gid] = list.slice();
    for (const [gid, v] of Object.entries(state.sliders))
      if (visible.has(gid)) sliders[gid] = v;
    const free_text = {};
    if (mode === "detailed") {
      for (const f of catalog.free_text_fields) {
        const text = (state.free_text[f.key] || "").trim();
        if (text) free_text[f.key] = text;
      }
    }
    const age = state.age === null || state.age === "" ? null : Number(state.age);
    return {
      mode,
      name: state.name.trim(),
      age: Number.isNaN(age) ? null : age,
      selections,
      tags,
      sliders,
      free_text,
    };
  }

  function clearHighlights() {
    for (const bad of document.querySelectorAll("#creator-form .field.bad"))
      bad.classList.remove("bad");
  }

  function highlightField(field) {
    const target = document.querySelector(
      `#creator-form .field[data-field="${CSS.escape(field)}"]`);
    if (!target) return;
    target.classList.add("bad");
    const region = target.closest("details");
    if (region) region.open = true; // surface a fault hidden in a collapsed region
    target.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  let saving = false;

  async function save() {
    if (!catalog || saving) return;
    const feedback = $("create-feedback");
    clearHighlights();

    // required-field checks up front — the backend re-validates, but normal
    // use shouldn't need a round trip to learn a name/age is missing
    const payload = buildPayload();
    if (!payload.name) {
      feedback.className = "feedback error";
      feedback.textContent = "A name is required.";
      highlightField("name");
      return;
    }
    if (payload.age === null) {
      feedback.className = "feedback error";
      feedback.textContent = "An age is required.";
      highlightField("age");
      return;
    }

    // in-flight guard: a double-click must not create two characters
    saving = true;
    const saveBtn = $("create-save");
    saveBtn.disabled = true;
    feedback.className = "feedback";
    feedback.textContent = "Creating…";
    let res;
    try {
      res = await window.pywebview.api.create_character(payload);
    } catch (err) {
      feedback.className = "feedback error";
      feedback.textContent = "Create failed: " + err;
      return;
    } finally {
      saving = false;
      saveBtn.disabled = false;
    }
    if (res.ok) {
      feedback.className = "feedback ok";
      feedback.textContent =
        `Created “${res.name}” (${res.mode}) — saved as ${res.id.slice(0, 8)}…`;
    } else {
      feedback.className = "feedback error";
      feedback.textContent = res.error;
      if (res.field) highlightField(res.field);
    }
  }

  function resetForm() {
    state.name = "";
    state.age = null;
    state.selections = {};
    state.tags = {};
    state.sliders = {};
    state.free_text = {};
    $("create-feedback").className = "feedback";
    $("create-feedback").textContent = "";
    if (catalog) render();
  }

  // Drop state the reloaded catalog no longer supports: vanished groups,
  // kind flips, and vanished option ids. Without this, a stale option id
  // rides a still-visible group into a backend reject while the re-rendered
  // control shows nothing selected — an error with no control to clear it.
  function pruneState() {
    const byId = new Map(catalog.groups.map((g) => [g.id, g]));
    const numeric = (g) => g.kind === "slider" || g.kind === "number";
    for (const gid of Object.keys(state.selections)) {
      const g = byId.get(gid);
      if (!g || g.multi || numeric(g) || g.field === "age" ||
          !g.options.some((o) => o.id === state.selections[gid]))
        delete state.selections[gid];
    }
    for (const gid of Object.keys(state.tags)) {
      const g = byId.get(gid);
      if (!g || !g.multi) { delete state.tags[gid]; continue; }
      state.tags[gid] = state.tags[gid]
        .filter((v) => g.options.some((o) => o.id === v));
      if (!state.tags[gid].length) delete state.tags[gid];
    }
    for (const gid of Object.keys(state.sliders)) {
      const g = byId.get(gid);
      if (!g || !numeric(g) || g.field === "age") delete state.sliders[gid];
    }
  }

  async function reloadOptions() {
    const feedback = $("create-feedback");
    try {
      catalog = await window.pywebview.api.creator_reload_options();
      pruneState();
      render();
      const formCount = catalog.groups.filter((g) => g.field !== "age").length;
      feedback.className = "feedback ok";
      feedback.textContent =
        `Options reloaded — ${formCount} option groups available.`;
    } catch (err) {
      feedback.className = "feedback error";
      feedback.textContent = "Reload failed: " + err;
    }
  }

  // ---------------------------------------------------------------- mode

  function setMode(next) {
    mode = next;
    for (const btn of $("mode-toggle").children)
      btn.classList.toggle("on", btn.dataset.mode === next);
    $("mode-hint").textContent = next === "quick"
      ? "Quick create — the minimal path: name, age, core looks. Identity " +
        "rides on an IP-Adapter reference (Stage 3); everything is editable later."
      : "Detailed create — the full path: anatomy by body region, personality, " +
        "wardrobe, and filtered free text. Detailed characters can be promoted " +
        "to a trained identity LoRA (Stage 3).";
    if (catalog) render();
  }

  // ---------------------------------------------------------------- init

  async function ensureStarted() {
    if (catalog || loading) return;
    loading = true;
    try {
      catalog = await window.pywebview.api.creator_catalog();
    } catch (err) {
      const box = $("creator-alerts");
      box.textContent = "";
      box.appendChild(el("div", "alert warn",
        "Could not load the option catalog: " + err + " — reopen this view to retry."));
      return;
    } finally {
      loading = false;
    }
    render();
  }

  // Static controls exist at parse time (script sits at the end of <body>);
  // they no-op until the catalog has loaded.
  for (const btn of $("mode-toggle").children)
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  setMode("quick"); // sets the hint text before first catalog load
  $("create-save").addEventListener("click", save);
  $("creator-reset").addEventListener("click", resetForm);
  $("creator-reload").addEventListener("click", () => {
    if (catalog) reloadOptions(); else ensureStarted();
  });

  return { ensureStarted };
})();
