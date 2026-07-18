"""Shared module-level pieces of the image service: artifact-load error
taxonomy, UI-input clamps, and small value objects used across the
service_* mixin modules. A leaf: imports nothing from service.py."""

from __future__ import annotations


import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..model import InvalidId
from . import matte as matte_mod

@dataclass(frozen=True)
class _BatchFrame:
    """One generated bootstrap candidate before culling."""

    candidate_id: str
    abs_path: Path
    rel_path: str
    seed: int


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Everything a hand-edited JSON artifact can raise through json.loads +
# from_dict, mapped to a structured *_corrupt/io by every loader guard (the
# 3d fix-across-loaders precedent): ValueError/TypeError (bad shapes/values,
# incl. json.JSONDecodeError and InvalidId subclasses), LookupError (missing
# required key), OverflowError (int(Infinity) — json.loads accepts
# Infinity/1e999 as floats), AttributeError (.get on a non-dict nested value
# — review catch, escaped every manifest bridge), RecursionError
# (pathologically nested JSON — red-team catch), OSError (fs faults).
ARTIFACT_LOAD_ERRORS = (
    OSError, json.JSONDecodeError, ValueError, TypeError, LookupError,
    OverflowError, AttributeError, RecursionError, InvalidId,
)

# Profile-tile thumbnail bound (5.5d). Default fits a grid tile; clamped so a
# hand-edited request can neither ask for a 0-px nor a memory-blowing decode.
THUMBNAIL_DEFAULT_PX = 384
THUMBNAIL_MIN_PX = 64
THUMBNAIL_MAX_PX = 1024


def _coerce_thumb_px(value: object) -> int:
    """Clamp a UI-supplied thumbnail size into ``[64, 1024]``, defaulting a
    missing/non-finite/non-numeric value (the recurring hand-edit hazard —
    Infinity/NaN into ``int()`` raises) to 384 rather than raising."""
    try:
        px = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return THUMBNAIL_DEFAULT_PX
    return max(THUMBNAIL_MIN_PX, min(THUMBNAIL_MAX_PX, px))


# Avatar-candidate batch bound (5.5d create-wizard reference step). Small — each
# render is ~15 s on the target and the user only picks one; a bad hand-edit
# neither renders zero nor a runaway batch.
CANDIDATE_DEFAULT_N = 4
CANDIDATE_MAX_N = 8


def _coerce_candidate_count(value: object) -> int:
    """Clamp a UI-supplied avatar-candidate count into ``[1, 8]``, defaulting a
    missing/non-finite/non-numeric value to 4 rather than raising."""
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return CANDIDATE_DEFAULT_N
    return max(1, min(CANDIDATE_MAX_N, n))


def _humanize(value: object) -> str:
    """A booru/id token as a picker label ('over_shoulder' -> 'Over Shoulder').
    Display-only — the id, never this string, is what reaches generation."""
    return str(value).replace("_", " ").replace("-", " ").strip().title() \
        or str(value)


class _MatteEscalation:
    """Owns the escalation (BiRefNet) matte toolkit for ONE matte run (5.5g,
    3f residual). Built LAZILY on the first frame that crosses the escalation
    coverage threshold, so a wide-frame-only catalog never loads the ~973 MB
    model. A missing/corrupt escalation model disables escalation for the run
    (the primary matte is used) — it never raises. Tracks how many frames were
    escalated for the manifest provenance."""

    def __init__(self, factory, settings, ec):
        self._factory = factory
        self._settings = settings
        self._ec = ec
        self._toolkit = None
        self._disabled = False
        self.escalated = 0

    @property
    def coverage(self) -> float:
        return self._ec.coverage

    @property
    def config(self):
        return self._ec.config

    def toolkit(self):
        """Return the escalation MatteToolkit, building it once on first use.
        None if the model is absent or the build failed (escalation disabled)."""
        if self._toolkit is not None or self._disabled:
            return self._toolkit
        path = matte_mod.matting_escalation_model_path(self._settings)
        if path is None or not path.is_file():  # cheap, import-free guard
            self._disabled = True
            return None
        try:
            self._toolkit = self._factory(self._settings, self._ec.config)
        except Exception:
            self._disabled = True
        return self._toolkit

    def close(self) -> None:
        if self._toolkit is not None:
            try:
                self._toolkit.close()
            finally:
                self._toolkit = None

