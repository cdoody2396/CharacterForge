/* Shell page: navigation, status readout, live settings, Layer-1 filter
   panel. The creator view is rendered by creator.js. All backend access
   goes through window.pywebview.api (shell.py Api). */

"use strict";

function $(id) { return document.getElementById(id); }

function bindNav() {
  const buttons = document.querySelectorAll("#sidebar .nav-item[data-view]");
  for (const btn of buttons) {
    btn.addEventListener("click", () => {
      for (const b of buttons) b.classList.toggle("active", b === btn);
      for (const view of document.querySelectorAll("#content .view"))
        view.hidden = view.id !== "view-" + btn.dataset.view;
      if (btn.dataset.view === "create") window.Creator.ensureStarted();
    });
  }
}

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
});
