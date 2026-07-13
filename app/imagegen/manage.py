"""Library & management logic over the imagegen artifacts (Stage 4 — §14).

Pure, sandbox-clean pieces the Stage-4 layer shares with the image service:

- ``LibraryConfig`` / ``coerce_library_config``: the §14 disk thresholds —
  the automatic per-character LRU cap on the on-demand cache (the backstop)
  and the deletion-recommendation threshold (the user-facing signal). These
  resolve the deferred "exact disk thresholds + LRU caps" item; defaults are
  sized against measured hardware artifacts (~2.2 MB per cached state).
- ``select_evictions``: the pure LRU pick — given the cache entries with
  their measured on-disk costs, choose which to evict to get back under the
  cap. Deletion itself stays in the service (it owns the purge trust rules).

No heavy imports; ``import app.imagegen.manage`` is clean without torch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import Settings
from ..model import CatalogEntry

# Floors/ceilings for the byte knobs: a cap below one frame's cost would
# thrash (evict-after-every-generate), and json.loads accepts Infinity.
_MIN_BYTES = 8 * 1024 * 1024           # 8 MB
_MAX_BYTES = 1024 * 1024 * 1024 * 1024  # 1 TB


@dataclass(frozen=True)
class LibraryConfig:
    """§14 disk-management knobs (settings ``library.*``)."""

    cache_cap_bytes: int = 268_435_456        # 256 MB — automatic LRU cap
    recommend_cache_bytes: int = 201_326_592  # 192 MB — deletion recommendation


def coerce_library_config(settings: Settings) -> LibraryConfig:
    """Build a LibraryConfig from ``library.*``, coerced defensively so a
    hand-edited Infinity/NaN/string never reaches the eviction math (the
    catalog/cull/train config stance). Bad values -> code defaults; clamped."""
    d = LibraryConfig()

    def _bytes(key: str, default: int) -> int:
        try:
            v = float(settings.get(f"library.{key}", default))
        except (TypeError, ValueError, OverflowError):
            return default
        if not math.isfinite(v):
            return default
        return int(min(_MAX_BYTES, max(_MIN_BYTES, v)))

    return LibraryConfig(
        cache_cap_bytes=_bytes("cache_cap_bytes", d.cache_cap_bytes),
        recommend_cache_bytes=_bytes(
            "recommend_cache_bytes", d.recommend_cache_bytes),
    )


def _lru_key(pair: tuple[CatalogEntry, int]) -> tuple[int, str, str]:
    """Least-recently-used first. ``last_used`` is an ISO-8601 UTC string
    (lexicographically ordered by construction); a missing one reads as
    oldest. ``frame_id`` is a stable tiebreak so eviction is deterministic."""
    entry, _cost = pair
    return (0 if entry.last_used is None else 1,
            entry.last_used or "", entry.frame_id)


def select_evictions(
    entries: list[tuple[CatalogEntry, int]], total_bytes: int, cap_bytes: int,
    *, protect_id: str | None = None,
) -> list[CatalogEntry]:
    """The pure §14 LRU pick: given ``(entry, on-disk cost)`` pairs for the
    cache and the recorded total, return the entries to evict — least-
    recently-used first — to bring the total back under ``cap_bytes``.

    The single most-recently-used entry is never selected: the cap is a
    backstop against unbounded growth, not a cache disable — with a cap set
    below one frame's cost, evicting the frame that was just generated would
    thrash (generate → evict → regenerate). ``protect_id`` additionally pins
    a specific frame (the just-inserted one) regardless of ordering —
    ``last_used`` has one-second resolution, so a same-second hit on another
    entry must not be able to out-MRU the fresh insert. Bounded either way:
    the recorded artifacts can never exceed cap + one state's for long.
    """
    if total_bytes <= cap_bytes or not entries:
        return []
    by_age = sorted(entries, key=_lru_key)
    evict: list[CatalogEntry] = []
    remaining = total_bytes
    for entry, cost in by_age[:-1]:  # by_age[-1] is the MRU survivor
        if remaining <= cap_bytes:
            break
        if protect_id is not None and entry.frame_id == protect_id:
            continue
        evict.append(entry)
        remaining -= cost
    return evict
