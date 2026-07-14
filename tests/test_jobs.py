"""Stage 5.5a — the long-running-job contract: JobRunner (single-slot worker,
bounded queue, persistence, reconcile reaper), the CancelToken thread-local
seam, and the CancellableEngine proxy. All [HERE] — no GPU, no weights."""

import json
import threading
import time

import pytest

from app.jobs import (
    CancellableEngine,
    CancelToken,
    JobCancelled,
    JobRunner,
    current_token,
    set_current_token,
)


@pytest.fixture()
def runner(tmp_path):
    r = JobRunner(tmp_path / "jobs")
    yield r


# -- CancelToken -------------------------------------------------------------


def test_token_progress_and_cancel():
    tok = CancelToken(total=10)
    assert tok.progress() == {"done": 0, "total": 10}
    tok.tick()
    tok.tick(2)
    assert tok.progress() == {"done": 3, "total": 10}
    assert tok.cancelled is False
    tok.cancel()
    assert tok.cancelled is True
    with pytest.raises(JobCancelled):
        tok.raise_if_cancelled()


def test_token_register_fires_terminate_on_cancel():
    tok = CancelToken()
    fired = []
    tok.register(lambda: fired.append(True))
    assert fired == []
    tok.cancel()
    assert fired == [True]


def test_token_register_after_cancel_fires_immediately():
    # cancel raced ahead of the subprocess launch -> the hook fires on register.
    tok = CancelToken()
    tok.cancel()
    fired = []
    tok.register(lambda: fired.append(True))
    assert fired == [True]


def test_token_terminate_oserror_is_swallowed():
    tok = CancelToken()

    def boom():
        raise OSError("child already exited")

    tok.register(boom)
    tok.cancel()  # must not raise (Windows terminate-after-exit)


def test_job_cancelled_is_not_caught_by_engine_except_tuples():
    # The loops catch (EngineBusy(RuntimeError), EngineUnavailable,
    # GenerationFailed, ReferenceUnreadable, ValueError). JobCancelled must
    # slip past all of them, so it unwinds through their finally:unload.
    assert not issubclass(JobCancelled, RuntimeError)
    assert not issubclass(JobCancelled, ValueError)
    assert issubclass(JobCancelled, Exception)


# -- CancellableEngine -------------------------------------------------------


class _FakeEngine:
    def __init__(self):
        self.calls = 0
        self.unloaded = 0
        self.loaded_checkpoint = "ckpt"

    def generate(self, *a, **k):
        self.calls += 1
        return "IMG"

    def generate_identity(self, *a, **k):
        self.calls += 1
        return "IMG"

    def generate_catalog(self, *a, **k):
        self.calls += 1
        return "IMG"

    def unload(self):
        self.unloaded += 1

    def status(self):
        return {"ok": True}


def test_cancellable_engine_is_passthrough_without_a_token():
    eng = _FakeEngine()
    wrapped = CancellableEngine(eng)
    set_current_token(None)
    # no token -> delegate, no tick, no raise (the 922-tests / harness path)
    assert wrapped.generate_catalog("r", "l") == "IMG"
    assert wrapped.status() == {"ok": True}
    assert wrapped.loaded_checkpoint == "ckpt"
    assert eng.calls == 1
    assert wrapped.engine is eng


def test_cancellable_engine_ticks_and_raises_with_a_token():
    eng = _FakeEngine()
    wrapped = CancellableEngine(eng)
    tok = CancelToken()
    set_current_token(tok)
    try:
        wrapped.generate_catalog("r", "l")
        assert tok.progress()["done"] == 1  # counted a completed frame
        tok.cancel()
        with pytest.raises(JobCancelled):
            wrapped.generate_catalog("r", "l")  # cancel checked BEFORE the call
        assert eng.calls == 1  # the second call never reached the engine
    finally:
        set_current_token(None)


# -- JobRunner: happy path + progress ----------------------------------------


