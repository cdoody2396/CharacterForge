/* Library & management view (§14), extended for scale (5.5e): list / sort /
   tag-filter saved characters, a grid⇄list layout toggle with a VIRTUALIZED
   list (only the visible window is in the DOM, so 200+ characters stay
   responsive), per-character footprint (read from the cached value the record
   carries — no per-row disk walk), staleness, cleanup recommendation, cache
   clearing, edit + OPEN (→ the 5.5d profile) hand-offs, and delete with an
   inline two-step confirm. Catalog regeneration runs through the JOB contract
   (progress + cancel) — never the synchronous bridge (the 287-s hang). All
   backend access goes through window.pywebview.api. */

"use strict";

window.Library = (function () {
  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  let data = null;          // library_list payload
  let loading = false;
  let confirmDeleteId = null; // id currently showing the two-step confirm
  const thumbs = new Map();   // id -> data URI | null (fetched lazily, cached)
  const busy = new Set();     // ids with an in-flight action (regen/clear/delete)

  const state = {
    search: "", sort: "updated_desc", filter: "all",
    tags: new Set(),          // selected tag labels (AND-match)
    layout: "grid",           // grid | list
  };

  const ROW_H = 76;           // fixed list-row height for virtualization
  const OVERSCAN = 6;

  // ------------------------------------------------------------- helpers

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

  function fmtDate(iso) {
    if (!iso) return "—";
    return String(iso).slice(0, 10);
  }

  function totalBytes(row) {
    return (row.footprint && row.footprint.total_bytes) || 0;
  }

  // ------------------------------------------------------------ filtering

  function visibleRows() {
    if (!data) return [];
    let rows = data.characters.slice();
    const q = state.search.trim().toLowerCase();
    if (q) {
      rows = rows.filter((r) =>
        (r.name || "").toLowerCase().includes(q) ||
        (r.id || "").toLowerCase().startsWith(q));
    }
    switch (state.filter) {
      case "lora": rows = rows.filter((r) => r.ok && r.has_lora); break;
      case "stale":
        rows = rows.filter((r) => r.ok &&
          ((r.catalog && r.catalog.stale) || (r.cache && r.cache.stale)));
        break;
      case "recommend": rows = rows.filter((r) => r.recommend_delete); break;
      case "broken": rows = rows.filter((r) => !r.ok); break;
    }
    if (state.tags.size) {
      rows = rows.filter((r) => {
        const rt = new Set(r.tags || []);
        for (const t of state.tags) if (!rt.has(t)) return false;
        return true;
      });
    }
    const cmp = {
      updated_desc: (a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")),
      created_desc: (a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")),
      name_asc: (a, b) => String(a.name || "￿").localeCompare(String(b.name || "￿")),
      footprint_desc: (a, b) => totalBytes(b) - totalBytes(a),
    }[state.sort] || (() => 0);
    rows.sort(cmp);
    return rows;
  }

  // Every tag label present across loadable rows, sorted, for the chip filter.
  function tagUniverse() {
    const set = new Set();
    if (data) for (const r of data.characters)
      for (const t of (r.tags || [])) set.add(t);
    return [...set].sort((a, b) => a.localeCompare(b));
  }

  // ------------------------------------------------------------- actions

  function status(text, isError) {
    const node = $("lib-status");
    if (!node) return;
    node.className = "feedback" + (isError ? " error" : (text ? " ok" : ""));
    node.textContent = text || "";
  }

  async function runAction(id, fn) {
    busy.add(id);
    render();
    try {
      await fn();
    } finally {
      busy.delete(id);
    }
  }

  function doDelete(row) {
    return runAction(row.id, async () => {
      try {
        const res = await window.pywebview.api.library_delete(row.id);
        if (!res.ok) throw new Error(res.error || res.kind);
        confirmDeleteId = null;
        status(`Deleted “${row.name || row.id.slice(0, 8)}”.`);
        await refresh();
      } catch (err) {
        status("Delete failed: " + err, true);
        render();
      }
    });
  }

  // Catalog regeneration through the JOB contract (progress + cancel). The old
  // synchronous image_generate_catalog call here was the shipped 287-s hang.
  function doRegenerate(row) {
    busy.add(row.id);
    render();
    status(`Regenerating “${row.name}”'s catalog…`);
    window.Jobs.mount($("lib-job"), {
      kind: "catalog", targetId: row.id, label: `Regenerate ${row.name}`,
      async onDone(st) {
        busy.delete(row.id);
        if (window.Jobs.isSuccess(st))
          status(`Catalog regenerated for “${row.name}” — ${st.result.frames ?? "?"} frames.`);
        else
          status(`Regeneration for “${row.name}” did not run: ` +
            window.Jobs.summarize(st), true);
        await refresh();
      },
    });
  }

  function doClearCache(row) {
    return runAction(row.id, async () => {
      try {
        const res = await window.pywebview.api.image_clear_cache(row.id);
        if (!res.ok) throw new Error(res.error || res.kind);
        status(`Cleared “${row.name}”'s on-demand cache.`);
        await refresh();
      } catch (err) {
        status("Clear cache failed: " + err, true);
        render();
      }
    });
  }

  function openProfile(row) {
    window.Profile.open(row.id);
    window.AppNav.show("profile");
  }

  // ------------------------------------------------------------ thumbnails

  async function loadThumb(row, img) {
    if (thumbs.has(row.id)) {
      const uri = thumbs.get(row.id);
      if (uri) { img.src = uri; img.hidden = false; }
      return;
    }
    try {
      const res = await window.pywebview.api.library_thumbnail(row.id);
      const uri = res && res.ok ? res.thumbnail : null;
      thumbs.set(row.id, uri);
      if (uri) { img.src = uri; img.hidden = false; }
    } catch (_) {
      thumbs.set(row.id, null);
    }
  }

  // -------------------------------------------------------------- render

  function badge(text, cls) {
    return el("span", "badge" + (cls ? " " + cls : ""), text);
  }

  function footprintLine(row) {
    const fp = row.footprint;
    if (!fp) return el("div", "lib-footprint", "footprint unavailable");
    const wrap = el("div", "lib-footprint");
    wrap.appendChild(el("span", "fp-total", fmtBytes(fp.total_bytes)));
    wrap.appendChild(el("span", "fp-part",
      `LoRA ${fmtBytes(fp.lora_bytes)} · catalog ${fmtBytes(fp.catalog_bytes)}` +
      ` · cache ${fmtBytes(fp.cache_bytes)}`));
    return wrap;
  }

  function badgesFor(row) {
    const badges = el("div", "badges");
    if (row.ok) {
      if (row.has_lora) badges.appendChild(badge("LoRA", "ok"));
      else if (row.has_reference) badges.appendChild(badge("reference"));
      if (row.catalog && row.catalog.error) badges.appendChild(badge("catalog?", "warn"));
      else if (row.catalog && row.catalog.frames)
        badges.appendChild(badge(`catalog ${row.catalog.frames}`,
          row.catalog.stale ? "warn" : ""));
      if (row.catalog && row.catalog.stale) badges.appendChild(badge("stale", "warn"));
      if (row.cache && row.cache.error) badges.appendChild(badge("cache?", "warn"));
      else if (row.cache && row.cache.frames)
        badges.appendChild(badge(`cache ${row.cache.frames}`));
    } else {
      badges.appendChild(badge(row.kind || "broken", "warn"));
    }
    return badges;
  }

  function actionsFor(row) {
    const isBusy = busy.has(row.id);
    const actions = el("div", "lib-actions");
    if (row.ok) {
      const open = el("button", "lib-btn accent", "Open");
      open.disabled = isBusy;
      open.title = "Open the character profile (identity, catalog, posing)";
      open.addEventListener("click", () => openProfile(row));
      actions.appendChild(open);

      const edit = el("button", "lib-btn", "Edit");
      edit.disabled = isBusy;
      edit.addEventListener("click", () => {
        window.Creator.beginEdit(row.id);
        window.AppNav.show("create");
      });
      actions.appendChild(edit);

      if (row.catalog && row.catalog.stale && row.has_lora) {
        const regen = el("button", "lib-btn", "Regenerate");
        regen.title = "Re-render the seed catalog to match the edited record";
        regen.disabled = isBusy;
        regen.addEventListener("click", () => doRegenerate(row));
        actions.appendChild(regen);
      }
      if (row.cache && row.cache.frames) {
        const clear = el("button", "lib-btn ghost", "Clear cache");
        clear.title = "Evicted frames regenerate on demand if needed again";
        clear.disabled = isBusy;
        clear.addEventListener("click", () => doClearCache(row));
        actions.appendChild(clear);
      }
    }

    if (confirmDeleteId === row.id) {
      const confirm = el("button", "lib-btn danger", "Confirm delete");
      confirm.disabled = isBusy;
      confirm.addEventListener("click", () => doDelete(row));
      const cancel = el("button", "lib-btn ghost", "Keep");
      cancel.disabled = isBusy;
      cancel.addEventListener("click", () => { confirmDeleteId = null; render(); });
      actions.appendChild(confirm);
      actions.appendChild(cancel);
    } else {
      const del = el("button", "lib-btn ghost", "Delete…");
      del.disabled = isBusy;
      del.addEventListener("click", () => { confirmDeleteId = row.id; render(); });
      actions.appendChild(del);
    }
    return actions;
  }

  function card(row) {
    const c = el("section", "card lib-card");
    const head = el("div", "lib-head");
    const thumb = el("div", "lib-thumb");
    const img = el("img");
    img.hidden = true;
    img.alt = "";
    thumb.appendChild(img);
    thumb.appendChild(el("span", "lib-thumb-fallback", row.ok ? "no image" : "!"));
    head.appendChild(thumb);
    if (row.ok) loadThumb(row, img);

    const info = el("div", "lib-info");
    if (row.ok) {
      const title = el("div", "lib-name", row.name);
      title.appendChild(el("span", "lib-age", ` ${row.age}`));
      info.appendChild(title);
      info.appendChild(el("div", "lib-meta",
        `created ${fmtDate(row.created_at)} · updated ${fmtDate(row.updated_at)}` +
        ` · id ${String(row.id).slice(0, 8)}…`));
    } else {
      info.appendChild(el("div", "lib-name", "Unreadable record"));
      info.appendChild(el("div", "lib-meta", `id ${row.id}`));
      info.appendChild(el("div", "alert warn",
        `${row.kind || "error"}: ${row.error || "this record cannot be loaded"}` +
        " — it can still be deleted."));
    }
    info.appendChild(badgesFor(row));
    if (row.ok && row.tags && row.tags.length) {
      const tagRow = el("div", "lib-tags");
      for (const t of row.tags.slice(0, 8))
        tagRow.appendChild(el("span", "lib-tag", t));
      info.appendChild(tagRow);
    }
    info.appendChild(footprintLine(row));
    if (row.recommend_delete) {
      info.appendChild(el("div", "lib-recommend",
        "Cache has grown past the cleanup threshold — consider clearing this " +
        "character's cache (evicted frames regenerate on demand)."));
    }
    head.appendChild(info);
    c.appendChild(head);
    c.appendChild(actionsFor(row));
    if (confirmDeleteId === row.id)
      c.appendChild(el("div", "lib-confirm-hint",
        "Deletes the record, reference, LoRA, catalog, and cache."));
    return c;
  }

  // A compact single-line row for the virtualized list layout.
  function listRow(row) {
    const r = el("div", "lib-row" + (row.ok ? "" : " broken"));
    const thumb = el("div", "lib-thumb sm");
    const img = el("img");
    img.hidden = true;
    img.alt = "";
    thumb.appendChild(img);
    thumb.appendChild(el("span", "lib-thumb-fallback", row.ok ? "" : "!"));
    if (row.ok) loadThumb(row, img);
    r.appendChild(thumb);

    const mid = el("div", "lib-row-mid");
    const name = el("div", "lib-name",
      row.ok ? row.name : "Unreadable record");
    if (row.ok) name.appendChild(el("span", "lib-age", ` ${row.age}`));
    mid.appendChild(name);
    mid.appendChild(badgesFor(row));
    r.appendChild(mid);

    r.appendChild(el("div", "lib-row-fp", fmtBytes(totalBytes(row))));
    r.appendChild(actionsFor(row));
    return r;
  }

  // -------------------------------------------------- virtualized list

  function renderVirtualList(list, rows) {
    list.className = "lib-list list";
    list.textContent = "";
    const spacer = el("div", "lib-vspacer");
    spacer.style.height = rows.length * ROW_H + "px";
    const win = el("div", "lib-window");
    spacer.appendChild(win);
    list.appendChild(spacer);

    function paint() {
      const scrollTop = list.scrollTop;
      const height = list.clientHeight || 600;
      let first = Math.floor(scrollTop / ROW_H) - OVERSCAN;
      let last = Math.ceil((scrollTop + height) / ROW_H) + OVERSCAN;
      first = Math.max(0, first);
      last = Math.min(rows.length, last);
      win.style.transform = `translateY(${first * ROW_H}px)`;
      win.textContent = "";
      for (let i = first; i < last; i++) win.appendChild(listRow(rows[i]));
    }
    list.onscroll = paint;
    paint();
  }

  function renderGrid(list, rows) {
    list.className = "lib-list grid";
    list.onscroll = null;
    list.textContent = "";
    for (const row of rows) list.appendChild(card(row));
  }

  function render() {
    const list = $("lib-list");
    if (!list) return;
    renderTagFilter();
    if (!data) { list.textContent = ""; return; }
    const rows = visibleRows();
    const total = data.characters.length;
    const shownBytes = data.characters.reduce((s, r) => s + totalBytes(r), 0);
    $("lib-summary").textContent = total
      ? `${rows.length} of ${total} character${total === 1 ? "" : "s"} shown — ` +
        `${fmtBytes(shownBytes)} on disk across the library.`
      : "No characters yet — create one to see it here.";
    if (!rows.length) {
      list.className = "lib-list " + state.layout;
      list.onscroll = null;
      list.textContent = "";
      if (total) list.appendChild(el("section", "card hint",
        "Nothing matches the current search/filter."));
      return;
    }
    if (state.layout === "list") renderVirtualList(list, rows);
    else renderGrid(list, rows);
  }

  function renderTagFilter() {
    const box = $("lib-tagfilter");
    if (!box) return;
    box.textContent = "";
    const tags = tagUniverse();
    if (!tags.length) return;
    for (const t of tags) {
      const chip = el("button", "tag-chip" + (state.tags.has(t) ? " on" : ""), t);
      chip.type = "button";
      chip.addEventListener("click", () => {
        if (state.tags.has(t)) state.tags.delete(t);
        else state.tags.add(t);
        render();
      });
      box.appendChild(chip);
    }
    if (state.tags.size) {
      const clr = el("button", "tag-chip clear", "clear tags");
      clr.type = "button";
      clr.addEventListener("click", () => { state.tags.clear(); render(); });
      box.appendChild(clr);
    }
  }

  // ---------------------------------------------------------------- load

  let refreshPending = false;

  async function refresh() {
    if (loading) { refreshPending = true; return; }
    loading = true;
    let ok = false;
    try {
      const next = await window.pywebview.api.library_list();
      data = next;
      const present = new Set(data.characters.map((r) => r.id));
      if (confirmDeleteId && !present.has(confirmDeleteId))
        confirmDeleteId = null;
      for (const id of [...busy]) if (!present.has(id)) busy.delete(id);
      // drop selected tags no longer present anywhere
      const universe = new Set(tagUniverse());
      for (const t of [...state.tags]) if (!universe.has(t)) state.tags.delete(t);
      ok = true;
    } catch (err) {
      const list = $("lib-list");
      if (list) {
        list.className = "lib-list " + state.layout;
        list.textContent = "";
        list.appendChild(el("section", "card alert warn",
          "Could not load the library: " + err));
      }
    } finally {
      loading = false;
    }
    if (ok) render();
    if (refreshPending) { refreshPending = false; return refresh(); }
  }

  // ---------------------------------------------------------------- init

  function setLayout(next) {
    state.layout = next;
    for (const btn of $("lib-layout").children)
      btn.classList.toggle("on", btn.dataset.layout === next);
    render();
  }

  $("lib-refresh").addEventListener("click", refresh);
  $("lib-search").addEventListener("input", (e) => {
    state.search = e.target.value;
    render();
  });
  $("lib-sort").addEventListener("change", (e) => {
    state.sort = e.target.value;
    render();
  });
  $("lib-filter").addEventListener("change", (e) => {
    state.filter = e.target.value;
    render();
  });
  for (const btn of $("lib-layout").children)
    btn.addEventListener("click", () => setLayout(btn.dataset.layout));

  return { refresh };
})();
