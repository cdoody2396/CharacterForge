"""Identity-bootstrap manifests (Stage 3c — DECISIONS.md §6).

Pure serialization, no heavy deps — the disk record of the auto-filter
pipeline that turns a single reference image (3b) into a vetted training set
(consumed by 3d). Two artifacts, both distinct from the §7 `CatalogManifest`
(that is 3e) and both confined to the character's own directory:

  characters/<id>/bootstrap/bootstrap.json   BootstrapManifest — every seed
      candidate with its cull scores + decision (append-only candidates so a
      re-cull needs no regeneration).
  characters/<id>/vetted/vetted.json         VettedManifest — the confirmed
      on-model set (~15-30 images) that 3d trains on. Its existence is the
      single source of truth that a character has a vetted set (the record is
      NOT flagged — §6 minimal-mutation ethos).

Every stored path is char-relative POSIX, never an absolute machine path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .character import SCHEMA_VERSION, ensure_safe_id

# Bootstrap lifecycle phases.
PHASE_GENERATING = "generating"
PHASE_CULLED = "culled"
PHASE_PROPOSED = "proposed"
PHASE_CONFIRMED = "confirmed"

# Per-candidate cull status. The rejected_* reasons mirror the cull gate order
# (content is safety-critical and always audited).
STATUS_CANDIDATE = "candidate"
STATUS_REJECTED_ERROR = "rejected_error"        # could not decode/score
STATUS_REJECTED_NO_FACE = "rejected_no_face"
STATUS_REJECTED_CONTENT = "rejected_content"    # Layer-2 pixel block
STATUS_REJECTED_QUALITY = "rejected_quality"
STATUS_REJECTED_SIMILARITY = "rejected_similarity"
STATUS_KEPT = "kept"
STATUS_PROPOSED = "proposed"                     # kept AND in the ~12 grid
STATUS_CONFIRMED = "confirmed"                    # promoted to the vetted set

# Statuses a candidate may hold to be eligible for confirmation.
CONFIRMABLE_STATUSES = (STATUS_PROPOSED, STATUS_KEPT)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class BootstrapCandidate:
    """One seed frame + its cull decision."""

    candidate_id: str
    path: str                      # char-relative PNG
    seed: int
    status: str = STATUS_CANDIDATE
    swapped_path: str | None = None  # char-relative face-swapped variant
    similarity: float = 0.0
    quality: dict[str, Any] = field(default_factory=dict)   # sharpness/aesthetic/det_score/face_area_fraction/face_count
    content: dict[str, Any] = field(default_factory=dict)   # blocked/category/matched
    rank: int | None = None

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "path": self.path,
            "seed": self.seed,
            "status": self.status,
            "swapped_path": self.swapped_path,
            "similarity": self.similarity,
            "quality": dict(self.quality),
            "content": dict(self.content),
            "rank": self.rank,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BootstrapCandidate":
        # A candidate_id becomes a filename stem for the face-swap output, so
        # confine it like a store id — a hand-edited '../x' cannot survive load.
        return cls(
            candidate_id=ensure_safe_id(data["candidate_id"]),
            path=str(data["path"]),
            seed=int(data.get("seed", 0)),
            status=str(data.get("status", STATUS_CANDIDATE)),
            swapped_path=data.get("swapped_path"),
            similarity=float(data.get("similarity", 0.0)),
            quality=dict(data.get("quality", {})),
            content=dict(data.get("content", {})),
            rank=data.get("rank"),
        )

    def final_path(self) -> str:
        """The pixels a downstream consumer should use: the swapped variant if
        one was produced and kept, else the original."""
        return self.swapped_path or self.path


@dataclass
class BootstrapManifest:
    character_id: str
    phase: str = PHASE_GENERATING
    reference: str | None = None            # char-relative reference used
    params: dict[str, Any] = field(default_factory=dict)
    candidates: list[BootstrapCandidate] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    updated_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        # A manifest id drives store paths — keep it confined (same guard as
        # CatalogManifest).
        self.character_id = ensure_safe_id(self.character_id)

    def counts_by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for cand in self.candidates:
            counts[cand.status] = counts.get(cand.status, 0) + 1
        return counts

    def get(self, candidate_id: str) -> BootstrapCandidate | None:
        for cand in self.candidates:
            if cand.candidate_id == candidate_id:
                return cand
        return None

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "character_id": self.character_id,
            "phase": self.phase,
            "reference": self.reference,
            "params": dict(self.params),
            "candidates": [c.to_dict() for c in self.candidates],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BootstrapManifest":
        return cls(
            character_id=str(data["character_id"]),
            phase=str(data.get("phase", PHASE_GENERATING)),
            reference=data.get("reference"),
            params=dict(data.get("params", {})),
            candidates=[
                BootstrapCandidate.from_dict(c) for c in data.get("candidates", [])
            ],
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            updated_at=str(data.get("updated_at", _now_iso())),
        )


@dataclass
class VettedEntry:
    """One confirmed training image (the pixels 3d trains on)."""

    path: str                       # char-relative PNG under vetted/
    source_candidate_id: str
    seed: int
    similarity: float = 0.0
    aesthetic: float = 0.0
    face_swapped: bool = False
    content_verdict: dict[str, Any] = field(default_factory=dict)
    reference: str | None = None
    checkpoint: str | None = None
    checkpoint_bytes: int | None = None
    created_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "source_candidate_id": self.source_candidate_id,
            "seed": self.seed,
            "similarity": self.similarity,
            "aesthetic": self.aesthetic,
            "face_swapped": self.face_swapped,
            "content_verdict": dict(self.content_verdict),
            "reference": self.reference,
            "checkpoint": self.checkpoint,
            "checkpoint_bytes": self.checkpoint_bytes,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VettedEntry":
        return cls(
            path=str(data["path"]),
            source_candidate_id=str(data.get("source_candidate_id", "")),
            seed=int(data.get("seed", 0)),
            similarity=float(data.get("similarity", 0.0)),
            aesthetic=float(data.get("aesthetic", 0.0)),
            face_swapped=bool(data.get("face_swapped", False)),
            content_verdict=dict(data.get("content_verdict", {})),
            reference=data.get("reference"),
            checkpoint=data.get("checkpoint"),
            checkpoint_bytes=data.get("checkpoint_bytes"),
            created_at=str(data.get("created_at", _now_iso())),
        )


@dataclass
class VettedManifest:
    character_id: str
    entries: list[VettedEntry] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        self.character_id = ensure_safe_id(self.character_id)

    @property
    def count(self) -> int:
        return len(self.entries)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "character_id": self.character_id,
            "count": self.count,
            "entries": [e.to_dict() for e in self.entries],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VettedManifest":
        return cls(
            character_id=str(data["character_id"]),
            entries=[VettedEntry.from_dict(e) for e in data.get("entries", [])],
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            created_at=str(data.get("created_at", _now_iso())),
            updated_at=str(data.get("updated_at", _now_iso())),
        )