def test_runner_runs_a_job_to_done_with_progress(runner):
    eng = _FakeEngine()
    wrapped = CancellableEngine(eng)

    def loop():
        try:
            for _ in range(5):
                wrapped.generate_catalog("r", "l")
        finally:
            eng.unload()
        return {"ok": True, "frames": 5}

    sub = runner.submit("catalog", loop, target_id="cid", total=5)
    assert sub["ok"] and sub["kind"] == "job"
    final = runner.wait_for(sub["job_id"], timeout=5)
    assert final["status"] == "done"
    assert final["progress"] == {"done": 5, "total": 5}
    assert final["result"] == {"ok": True, "frames": 5}
    assert eng.unloaded >= 1


def test_runner_persists_the_record_to_disk(runner, tmp_path):
    def quick():
        return {"ok": True}

    sub = runner.submit("catalog", quick)
    runner.wait_for(sub["job_id"], timeout=5)
    path = tmp_path / "jobs" / f"{sub['job_id']}.json"
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == "done" and data["kind"] == "catalog"


def test_runner_failed_result_becomes_error_status(runner):
    def fails():
        return {"ok": False, "kind": "engine", "error": "no GPU"}

    sub = runner.submit("catalog", fails)
    final = runner.wait_for(sub["job_id"], timeout=5)
    assert final["status"] == "error"
    assert final["error"]["kind"] == "engine"
    assert final["result"] == {"ok": False, "kind": "engine", "error": "no GPU"}


def test_runner_exception_in_fn_never_kills_the_worker(runner):
    def raises():
        raise RuntimeError("boom")

    sub = runner.submit("catalog", raises)
    final = runner.wait_for(sub["job_id"], timeout=5)
    assert final["status"] == "error" and final["error"]["kind"] == "job_error"
    # the worker survives -> a subsequent job still runs
    sub2 = runner.submit("catalog", lambda: {"ok": True})
    final2 = runner.wait_for(sub2["job_id"], timeout=5)
    assert final2["status"] == "done"


# -- JobRunner: cancellation -------------------------------------------------


def test_runner_cancels_an_in_process_loop_and_releases_the_slot(runner):
    # The DoD "cancel works on an in-process loop": the loop mirrors the real
    # service loops' `finally: engine.unload()` + per-frame engine call. Cancel
    # -> JobCancelled from the proxy -> finally unloads -> status cancelled.
    eng = _FakeEngine()
    wrapped = CancellableEngine(eng)
    started = threading.Event()

    def loop_forever():
        try:
            while True:
                wrapped.generate_catalog("r", "l")
                started.set()
                time.sleep(0.005)
        finally:
            eng.unload()
        return {"ok": True}

    sub = runner.submit("bootstrap", loop_forever)
    assert started.wait(timeout=5)
    runner.cancel(sub["job_id"])
    final = runner.wait_for(sub["job_id"], timeout=5)
    assert final["status"] == "cancelled"
    assert eng.unloaded >= 1  # VRAM slot released on the cancel path


def test_runner_cancel_of_a_queued_job_never_runs_it(runner):
    # Occupy the single worker with a blocker, queue a second job, cancel it
    # while still queued -> it is finished as cancelled without running.
    release = threading.Event()
    ran_second = []

    def blocker():
        release.wait(timeout=5)
        return {"ok": True}

    def second():
        ran_second.append(True)
        return {"ok": True}

    first = runner.submit("catalog", blocker)
    queued = runner.submit("catalog", second)
    runner.cancel(queued["job_id"])
    release.set()
    final = runner.wait_for(queued["job_id"], timeout=5)
    runner.wait_for(first["job_id"], timeout=5)
    assert final["status"] == "cancelled"
    assert ran_second == []  # never executed


def test_runner_cancelled_failure_dict_is_reported_as_cancelled(runner):
    # The train cancel path: terminate -> the method returns a normal
    # {"ok": False, "kind": "train_failed"} dict. token.cancelled is
    # authoritative, so the job is CANCELLED, not error.
    tok_seen = []

    def fake_train():
        tok = current_token()
        tok_seen.append(tok)
        tok.cancel()  # simulate an external cancel that terminated the subprocess
        return {"ok": False, "kind": "train_failed", "error": "trainer exited -15"}

    sub = runner.submit("train", fake_train)
    final = runner.wait_for(sub["job_id"], timeout=5)
    assert final["status"] == "cancelled"
    assert tok_seen and tok_seen[0] is not None


# -- JobRunner: bounded queue ------------------------------------------------


