/* Long-running-job client (5.5d, on the 5.5a contract). The heavy image
   operations — train (~31 min), bootstrap (~15 min), catalog (287 s), matte,
   on-demand, avatar candidates — run as background jobs the UI POLLS at ~1 Hz
   (never a synchronous bridge call: that is the five-minute silent-hang class
   5.5a exists to kill). This module owns the submit → poll → terminal loop and
   a reusable progress+cancel widget every panel mounts.

   window.Jobs.run(kind, targetId, options, {onUpdate})  -> {promise, cancel, jobId}
     promise resolves with the TERMINAL status object (status done|cancelled|
     error + result|error). It rejects only on a bridge/transport failure — a
     job that ran and failed is a normal resolution the caller inspects.
   window.Jobs.mount(container, {kind, targetId, options, label, onDone, onUpdate})
     renders a bar + Cancel into container and drives run(); returns the ctl. */

"use strict";

window.Jobs = (function () {
  const POLL_MS = 1000;              // §3: 1 Hz over a 31-min train costs nil
  const TERMINAL = new Set(["done", "cancelled", "error"]);

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function run(kind, targetId, options, opts) {
    const onUpdate = (opts && opts.onUpdate) || null;
    let jobId = null;
    let cancelled = false;
    let timer = null;
    const ctl = { jobId: null };

    ctl.promise = new Promise((resolve, reject) => {
      (async () => {
        let sub;
        try {
          sub = await window.pywebview.api.job_submit(
            kind, targetId, options || {});
        } catch (err) { reject(err); return; }
        if (!sub || !sub.ok) {
          // Rejected before it ran (unknown kind / full queue) — surface it as
          // a terminal error status so callers have one shape to handle.
          resolve({ status: "error", progress: {},
                    error: { error: (sub && sub.error) || "could not start job",
                             kind: (sub && sub.reason) || "job" } });
          return;
        }
        jobId = sub.job_id;
        ctl.jobId = jobId;
        // A cancel that arrived before the id existed still lands now.
        if (cancelled) {
          try { await window.pywebview.api.job_cancel(jobId); } catch (_) {}
        }
        const poll = async () => {
          let st;
          try { st = await window.pywebview.api.job_status(jobId); }
          catch (err) { reject(err); return; }
          if (onUpdate) { try { onUpdate(st); } catch (_) {} }
          if (TERMINAL.has(st.status)) { resolve(st); return; }
          timer = setTimeout(poll, POLL_MS);
        };
        poll();
      })();
    });

    ctl.cancel = async () => {
      cancelled = true;
      if (timer) { clearTimeout(timer); timer = null; }
      if (jobId) {
        try { return await window.pywebview.api.job_cancel(jobId); }
        catch (_) { /* the poll will still resolve on the next tick */ }
      }
    };
    return ctl;
  }

  function progressText(st) {
    const p = st.progress || {};
    if (p.total) return `${p.done || 0} / ${p.total} frames`;
    if (p.done) return `${p.done} done`;
    return st.status === "queued" ? "queued…" : "working…";
  }

  // A terminal one-liner. A job whose service call returned {ok:false} is a
  // "did not run" with its structured reason, never a bare failure.
  function summarize(st) {
    if (st.status === "cancelled") return "Cancelled.";
    if (st.status === "error") {
      const e = st.error || {};
      return "Failed: " + (e.error || e.kind || "error");
    }
    const r = st.result || {};
    if (!r.ok) return "Did not run: " + (r.error || r.kind || "unknown");
    if (typeof r.frames === "number") return `Done — ${r.frames} frames.`;
    if (typeof r.count === "number") return `Done — ${r.count}.`;
    return "Done.";
  }

  function isSuccess(st) {
    return st.status === "done" && st.result && st.result.ok;
  }

  function mount(container, cfg) {
    container.textContent = "";
    const box = el("div", "job-widget");
    const head = el("div", "job-head");
    head.appendChild(el("span", "job-label", cfg.label || cfg.kind));
    const cancelBtn = el("button", "lib-btn ghost", "Cancel");
    cancelBtn.type = "button";
    head.appendChild(cancelBtn);
    box.appendChild(head);
    const bar = el("div", "job-bar");
    const fill = el("div", "job-fill indeterminate");
    bar.appendChild(fill);
    box.appendChild(bar);
    const meta = el("div", "job-meta", "Starting…");
    box.appendChild(meta);
    container.appendChild(box);

    const ctl = run(cfg.kind, cfg.targetId, cfg.options, {
      onUpdate(st) {
        const p = st.progress || {};
        const pct = p.total
          ? Math.min(100, Math.round((100 * (p.done || 0)) / p.total))
          : null;
        if (pct === null) {
          fill.classList.add("indeterminate");
          fill.style.width = "40%";
        } else {
          fill.classList.remove("indeterminate");
          fill.style.width = pct + "%";
        }
        meta.textContent = (st.phase ? st.phase + " — " : "") + progressText(st);
        if (cfg.onUpdate) { try { cfg.onUpdate(st); } catch (_) {} }
      },
    });

    cancelBtn.addEventListener("click", () => {
      cancelBtn.disabled = true;
      cancelBtn.textContent = "Cancelling…";
      ctl.cancel();
    });

    ctl.promise.then((st) => {
      cancelBtn.remove();
      fill.classList.remove("indeterminate");
      fill.style.width = "100%";
      const ok = isSuccess(st);
      fill.classList.add(ok ? "ok" : "bad");
      box.classList.add("done");
      meta.textContent = summarize(st);
      if (cfg.onDone) { try { cfg.onDone(st); } catch (_) {} }
    }).catch((err) => {
      cancelBtn.remove();
      fill.classList.remove("indeterminate");
      fill.classList.add("bad");
      box.classList.add("done");
      meta.textContent = "Job failed: " + err;
      if (cfg.onDone) {
        try { cfg.onDone({ status: "error", error: { error: String(err) } }); }
        catch (_) {}
      }
    });
    return ctl;
  }

  return { run, mount, summarize, isSuccess };
})();
