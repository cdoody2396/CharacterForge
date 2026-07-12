"""Seed-catalog matrix logic (Stage 3e — DECISIONS.md §7).

Pure, sandbox-verifiable: builds the core state matrix (expressions × poses ×
the character's wardrobe) that the LoRA-steered catalog renders, and coerces
the catalog knobs. The generation + auto-filter (which reuse the engine's LoRA
backend and the 3c cull) live in ``service.py``; the *shape* of the matrix and
the cell prompts live here so they can be tested without any model.

The identity prompt is held constant (the trigger + the record's gated
description, minus the wardrobe group); each cell varies one outfit +
expression + pose. Identity comes from the LoRA, pose/expression from the base
model — so, unlike the 3c bootstrap (identity-tight), the catalog deliberately
varies state.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..model import CharacterRecord, OptionCatalog

DATA_DIR = Path(__file__).resolve().parent / "data"
STATES_FILE = DATA_DIR / "catalog_states.json"

# The wardrobe group id (from the bundled option files) — excluded from the
# constant identity prompt so the catalog can vary the outfit per cell.
OUTFIT_GROUP = "outfit"
# The synthetic "outfit" used when the character defined no wardrobe: render the
# base identity as-is (no outfit fragment) as a single outfit dimension.
ASIS_OUTFIT = "asis"


@dataclass(frozen=True)
class CatalogState:
    id: str
    prompt: str


@dataclass(frozen=True)
class CatalogCell:
    """One core-matrix cell: a state triple + its prompt fragments."""

    expression_id: str
    pose_id: str
    outfit_id: str
    expression_prompt: str
    pose_prompt: str
    outfit_prompt: str

    def state(self) -> dict[str, str]:
        return {"expression": self.expression_id, "pose": self.pose_id,
                "outfit": self.outfit_id}

    def extra(self) -> tuple[tuple[str, str], ...]:
        """The (source, text) fragments the assembler appends for this cell."""
        out: list[tuple[str, str]] = []
        if self.outfit_prompt:
            out.append((f"state.outfit.{self.outfit_id}", self.outfit_prompt))
        if self.expression_prompt:
            out.append((f"state.expression.{self.expression_id}", self.expression_prompt))
        if self.pose_prompt:
            out.append((f"state.pose.{self.pose_id}", self.pose_prompt))
        return tuple(out)


@dataclass(frozen=True)
class CatalogConfig:
    max_expressions: int = 5
    max_poses: int = 4
    max_outfits: int = 4
    max_frames: int = 48      # hard cap on the matrix (slow is fine, but bounded)
    max_attempts: int = 2     # generate+cull passes to fill rejected cells
    lora_scale: float = 1.0
    # The catalog deliberately varies pose (full-body / over-shoulder), so its
    # faces are smaller than the 3c identity-tight portrait cluster. Relax ONLY
    # the face-area floor of the auto-filter for the catalog — the Layer-2
    # content gate (whole-image, safety) and the similarity gate are unchanged.
    face_area_min: float = 0.01


def load_catalog_states() -> tuple[list[CatalogState], list[CatalogState]]:
    """The editable expression + pose state lists. A malformed file yields
    empty lists (the catalog then has nothing to vary — a structured
    'no_states' at the service, never a crash)."""
    try:
        data = json.loads(STATES_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return [], []
    if not isinstance(data, dict):  # valid JSON but not an object (e.g. [] / null)
        return [], []
    return _states(data.get("expressions")), _states(data.get("poses"))


def _states(raw: object) -> list[CatalogState]:
    out: list[CatalogState] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and "id" in entry:
                out.append(CatalogState(str(entry["id"]),
                                        str(entry.get("prompt", ""))))
    return out


def record_outfits(
    record: CharacterRecord, catalog: OptionCatalog
) -> list[tuple[str, str]]:
    """The character's wardrobe as (outfit_id, prompt) pairs, or a single
    as-is dimension when no wardrobe was defined."""
    out: list[tuple[str, str]] = []
    group = catalog.get(OUTFIT_GROUP)
    if group is not None:
        for outfit_id in record.tags.get(OUTFIT_GROUP, []):
            option = group.get_option(outfit_id)
            if option is not None:
                out.append((outfit_id, option.prompt))
    if not out:
        out = [(ASIS_OUTFIT, "")]
    return out


def build_cells(
    record: CharacterRecord,
    catalog: OptionCatalog,
    expressions: list[CatalogState],
    poses: list[CatalogState],
    config: CatalogConfig,
) -> list[CatalogCell]:
    """The core matrix = (capped) outfits × expressions × poses, bounded by
    ``max_frames``. Empty when there are no states to render."""
    exprs = expressions[: config.max_expressions]
    ps = poses[: config.max_poses]
    outfits = record_outfits(record, catalog)[: config.max_outfits]
    cells: list[CatalogCell] = []
    for outfit_id, outfit_prompt in outfits:
        for expr in exprs:
            for pose in ps:
                cells.append(CatalogCell(
                    expression_id=expr.id, pose_id=pose.id, outfit_id=outfit_id,
                    expression_prompt=expr.prompt, pose_prompt=pose.prompt,
                    outfit_prompt=outfit_prompt))
                if len(cells) >= config.max_frames:
                    return cells
    return cells


def coerce_catalog_config(settings: Settings) -> CatalogConfig:
    """Build a CatalogConfig from image_gen.catalog.*, coerced defensively so a
    hand-edited Infinity/NaN/string never reaches the matrix (mirrors the cull
    / train config coercion). Bad values -> code defaults; ints clamped."""
    d = CatalogConfig()

    def _int(key: str, default: int, *, lo: int = 0, hi: int = 512) -> int:
        try:
            v = float(settings.get(f"image_gen.catalog.{key}", default))
        except (TypeError, ValueError, OverflowError):
            return default
        if not math.isfinite(v):
            return default
        return int(min(hi, max(lo, v)))

    def _float(key: str, default: float, *, lo: float, hi: float) -> float:
        try:
            v = float(settings.get(f"image_gen.catalog.{key}", default))
        except (TypeError, ValueError, OverflowError):
            return default
        if not math.isfinite(v):
            return default
        return min(hi, max(lo, v))

    return CatalogConfig(
        max_expressions=_int("max_expressions", d.max_expressions, lo=1, hi=64),
        max_poses=_int("max_poses", d.max_poses, lo=1, hi=64),
        max_outfits=_int("max_outfits", d.max_outfits, lo=1, hi=64),
        max_frames=_int("max_frames", d.max_frames, lo=1, hi=512),
        max_attempts=_int("max_attempts", d.max_attempts, lo=1, hi=10),
        lora_scale=_float("lora_scale", d.lora_scale, lo=0.0, hi=2.0),
        face_area_min=_float("face_area_min", d.face_area_min, lo=0.0, hi=1.0),
    )
