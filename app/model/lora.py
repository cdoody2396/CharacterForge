"""Identity-LoRA manifest (Stage 3d — DECISIONS.md §6).

Pure serialization for the trained identity LoRA's provenance. The LoRA file
itself lives at ``characters/<id>/lora/<name>.safetensors``; this manifest
(``characters/<id>/lora/lora.json``) records how it was trained so a promotion
is reproducible/auditable. The record's ``IdentityAnchor.has_lora`` +
``lora_path`` are the authoritative promotion state; this manifest is the
provenance sidecar (mirrors the §7 catalog / §3c bootstrap manifest pattern).

Only char-relative POSIX paths are stored, never an absolute machine path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .character import SCHEMA_VERSION, ensure_safe_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class LoraManifest:
    character_id: str
    trigger: str                       # the token that invokes this identity
    lora_file: str                     # char-relative .safetensors path
    base_checkpoint: str | None = None  # basename of the checkpoint trained on
    base_checkpoint_bytes: int | None = None
    network_dim: int = 0
    network_alpha: float = 0.0
    steps: int = 0
    resolution: int = 0
    learning_rate: float = 0.0
    dataset_size: int = 0              # number of vetted images trained on
    lora_bytes: int = 0
    schema_version: int = SCHEMA_VERSION
    created_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        self.character_id = ensure_safe_id(self.character_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "character_id": self.character_id,
            "trigger": self.trigger,
            "lora_file": self.lora_file,
            "base_checkpoint": self.base_checkpoint,
            "base_checkpoint_bytes": self.base_checkpoint_bytes,
            "network_dim": self.network_dim,
            "network_alpha": self.network_alpha,
            "steps": self.steps,
            "resolution": self.resolution,
            "learning_rate": self.learning_rate,
            "dataset_size": self.dataset_size,
            "lora_bytes": self.lora_bytes,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LoraManifest":
        return cls(
            character_id=str(data["character_id"]),
            trigger=str(data.get("trigger", "")),
            lora_file=str(data["lora_file"]),
            base_checkpoint=data.get("base_checkpoint"),
            base_checkpoint_bytes=data.get("base_checkpoint_bytes"),
            network_dim=int(data.get("network_dim", 0)),
            network_alpha=float(data.get("network_alpha", 0.0)),
            steps=int(data.get("steps", 0)),
            resolution=int(data.get("resolution", 0)),
            learning_rate=float(data.get("learning_rate", 0.0)),
            dataset_size=int(data.get("dataset_size", 0)),
            lora_bytes=int(data.get("lora_bytes", 0)),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            created_at=str(data.get("created_at", _now_iso())),
        )
