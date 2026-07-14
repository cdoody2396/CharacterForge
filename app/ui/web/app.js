/* Shell page: navigation, status readout, live settings, Layer-1 filter
   panel. Library is the landing view; Create is reached from its "New
   character" button and settings live behind the gear (5.5f). The creator
   view is rendered by creator.js. All backend access goes through
   window.pywebview.api (shell.py Api). */

"use strict";

function $(id) { return document.getElementById(id); }

function showView(name) {
  const buttons = document.querySelectorAll("#sidebar .nav-item[data-view]");
  for (const b of buttons) b.classList.toggle("active", b.dataset.view === name);
  for (const view of document.querySelectorAll("#content .view"))
    view.hidden = view.id !== "view-" + name;
  if (name === "create") window.Creator.ensureStarted();
  if (name === "library") window.Library.refresh();
  if (name === "builders") window.Builders.ensureStarted();
  if (name === "settings") loadEngineStatus();
}

// Image-engine diagnostic (Settings): load state + the §3 VRAM slot. Re-homed
// here after 5.5f deleted the Home view that used to surface it.
async function loadEngineStatus() {
  const out = $("engine-status");
  if (!out) return;
  try {
    const s = await window.pywebview.api.image_engine_status();
    out.className = "feedback";
    out.textContent = s.loaded
      ? `Loaded (${s.loaded_mode || "?"}) · checkpoint ${s.checkpoint_exists ? "present" : "missing"}.`
      : `Idle · checkpoint ${s.checkpoint_exists ? "present" : "missing"} · ` +
        `torch ${s.torch_installed ? "installed" : "absent"}.`;
  } catch (err) {
    out.className = "feedback error";
    out.textContent = "Engine status unavailable: " + err;
  }
}

function bindEngine() {
  const btn = $("engine-release");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try { await window.pywebview.api.image_engine_release(); }
    catch (_) { /* best-effort */ }
    btn.disabled = false;
    loadEngineStatus();
  });
}

function bindNav() {
  const buttons = document.querySelectorAll("#sidebar .nav-item[data-view]");
  for (const btn of buttons) {
    btn.addEventListener("click", () => showView(btn.dataset.view));
  }
  // "New character" lives on the Library toolbar now (5.5f): reset the creator
  // to a fresh form, then navigate to it.
  const create = $("lib-create");
  if (create) create.addEventListener("click", () => {
    window.Creator.beginCreate();
    showView("create");
  });
}

// Programmatic navigation for cross-view flows (library → edit → library).
window.AppNav = { show: showView };

async function loadInfo() {
  const info = await window.pywebview.api.app_info();
  $("info-version").textContent = info.version;
  $("info-stage").textContent = info.stage;
  $("info-settings-path").textContent = info.settings_path;
  $("stage-tag").textContent = info.stage;
  $("status-line").textContent = "Backend connected — " + info.stage + ".";
}

async function loadSettings() {
  const s = await window.pywebview.api.get_settings();
  $("image-variant").value = s.models.image.variant;
  $("chat-variant").value = s.models.chat.variant;
  $("logging-enabled").checked = !!s.safety.logging_enabled;
}

function bindSettings() {
  const feedback = $("settings-feedback");

  async function apply(key, value) {
    try {
      const res = await window.pywebview.api.set_setting(key, value);
      if (res.ok) {
        feedback.textContent = "Saved.";
        feedback.classList.remove("error");
      } else {
        feedback.textContent = res.error;
        feedback.classList.add("error");
        await loadSettings(); // revert the control to persisted state
      }
    } catch (err) {
      // Bridge/persistence rejection — surface it and resync the controls
      // rather than leaving an unhandled promise rejection.
      feedback.textContent = "Could not save: " + err;
      feedback.classList.add("error");
      try { await loadSettings(); } catch (_) { /* leave as-is */ }
    }
  }

  for (const sel of [$("image-variant"), $("chat-variant")]) {
    sel.addEventListener("change", () => apply(sel.dataset.key, sel.value));
  }
  const toggle = $("logging-enabled");
  toggle.addEventListener("change", () => apply(toggle.dataset.key, toggle.checked));
}

function bindFilterPanel() {
  const out = $("filter-result");
  $("filter-run").addEventListener("click", async () => {
    const text = $("filter-input").value;
    const context = $("filter-context").value;
    try {
      const res = await window.pywebview.api.check_text(text, context);
      out.className = "filter-result " + (res.allowed ? "ok" : "blocked");
      out.textContent = res.allowed
        ? "Allowed."
        : `Blocked — category: ${res.category}` +
          (res.matched ? ` (matched: "${res.matched}")` : "");
    } catch (err) {
      out.className = "filter-result blocked";
      out.textContent = "Filter error: " + err;
    }
  });
}

window.addEventListener("pywebviewready", async () => {
  // Bind each panel independently: one panel's backend error must not leave
  // the whole page inert.
  bindNav();
  try {
    await loadInfo();
  } catch (err) {
    $("status-line").textContent = "Backend error: " + err;
  }
  try {
    await loadSettings();
    bindSettings();
  } catch (err) {
    $("settings-feedback").textContent = "Settings unavailable: " + err;
    $("settings-feedback").classList.add("error");
  }
  bindFilterPanel();
  bindEngine();
  // Library is the landing view (5.5f): initialize it now so the app opens on
  // the character list rather than a blank card.
  showView("library");
});
