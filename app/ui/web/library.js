/* Stage-4 library & management view (§14): list / sort / filter saved
   characters, per-character footprint (LoRA + catalog + cached frames),
   deletion recommendation past the cache threshold, stale-catalog badge with
   an OFFERED (never forced) regeneration, cache clearing, edit hand-off to
   the creator, and delete with an inline two-step confirm (no dialogs — the
   one-window rule). All backend access goes through window.pywebview.api. */

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

  const state = { search: "", sort: "updated_desc", filter: "all" };

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
    // ISO-8601 UTC from the backend; show the date part plainly.
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
    const cmp = {
      updated_desc: (a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")),
      created_desc: (a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")),
      name_asc: (a, b) => String(a.name || "￿").localeCompare(String(b.name || "￿")),
      footprint_desc: (a, b) => totalBytes(b) - totalBytes(a),
    }[state.sort] || (() => 0);
    rows.sort(cmp);
    return rows;
  }

  // ------------------------------------------------------------- actions

  // Action feedback goes to the PERSISTENT #lib-status line, never a per-card
  // span — render() rebuilds all cards, so a captured span would be detached
  // by the time an action resolves (a failed delete/clear would show nothing).
  function status(text, isError) {
    const node = $("lib-status");
    if (!node) return;
    node.className = "feedback" + (isError ? " error" : (text ? " ok" : ""));
    node.textContent = text || "";
  }

  // busy ids are managed here (add before, delete in finally) and survive a
  // refresh, so an in-flight row's buttons stay disabled across a re-list.
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
        render();  // re-enable this row's buttons
      }
    });
  }

  function doRegenerate(row) {
    return runAction(row.id, async () => {
      status(`Regenerating “${row.name}”'s catalog — this can take minutes…`);
      let res;
      try {
        res = await window.pywebview.api.image_generate_catalog(row.id);
      } catch (err) {
        res = { ok: false, error: String(err) };
      }
      if (res.ok) {
        status(`Catalog regenerated for “${row.name}” — ${res.frames ?? "?"} frames.`);
      } else {
        status(`Regeneration for “${row.name}” did not run: ${res.error || res.kind}`, true);
      }
      await refresh();
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

  function card(row) {
    const isBusy = busy.has(row.id);
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
      info.appendChild(el("div", "lib-name", `Unreadable record`));
      info.appendChild(el("div", "lib-meta", `id ${row.id}`));
      const err = el("div", "alert warn",
        `${row.kind || "error"}: ${row.error || "this record cannot be loaded"}` +
        " — it can still be deleted.");
      info.appendChild(err);
    }

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
    info.appendChild(badges);
    info.appendChild(footprintLine(row));

    if (row.recommend_delete) {
      info.appendChild(el("div", "lib-recommend",
        "Cache has grown past the cleanup threshold — consider clearing this " +
        "character's cache (evicted frames regenerate on demand)."));
    }

    head.appendChild(info);
    c.appendChild(head);

    const actions = el("div", "lib-actions");

    if (row.ok) {
      const edit = el("button", "lib-btn", "Edit");
      edit.disabled = isBusy;
      edit.addEventListener("click", () => {
        window.Creator.beginEdit(row.id);
        window.AppNav.show("create");
      });
      actions.appendChild(edit);

      if (row.catalog && row.catalog.stale && row.has_lora) {
        const regen = el("button", "lib-btn accent", "Regenerate catalog");
        regen.title = "Re-render the seed catalog to match the edited record";
        regen.disabled = isBusy;
        regen.addEventListener("click", () => doRegenerate(row));
        actions.appendChild(regen);
      }

      if (row.cache && row.cache.frames) {
        const clear = el("button", "lib-btn", "Clear cache");
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
      cancel.addEventListener("click", () => {
        confirmDeleteId = null;
        render();
      });
      actions.appendChild(confirm);
      actions.appendChild(cancel);
      actions.appendChild(el("span", "lib-confirm-hint",
        "Deletes the record, reference, LoRA, catalog, and cache."));
    } else {
      const del = el("button", "lib-btn ghost", "Delete…");
      del.disabled = isBusy;
      del.addEventListener("click", () => {
        confirmDeleteId = row.id;
        render();
      });
      actions.appendChild(del);
    }

    c.appendChild(actions);
    return c;
  }

  function render() {
    const list = $("lib-list");
    if (!list) return;
    list.textContent = "";
    if (!data) return;
    const rows = visibleRows();
    const total = data.characters.length;
    const shownBytes = data.characters.reduce((s, r) => s + totalBytes(r), 0);
    $("lib-summary").textContent = total
      ? `${rows.length} of ${total} character${total === 1 ? "" : "s"} shown — ` +
        `${fmtBytes(shownBytes)} on disk across the library.`
      : "No characters yet — create one to see it here.";
    for (const row of rows) list.appendChild(card(row));
    if (total && !rows.length) {
      list.appendChild(el("section", "card hint",
        "Nothing matches the current search/filter."));
    }
  }

  // ---------------------------------------------------------------- load

  // A refresh requested while one is in flight is coalesced into a single
  // re-run afterwards, so an action's `await refresh()` (post-delete) can
  // never silently no-op against a background refresh and leave the deleted
  // row on screen. busy/confirm state is deliberately NOT cleared here —
  // an in-flight row must stay disabled across a re-list.
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
      ok = true;
    } catch (err) {
      const list = $("lib-list");
      if (list) {
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

  return { refresh };
})();
