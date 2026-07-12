"""Config/settings system.

JSON on disk inside the app folder (self-contained per DECISIONS.md §2),
atomic writes, and defaults deep-merged on load so new keys ship without a
migration step. Includes the model-swap toggle scaffold (§3): each heavy
model slot carries a default/heavy variant selector plus path fields that
Stage 3 (image) and Stage 6 (chat) fill with real checkpoints.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "schema_version": 1,
    "window": {
        "width": 1280,
        "height": 800,
        "title": "CharacterForge",
    },
    "models": {
        # One heavy model holds VRAM at a time (§3); "active" tracks which.
        # Swap sequencing itself lands at Stage 6a — these keys are the
        # scaffold every stage reads/writes.
        "active": None,  # None | "image" | "chat"
        "image": {
            "variant": "default",  # "default" | "heavy"
            "checkpoint_path": None,
            "heavy_checkpoint_path": None,
            # Optional local diffusers pipeline-config dir: set it for a
            # fully-offline single-file load (§2); unset, the first load
            # warms the HF config cache once (docs/IMAGE_PIPELINE.md §6).
            "pipeline_config_dir": None,
            # Stage-3b IP-Adapter (identity steer, §6). `dir` is a local
            # h94/IP-Adapter mirror (user-placed, offline — never a hub id);
            # `variant` picks the weight from a code table so the weight<->
            # image-encoder pairing footgun is unhittable by hand-edit.
            "ip_adapter": {
                "dir": None,               # local mirror dir; None = 3b disabled
                "variant": "standard",     # "standard" | "plus"
            },
            # Stage-3c auto-filter model dirs (user-placed, offline, §6/§11).
            "face_recognition_dir": None,   # dir CONTAINING models/buffalo_l/ (InsightFace)
            "content_classifier_dir": None,  # imgutils HF cache (Layer-2 pixel gate)
            "face_swapper_path": None,       # inswapper_128.onnx (optional identity lock)
            # ONNX providers for the light cull models. CPU default = zero VRAM
            # (they run after the SDXL slot is freed, §3 "slow is fine").
            "onnx_providers": ["CPUExecutionProvider"],
            # Stage-3d LoRA training (§6). `lora_trainer_dir` is a user-placed
            # kohya-ss sd-scripts checkout (contains sdxl_train_network.py);
            # `lora_trainer_python` is the interpreter for its env (None = the
            # app's own). Training runs headless as a subprocess (§2).
            "lora_trainer_dir": None,
            "lora_trainer_python": None,
            # Stage-3f matting model (user-placed, offline, like the
            # checkpoint): isnet-anime.onnx (default variant; ~176 MB; rembg
            # v0.0.0 release asset; provenance SkyTNT/anime-segmentation,
            # Apache-2.0) — or isnet-general-use / a BiRefNet ONNX export per
            # image_gen.matting.variant.
            "matting_model_path": None,
        },
        "chat": {
            "variant": "default",  # "default" | "heavy"
            "model_path": None,
            "heavy_model_path": None,
        },
    },
    "image_gen": {
        # Stage-3a generation knobs (docs/IMAGE_PIPELINE.md). Defaults are the
        # Illustrious-XL-family community baseline: ~1MP portrait, Euler-a.
        # Hand-editable; the engine re-validates every request.
        "width": 832,
        "height": 1216,
        "steps": 28,
        "cfg_scale": 5.5,
        "sampler": "euler_a",  # euler_a | euler | dpmpp_2m | dpmpp_2m_karras
        # Stage-3b IP-Adapter identity steer strength. Useful band ~0.3-0.8
        # (advisory); engine bound is [0, 1], and a bad hand-edit degrades to
        # this default rather than crashing.
        "ip_adapter_scale": 0.55,
        # Stage-3c identity-bootstrap auto-filter knobs (§6). Over-generate a
        # batch, cull hard (drift makes identity worse), net ~15-30 vetted.
        # Every threshold is a hardware-tuned default (§16); a bad hand-edit
        # degrades to the code default, never crashes.
        "bootstrap": {
            "batch": 64,             # candidates to generate (~3-4x over)
            "keep_cap": 30,          # suggested confirmation ceiling
            "floor": 15,             # below this -> offer generate-more
            "grid_size": 12,         # the confirmation grid
            "similarity_floor": 0.5,  # same-person cosine (conservative)
            "det_score_floor": 0.5,
            "sharpness_floor": 100.0,
            "face_area_min": 0.04,
            "face_area_max": 0.9,
            "face_swap_enabled": False,  # optional identity lock (inswapper)
        },
        # Stage-3e seed-catalog matrix (§7): expressions × poses × wardrobe,
        # rendered LoRA-steered and auto-filtered by the 3c cull. Bounded +
        # coerced; a bad hand-edit degrades to the default.
        "catalog": {
            "max_expressions": 5,
            "max_poses": 4,
            "max_outfits": 4,
            "max_frames": 48,     # hard cap on the matrix size
            "max_attempts": 2,    # generate+cull passes to fill rejected cells
            "lora_scale": 1.0,    # identity-LoRA strength at catalog render
            # Relaxed face-area floor for pose-varied (small-face) frames; the
            # Layer-2 content gate + similarity floor stay at the 3c values.
            "face_area_min": 0.01,
        },
        # Stage-3f matting knobs (§7, §13). Coerced + clamped; a bad hand-edit
        # degrades to the code default, never crashes. Defaults are exact
        # rembg parity (no erode/feather); tune at hardware validation (§16).
        "matting": {
            "variant": "isnet_anime",  # isnet_anime | isnet_general | birefnet
            "erode_px": 0,             # halo choke, int [0, 8]
            "feather_px": 0,           # Gaussian re-soften after erode, int [0, 8]
            "coverage_min": 0.02,      # degenerate floor (solid-alpha fraction)
            "coverage_max": 0.98,      # degenerate ceiling
        },
        # Stage-3d identity-LoRA hyperparameters (§6, quality-max). Every value
        # is a hardware-tuned default (§16); a bad hand-edit clamps/degrades.
        "lora_train": {
            "network_dim": 16,
            "network_alpha": 8.0,
            "learning_rate": 0.0001,
            "resolution": 1024,
            "repeats": 10,
            "max_train_steps": 1600,
            "train_batch_size": 1,
            "mixed_precision": "fp16",   # fp16 | bf16 | no
            "optimizer": "AdamW8bit",
            "lr_scheduler": "cosine",
            "clip_skip": 2,
            "timeout_seconds": 21600,    # 6h ceiling ("slow is fine", §3)
        },
    },
    "safety": {
        # Layer 4 (§11): local audit logging of generations/conversations.
        "logging_enabled": True,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Values from ``override`` win; dicts merge recursively."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class Settings:
    """Persistent app settings with dotted-path access."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self._lock = threading.RLock()
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                raw = self.path.read_text(encoding="utf-8")
            except OSError:
                # Transient unreadability (AV/backup/indexer holding the file
                # on Windows) is NOT corruption — keep the file, run on
                # in-memory defaults, and do not overwrite it.
                self._data = copy.deepcopy(DEFAULTS)
                return
            try:
                on_disk = json.loads(raw)
                if not isinstance(on_disk, dict):
                    raise ValueError("settings root must be an object")
                self._data = _deep_merge(copy.deepcopy(DEFAULTS), on_disk)
                return
            except (json.JSONDecodeError, ValueError):
                # Genuinely corrupt content: preserve the bad file for
                # inspection and fall back to defaults rather than failing
                # launch.
                backup = self.path.with_suffix(".json.corrupt")
                try:
                    os.replace(self.path, backup)
                except OSError:
                    pass
                self._data = copy.deepcopy(DEFAULTS)
        self.save()

    def save(self) -> None:
        """Atomic write under a lock: a unique temp file in the same dir +
        os.replace, so a crash mid-write can never leave a half-written file
        and concurrent writers (pywebview dispatches each bridge call on its
        own thread) cannot race a shared temp path."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._data, indent=2, ensure_ascii=False) + "\n"
            fd, tmp_name = tempfile.mkstemp(
                dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp_name, self.path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise

    # -- dotted-path access ---------------------------------------------------

    def get(self, dotted_key: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted_key: str, value: Any, save: bool = True) -> None:
        # RLock: guard the mutation and the (re-entrant) save together so a
        # concurrent writer never serializes a half-mutated tree.
        with self._lock:
            parts = dotted_key.split(".")
            node = self._data
            for part in parts[:-1]:
                child = node.get(part)
                if not isinstance(child, dict):
                    child = {}
                    node[part] = child
                node = child
            node[parts[-1]] = value
            if save:
                self.save()

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)
