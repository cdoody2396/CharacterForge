/* Stage-2 creator, rebuilt for 5.5c: quick + detailed create paths rendered
   entirely from the option catalog (creator_catalog). The form is data-driven
   — groups, sections, anatomy regions, quick-path membership, AND the widget
   for every group all come from the option data files, so a drop-in file
   surfaces here with no code change (§15). The old `<select>` is gone: the
   backend derives one of five widgets (segmented / chips / swatch / picker /
   slider) per group and the front-end renders it verbatim, so a large drop-in
   option list becomes a searchable picker automatically.

   5.6a: groups may carry a `visible_when` condition (evaluated HERE against
   live selections — the backend has no record context at describe() time); a
   selection change in a condition-referenced group re-renders the form so
   conditional groups appear/disappear. Hidden groups keep their state (for
   re-reveal) but buildPayload() sends visible groups only.

   All free text is checked live against Layer 1 (check_text); the live check
   surfaces only BLOCKS (not a per-keystroke "passes" line) and the record is
   re-gated in the backend on save — the live check is UX, not the boundary.

   The live prompt panel reads image_prompt_preview(id): the assembled positive,
   per-fragment provenance, the CLIP token count, and the 77-token boundary. The
   bridge loads a SAVED record, so the panel reflects the stored character —
   refreshed on entering edit mode and after every successful save. */

"use strict";

