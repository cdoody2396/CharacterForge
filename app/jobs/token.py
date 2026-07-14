"""Cancellation token + the thread-local seam that reaches the byte-frozen
image-service loops (Stage 5.5a — DECISIONS.md §3).

The Stage-3 ``ImageService`` bridge methods are synchronous and stay
byte-unchanged (922 tests + every hardware harness call them). A job cannot
add a ``cancel`` parameter to them, so the running job's ``CancelToken`` is
published on a **thread-local** that the two out-of-band seams read:

* ``CancellableEngine`` (``cancellable.py``) — checks the token before each
  ``generate*`` call, so the in-process per-frame loops (bootstrap per
  candidate, catalog / on-demand per cell) cancel between frames and report
  per-frame progress.
* ``_KohyaSubprocessTrainer.train`` (``imagegen/lora.py``) — registers its
  ``Popen.terminate`` on the token, so a cancel kills the kohya subprocess.

When no job is running (``current_token()`` is ``None``) both seams are pure
pass-throughs, which is what keeps the synchronous path byte-identical.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional


class JobCancelled(Exception):
    """Raised by :class:`CancellableEngine` inside a wrapped ``generate*`` call
    when the running job's token is cancelled.

    Subclasses ``Exception`` **directly** — NOT ``RuntimeError`` / ``ValueError``
    — so none of the image loops' ``except`` tuples (``EngineBusy(RuntimeError)``,
    ``EngineUnavailable``, ``GenerationFailed``, ``ReferenceUnreadable``,
    ``ValueError``) catch it. It unwinds through each loop's
    ``finally: self._engine.unload()`` (the VRAM slot is freed) and propagates
    uncaught to the :class:`~app.jobs.runner.JobRunner` worker, which records the
    job as cancelled."""


class CancelToken:
    """One job's cancellation flag + progress counter + optional subprocess
    terminate hook. Every mutator is lock-guarded because ``cancel`` fires from
    the bridge thread while the worker thread runs the job."""

    def __init__(self, total: Optional[int] = None):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._terminate: Optional[Callable[[], None]] = None
        self._done = 0
        self._total = total

    # -- cancellation --------------------------------------------------------

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        """Set the flag and, if a subprocess terminate hook is registered, fire
        it. Guarded so a hook that raises (Windows ``OSError`` when the child
        already exited) never propagates to the caller."""
        with self._lock:
            self._event.set()
            terminate = self._terminate
        _fire(terminate)

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise JobCancelled()

    def register(self, terminate: Callable[[], None]) -> None:
        """Register a subprocess terminate hook. If cancel already raced ahead
        of the subprocess launch, fire immediately so the child does not run
        unmonitored."""
        with self._lock:
            self._terminate = terminate
            already = self._event.is_set()
        if already:
            _fire(terminate)

    def deregister(self) -> None:
        """Drop the terminate hook once the subprocess has exited — a stale
        hook must never fire on a later, unrelated job."""
        with self._lock:
            self._terminate = None

    # -- progress ------------------------------------------------------------

    def tick(self, n: int = 1) -> None:
        with self._lock:
            self._done += n

    def progress(self) -> dict:
        with self._lock:
            return {"done": self._done, "total": self._total}


def _fire(terminate: Optional[Callable[[], None]]) -> None:
    if terminate is None:
        return
    try:
        terminate()
    except OSError:
        pass  # the child already exited (Windows TerminateProcess -> OSError)


# -- the thread-local current token -----------------------------------------

_local = threading.local()


def current_token() -> Optional[CancelToken]:
    """The token of the job running on THIS thread, or ``None`` when no job is
    active (the synchronous / test / hardware-harness path)."""
    return getattr(_local, "token", None)


def set_current_token(token: Optional[CancelToken]) -> None:
    """Set (or clear, with ``None``) the running job's token for this thread.
    Called only by the :class:`~app.jobs.runner.JobRunner` worker around each
    job."""
    _local.token = token
