"""``CancellableEngine`` — a transparent proxy over ``ImageEngine`` that makes
the byte-frozen Stage-3 image loops cooperatively cancellable and
progress-reporting WITHOUT modifying them (Stage 5.5a — DECISIONS.md §3).

The only dependency the per-frame loops (``_generate_batch``,
``_catalog_generate_pass``) cross on every iteration is the engine's
``generate*`` call. This proxy intercepts exactly those three calls: before
delegating it checks the running job's token (raising :class:`JobCancelled`,
which the loops do not catch, so it unwinds through their ``finally: unload()``)
and after a successful frame it ticks the token's progress counter.

When ``current_token()`` is ``None`` (the synchronous / test / hardware-harness
path — always the main thread, no job) it is a pure pass-through, so the 922
tests and every scripted harness see byte-identical behavior. Installed in
``build_image_service``; a test that injects a fake ``engine=`` bypasses it.
"""

from __future__ import annotations

from typing import Any

from .token import current_token


class CancellableEngine:
    def __init__(self, engine: Any):
        # object.__setattr__ so __getattr__ never recurses before _engine exists.
        object.__setattr__(self, "_engine", engine)

    # -- the three intercepted heavy calls -----------------------------------

    def generate(self, *args, **kwargs):
        return self._guarded(self._engine.generate, *args, **kwargs)

    def generate_identity(self, *args, **kwargs):
        return self._guarded(self._engine.generate_identity, *args, **kwargs)

    def generate_catalog(self, *args, **kwargs):
        return self._guarded(self._engine.generate_catalog, *args, **kwargs)

    def _guarded(self, fn, *args, **kwargs):
        token = current_token()
        if token is not None:
            token.raise_if_cancelled()  # cancel observed at the frame boundary
        result = fn(*args, **kwargs)
        if token is not None:
            token.tick()  # count a COMPLETED frame
        return result

    # -- explicit pass-through of the surface ImageService uses --------------
    #
    # (Enumerated rather than left to __getattr__ so the delegated shape is
    # visible and stable; __getattr__ is only a safety net.)

    def status(self, *args, **kwargs):
        return self._engine.status(*args, **kwargs)

    def load(self, *args, **kwargs):
        return self._engine.load(*args, **kwargs)

    def unload(self, *args, **kwargs):
        return self._engine.unload(*args, **kwargs)

    def close(self, *args, **kwargs):
        return self._engine.close(*args, **kwargs)

    def checkpoint_path(self, *args, **kwargs):
        return self._engine.checkpoint_path(*args, **kwargs)

    def config_dir(self, *args, **kwargs):
        return self._engine.config_dir(*args, **kwargs)

    @property
    def loaded_checkpoint(self):
        return self._engine.loaded_checkpoint

    @property
    def loaded_ip_config(self):
        return self._engine.loaded_ip_config

    @property
    def loaded_lora(self):
        return self._engine.loaded_lora

    @property
    def loaded(self):
        return self._engine.loaded

    @property
    def engine(self):
        """The wrapped engine (for callers that need the concrete instance)."""
        return self._engine

    def __getattr__(self, name: str):
        if name == "_engine":  # not yet set / being unpickled
            raise AttributeError(name)
        return getattr(self._engine, name)
