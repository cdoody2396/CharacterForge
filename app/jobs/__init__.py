"""Long-running-job contract (Stage 5.5a — DECISIONS.md §3).

Backgrounds the slow synchronous Stage-3 image operations behind a single-slot
worker with polling status and cooperative cancellation, WITHOUT modifying the
byte-frozen ``ImageService`` methods. See ``runner.py`` for the contract.
"""

from .cancellable import CancellableEngine
from .runner import JOB_KINDS, JobRunner
from .token import CancelToken, JobCancelled, current_token, set_current_token

__all__ = [
    "CancellableEngine",
    "CancelToken",
    "JobCancelled",
    "JobRunner",
    "JOB_KINDS",
    "current_token",
    "set_current_token",
]
