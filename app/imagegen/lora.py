"""Identity-LoRA training (Stage 3d — DECISIONS.md §6).

Promotes a vetted image set (3c) into a per-character identity LoRA. Training
is the heaviest [HARDWARE] operation and cannot run in the GPU-less sandbox,
so the trainer is a **fakeable** ``LoraTrainer`` behind a factory (injected
like the engine's backend factory and the cull toolkit factory); the pure
parts — training-config coercion, dataset preparation, output collection,
record promotion — are verified here with a fake trainer.

Trainer backend (DECISIONS §6, spec-time pick — swappable): **kohya-ss
``sd-scripts``** driven as a headless subprocess (``CREATE_NO_WINDOW`` — no
console popup, §2). It is the community-standard, quality-max identity-LoRA
trainer; the app builds a config + dataset and invokes
``sdxl_train_network.py``, then collects the produced ``.safetensors``. The
``sd-scripts`` checkout is user-placed (like the checkpoint), pointed at by
``models.image.lora_trainer_dir``. A different trainer (a diffusers/peft loop)
can replace ``_default_trainer_factory`` with no change above this module.

Unlike ``cull.py`` this module has no heavy in-process imports — the weight of
training lives in the subprocess — so nothing here needs lazy importing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from ..config import Settings
from .engine import APP_ROOT

TRAINER_SCRIPT = "sdxl_train_network.py"  # kohya sd-scripts SDXL LoRA entrypoint


class TrainUnavailable(RuntimeError):
    """A prerequisite for training is missing (fail-closed). ``kind`` is a
    structured reason: trainer_unavailable / no_checkpoint."""

    def __init__(self, kind: str, message: str = ""):
        self.kind = kind
        super().__init__(message or kind)


class TrainFailed(RuntimeError):
    """The trainer ran but did not produce a usable LoRA (nonzero exit,
    timeout, or no output file)."""


@dataclass(frozen=True)
class TrainConfig:
    """Identity-LoRA hyperparameters. Quality-max defaults (§6 authorizes a
    heavier pipeline); every value is a hardware-tuned default (§16), coerced
    defensively so a hand-edit degrades to the default rather than crashing."""

    network_dim: int = 16
    network_alpha: float = 8.0
    learning_rate: float = 1e-4
    resolution: int = 1024
    repeats: int = 10
    max_train_steps: int = 1600
    train_batch_size: int = 1
    mixed_precision: str = "fp16"       # fp16 | bf16 | no
    optimizer: str = "AdamW8bit"
    lr_scheduler: str = "cosine"
    clip_skip: int = 2                  # anime-derived base convention (§4)
    timeout_seconds: int = 6 * 60 * 60  # slow is fine (§3); a hard ceiling


@dataclass(frozen=True)
class TrainItem:
    """One training image + its caption."""

    image_path: Path      # absolute, already containment-validated
    caption: str


@dataclass(frozen=True)
class TrainRequest:
    dataset_dir: Path
    output_dir: Path
    output_name: str
    base_checkpoint: Path
    trigger: str
    config: TrainConfig


@runtime_checkable
class LoraTrainer(Protocol):
    def train(self, request: TrainRequest) -> Path:
        """Run training and return the produced ``.safetensors`` path, or raise
        TrainFailed / TrainUnavailable."""
        ...


# A trainer factory takes settings and returns a LoraTrainer, or raises
# TrainUnavailable if the trainer itself is unavailable.
TrainerFactory = Callable[[Settings], LoraTrainer]


# -- settings resolution -----------------------------------------------------


def _resolve(raw: object) -> Path | None:
    if raw is None or not str(raw).strip():
        return None
    path = Path(str(raw))
    return path if path.is_absolute() else APP_ROOT / path


def lora_trainer_dir(settings: Settings) -> Path | None:
    return _resolve(settings.get("models.image.lora_trainer_dir"))


def preflight_train(settings: Settings) -> str | None:
    """Cheap existence check of the trainer, run before doing any work. Returns
    a TrainUnavailable kind or None."""
    trainer = lora_trainer_dir(settings)
    if trainer is None or not (trainer / TRAINER_SCRIPT).is_file():
        return "trainer_unavailable"
    return None


def coerce_train_config(settings: Settings) -> TrainConfig:
    """Build a TrainConfig from image_gen.lora_train.*, coerced defensively so a
    hand-edited Infinity/NaN/string never reaches the trainer (mirrors the cull
    config coercion). Bad values -> code defaults."""
    d = TrainConfig()

    def _int(key: str, default: int, *, lo: int = 1, hi: int = 100_000) -> int:
        try:
            v = float(settings.get(f"image_gen.lora_train.{key}", default))
        except (TypeError, ValueError, OverflowError):
            return default
        if not math.isfinite(v):
            return default
        return int(min(hi, max(lo, v)))

    def _float(key: str, default: float, *, lo: float, hi: float) -> float:
        try:
            v = float(settings.get(f"image_gen.lora_train.{key}", default))
        except (TypeError, ValueError, OverflowError):
            return default
        if not math.isfinite(v):
            return default
        return min(hi, max(lo, v))

    def _str(key: str, default: str, allowed: tuple[str, ...]) -> str:
        v = settings.get(f"image_gen.lora_train.{key}", default)
        return str(v) if v in allowed else default

    return TrainConfig(
        network_dim=_int("network_dim", d.network_dim, lo=1, hi=320),
        network_alpha=_float("network_alpha", d.network_alpha, lo=0.0, hi=320.0),
        learning_rate=_float("learning_rate", d.learning_rate, lo=1e-7, hi=1.0),
        resolution=_int("resolution", d.resolution, lo=256, hi=2048),
        repeats=_int("repeats", d.repeats, lo=1, hi=1000),
        max_train_steps=_int("max_train_steps", d.max_train_steps, lo=1, hi=100_000),
        train_batch_size=_int("train_batch_size", d.train_batch_size, lo=1, hi=64),
        mixed_precision=_str("mixed_precision", d.mixed_precision, ("fp16", "bf16", "no")),
        optimizer=_str("optimizer", d.optimizer,
                       ("AdamW8bit", "AdamW", "Lion", "Prodigy")),
        lr_scheduler=_str("lr_scheduler", d.lr_scheduler,
                          ("cosine", "constant", "constant_with_warmup", "linear")),
        clip_skip=_int("clip_skip", d.clip_skip, lo=1, hi=12),
        timeout_seconds=_int("timeout_seconds", d.timeout_seconds, lo=60, hi=48 * 3600),
    )


# -- dataset preparation (pure; sandbox-verifiable) --------------------------


def build_dataset(dataset_dir: Path, items: list[TrainItem], config: TrainConfig) -> int:
    """Lay out the kohya training folder — ``<dataset_dir>/<repeats>_identity/``
    with one ``img-NN.png`` + ``img-NN.txt`` caption per vetted image. Returns
    the image count. The caller resolves + containment-checks each image path
    before handing it here."""
    import shutil

    concept_dir = dataset_dir / f"{config.repeats}_identity"
    concept_dir.mkdir(parents=True, exist_ok=True)
    for i, item in enumerate(items, start=1):
        dest = concept_dir / f"img-{i:02d}.png"
        shutil.copyfile(item.image_path, dest)
        dest.with_suffix(".txt").write_text(item.caption, encoding="utf-8")
    return len(items)


# ===========================================================================
# [HARDWARE] real backend — kohya sd-scripts subprocess. Structurally complete;
# validated on the 16 GB target. Nothing heavy is imported in-process.
# ===========================================================================


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


class _KohyaSubprocessTrainer:
    def __init__(self, trainer_dir: Path, python_exe: str):
        self._trainer_dir = trainer_dir
        self._python = python_exe

    def _write_config(self, request: TrainRequest) -> Path:
        c = request.config
        request.output_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            f'pretrained_model_name_or_path = "{_toml_escape(str(request.base_checkpoint))}"',
            f'train_data_dir = "{_toml_escape(str(request.dataset_dir))}"',
            f'output_dir = "{_toml_escape(str(request.output_dir))}"',
            f'output_name = "{_toml_escape(request.output_name)}"',
            'save_model_as = "safetensors"',
            'network_module = "networks.lora"',
            f"network_dim = {c.network_dim}",
            f"network_alpha = {c.network_alpha}",
            # STRING, not int: sd-scripts declares --resolution type=str and
            # unconditionally does args.resolution.split(",") — a toml int
            # bypasses argparse coercion straight onto the namespace and
            # AttributeErrors the trainer (hardware-validation catch,
            # 2026-07-12, sd-scripts rev 0128ca00).
            f'resolution = "{c.resolution}"',
            f"learning_rate = {c.learning_rate}",
            f"max_train_steps = {c.max_train_steps}",
            f"train_batch_size = {c.train_batch_size}",
            f'mixed_precision = "{c.mixed_precision}"',
            f'save_precision = "{c.mixed_precision if c.mixed_precision != "no" else "fp16"}"',
            f'optimizer_type = "{c.optimizer}"',
            f'lr_scheduler = "{c.lr_scheduler}"',
            f"clip_skip = {c.clip_skip}",
            "sdxl = true",
            "cache_latents = true",
            "gradient_checkpointing = true",  # fits the 16 GB floor (§3)
            "sdpa = true",   # PyTorch-native mem-efficient attn (no xformers dep)
            # UNet-only (hardware-validation pick, 2026-07-12): standard SDXL
            # LoRA practice — the identity payload lives in the UNet, TE
            # training buys little here, VRAM/time drop, AND diffusers 0.39's
            # kohya converter has a te1/te2 regression (empty rank_dict ->
            # IndexError) that a TE-carrying LoRA trips at 3e load time (the
            # engine also degrades to UNet-only on that failure).
            "network_train_unet_only = true",
        ]
        config_path = request.output_dir / "train_config.toml"
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return config_path

    def train(self, request: TrainRequest) -> Path:
        import os
        import subprocess
        import sys

        script = self._trainer_dir / TRAINER_SCRIPT
        if not script.is_file():
            raise TrainUnavailable("trainer_unavailable",
                                   f"trainer script not found: {script}")
        config_path = self._write_config(request)
        creationflags = 0
        if sys.platform == "win32":
            # §2: no console/terminal popup for the background training process.
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # UTF-8 on BOTH sides of the pipe (hardware-validation catch,
        # 2026-07-12): sd-scripts logs bilingual (Japanese) text; on Windows
        # a non-console pipe defaults the CHILD to cp1252 (UnicodeEncodeError
        # kills training mid-run) and text=True decodes with the locale
        # codec in the PARENT (UnicodeDecodeError on UTF-8 bytes). PYTHONUTF8
        # pins the child; encoding+errors pin the parent, replace never
        # raises over a log byte.
        env = {**os.environ, "PYTHONUTF8": "1"}
        try:
            proc = subprocess.run(
                [self._python, str(script), "--config_file", str(config_path)],
                cwd=str(self._trainer_dir),
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=request.config.timeout_seconds,
                creationflags=creationflags,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise TrainFailed(
                f"training timed out after {request.config.timeout_seconds}s"
            ) from exc
        if proc.returncode != 0:
            tail = (proc.stderr or "")[-2000:]
            raise TrainFailed(f"trainer exited {proc.returncode}: {tail}")
        # Collect the output. Prefer the exact name; fall back to the newest
        # .safetensors in output_dir — some sd-scripts versions append a
        # step/epoch suffix, which would otherwise false-fail a good train.
        out = request.output_dir / f"{request.output_name}.safetensors"
        if not out.is_file():
            candidates = sorted(
                request.output_dir.glob("*.safetensors"),
                key=lambda p: p.stat().st_mtime)
            if not candidates:
                raise TrainFailed("the trainer produced no LoRA file")
            out = candidates[-1]
        return out


def _default_trainer_factory(settings: Settings) -> LoraTrainer:
    """Build the real kohya trainer. Re-guards existence (fail-closed)."""
    import sys

    trainer_dir = lora_trainer_dir(settings)
    if trainer_dir is None or not (trainer_dir / TRAINER_SCRIPT).is_file():
        raise TrainUnavailable("trainer_unavailable")
    python_exe = settings.get("models.image.lora_trainer_python") or sys.executable
    return _KohyaSubprocessTrainer(trainer_dir, str(python_exe))