window.Creator = (function () {
  function $(id) { return document.getElementById(id); }

  let catalog = null;      // creator_catalog() payload
  let mode = "quick";
  let loading = false;
  let editing = null;      // {id, name, snapshot} while editing (Stage 4)
  let lastSavedId = null;  // most recent persisted id (drives the prompt panel)

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

  function requiredIds() {
    return new Set(catalog && catalog.required_groups ? catalog.required_groups : []);
  }

  // ------------------------------------------------------ catalog shaping

  function ageGroup() {
    return catalog.groups.find((g) => g.field === "age") || null;
  }

  // ---- 5.6a data-driven conditionality (visible_when) -------------------
  // The backend ships each group's load-normalized condition (or null); it is
  // evaluated HERE against live selections — describe() has no record context.
  // Anything unrecognized reads as visible (the doc's degrade semantics; the
  // backend already normalized, this is defense in depth).

  let condMemo = null; // rebuilt when the catalog object changes
  function condIndex() {
    if (!condMemo || condMemo.catalog !== catalog) {
      const refs = new Set();   // group ids some condition references
      const groups = new Map(); // id -> group payload
      for (const g of (catalog && catalog.groups) || []) {
        groups.set(g.id, g);
        const c = g.visible_when;
        if (c && typeof c === "object" && typeof c.group === "string")
          refs.add(c.group);
      }
      condMemo = { catalog, refs, groups };
    }
    return condMemo;
  }

  function chosenIn(group) {
    return group.multi
      ? (state.tags[group.id] || [])
      : (state.selections[group.id] ? [state.selections[group.id]] : []);
  }

  function visibleNow(g) {
    const cond = g.visible_when;
    if (!cond || typeof cond !== "object") return true;
    const ref = condIndex().groups.get(cond.group);
    if (!ref) return true; // missing referenced group -> degrade to visible
    // Conditions read SELECTIONS; a numeric target (slider/number, incl. the
    // age group) is an unsupported reference and degrades to visible — the
    // degrade principle: bad data may only ever make a group MORE visible.
    if (ref.kind === "slider" || ref.kind === "number") return true;
    const chosen = chosenIn(ref);
    if (cond.any === true) return chosen.length > 0;
    if (Array.isArray(cond.in))
      return chosen.some((id) => cond.in.includes(id));
    // 5.7 negative predicate: visible unless a chosen id matches — an EMPTY
    // selection reads VISIBLE (quick mode may not show the referenced group;
    // required-when-visible depends on this polarity).
    if (Array.isArray(cond.not_in))
      return !chosen.some((id) => cond.not_in.includes(id));
    if (typeof cond.class === "string")
      return chosen.some((id) => {
        const o = (ref.options || []).find((opt) => opt.id === id);
        return !!(o && Array.isArray(o["class"]) &&
                  o["class"].includes(cond.class));
      });
    return true;
  }

  // Re-render (re-evaluating visible_when) only when the changed group is
  // referenced by some condition — zero re-renders on a condition-free
  // catalog, so pre-5.6 interaction behavior is untouched. Hidden groups'
  // state is kept (re-revealing restores it); buildPayload() already sends
  // visible groups only, so hidden values never round-trip. Every change
  // refreshes the tab badges (5.7) — a cheap in-place DOM update.
  function selectionChanged(groupId) {
    if (condIndex().refs.has(groupId)) { render(); return; }
    refreshTabBadges();
  }

  // ------------------------------------------------------------ tabs (5.7)

  let activeTab = null; // section title; survives re-renders + mode flips

  // Unmet, currently-visible required groups bucketed by owning section —
  // the per-tab badge source. Anatomy-regioned groups bucket under Anatomy.
  function missingBySection() {
    const counts = new Map();
    if (!catalog) return counts;
    const req = requiredIds();
    for (const g of catalog.groups) {
      if (!req.has(g.id) || !visibleNow(g) || state.selections[g.id]) continue;
      const title = g.region ? "Anatomy" : (g.section || "Options");
      counts.set(title, (counts.get(title) || 0) + 1);
    }
    return counts;
  }

  function refreshTabBadges() {
    const strip = document.querySelector("#creator-form .tab-strip");
    if (!strip) return;
    const missing = missingBySection();
    for (const tab of strip.children) {
      const n = missing.get(tab.dataset.title) || 0;
      let badge = tab.querySelector(".tab-badge");
      if (n && badge) badge.textContent = String(n);
      else if (n) tab.appendChild(el("span", "tab-badge", String(n)));
      else if (badge) badge.remove();
    }
  }

  function activateTab(title) {
    activeTab = title;
    const root = $("creator-form");
    for (const p of root.querySelectorAll(".tab-panel"))
      p.hidden = p.dataset.section !== title;
    for (const b of root.querySelectorAll(".tab-strip .tab"))
      b.classList.toggle("active", b.dataset.title === title);
  }

  // Groups the current mode renders as controls; the age group feeds the
  // header input instead. visible_when filters after the mode filter.
  function formGroups() {
    const groups = catalog.groups.filter((g) => g.field !== "age");
    const modal = mode === "quick" ? groups.filter((g) => g.quick) : groups;
    return modal.filter(visibleNow);
  }

  // ------------------------------------------------------- field controls

  function fieldWrap(dataField, labelText, hint, required) {
    const wrap = el("div", "field");
    wrap.dataset.field = dataField;
    const label = el("div", "field-label", labelText);
    if (required) {
      const star = el("span", "req-mark", " *");
      star.title = "Required — part of the render-identity minimum";
      label.appendChild(star);
    }
    wrap.appendChild(label);
    if (hint) wrap.appendChild(el("div", "field-hint", hint));
    return wrap;
  }

  function swatchBg(option) {
    return option.color || null;
  }

  // A chip/segment button for one option. `variant` tunes the look:
  // "swatch" renders a colour tile / thumbnail, others a labelled pill.
  function optionButton(option, isOn, variant) {
    const btn = el("button", "opt", option.label);
    btn.type = "button";
    if (variant === "swatch" && (option.color || option.image)) {
      btn.classList.add("swatch");
      const tile = el("span", "swatch-tile");
      if (option.image) {
        const img = el("img");
        img.src = option.image;
        img.alt = "";
        tile.appendChild(img);
      } else {
        tile.style.backgroundColor = option.color;
      }
      btn.prepend(tile);
    } else if (option.color) {
      const dot = el("span", "dot");
      dot.style.backgroundColor = option.color;
      btn.prepend(dot);
    }
    if (isOn) btn.classList.add("on");
    return btn;
  }

  // Single-select pill/segment/swatch row: re-click clears.
  function singleRow(group, wrap, variant, rowCls) {
    const row = el("div", rowCls || "chips");
    for (const o of group.options) {
      const btn = optionButton(o, state.selections[group.id] === o.id, variant);
      btn.addEventListener("click", () => {
        const wasOn = state.selections[group.id] === o.id;
        if (wasOn) delete state.selections[group.id];
        else state.selections[group.id] = o.id;
        for (const sib of row.children)
          sib.classList.toggle("on", sib === btn && !wasOn);
        clearFieldError(wrap);
        selectionChanged(group.id);
      });
      row.appendChild(btn);
    }
    wrap.appendChild(row);
  }

  // Multi-select pill/swatch row.
  function multiRow(group, wrap, variant) {
    const row = el("div", "chips");
    for (const o of group.options) {
      const current = state.tags[group.id] || [];
      const btn = optionButton(o, current.includes(o.id), variant);
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
        selectionChanged(group.id);
      });
      row.appendChild(btn);
    }
    wrap.appendChild(row);
  }

  // Searchable / filterable / tiled picker (holds ~200 options). Renders image
  // thumbnails and colour swatches when present, labels otherwise. Only the
  // filtered subset is rendered (capped) so a large catalog stays responsive.
  const PICKER_RENDER_CAP = 120;

  // Picker search text survives the visible_when re-render (5.6a): picking a
  // race from a filtered picker may re-render the form (conditional groups
  // appear), and losing the search mid-flow would be hostile. Cleared on
  // reset / record-fill / end-of-edit so a stale search never prefills the
  // next session's pickers.
  const pickerSearchText = {}; // group id -> last search string

  function clearPickerSearch() {
    for (const gid of Object.keys(pickerSearchText))
      delete pickerSearchText[gid];
  }

  function pickerControl(group, wrap) {
    const multi = group.multi;
    const chosen = () => multi
      ? (state.tags[group.id] || [])
      : (state.selections[group.id] ? [state.selections[group.id]] : []);

    const box = el("div", "picker");
    const search = el("input", "picker-search");
    search.type = "search";
    search.placeholder = `Search ${group.label.toLowerCase()}… (${group.options.length})`;
    search.value = pickerSearchText[group.id] || "";
    const grid = el("div", "picker-grid");
    const more = el("div", "picker-more");

    function isOn(id) { return chosen().includes(id); }

    function toggle(id) {
      if (multi) {
        const list = state.tags[group.id] || (state.tags[group.id] = []);
        const at = list.indexOf(id);
        if (at >= 0) { list.splice(at, 1); if (!list.length) delete state.tags[group.id]; }
        else list.push(id);
      } else {
        if (state.selections[group.id] === id) delete state.selections[group.id];
        else state.selections[group.id] = id;
      }
      clearFieldError(wrap);
      paint();
      selectionChanged(group.id);
    }

    function paint() {
      const q = search.value.trim().toLowerCase();
      const matches = group.options.filter((o) =>
        !q || o.label.toLowerCase().includes(q) || o.id.toLowerCase().includes(q));
      grid.textContent = "";
      for (const o of matches.slice(0, PICKER_RENDER_CAP)) {
        const tile = el("button", "picker-tile");
        tile.type = "button";
        if (isOn(o.id)) tile.classList.add("on");
        if (o.image) {
          const img = el("img"); img.src = o.image; img.alt = "";
          tile.appendChild(img);
        } else if (o.color) {
          const sw = el("span", "picker-color");
          sw.style.backgroundColor = o.color;
          tile.appendChild(sw);
        }
        tile.appendChild(el("span", "picker-label", o.label));
        tile.addEventListener("click", () => toggle(o.id));
        grid.appendChild(tile);
      }
      const hidden = matches.length - Math.min(matches.length, PICKER_RENDER_CAP);
      more.textContent = hidden > 0
        ? `${hidden} more — refine your search`
        : (matches.length ? "" : "No matches.");
    }

    search.addEventListener("input", () => {
      pickerSearchText[group.id] = search.value;
      paint();
    });
    box.appendChild(search);
    box.appendChild(grid);
    box.appendChild(more);
    wrap.appendChild(box);
    paint();
  }

  // Height/weight/muscle slider: metric value (+ imperial for cm/kg, display
  // only) and the live prompt_ranges band label — the semantic the model is
  // actually told. Storage stays metric.
  function bandFor(group, value) {
    for (const r of group.prompt_ranges || []) {
      const lo = r.min, hi = r.max;
      if ((lo === undefined || lo === null || value >= lo) &&
          (hi === undefined || hi === null || value <= hi))
        return r.prompt || "";
    }
    return "";
  }

  function imperial(group, value) {
    if (group.unit === "cm") {
      const totalIn = value / 2.54;
      const ft = Math.floor(totalIn / 12);
      const inch = Math.round(totalIn - ft * 12);
      return `${ft}′${inch}″`;
    }
    if (group.unit === "kg") return `${Math.round(value * 2.2046)} lb`;
    return "";
  }

  function numericControl(group, label, required) {
    const wrap = fieldWrap("sliders." + group.id, label, null, required);
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
    const band = el("div", "slider-band");
    const show = () => {
      const v = Number(input.value);
      const imp = imperial(group, v);
      const metric = group.unit ? `${input.value} ${group.unit}` : `${input.value}`;
      value.textContent = imp ? `${metric} · ${imp}` : metric;
      band.textContent = bandFor(group, v);
    };
    show();
    input.addEventListener("input", () => {
      // number inputs report "" while cleared/invalid, and Number("") is 0 —
      // keep the last valid value instead of silently recording a zero
      const v = Number(input.value);
      if (input.value !== "" && !Number.isNaN(v)) {
        state.sliders[group.id] = v;
        show();
        // no selectionChanged: numeric-targeted conditions degrade to
        // always-visible (visibleNow), so a slider can never flip visibility
        // — and a mid-drag re-render would tear the control out from under
        // the pointer.
      }
    });
    row.appendChild(input);
    row.appendChild(value);
    wrap.appendChild(row);
    wrap.appendChild(band);
    return wrap;
  }

  // Widget dispatch — the backend hands us the resolved widget per group
  // (derivation already applied), so there is one place that maps widget->DOM.
  function control(group, labelOverride) {
    const label = labelOverride || group.label;
    const required = !!group.required;
    if (group.widget === "slider") return numericControl(group, label, required);
    const wrap = fieldWrap(
      (group.multi ? "tags." : "selections.") + group.id, label, null, required);
    if (group.widget === "picker") pickerControl(group, wrap);
    else if (group.multi) multiRow(group, wrap, group.widget);
    else singleRow(group, wrap, group.widget,
                   group.widget === "segmented" ? "seg-row" : "chips");
    return wrap;
  }

  function clearFieldError(wrap) { wrap.classList.remove("bad"); }

  // Live Layer-1 feedback for a text field — shows ONLY on a block (5.5c: no
  // per-keystroke "passes the filter" reassurance). A sequence token drops
  // responses that resolve after a newer check started.
  function makeChecker(statusEl) {
    let seq = 0;
    return debounce(async (text, context) => {
      const token = ++seq;
      text = (text || "").trim();
      if (!text) { statusEl.className = "field-status"; statusEl.textContent = ""; return; }
      try {
        const res = await window.pywebview.api.check_text(text, context);
        if (token !== seq) return; // superseded while in flight
        if (res.allowed) {
          statusEl.className = "field-status";
          statusEl.textContent = "";
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
    const check = makeChecker(status);
    area.addEventListener("input", () => {
      state.free_text[fieldDef.key] = area.value;
      check(area.value, "freetext");
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

    const nameWrap = fieldWrap("name", "Name", null, true);
    const nameInput = el("input");
    nameInput.type = "text";
    nameInput.maxLength = catalog.name_max_len || 120;
    nameInput.placeholder = "Character name";
    nameInput.value = state.name;
    const nameStatus = el("div", "field-status");
    const nameCheck = makeChecker(nameStatus);
    nameInput.addEventListener("input", () => {
      state.name = nameInput.value;
      nameCheck(nameInput.value, "name");
    });
    nameWrap.appendChild(nameInput);
    nameWrap.appendChild(nameStatus);

    const age = ageGroup();
    const min = age?.min ?? catalog.min_age;
    const max = age?.max ?? catalog.max_age;
    const ageWrap = fieldWrap("age", `Age (${min}+)`, null, true);
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
    // remember which anatomy regions are open so a re-render (visible_when,
    // mode switch, reload) doesn't collapse the user's place
    const openKeys = new Set(
      [...root.querySelectorAll("details[open] > summary")]
        .map((s) => s.dataset.key || s.textContent));
    root.textContent = "";
    renderAlerts();
    root.appendChild(identityCard());

    const groups = formGroups();
    const plain = groups.filter((g) => !g.region);
    const anatomy = groups.filter((g) => g.region);

    // section buckets; a group's `section` places it, anatomy groups collapse
    // under one section by body region (§12 disclosure)
    const sections = new Map(); // title -> {order, fields}
    function sectionFields(title, order) {
      let s = sections.get(title);
      if (!s) {
        s = { order, fields: el("div", "fields") };
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
        const summary = el("summary", null, region);
        summary.dataset.key = "reg:" + region;
        if (openKeys.has("reg:" + region)) details.open = true;
        details.appendChild(summary);
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

    const ordered = [...sections.entries()]
      .sort((a, b) => a[1].order - b[1].order);

    if (mode === "quick") {
      // quick create stays one short page: a handful of groups, plain cards
      for (const [title, s] of ordered) {
        const card = el("section", "card section");
        card.appendChild(el("h2", "section-title", title));
        card.appendChild(s.fields);
        root.appendChild(card);
      }
      return;
    }

    // detailed mode (5.7): one tab per section, free jumping, badge = unmet
    // visible required fields — the collapsible long page is retired
    const titles = ordered.map(([t]) => t);
    if (!titles.includes(activeTab)) activeTab = titles[0] || null;
    const missing = missingBySection();
    const strip = el("div", "tab-strip");
    strip.setAttribute("role", "tablist");
    for (const [title] of ordered) {
      const tab = el("button", "tab", title);
      tab.type = "button";
      tab.dataset.title = title;
      tab.setAttribute("role", "tab");
      const n = missing.get(title) || 0;
      if (n) tab.appendChild(el("span", "tab-badge", String(n)));
      if (title === activeTab) tab.classList.add("active");
      tab.addEventListener("click", () => activateTab(title));
      strip.appendChild(tab);
    }
    root.appendChild(strip);
    for (const [title, s] of ordered) {
      const panel = el("section", "card section tab-panel");
      panel.dataset.section = title;
      panel.appendChild(s.fields);
      panel.hidden = title !== activeTab;
      root.appendChild(panel);
    }
  }

  // -------------------------------------------------- live prompt panel

  // The panel reads the SAVED record via image_prompt_preview: assembled
  // positive, per-fragment provenance, token count, 77-boundary marker. It
  // refreshes on entering edit mode and after every successful save.
  function clearPromptPanel(message) {
    const panel = $("creator-prompt");
    if (!panel) return;
    panel.textContent = "";
    panel.appendChild(el("h2", null, "Prompt preview"));
    panel.appendChild(el("p", "hint", message ||
      "Pick options to see the assembled image prompt, its fragments, and " +
      "the CLIP token budget — live, before anything is saved."));
  }

  async function refreshPromptPanel(id) {
    const panel = $("creator-prompt");
    if (!panel) return;
    if (!id) { clearPromptPanel(); return; }
    let res;
    try {
      res = await window.pywebview.api.image_prompt_preview(id);
    } catch (err) {
      clearPromptPanel("Prompt preview unavailable: " + err);
      return;
    }
    if (!res || !res.ok) {
      clearPromptPanel("Prompt preview unavailable" +
        (res && (res.error || res.kind) ? `: ${res.error || res.kind}` : "."));
      return;
    }
    renderPromptPanel(res);
  }

  // Live preview of the IN-PROGRESS form (5.5 acceptance fix: the panel was
  // dead until the first save — "Save the character to see" with only a
  // Create button). Debounced; a sequence counter drops out-of-order
  // responses. Nothing is persisted; partial forms preview (the backend
  // builds a transient record with the required-selection gate off, every
  // other gate on).
  let previewTimer = null;
  let previewSeq = 0;
  function schedulePromptPreview() {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(previewFromForm, 400);
  }

  async function previewFromForm() {
    previewTimer = null;
    const panel = $("creator-prompt");
    if (!panel || !catalog) return;
    const seq = ++previewSeq;
    let res;
    try {
      res = await window.pywebview.api.creator_prompt_preview(buildPayload());
    } catch (err) {
      res = { ok: false, error: String(err) };
    }
    if (seq !== previewSeq) return; // a newer preview superseded this one
    if (!res || !res.ok) {
      if (res && res.kind === "age") {
        clearPromptPanel("Set a valid age to preview the prompt.");
      } else if (res && res.kind === "blocked") {
        clearPromptPanel("Prompt blocked by the content policy" +
          (res.category ? ` (${res.category})` : "") + " — adjust the field.");
      } else {
        clearPromptPanel("Prompt preview unavailable" +
          (res && (res.error || res.kind) ? `: ${res.error || res.kind}` : "."));
      }
      return;
    }
    renderPromptPanel(res);
  }

  function renderPromptPanel(res) {
    const panel = $("creator-prompt");
    panel.textContent = "";
    panel.appendChild(el("h2", null, "Prompt preview"));

    const tokens = res.tokens || {};
    const meta = el("div", "prompt-meta");
    if (tokens.available) {
      const over = !tokens.within_budget;
      const chip = el("span", "token-chip" + (over ? " over" : " ok"),
        `${tokens.total} / ${tokens.content_budget} CLIP tokens`);
      meta.appendChild(chip);
      meta.appendChild(el("span", "prompt-note", over
        ? "Over the single-window budget — chunked encoding carries the rest, " +
          "but fragments past the marker attend more weakly."
        : "Within the 75-token window."));
    } else {
      meta.appendChild(el("span", "token-chip muted", "token count unavailable"));
      meta.appendChild(el("span", "prompt-note",
        "The CLIP tokenizer is not on this machine — counts show on the target."));
    }
    panel.appendChild(meta);

    const pos = el("div", "prompt-positive");
    pos.appendChild(el("div", "field-label", "Assembled positive"));
    pos.appendChild(el("div", "prompt-text", res.positive || "(empty)"));
    panel.appendChild(pos);

    // per-fragment provenance, with the 77-boundary marker between the pieces
    // that fit and the pieces that overran the single window
    const pieces = res.pieces || [];
    const perPiece = tokens.per_piece || [];
    const boundary = tokens.available ? tokens.boundary_index : pieces.length;
    const list = el("div", "prompt-pieces");
    list.appendChild(el("div", "field-label", "Fragments (in assembly order)"));
    pieces.forEach((p, i) => {
      if (i === boundary && boundary < pieces.length) {
        list.appendChild(el("div", "boundary", "— 77-token boundary —"));
      }
      const row = el("div", "piece" + (i >= boundary ? " past" : ""));
      row.appendChild(el("span", "piece-src", p.source));
      row.appendChild(el("span", "piece-text", p.text));
      const pp = perPiece[i];
      if (pp) row.appendChild(el("span", "piece-tok", `+${pp.tokens}`));
      list.appendChild(row);
    });
    if (!pieces.length) list.appendChild(el("p", "hint", "No fragments."));
    panel.appendChild(list);

    const neg = el("details", "prompt-negative");
    neg.appendChild(el("summary", null, "Negative prompt"));
    neg.appendChild(el("div", "prompt-text", res.negative || "(empty)"));
    panel.appendChild(neg);
  }

  // ------------------------------------------------------ edit mode (Stage 4)

  function applyChrome() {
    const on = !!editing;
    $("creator-title").textContent = on ? "Edit character" : "Create a character";
    $("create-save").textContent = on ? "Save changes" : "Create character";
    $("creator-cancel-edit").hidden = !on;
    // Identity tier is set at creation (§10); the full form shows everything
    // either way, so the quick/detailed toggle is a create-path concern.
    for (const btn of $("mode-toggle").children) btn.disabled = on;
  }

  function fillFromRecord(res) {
    state.name = res.name || "";
    state.age = res.age;
    state.selections = Object.assign({}, res.selections || {});
    state.tags = {};
    for (const [k, v] of Object.entries(res.tags || {}))
      state.tags[k] = (v || []).slice();
    state.sliders = Object.assign({}, res.sliders || {});
    state.free_text = Object.assign({}, res.free_text || {});
    clearPickerSearch(); // a fresh record starts with unfiltered pickers
  }

  function showRecordIssues(issues) {
    if (!issues || !issues.length) return;
    const warn = el("div", "alert warn");
    warn.appendChild(el("div", null,
      "This record references options that are no longer loaded, or is missing " +
      "part of the render-identity minimum — fix or re-pick below:"));
    for (const line of issues) warn.appendChild(el("div", "alert-line", line));
    $("creator-alerts").appendChild(warn);
  }

  async function beginEdit(id) {
    await ensureStarted();
    if (!catalog) return; // catalog load failure already surfaced in alerts
    let res;
    try {
      res = await window.pywebview.api.library_get(id);
    } catch (err) {
      res = { ok: false, error: String(err) };
    }
    const box = $("creator-alerts");
    box.textContent = "";
    if (!res.ok) {
      box.appendChild(el("div", "alert warn",
        "Could not load the character for editing: " +
        (res.error || res.kind)));
      return;
    }
    editing = { id: res.id, name: res.name, snapshot: res };
    lastSavedId = res.id;
    fillFromRecord(res);
    mode = "detailed"; // edits always see the full form
    for (const btn of $("mode-toggle").children)
      btn.classList.toggle("on", btn.dataset.mode === "detailed");
    $("mode-hint").textContent =
      `Editing “${res.name}” — every gate re-runs on save; identity ` +
      "(reference/LoRA) is preserved. Visual changes mark the catalog stale " +
      "and regeneration is offered, never forced.";
    applyChrome();
    const feedback = $("create-feedback");
    feedback.className = "feedback";
    feedback.textContent = "";
    render();
    showRecordIssues(res.issues);
    refreshPromptPanel(res.id);
  }

  function endEdit(goLibrary) {
    editing = null;
    lastSavedId = null;
    applyChrome();
    state.name = "";
    state.age = null;
    state.selections = {};
    state.tags = {};
    state.sliders = {};
    state.free_text = {};
    clearPickerSearch();
    $("creator-alerts").textContent = "";
    $("create-feedback").className = "feedback";
    $("create-feedback").textContent = "";
    setMode("quick"); // restores the hint + re-renders
    clearPromptPanel();
    if (goLibrary && window.AppNav) window.AppNav.show("library");
  }

  // The §14 offer: after a render-relevant edit, regeneration is OFFERED —
  // one click away, never automatic.
  function showUpdateOffer(res) {
    const box = $("creator-alerts");
    box.textContent = "";
    if (!res.render_changed) return;
    const cid = editing ? editing.id : res.id;
    const wrap = el("div", "alert offer");
    const staleBits = [];
    if (res.stale_marked && res.stale_marked.catalog) staleBits.push("catalog");
    if (res.stale_marked && res.stale_marked.cache) staleBits.push("cache");
    wrap.appendChild(el("div", null, staleBits.length
      ? `This edit changes how the character renders — the ${staleBits.join(" and ")} ` +
        "no longer match the record and are marked stale."
      : "This edit changes how the character renders. No frames exist yet, " +
        "so there is nothing to regenerate."));
    // Regeneration renders through the identity LoRA; without one, offering
    // the button would guarantee a no_lora failure. Point at training instead.
    const hasLora = !!(editing && editing.snapshot &&
                       editing.snapshot.identity &&
                       editing.snapshot.identity.has_lora);
    if (staleBits.length && !hasLora) {
      wrap.appendChild(el("div", "alert-line",
        "This character has no trained identity LoRA, so the catalog can't be " +
        "regenerated yet — train one first (Stage 3)."));
    }
    if (staleBits.length && hasLora) {
      const row = el("div", "offer-row");
      const jobArea = el("div", "offer-job");
      // Route regeneration through the JOB contract (progress + cancel) — a
      // synchronous image_generate_catalog here is the shipped 287-s silent
      // hang 5.5a exists to kill.
      const regen = el("button", "lib-btn accent", "Regenerate catalog now");
      regen.addEventListener("click", () => {
        regen.disabled = true;
        window.Jobs.mount(jobArea, {
          kind: "catalog", targetId: cid, label: "Regenerate catalog",
        });
      });
      const later = el("button", "lib-btn ghost", "Keep current frames");
      later.addEventListener("click", () => { box.textContent = ""; });
      row.appendChild(regen);
      row.appendChild(later);
      wrap.appendChild(row);
      wrap.appendChild(jobArea);
    }
    box.appendChild(wrap);
  }

  // The create wizard's final step (5.5d, §10 quick-create): OFFER a reference
  // image. The character already saved without one — this only invites it.
  // Generate N base candidates as a job, pick one → set_reference.
  function showCreateReferenceStep(id, name) {
    const box = $("creator-alerts");
    box.textContent = "";
    const wrap = el("div", "alert offer");
    wrap.appendChild(el("div", null,
      `“${name}” is saved. Add a reference image now? This is the quick-create ` +
      "identity tier (IP-Adapter) — optional; you can always add or change it " +
      "later from the character's profile."));
    const row = el("div", "offer-row");
    const jobArea = el("div", "offer-job");
    const gridArea = el("div", "offer-grid");
    const gen = el("button", "lib-btn accent", "Generate avatar candidates");
    gen.addEventListener("click", () => {
      gen.disabled = true;
      gridArea.textContent = "";
      window.Jobs.mount(jobArea, {
        kind: "avatar", targetId: id, options: { count: 4 },
        label: "Avatar candidates",
        onDone: (st) => {
          if (window.Jobs.isSuccess(st) && st.result.candidates &&
              st.result.candidates.length)
            renderCreateCandidates(id, st.result.candidates, gridArea, wrap);
          else gen.disabled = false;  // let them retry (engine unavailable etc.)
        },
      });
    });
    const skip = el("button", "lib-btn ghost", "Skip — go to Library");
    skip.addEventListener("click", () => endEdit(true));
    row.appendChild(gen);
    row.appendChild(skip);
    wrap.appendChild(row);
    wrap.appendChild(jobArea);
    wrap.appendChild(gridArea);
    box.appendChild(wrap);
  }

  function renderCreateCandidates(id, cands, gridArea, wrap) {
    gridArea.textContent = "";
    gridArea.appendChild(el("div", "field-label", "Pick one as the reference:"));
    const grid = el("div", "pf-grid wide");
    for (const c of cands) {
      const cell = el("button", "pf-grid-cell");
      cell.type = "button";
      const img = el("img");
      img.hidden = true;
      img.alt = "";
      cell.appendChild(img);
      // CSP forbids disk paths — fetch a data-URI thumbnail for each
      // candidate (512: 256 was too small to judge identity on, 5.5).
      window.pywebview.api.image_frame_thumbnail(id, c.path, 512)
        .then((r) => {
          if (r && r.ok && r.thumbnail) { img.src = r.thumbnail; img.hidden = false; }
        })
        .catch(() => { /* leave the placeholder */ });
      cell.addEventListener("click", async () => {
        let res;
        try { res = await window.pywebview.api.image_set_reference(id, c.path); }
        catch (err) { res = { ok: false, error: String(err) }; }
        if (res.ok) {
          gridArea.textContent = "";
          const done = el("div", "offer-row");
          done.appendChild(el("div", "alert-line", "Reference set."));
          const go = el("button", "lib-btn accent", "Go to Library");
          go.addEventListener("click", () => endEdit(true));
          done.appendChild(go);
          wrap.appendChild(done);
        } else {
          wrap.appendChild(el("div", "alert-line",
            "Could not set reference: " + (res.error || res.kind)));
        }
      });
      grid.appendChild(cell);
    }
    gridArea.appendChild(grid);
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
    const panel = target.closest(".tab-panel");
    if (panel && panel.hidden) activateTab(panel.dataset.section); // 5.7 tabs
    const region = target.closest("details");
    if (region) region.open = true; // surface a fault in a collapsed region
    target.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  // Client-side render-identity-minimum check: the backend gate is the truth,
  // but a missing required group shouldn't need a round trip to surface.
  // 5.7 required-when-visible: a condition-hidden required group (skin_tone
  // on a metal-chassis surface, hair_style on bald) is not required.
  function firstMissingRequired(payload) {
    for (const gid of requiredIds()) {
      const group = condIndex().groups.get(gid);
      if (group && !visibleNow(group)) continue;
      if (!payload.selections[gid]) return gid;
    }
    return null;
  }

  let saving = false;

  async function save() {
    if (!catalog || saving) return;
    const feedback = $("create-feedback");
    clearHighlights();

    // required-field checks up front — the backend re-validates, but normal
    // use shouldn't need a round trip to learn a name/age/required is missing
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
    const missing = firstMissingRequired(payload);
    if (missing) {
      const g = catalog.groups.find((x) => x.id === missing);
      feedback.className = "feedback error";
      feedback.textContent =
        `${g ? g.label : missing} is required — it's part of the render-identity minimum.`;
      highlightField("selections." + missing);
      return;
    }

    // in-flight guard: a double-click must not create two characters
    saving = true;
    const saveBtn = $("create-save");
    saveBtn.disabled = true;
    feedback.className = "feedback";
    feedback.textContent = editing ? "Saving…" : "Creating…";
    let res;
    try {
      res = editing
        ? await window.pywebview.api.library_update(editing.id, payload)
        : await window.pywebview.api.create_character(payload);
    } catch (err) {
      feedback.className = "feedback error";
      feedback.textContent = (editing ? "Save" : "Create") + " failed: " + err;
      return;
    } finally {
      saving = false;
      saveBtn.disabled = false;
    }
    if (res.ok) {
      feedback.className = "feedback ok";
      lastSavedId = res.id;
      if (editing) {
        editing.name = res.name;
        feedback.textContent = `Saved “${res.name}”` +
          (res.render_changed ? " — appearance changed." : " — no visual change.");
        showUpdateOffer(res);
        // Refresh the revert snapshot so "Start over" returns to the SAVED
        // values, not the pre-edit ones. Quiet best-effort.
        try {
          const fresh = await window.pywebview.api.library_get(editing.id);
          if (fresh.ok) editing.snapshot = fresh;
        } catch (_) { /* keep the old snapshot */ }
      } else {
        feedback.textContent =
          `Created “${res.name}” (${res.mode}) — saved as ${res.id.slice(0, 8)}…` +
          " Further saves update this character.";
        // 5.5 acceptance fix: ADOPT EDIT MODE on the new record. The old path
        // stayed in create mode, so the button still read "Create character"
        // and a second click made a DUPLICATE library record; there was also
        // no way to keep saving while working. From here on, saves route
        // through library_update on this id (the beginEdit machinery).
        editing = { id: res.id, name: res.name, snapshot: null };
        try {
          const fresh = await window.pywebview.api.library_get(res.id);
          if (fresh.ok) editing.snapshot = fresh;
        } catch (_) { /* revert snapshot stays unavailable; saves still work */ }
        // Like beginEdit: edits always see the FULL form — applyChrome
        // disables the mode toggle, so a quick create must not stay locked
        // to the quick subset (session-5 review F3).
        mode = "detailed";
        for (const btn of $("mode-toggle").children)
          btn.classList.toggle("on", btn.dataset.mode === "detailed");
        applyChrome();
        render();
        // The create wizard's final, optional step: offer a reference image.
        showCreateReferenceStep(res.id, res.name);
      }
      refreshPromptPanel(res.id);
    } else {
      feedback.className = "feedback error";
      feedback.textContent = res.error;
      if (res.field) highlightField(res.field);
    }
  }

  function resetForm() {
    if (editing && editing.snapshot) {
      // In edit mode "Start over" means "back to the saved record", not a
      // blank form (that would read as data loss).
      fillFromRecord(editing.snapshot);
      $("create-feedback").className = "feedback";
      $("create-feedback").textContent = "Reverted to the saved values.";
      if (catalog) render();
      return;
    }
    if (editing) {
      // Adopted-edit with no snapshot (the post-create library_get failed):
      // blanking the form here would leave an EMPTY form still bound to
      // library_update — the next save would wipe the just-created record.
      $("create-feedback").className = "feedback error";
      $("create-feedback").textContent =
        "Nothing to revert to — the saved record could not be reloaded. " +
        "Reopen it from the Library to start over.";
      return;
    }
    state.name = "";
    state.age = null;
    state.selections = {};
    state.tags = {};
    state.sliders = {};
    state.free_text = {};
    clearPickerSearch();
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

  // Returns true on success, false on failure — the error is handled (and
  // shown) here, so callers (the Settings gate toggle, 5.6a) check the
  // return value rather than a rejection that never comes.
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
      return true;
    } catch (err) {
      feedback.className = "feedback error";
      feedback.textContent = "Reload failed: " + err;
      return false;
    }
  }

  // ---------------------------------------------------------------- mode

  function setMode(next) {
    mode = next;
    for (const btn of $("mode-toggle").children)
      btn.classList.toggle("on", btn.dataset.mode === next);
    $("mode-hint").textContent = next === "quick"
      ? "Quick create — the minimal path: name, age, and the render-identity " +
        "minimum. Identity rides on an IP-Adapter reference (Stage 3); " +
        "everything is editable later."
      : "Detailed create — the full path: anatomy by body region, personality, " +
        "wardrobe, and filtered free text. Detailed characters can be promoted " +
        "to a trained identity LoRA (Stage 3).";
    if (catalog) render();
  }

  // ---------------------------------------------------------------- init

  // A single shared start promise, so a caller that arrives while the catalog
  // request is in flight (e.g. beginEdit right after clicking Create) awaits
  // the SAME load instead of no-opping and landing on a blank form.
  let startPromise = null;

  function ensureStarted() {
    if (catalog) return Promise.resolve();
    if (startPromise) return startPromise;
    loading = true;
    startPromise = (async () => {
      try {
        catalog = await window.pywebview.api.creator_catalog();
      } catch (err) {
        const box = $("creator-alerts");
        box.textContent = "";
        box.appendChild(el("div", "alert warn",
          "Could not load the option catalog: " + err +
          " — reopen this view to retry."));
        return;
      } finally {
        loading = false;
        startPromise = null;
      }
      render();
      // A fresh form previews immediately (age defaults on first render) —
      // the panel is live from the first interaction, not from the first save.
      if (!editing) schedulePromptPreview();
    })();
    return startPromise;
  }

  // Static controls exist at parse time (script sits at the end of <body>);
  // they no-op until the catalog has loaded.
  for (const btn of $("mode-toggle").children)
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  setMode("quick"); // sets the hint text before first catalog load
  $("create-save").addEventListener("click", save);
  // Live prompt preview: every form interaction (widget click, slider drag,
  // text input) schedules a debounced re-preview of the in-progress payload.
  // Delegated on the form root so dynamically-rendered widgets are covered.
  for (const evt of ["click", "input", "change"])
    $("creator-form").addEventListener(evt, schedulePromptPreview);
  $("creator-reset").addEventListener("click", resetForm);
  $("creator-cancel-edit").addEventListener("click", () => endEdit(true));
  $("creator-reload").addEventListener("click", () => {
    if (catalog) reloadOptions(); else ensureStarted();
  });

  // beginCreate resets to a fresh quick form (used by the Library "Create"
  // button, 5.5f).
  function beginCreate() {
    if (editing) endEdit(false);
    else { resetForm(); schedulePromptPreview(); }
    ensureStarted();
  }

  // reloadOptions is exposed for the Settings content-gate toggle (5.6a): a
  // gate flip re-reads the option directories backend-side, and the form
  // must adopt the fresh catalog (gated groups appear/disappear) immediately.
  return { ensureStarted, beginEdit, beginCreate, reloadOptions };
})();
