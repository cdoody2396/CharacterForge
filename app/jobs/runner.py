"""``JobRunner`` — the long-running-job contract (Stage 5.5a — DECISIONS.md §3).

The Stage-3 image operations are slow ([HARDWARE] measurements: train 31.5 min,
bootstrap ~15 min, catalog 287 s) and were shipped as **synchronous** bridges —
``image_generate_catalog`` is already wired into ``library.js``, so the shipped
app has a live five-minute silent hang. This runner backgrounds them:

* **Single worker thread** draining a **bounded** queue. One heavy job runs at a
  time — this *is* the single GPU slot (§3 forbids two resident heavy models),
  made structural rather than incidental.
* **Progress by polling.** ``status(job_id)`` reads the in-memory record (fast,
  non-blocking); the UI polls at ~1 Hz. No ``window.evaluate_js`` push (it can
  deadlock the bridge thread and is fragile across view switches).
* **Cancellation** via the :class:`~app.jobs.token.CancelToken` published on a
  thread-local (see ``token.py``): cooperative for the in-process loops
  (``CancellableEngine`` raises ``JobCancelled`` between frames) and
  ``Popen.terminate`` for the kohya subprocess. The VRAM slot is released in a
  ``finally`` on every path.
* **Persistence.** Each record is written to ``data/jobs/<job_id>.json`` on every
  state change; a hard kill leaves a recoverable record that :meth:`reconcile`
  reaps at next boot (mirroring the Stage-4/5 vouching sweeps).

The runner never rewrites the synchronous service methods — it wraps them: a job
``fn`` is a service method bound to its arguments, run on the worker thread.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from ..model.store import atomic_write_json

# A job record is our own JSON (not a hand-edited manifest), so a compact guard
# suffices — the same never-raise-through-the-sweep stance as ARTIFACT_LOAD_ERRORS.
_LOAD_ERRORS = (OSError, json.JSONDecodeError, ValueError, TypeError)

# job_ids are minted here as uuid4 hex; a job_id arriving from the UI is
# untrusted, so it is shape-checked before it ever touches the filesystem.
_JOB_ID_RE = re.compile(r"[0-9a-f]{32}\Z")

# Terminal statuses — a record in any of these is finished and safe to prune.
_TERMINAL = frozenset({"done", "cancelled", "error"})

# The heavy-job kinds. The bridge maps each to a synchronous ImageService method.
JOB_KINDS = ("bootstrap", "train", "catalog", "on_demand", "matte", "background")


class _Job:
    """In-memory job handle: the mutable record plus its cancel token."""

    __slots__ = ("job_id", "kind", "target_id", "status", "phase",
                 "started_at", "finished_at", "result", "error", "token")

    def __init__(self, job_id: str, kind: str, target_id: Optional[str],
                 token):
        self.job_id = job_id
        self.kind = kind
        self.target_id = target_id
        self.status = "queued"
        self.phase = "queued"
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.result: Optional[dict] = None
        self.error: Optional[dict] = None
        self.token = token

    def record(self) -> dict:
        """The JSON-serializable record (no token)."""
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "target_id": self.target_id,
            "status": self.status,
            "phase": self.phase,
            "progress": self.token.progress(),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
        }


class JobRunner:
    def __init__(self, jobs_dir: Path, *, audit: Any = None,
                 queue_size: int = 16, retain_seconds: Optional[int] = 7 * 24 * 3600,
                 release: Optional[Callable[[], None]] = None,
                 clock: Callable[[], float] = time.time):
        self._jobs_dir = Path(jobs_dir)
        self._audit = audit
        self._retain_seconds = retain_seconds
        self._release = release            # best-effort VRAM-slot release (unload)
        self._clock = clock
        self._queue: "queue.Queue[str]" = queue.Queue(maxsize=_coerce_queue_size(queue_size))
        self._jobs: dict[str, _Job] = {}
        self._fns: dict[str, Callable[[], dict]] = {}
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, name="job-runner",
                                        daemon=True)
        self._worker.start()

    # -- submission ----------------------------------------------------------

    def submit(self, kind: str, fn: Callable[[], dict], *,
               target_id: Optional[str] = None, total: Optional[int] = None) -> dict:
        """Enqueue ``fn`` (a bound service method) as a background job. Returns
        immediately with a ``job_id``; a full queue returns a structured busy
        result rather than blocking."""
        from .token import CancelToken

        job_id = uuid.uuid4().hex
        job = _Job(job_id, kind, target_id, CancelToken(total=total))
        with self._lock:
            self._jobs[job_id] = job
            self._fns[job_id] = fn
        self._persist(job)
        try:
            self._queue.put_nowait(job_id)
        except queue.Full:
            with self._lock:
                self._jobs.pop(job_id, None)
                self._fns.pop(job_id, None)
            try:
                (self._jobs_dir / f"{job_id}.json").unlink()
            except OSError:
                pass
            return {"ok": False, "kind": "job", "error": "the job queue is full — "
                    "wait for the running job to finish", "reason": "queue_full"}
        return {"ok": True, "kind": "job", "job_id": job_id, "status": "queued"}

    # -- polling / control ---------------------------------------------------

    def status(self, job_id: Any) -> dict:
        """Non-blocking status. Reads the live in-memory record if the job
        belongs to this process, else the persisted record on disk."""
        jid = self._safe_id(job_id)
        if jid is None:
            return {"ok": False, "kind": "job", "error": "unknown job id"}
        with self._lock:
            job = self._jobs.get(jid)
        if job is not None:
            return {"ok": True, "kind": "job", **job.record()}
        data = self._read(jid)
        if data is None:
            return {"ok": False, "kind": "job", "error": "no such job",
                    "reason": "not_found"}
        return {"ok": True, "kind": "job", **data}

    def cancel(self, job_id: Any) -> dict:
        jid = self._safe_id(job_id)
        if jid is None:
            return {"ok": False, "kind": "job", "error": "unknown job id"}
        with self._lock:
            job = self._jobs.get(jid)
        if job is None:
            return {"ok": False, "kind": "job", "error": "no such active job",
                    "reason": "not_found"}
        job.token.cancel()
        return {"ok": True, "kind": "job", "job_id": jid, "status": job.status,
                "cancelling": True}

    def list_jobs(self) -> dict:
        with self._lock:
            live = [job.record() for job in self._jobs.values()]
        return {"ok": True, "kind": "job", "jobs": live}

    def wait_for(self, job_id: Any, timeout: float = 30.0,
                 interval: float = 0.02) -> Optional[dict]:
        """Block until the job reaches a terminal status (or timeout). For
        scripted smokes and tests; the UI polls ``status`` instead."""
        jid = self._safe_id(job_id)
        if jid is None:
            return None
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            res = self.status(jid)
            if res.get("status") in _TERMINAL:
                return res
            time.sleep(interval)
        return self.status(jid)

    # -- the worker ----------------------------------------------------------

    def _run(self) -> None:
        from .token import JobCancelled, set_current_token

        while True:
            job_id = self._queue.get()
            try:
                with self._lock:
                    job = self._jobs.get(job_id)
                    fn = self._fns.get(job_id)
                if job is None or fn is None:
                    continue
                # A cancel that landed while the job was still queued.
                if job.token.cancelled:
                    self._finish(job, status="cancelled")
                    continue
                job.status = "running"
                job.phase = "running"
                job.started_at = self._clock()
                self._persist(job)
                set_current_token(job.token)
                try:
                    result = fn()
                except JobCancelled:
                    self._finish(job, status="cancelled")
                    continue
                except BaseException as exc:  # never let the worker thread die
                    self._finish(job, status="error",
                                 error={"kind": "job_error", "error": str(exc)})
                    continue
                finally:
                    set_current_token(None)
                    self._release_slot()
                # token.cancelled is authoritative: a cancelled train returns a
                # normal {"ok": False, "kind": "train_failed"} dict (subprocess
                # terminated), indistinguishable from a real failure by value.
                if job.token.cancelled:
                    self._finish(job, status="cancelled", result=_as_dict(result))
                elif isinstance(result, dict) and result.get("ok") is False:
                    self._finish(job, status="error", result=result,
                                 error={"kind": result.get("kind", "error"),
                                        "error": result.get("error", "")})
                else:
                    self._finish(job, status="done", result=_as_dict(result))
            finally:
                with self._lock:
                    self._fns.pop(job_id, None)  # drop the bound-method closure
                self._queue.task_done()

    def _release_slot(self) -> None:
        if self._release is None:
            return
        try:
            self._release()  # idempotent engine.unload() — belt-and-suspenders
        except Exception:
            pass

    def _finish(self, job: _Job, *, status: str, result: Optional[dict] = None,
                error: Optional[dict] = None) -> None:
        job.status = status
        job.phase = status
        job.finished_at = self._clock()
        if result is not None:
            job.result = result
        if error is not None:
            job.error = error
        self._persist(job)

    # -- persistence ---------------------------------------------------------

    def _path(self, job_id: str) -> Path:
        return self._jobs_dir / f"{job_id}.json"

    def _persist(self, job: _Job) -> None:
        try:
            atomic_write_json(self._path(job.job_id), job.record())
        except (OSError, ValueError, TypeError):
            pass  # a persist fault must never crash the worker or the bridge

    def _read(self, job_id: str) -> Optional[dict]:
        path = self._path(job_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except _LOAD_ERRORS:
            return None

    def _safe_id(self, job_id: Any) -> Optional[str]:
        if not isinstance(job_id, str) or not _JOB_ID_RE.match(job_id):
            return None
        return job_id

    # -- startup reap sweep --------------------------------------------------

    def reconcile(self) -> dict:
        """Reap ``data/jobs/*.json`` left by a hard kill, mirroring the Stage-4/5
        vouching sweeps: scan only ``.json`` directly in the jobs dir; a corrupt
        record is skipped, never blind-deleted. A fresh process owns no running
        jobs, so any persisted **non-terminal** record is a dead orphan → marked
        terminal ``interrupted``. Terminal records past the retention window are
        pruned. Idempotent; safe to call from the UI as well as at startup."""
        counts = {"interrupted": 0, "pruned": 0, "skipped": 0}
        if not self._jobs_dir.is_dir():
            return {"ok": True, "kind": "job", **counts}
        now = self._clock()
        for path in sorted(self._jobs_dir.iterdir()):
            if path.suffix != ".json" or not path.is_file():
                continue
            with self._lock:
                owned = path.stem in self._jobs
            if owned:
                continue  # a live job this process is running — never touch it
            data = self._read(path.stem)
            if data is None or not isinstance(data, dict):
                counts["skipped"] += 1
                continue
            status = data.get("status")
            if status not in _TERMINAL:
                data["status"] = "error"
                data["phase"] = "error"
                data["error"] = {"kind": "interrupted",
                                 "error": "the app closed while this job was running"}
                data["finished_at"] = now
                try:
                    atomic_write_json(path, data)
                    counts["interrupted"] += 1
                except (OSError, ValueError, TypeError):
                    counts["skipped"] += 1
            elif self._retain_seconds is not None:
                finished = data.get("finished_at") or 0
                try:
                    stale = (now - float(finished)) > self._retain_seconds
                except (TypeError, ValueError):
                    stale = True
                if stale:
                    try:
                        path.unlink()
                        counts["pruned"] += 1
                    except OSError:
                        counts["skipped"] += 1
        if self._audit is not None:
            try:
                self._audit.log("jobs_reconciled", **counts)
            except Exception:
                pass
        return {"ok": True, "kind": "job", **counts}


def _as_dict(result: Any) -> Optional[dict]:
    return result if isinstance(result, dict) else None


def _coerce_queue_size(value: Any, default: int = 16) -> int:
    """A hand-edited ``jobs.queue_size`` (null / "16x" / a float) must never
    crash the launch under pythonw — mirrors the shell's ``_safe_int`` posture.
    Bad values degrade to the default; the bound is always >= 1."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, n)