def test_runner_full_queue_refuses_new_submissions(tmp_path):
    r = JobRunner(tmp_path / "jobs", queue_size=1)
    release = threading.Event()

    def blocker():
        release.wait(timeout=5)
        return {"ok": True}

    a = r.submit("catalog", blocker)          # takes the worker
    # Fill the queue (size 1) then overflow. The worker may pull one, so try a
    # few to reliably hit Full.
    refused = None
    for _ in range(5):
        res = r.submit("catalog", lambda: {"ok": True})
        if not res["ok"]:
            refused = res
            break
    assert refused is not None
    assert refused["reason"] == "queue_full" and refused["kind"] == "job"
    release.set()
    r.wait_for(a["job_id"], timeout=5)


# -- JobRunner: status / list / bad ids --------------------------------------


def test_runner_tolerates_a_hand_edited_queue_size(tmp_path):
    # A null / non-numeric jobs.queue_size must not crash launch (pythonw has no
    # console) — the runner coerces to the default, mirroring _safe_int.
    for bad in (None, "16x", float("nan"), "", {}):
        r = JobRunner(tmp_path / f"jobs-{id(bad)}", queue_size=bad)
        sub = r.submit("catalog", lambda: {"ok": True})
        assert r.wait_for(sub["job_id"], timeout=5)["status"] == "done"


def test_runner_status_of_unknown_id_is_structured(runner):
    assert runner.status("not-a-real-id")["ok"] is False
    # a well-formed but unknown id
    res = runner.status("0" * 32)
    assert res["ok"] is False and res["reason"] == "not_found"


def test_runner_rejects_unsafe_job_ids(runner):
    for bad in ("../escape", "", None, "a/b", "A" * 32):
        assert runner.status(bad)["ok"] is False
        assert runner.cancel(bad)["ok"] is False


def test_runner_list_and_json_safety(runner):
    sub = runner.submit("catalog", lambda: {"ok": True})
    runner.wait_for(sub["job_id"], timeout=5)
    listed = runner.list_jobs()
    assert listed["ok"] and any(j["job_id"] == sub["job_id"] for j in listed["jobs"])
    json.dumps(runner.status(sub["job_id"]), allow_nan=False)  # never NaN/Infinity


# -- JobRunner: reconcile reaper ---------------------------------------------


def _write_job(tmp_path, job_id, **fields):
    d = tmp_path / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"job_id": job_id, "kind": "train", "status": "running"}
    rec.update(fields)
    (d / f"{job_id}.json").write_text(json.dumps(rec), encoding="utf-8")
    return d / f"{job_id}.json"


def test_reconcile_marks_a_killed_running_job_interrupted(tmp_path):
    path = _write_job(tmp_path, "a" * 32, status="running", finished_at=None)
    r = JobRunner(tmp_path / "jobs")
    res = r.reconcile()
    assert res["interrupted"] == 1
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == "error" and data["error"]["kind"] == "interrupted"


def test_reconcile_skips_a_corrupt_record_without_deleting_it(tmp_path):
    d = tmp_path / "jobs"
    d.mkdir(parents=True)
    corrupt = d / ("b" * 32 + ".json")
    corrupt.write_text("{not json", encoding="utf-8")
    r = JobRunner(tmp_path / "jobs")
    res = r.reconcile()
    assert res["skipped"] == 1
    assert corrupt.exists()  # never blind-deleted (vouching: trusted or nothing)


def test_reconcile_prunes_old_terminal_records(tmp_path):
    old = _write_job(tmp_path, "c" * 32, status="done", finished_at=1.0)
    recent = _write_job(tmp_path, "d" * 32, status="done",
                        finished_at=time.time())
    r = JobRunner(tmp_path / "jobs", retain_seconds=3600)
    res = r.reconcile()
    assert res["pruned"] == 1
    assert not old.exists() and recent.exists()


def test_reconcile_leaves_a_live_owned_job_untouched(runner, tmp_path):
    # A job THIS process is running must never be reaped by a reconcile call.
    release = threading.Event()
    sub = runner.submit("catalog", lambda: release.wait(timeout=5) or {"ok": True})
    res = runner.reconcile()  # while the job is queued/running in-memory
    assert res["interrupted"] == 0
    release.set()
    runner.wait_for(sub["job_id"], timeout=5)
