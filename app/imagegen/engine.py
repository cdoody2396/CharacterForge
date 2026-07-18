"""Image engine — the SDXL-derived model call behind the swap scaffold
(Stage 3a — DECISIONS.md §3, §4).

**[HARDWARE]** component: this sandbox has no GPU and no weights, so the
heavy stack (torch/diffusers) is imported lazily inside the default backend
factory. The engine itself — VRAM-slot sequencing against the ``models.active``
scaffold, checkpoint resolution, seed handling, request validation — is pure
logic, verified here with an injected fake backend; the diffusers backend is
structurally complete and validates on the 16 GB target machine.

Checkpoint (deferred spec item, resolved at 3a — see docs/IMAGE_PIPELINE.md):
Illustrious-XL-family SDXL checkpoint, single ``.safetensors`` file, path in
``models.image.checkpoint_path`` (``heavy_checkpoint_path`` for the opt-in
heavy variant, §3). The file itself is user-placed on the target machine —
weights are never bundled with the repo, only with the packaged app (Stage 7).

Swap scaffold (§3, formalized at Stage 6a): one heavy model holds VRAM at a
time. ``load()`` refuses while the chat model holds the slot, then takes it
(``models.active = "image"``); ``unload()`` frees it. Stage 6a replaces this
courtesy protocol with the real sequenced swap manager.
"""

from __future__ import annotations

import math
import random
import threading
from dataclasses import dataclass, replace
from importlib import util as _importlib_util
from pathlib import Path
from typing import Any, Callable

from ..config import Settings

APP_ROOT = Path(__file__).resolve().parents[2]

MAX_SEED = 2**32 - 1

# Sampler names the app exposes -> how the diffusers backend realizes them.
# Illustrious-family guidance: Euler-ancestral is the community default.
SAMPLERS: dict[str, tuple[str, dict]] = {
    "euler_a": ("EulerAncestralDiscreteScheduler", {}),
    "euler": ("EulerDiscreteScheduler", {}),
    "dpmpp_2m": ("DPMSolverMultistepScheduler", {}),
    "dpmpp_2m_karras": ("DPMSolverMultistepScheduler", {"use_karras_sigmas": True}),
}

# SDXL latent-space bounds: dimensions must be multiples of 8; the sweet spot
# is ~1MP. Hard bounds here keep a hand-edited settings file from feeding the
# UNet an impossible shape.
MIN_DIM = 512
MAX_DIM = 2048

# -- IP-Adapter (Stage 3b — DECISIONS.md §6) ---------------------------------
#
# IP-Adapter steers generation by a reference image for immediate identity
# consistency (the quick-create path). The one load-bearing footgun is the
# weight <-> image-encoder pairing: diffusers resolves ``image_encoder_folder``
# by whether it contains a "/". A bare name loads from ``<subfolder>/<name>``;
# a slashed value loads from the repo ROOT. The ViT-H weights below were
# trained on OpenCLIP ViT-H (projection dim 1024), which lives at
# ``<dir>/models/image_encoder`` in the h94/IP-Adapter tree — so the encoder
# folder MUST be the slash-form "models/image_encoder". The diffusers default
# ("image_encoder") would resolve to ``sdxl_models/image_encoder`` = ViT-bigG
# (dim 1280) and mismatch the projection. We keep weight_name + encoder folder
# as CODE CONSTANTS behind a "standard"|"plus" selector so no hand-edit can
# ever unpair them. Both variants are ViT-H, so they share one encoder folder.
IP_ADAPTER_SUBFOLDER = "sdxl_models"
IP_ADAPTER_ENCODER_FOLDER = "models/image_encoder"  # slash-form -> ViT-H (see above)
IP_ADAPTER_VARIANTS: dict[str, dict[str, Any]] = {
    # whole-image identity steer (the 3b baseline)
    "standard": {
        "weight_name": "ip-adapter_sdxl_vit-h.safetensors",
        "band": (0.3, 0.8),   # advisory useful range (UI labeling only)
        "default": 0.55,
    },
    # stronger whole-image identity (the §6 non-human mitigation at the no-LoRA tier)
    "plus": {
        "weight_name": "ip-adapter-plus_sdxl_vit-h.safetensors",
        "band": (0.3, 0.6),
        "default": 0.45,
    },
}
DEFAULT_IP_ADAPTER_SCALE = 0.55
DEFAULT_LORA_SCALE = 1.0
MAX_LORA_SCALE = 2.0

# Hardware-measured (2026-07-12, RTX 4070 Super 12 GB): the fully-resident
# identity stack (SDXL fp16 ~6.6 GB + ViT-H encoder + adapter ~1.9 GB) peaks
# 12.2-12.3 GB at 832x1216 with the fp32-upcast VAE decode — past a 12 GB
# card's budget, where the Windows (WDDM) driver silently spills to system
# RAM and roughly halves throughput (18.6 s/frame vs 9.7 base). Below this
# total-VRAM floor the identity backend uses accelerate model-cpu-offload
# (peak ~= largest single component) instead of a resident `.to("cuda")`.
IDENTITY_RESIDENT_VRAM_MIN_GB = 14.0


def identity_needs_cpu_offload(total_vram_bytes: object) -> bool:
    """True when the card cannot hold the fully-resident identity stack
    without WDDM spill (pure; the [HARDWARE] backend calls it with the CUDA
    device's total memory)."""
    try:
        total = float(total_vram_bytes)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False  # unknown -> keep the default resident path
    return total < IDENTITY_RESIDENT_VRAM_MIN_GB * (1 << 30)


def pin_hf_offline(settings: Any) -> None:
    """Pin the process's Hugging Face posture OFFLINE at app startup when the
    §2 offline configuration is complete (a local ``pipeline_config_dir`` is
    set). MUST run before any heavy import: huggingface_hub freezes
    ``HF_HUB_OFFLINE`` at import time, and the normal flow's first heavy
    import is the BASE backend's — which predates the 3b identity-backend
    offline gate — so an env set at identity/cull build time is silently
    ineffective in a process warmed by a base render (hardware-validation
    catch, 2026-07-12: a bootstrap cull's cached-model resolutions made live
    etag requests even with every model local). Hard-set, not setdefault:
    the app setting is authoritative. With the config dir unset the hub
    stays reachable for the documented one-time config warm (§1); the
    backend-level setdefaults remain as backstops for direct construction."""
    import os

    raw = settings.get("models.image.pipeline_config_dir")
    if raw is not None and str(raw).strip():
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"


def _pipeline_config_dir(settings: Any) -> Path | None:
    """Resolve ``models.image.pipeline_config_dir`` to an absolute path (mirrors
    ``ImageEngine._resolve``), or None if unset."""
    raw = settings.get("models.image.pipeline_config_dir")
    if raw is None or not str(raw).strip():
        return None
    path = Path(str(raw))
    return path if path.is_absolute() else APP_ROOT / path


def clip_token_counter(settings: Any):
    """A ``Callable[[str], int]`` over the MODEL's own CLIP tokenizer
    (``<pipeline_config_dir>/tokenizer``, already on disk from the 3b offline
    posture), or ``None`` when it is unavailable — no ``pipeline_config_dir``
    configured, the tokenizer files absent, or ``transformers`` not installed
    (the GPU-less sandbox). Deliberately returns None rather than vendoring a
    second BPE that could drift from the model's — an honest "unavailable" beats
    a wrong number (Stage 5.5b). Counts CONTENT tokens (no BOS/EOS), matching
    the 75-token-per-window budget."""
    config = _pipeline_config_dir(settings)
    if config is None:
        return None
    tok_dir = config / "tokenizer"
    if not (tok_dir / "vocab.json").is_file():
        return None
    try:
        from transformers import CLIPTokenizer
    except ImportError:
        return None
    try:
        tokenizer = CLIPTokenizer.from_pretrained(str(tok_dir), local_files_only=True)
    except Exception:  # noqa: BLE001 — any load failure -> honestly unavailable
        return None
    # We deliberately measure prompts LONGER than 77 (that is the whole point —
    # showing the overflow); raise the cap so the tokenizer does not log a
    # spurious "sequence longer than 77" warning on every over-budget count.
    tokenizer.model_max_length = 10 ** 9

    def count(text: str) -> int:
        return len(tokenizer(text, add_special_tokens=False).input_ids)

    return count


class EngineUnavailable(RuntimeError):
    """Generation cannot run here/now: missing deps, GPU, or checkpoint."""


class EngineBusy(RuntimeError):
    """The VRAM slot is held by the other heavy model (§3)."""


class GenerationFailed(RuntimeError):
    """The backend raised mid-generation (CUDA OOM, corrupt weights, ...).
    Wrapped so a hardware fault surfaces as a structured bridge error, never
    a leaked traceback (the Stage-0 error contract)."""


class ReferenceUnreadable(RuntimeError):
    """A present, containment-validated reference image could not be decoded
    (corrupt / not an image). Surfaced structured, never a mid-denoise crash."""


# -- chunked (long-prompt) CLIP encoding (Stage 5.5b) -------------------------
#
# SDXL's CLIP text encoders cap at 77 tokens; diffusers truncates a longer
# prompt silently. A fully-detailed character record assembles to 106–137
# tokens (measured with the real BPE), so outfit / style / free-text / pose
# fragments are dropped past 77 — it has never bitten only because no
# fully-detailed character had ever been rendered. The fix (no new dependency —
# compel is rejected, it drags conflicting transformers/diffusers pins; the 3f
# precedent governs): split the assembled prompt on commas into <=77-token
# windows, encode_prompt each, and concatenate the embeddings along the
# SEQUENCE axis so nothing is dropped.
#
# API locked against diffusers 0.39 source (StableDiffusionXLPipeline):
# encode_prompt(prompt, prompt_2=None, device=None, num_images_per_prompt=1,
#   do_classifier_free_guidance=True, negative_prompt=None, negative_prompt_2=None
#   ...) -> (prompt_embeds[B,77,2048], negative_prompt_embeds[B,77,2048],
#            pooled_prompt_embeds[B,1280], negative_pooled_prompt_embeds[B,1280]).
# Each call pads/truncates to 77, and the negative is tokenized to the positive's
# length — so padding BOTH chunk lists to a common K and encoding pairwise keeps
# prompt_embeds and negative_prompt_embeds equal-length (diffusers requires that
# under CFG) by construction; pooled embeds come from the first window.

# 77 CLIP slots = BOS + 75 content + EOS. Pack windows to <=75 content tokens so
# encode_prompt's max_length pad never truncates a fragment out of a window.
CLIP_WINDOW = 77
CLIP_CONTENT_BUDGET = CLIP_WINDOW - 2


def _comma_windows(tokenizer: Any, text: str,
                   budget: int = CLIP_CONTENT_BUDGET) -> list[str]:
    """Greedily pack the comma-separated fragments of ``text`` into windows
    whose CLIP content-token count is ``<= budget``. A single fragment longer
    than a window is emitted alone (encode_prompt truncates it — nothing
    shorter is possible without splitting a word). Always returns >= 1 window."""
    pieces = [p.strip() for p in text.split(",") if p.strip()]
    if not pieces:
        return [""]
    windows: list[str] = []
    current: list[str] = []
    for piece in pieces:
        # Measure the ACTUAL joined window — the ", " separators cost tokens too,
        # so summing per-fragment counts under-counts and overflows the window.
        candidate = ", ".join(current + [piece])
        n = len(tokenizer(candidate, add_special_tokens=False).input_ids)
        if current and n > budget:
            windows.append(", ".join(current))
            current = [piece]
        else:
            current.append(piece)
    if current:
        windows.append(", ".join(current))
    return windows or [""]


def encode_chunked(pipe: Any, torch: Any, positive: str, negative: str,
                   chunked: bool = True) -> dict:
    """Long-prompt SDXL encoding: window each prompt on commas, ``encode_prompt``
    each window, concatenate along the sequence axis. Returns the four embed
    tensors as pipe kwargs (``prompt_embeds`` / ``negative_prompt_embeds`` /
    ``pooled_prompt_embeds`` / ``negative_pooled_prompt_embeds``) to pass in
    place of the ``prompt`` / ``negative_prompt`` strings. A short prompt yields
    one window — behaviourally identical to the old string path.

    ``chunked=False`` (5.5b A/B baseline, driven by image_gen.encode_chunked)
    is the pre-5.5b path: a SINGLE ``encode_prompt`` over the raw strings, which
    diffusers truncates at 77 tokens — the shape the truncation table measured.
    Returns the same four-embed dict either way, so callers are unchanged."""
    if not chunked:
        pe, ne, ppe, npe = pipe.encode_prompt(
            prompt=positive,
            negative_prompt=negative,
            num_images_per_prompt=1,
            do_classifier_free_guidance=True,
        )
        return {
            "prompt_embeds": pe,
            "negative_prompt_embeds": ne,
            "pooled_prompt_embeds": ppe,
            "negative_pooled_prompt_embeds": npe,
        }
    pos_windows = _comma_windows(pipe.tokenizer, positive)
    neg_windows = _comma_windows(pipe.tokenizer, negative)
    k = max(len(pos_windows), len(neg_windows))
    pos_windows += [""] * (k - len(pos_windows))
    neg_windows += [""] * (k - len(neg_windows))
    pos_list = []
    neg_list = []
    pooled = None
    neg_pooled = None
    for i in range(k):
        pe, ne, ppe, npe = pipe.encode_prompt(
            prompt=pos_windows[i],
            negative_prompt=neg_windows[i],
            num_images_per_prompt=1,
            do_classifier_free_guidance=True,
        )
        pos_list.append(pe)
        neg_list.append(ne)
        if i == 0:
            pooled, neg_pooled = ppe, npe
    return {
        "prompt_embeds": torch.cat(pos_list, dim=1),
        "negative_prompt_embeds": torch.cat(neg_list, dim=1),
        "pooled_prompt_embeds": pooled,
        "negative_pooled_prompt_embeds": neg_pooled,
    }


@dataclass(frozen=True)
class IPAdapterConfig:
    """The resolved IP-Adapter model location for one variant. ``dir`` is a
    local h94/IP-Adapter mirror (never a hub repo id — §2 offline); the
    weight name + encoder folder come from the code table, so the variant
    selector is the only user-facing knob."""

    dir: Path
    variant: str
    weight_name: str
    image_encoder_folder: str = IP_ADAPTER_ENCODER_FOLDER

    def weight_path(self) -> Path:
        return self.dir / IP_ADAPTER_SUBFOLDER / self.weight_name

    def encoder_dir(self) -> Path:
        return self.dir / self.image_encoder_folder


@dataclass(frozen=True)
class GenerationRequest:
    positive: str
    negative: str
    width: int = 832
    height: int = 1216
    steps: int = 28
    cfg_scale: float = 5.5
    sampler: str = "euler_a"
    seed: int | None = None
    # None on a base (3a) request; a float in [0, 1] on a steered (3b) request.
    ip_adapter_scale: float | None = None
    # None unless the request runs the LoRA/catalog (3e) backend; a float in
    # [0, MAX_LORA_SCALE] applied to the fused identity LoRA at inference.
    lora_scale: float | None = None
    # 5.5b: True (default) = chunked long-prompt encoding (carries a >77-token
    # prompt in full); False = the pre-5.5b single-encode path (truncates at
    # 77), the A/B baseline. Byte-identical to before when True.
    chunked: bool = True

    def validate(self) -> None:
        for name, dim in (("width", self.width), ("height", self.height)):
            if not isinstance(dim, int) or not (MIN_DIM <= dim <= MAX_DIM):
                raise ValueError(
                    f"{name} must be an integer in [{MIN_DIM}, {MAX_DIM}], got {dim!r}"
                )
            if dim % 8:
                raise ValueError(f"{name} must be a multiple of 8, got {dim}")
        if not isinstance(self.steps, int) or not (1 <= self.steps <= 200):
            raise ValueError(f"steps must be an integer in [1, 200], got {self.steps!r}")
        if not (1.0 <= float(self.cfg_scale) <= 30.0):
            raise ValueError(f"cfg_scale must be in [1, 30], got {self.cfg_scale!r}")
        if self.sampler not in SAMPLERS:
            raise ValueError(
                f"unknown sampler {self.sampler!r}; expected one of "
                f"{tuple(SAMPLERS)}"
            )
        if self.seed is not None and (
            not isinstance(self.seed, int)
            or isinstance(self.seed, bool)
            or not (0 <= self.seed <= MAX_SEED)
        ):
            raise ValueError(f"seed must be an integer in [0, {MAX_SEED}], got {self.seed!r}")
        if self.ip_adapter_scale is not None:
            scale = self.ip_adapter_scale
            if (
                isinstance(scale, bool)
                or not isinstance(scale, (int, float))
                or not math.isfinite(scale)
                or not (0.0 <= scale <= 1.0)
            ):
                raise ValueError(
                    f"ip_adapter_scale must be a number in [0, 1], got {scale!r}"
                )
        if self.lora_scale is not None:
            scale = self.lora_scale
            if (
                isinstance(scale, bool)
                or not isinstance(scale, (int, float))
                or not math.isfinite(scale)
                or not (0.0 <= scale <= MAX_LORA_SCALE)
            ):
                raise ValueError(
                    f"lora_scale must be a number in [0, {MAX_LORA_SCALE}], got {scale!r}"
                )
        if not self.positive.strip():
            raise ValueError("positive prompt is empty")

    def to_dict(self) -> dict:
        data = {
            "positive": self.positive,
            "negative": self.negative,
            "width": self.width,
            "height": self.height,
            "steps": self.steps,
            "cfg_scale": self.cfg_scale,
            "sampler": self.sampler,
            "seed": self.seed,
        }
        # Omit-if-None so a base (3a) request's sidecar stays byte-identical.
        if self.ip_adapter_scale is not None:
            data["ip_adapter_scale"] = self.ip_adapter_scale
        if self.lora_scale is not None:
            data["lora_scale"] = self.lora_scale
        return data


@dataclass(frozen=True)
class GenerationResult:
    """``image`` is a PIL image from the real backend (anything exposing
    ``save(path)`` from a fake); ``request`` carries the resolved seed."""

    image: Any
    request: GenerationRequest


# A backend factory takes (checkpoint path, optional local pipeline-config
# dir, optional IPAdapterConfig) and returns a backend exposing
#   generate(request[, reference]) -> image   # reference only in identity mode
#   close() -> None
# ip_config's None-ness selects the mode: None -> base (3a), set -> identity (3b).
BackendFactory = Callable[..., Any]


class _SDXLBackendBase:
    """Shared mechanics of the three real [HARDWARE] backends: §2 env
    hygiene, the guarded heavy-import stack, single-file pipeline
    construction, sampler application, and §3 whole-pipe teardown. Pipeline
    assembly order and ``generate()`` stay per-subclass — they ARE the mode.
    Every heavy import lives behind ``_import_stack`` so the module imports
    clean without torch."""

    # The noun in the CUDA-guard message ("<mode> runs on the 16 GB ...").
    _MODE = "base generation"

    @staticmethod
    def _pin_env(config_dir: Path | None, offline_with_config: bool) -> None:
        # §2 hygiene BEFORE any heavy import: no telemetry, and no tqdm bars —
        # under pythonw sys.stderr is None and a hub/diffusers progress bar
        # firing during from_single_file would raise before our own
        # set_progress_bar_config ever ran.
        import os

        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        if offline_with_config and config_dir is not None:
            # Fully-offline mode: every artifact this backend loads is local,
            # so pin every loader offline. Gated on config_dir so the 3a
            # validation-phase one-time config warm (config_dir unset) still
            # works exactly as documented.
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    def _import_stack(self):
        """Import torch/diffusers behind the §16 guard; returns
        (torch, StableDiffusionXLPipeline) with progress bars disabled."""
        try:
            import torch
            from diffusers import StableDiffusionXLPipeline
            from diffusers.utils import logging as diffusers_logging
        except ImportError as exc:
            raise EngineUnavailable(
                "image dependencies are not installed — on the target machine "
                "run: pip install -r requirements-full.txt "
                f"(missing: {exc.name or exc})"
            ) from exc
        if not torch.cuda.is_available():
            raise EngineUnavailable(
                f"no CUDA GPU available — {self._MODE} runs on the 16 GB "
                "target machine (DECISIONS.md §3)"
            )
        try:
            diffusers_logging.disable_progress_bar()
        except AttributeError:
            pass  # older diffusers without the helper; env vars still hold
        return torch, StableDiffusionXLPipeline

    @staticmethod
    def _from_single_file(pipeline_cls, torch, checkpoint: Path,
                          config_dir: Path | None):
        # Offline posture (§2): with a bundled pipeline-config dir configured
        # (models.image.pipeline_config_dir) the load is fully local; without
        # one, diffusers resolves the SDXL component configs from the HF Hub
        # cache — a documented ONE-TIME warm during hardware validation, and
        # Stage 7 bundles the config so the packaged app never needs it.
        kwargs: dict = {"torch_dtype": torch.float16, "use_safetensors": True}
        if config_dir is not None:
            kwargs["config"] = str(config_dir)
            kwargs["local_files_only"] = True
        return pipeline_cls.from_single_file(str(checkpoint), **kwargs)

    def _apply_sampler(self, sampler: str) -> None:
        if sampler == self._sampler_applied:
            return
        import diffusers

        cls_name, kwargs = SAMPLERS[sampler]
        scheduler_cls = getattr(diffusers, cls_name)
        self._pipe.scheduler = scheduler_cls.from_config(
            self._pipe.scheduler.config, **kwargs
        )
        self._sampler_applied = sampler

    def close(self) -> None:
        # Pipelines sit in reference cycles: drop the reference, force the
        # collect, THEN empty the cache — otherwise the tensors outlive the
        # release and the swap scaffold's whole point (§3) is defeated.
        # Dropping the whole pipe frees everything it carries (checkpoint +
        # any adapter/encoder/LoRA) together.
        import gc

        pipe, self._pipe = self._pipe, None
        del pipe
        gc.collect()
        self._torch.cuda.empty_cache()


class _DiffusersSDXLBackend(_SDXLBackendBase):
    """The real backend. Constructed only on the target machine."""

    _MODE = "base generation"

    def __init__(self, checkpoint: Path, config_dir: Path | None = None):
        self._pin_env(config_dir, offline_with_config=False)
        torch, pipeline_cls = self._import_stack()
        self._torch = torch
        pipe = self._from_single_file(pipeline_cls, torch, checkpoint, config_dir)
        pipe.to("cuda")
        # ~1MP decodes fit 16 GB comfortably; slicing costs little and keeps
        # headroom for the heavy variant (§3: slow is fine).
        pipe.enable_vae_slicing()
        pipe.set_progress_bar_config(disable=True)  # no console (§2)
        self._pipe = pipe
        self._sampler_applied: str | None = None

    def generate(self, request: GenerationRequest):
        torch = self._torch
        self._apply_sampler(request.sampler)
        generator = torch.Generator("cuda").manual_seed(request.seed)
        with torch.inference_mode():
            embeds = encode_chunked(self._pipe, torch,
                                    request.positive, request.negative,
                                    chunked=request.chunked)
            out = self._pipe(
                **embeds,
                width=request.width,
                height=request.height,
                num_inference_steps=request.steps,
                guidance_scale=request.cfg_scale,
                generator=generator,
            )
        return out.images[0]


class _DiffusersIPAdapterSDXLBackend(_SDXLBackendBase):
    """The real [HARDWARE] steered backend (Stage 3b). Same SDXL pipeline as
    the base backend, plus a loaded IP-Adapter and its image encoder. Built
    fresh for the identity mode and torn down whole on a mode switch — the
    engine never toggles ``load_ip_adapter``/``unload_ip_adapter`` on a
    resident pipe (that stateful path is hardware-only-testable, and a plain
    call on a loaded adapter raises), so all mode logic lives in the
    sandbox-verifiable engine layer."""

    _MODE = "identity generation"

    def __init__(
        self,
        checkpoint: Path,
        config_dir: Path | None = None,
        ip_config: IPAdapterConfig | None = None,
    ):
        if ip_config is None:  # defensive: the factory only builds this with one
            raise EngineUnavailable("no IP-Adapter configured for identity generation")
        self._pin_env(config_dir, offline_with_config=True)
        torch, pipeline_cls = self._import_stack()
        self._torch = torch
        pipe = self._from_single_file(pipeline_cls, torch, checkpoint, config_dir)
        pipe.set_progress_bar_config(disable=True)
        # Load the IP-Adapter + its ViT-H image encoder. image_encoder_folder is
        # the pinned slash-form (repo-root -> ViT-H) — see the module constant
        # comment; local_files_only is ALWAYS on (the adapter + encoder are
        # user-placed local files, never fetched — §2). Loaded BEFORE the
        # device placement below (diffusers' documented order for the offload
        # path; `.to("cuda")` moves the adapter + encoder along with the pipe).
        try:
            pipe.load_ip_adapter(
                str(ip_config.dir),
                subfolder=IP_ADAPTER_SUBFOLDER,
                weight_name=ip_config.weight_name,
                image_encoder_folder=ip_config.image_encoder_folder,
                local_files_only=True,
            )
        except Exception as exc:
            raise EngineUnavailable(
                f"failed to load the IP-Adapter ({ip_config.variant}): {exc}"
            ) from exc
        if identity_needs_cpu_offload(
            torch.cuda.get_device_properties(0).total_memory
        ):
            # 12 GB-class card (see the module constant): a resident stack
            # spills WDDM and halves throughput; offload keeps the peak at
            # ~the largest single component for a few seconds of PCIe
            # transfer per render (§3: slow is fine, spill-thrash is not).
            pipe.enable_model_cpu_offload()
        else:
            pipe.to("cuda")
        pipe.enable_vae_slicing()
        self._pipe = pipe
        self._sampler_applied: str | None = None
        self._scale_applied: float | None = None

    def generate(self, request: GenerationRequest, reference: Path):
        torch = self._torch
        self._apply_sampler(request.sampler)
        try:
            from PIL import Image

            image = Image.open(str(reference))
            image.load()
            image = image.convert("RGB")
        except Exception as exc:
            # A present, contained-but-corrupt reference must not crash the
            # denoise loop — surface it structured.
            raise ReferenceUnreadable(
                f"could not read the reference image {reference}: {exc}"
            ) from exc
        scale = request.ip_adapter_scale
        if scale is None:  # defensive: the service always sets it for identity
            scale = DEFAULT_IP_ADAPTER_SCALE
        if scale != self._scale_applied:
            self._pipe.set_ip_adapter_scale(scale)
            self._scale_applied = scale
        generator = torch.Generator("cuda").manual_seed(request.seed)
        with torch.inference_mode():
            embeds = encode_chunked(self._pipe, torch,
                                    request.positive, request.negative,
                                    chunked=request.chunked)
            out = self._pipe(
                **embeds,
                width=request.width,
                height=request.height,
                num_inference_steps=request.steps,
                guidance_scale=request.cfg_scale,
                generator=generator,
                ip_adapter_image=image,
            )
        return out.images[0]


class _DiffusersLoraSDXLBackend(_SDXLBackendBase):
    """The real [HARDWARE] catalog backend (Stage 3e). Same SDXL pipeline as
    the base backend plus a loaded per-character identity LoRA (unfused; the
    strength is applied per-generate via ``cross_attention_kwargs``, so no
    reload is needed to retune it). Built fresh for the catalog mode and torn
    down whole on a mode switch."""

    _MODE = "catalog generation"

    def __init__(
        self,
        checkpoint: Path,
        config_dir: Path | None = None,
        lora: Path | None = None,
    ):
        if lora is None:  # defensive: the factory only builds this with one
            raise EngineUnavailable("no LoRA configured for catalog generation")
        self._pin_env(config_dir, offline_with_config=True)
        torch, pipeline_cls = self._import_stack()
        self._torch = torch
        pipe = self._from_single_file(pipeline_cls, torch, checkpoint, config_dir)
        pipe.to("cuda")
        pipe.enable_vae_slicing()
        pipe.set_progress_bar_config(disable=True)
        # Load the character's trained LoRA (a local file — §2). Kept UNFUSED so
        # the scale can vary per-generate; the LoRA weights are user-placed
        # (trained at 3d), never fetched.
        try:
            pipe.load_lora_weights(str(lora.parent), weight_name=lora.name)
        except Exception as exc:
            # Degrade to the UNet-only subset before refusing (hardware-
            # validation catch, 2026-07-12): diffusers 0.39's kohya converter
            # has a te1/te2 regression (empty text-encoder rank_dict ->
            # IndexError), so a TE-carrying kohya LoRA — e.g. one trained
            # before the network_train_unet_only default, or by a user-swapped
            # trainer — would brick catalog mode. The UNet part carries the
            # identity payload (verified on hardware); newly trained LoRAs are
            # UNet-only anyway, so this path is for legacy/foreign files.
            try:
                from safetensors.torch import load_file

                unet_only = {
                    k: v for k, v in load_file(str(lora)).items()
                    if k.startswith("lora_unet_")
                }
                if not unet_only:
                    raise ValueError("no lora_unet_ keys to fall back on")
                pipe.load_lora_weights(unet_only)
            except Exception:
                # surface the ORIGINAL failure — the fallback is best-effort
                raise EngineUnavailable(f"failed to load the LoRA: {exc}") from exc
        self._pipe = pipe
        self._sampler_applied: str | None = None

    def generate(self, request: GenerationRequest):
        torch = self._torch
        self._apply_sampler(request.sampler)
        scale = request.lora_scale
        if scale is None:  # defensive: the service always sets it for catalog
            scale = DEFAULT_LORA_SCALE
        generator = torch.Generator("cuda").manual_seed(request.seed)
        with torch.inference_mode():
            embeds = encode_chunked(self._pipe, torch,
                                    request.positive, request.negative,
                                    chunked=request.chunked)
            out = self._pipe(
                **embeds,
                width=request.width,
                height=request.height,
                num_inference_steps=request.steps,
                guidance_scale=request.cfg_scale,
                generator=generator,
                cross_attention_kwargs={"scale": scale},
            )
        return out.images[0]


def _default_backend_factory(
    checkpoint: Path,
    config_dir: Path | None = None,
    ip_config: IPAdapterConfig | None = None,
    lora: Path | None = None,
) -> Any:
    """Dispatch on mode: IP-Adapter backend (3b identity) when ip_config is
    set, LoRA backend (3e catalog) when lora is set, else the base (3a)
    backend. ip_config and lora are mutually exclusive by mode."""
    if ip_config is not None:
        return _DiffusersIPAdapterSDXLBackend(checkpoint, config_dir, ip_config)
    if lora is not None:
        return _DiffusersLoraSDXLBackend(checkpoint, config_dir, lora)
    return _DiffusersSDXLBackend(checkpoint, config_dir)


class ImageEngine:
    """Owns the loaded pipeline + the VRAM slot. Thread-safe: pywebview
    dispatches each bridge call on its own thread."""

    def __init__(
        self,
        settings: Settings,
        *,
        backend_factory: BackendFactory | None = None,
    ):
        self._settings = settings
        self._factory = backend_factory or _default_backend_factory
        self._backend: Any = None
        self._loaded_checkpoint: Path | None = None
        self._loaded_ip_config: IPAdapterConfig | None = None
        self._loaded_lora: Path | None = None
        self._lock = threading.RLock()

    # -- checkpoint resolution ------------------------------------------------

    @staticmethod
    def _resolve(raw: object) -> Path | None:
        """A settings path value, or None for unset/blank. Relative paths
        resolve against the app folder (self-contained, §2)."""
        if raw is None or not str(raw).strip():
            return None
        path = Path(str(raw))
        return path if path.is_absolute() else APP_ROOT / path

    def checkpoint_path(self) -> Path | None:
        """The active variant's checkpoint (§3): the heavy path when the heavy
        variant is selected AND configured (blank counts as unconfigured),
        else the default path."""
        checkpoint = None
        if self._settings.get("models.image.variant") == "heavy":
            checkpoint = self._resolve(
                self._settings.get("models.image.heavy_checkpoint_path")
            )
        if checkpoint is None:
            checkpoint = self._resolve(
                self._settings.get("models.image.checkpoint_path")
            )
        return checkpoint

    def config_dir(self) -> Path | None:
        """Optional local diffusers pipeline-config dir for a fully-offline
        single-file load (§2; Stage 7 bundles one)."""
        return self._resolve(self._settings.get("models.image.pipeline_config_dir"))

    def ip_adapter_config(self) -> IPAdapterConfig | None:
        """The resolved IP-Adapter model config (Stage 3b), or None when no
        local IP-Adapter dir is configured. The variant selects the weight
        from the code table; an unknown variant falls back to 'standard' so a
        hand-edit cannot pick an unpaired weight/encoder."""
        directory = self._resolve(self._settings.get("models.image.ip_adapter.dir"))
        if directory is None:
            return None
        variant = self._settings.get("models.image.ip_adapter.variant", "standard")
        if variant not in IP_ADAPTER_VARIANTS:
            variant = "standard"
        weight_name = IP_ADAPTER_VARIANTS[variant]["weight_name"]
        return IPAdapterConfig(
            dir=directory,
            variant=variant,
            weight_name=weight_name,
            image_encoder_folder=IP_ADAPTER_ENCODER_FOLDER,
        )

    @property
    def loaded_checkpoint(self) -> Path | None:
        """The checkpoint the live backend was ACTUALLY built from — the
        sidecar records this, never the current settings value (which may
        have changed since load)."""
        return self._loaded_checkpoint

    @property
    def loaded_ip_config(self) -> IPAdapterConfig | None:
        """The IP-Adapter config the live backend was ACTUALLY built with
        (None in base mode) — the sidecar records this, not the settings."""
        return self._loaded_ip_config

    @property
    def loaded_lora(self) -> Path | None:
        """The LoRA the live catalog backend was ACTUALLY built with (None
        outside catalog mode)."""
        return self._loaded_lora

    # -- status / lifecycle -----------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._backend is not None

    def status(self) -> dict:
        """Structural availability probe — cheap, import-free, callable from
        the UI and the hardware-validation checklist."""
        checkpoint = self.checkpoint_path()
        ip = self.ip_adapter_config()
        if not self.loaded:
            loaded_mode = None
        elif self._loaded_ip_config:
            loaded_mode = "identity"
        elif self._loaded_lora:
            loaded_mode = "catalog"
        else:
            loaded_mode = "base"
        return {
            "loaded": self.loaded,
            "loaded_mode": loaded_mode,
            "loaded_lora": str(self._loaded_lora) if self._loaded_lora else None,
            "loaded_checkpoint": (
                str(self._loaded_checkpoint) if self._loaded_checkpoint else None
            ),
            "active_model": self._settings.get("models.active"),
            "variant": self._settings.get("models.image.variant"),
            "checkpoint": str(checkpoint) if checkpoint else None,
            "checkpoint_exists": bool(checkpoint and checkpoint.is_file()),
            "torch_installed": _importlib_util.find_spec("torch") is not None,
            "diffusers_installed": _importlib_util.find_spec("diffusers") is not None,
            "samplers": list(SAMPLERS),
            # -- IP-Adapter (3b) availability --
            "ip_adapter": (
                {
                    "dir": str(ip.dir),
                    "variant": ip.variant,
                    "weight_name": ip.weight_name,
                    "image_encoder_folder": ip.image_encoder_folder,
                }
                if ip
                else None
            ),
            "ip_adapter_configured": ip is not None,
            "ip_adapter_dir_exists": bool(ip and ip.dir.is_dir()),
            "ip_adapter_weight_exists": bool(ip and ip.weight_path().is_file()),
            "ip_adapter_encoder_exists": bool(ip and ip.encoder_dir().is_dir()),
            "ip_adapter_variants": list(IP_ADAPTER_VARIANTS),
        }

    def load(self, mode: str = "base", lora: Path | None = None) -> None:
        """Take the VRAM slot and construct the backend for ``mode`` ('base',
        'identity', or 'catalog'). The load-key is the triple (checkpoint,
        ip_config, lora): idempotent while all three are unchanged; any change
        — a checkpoint/variant swap, a base/identity/catalog mode flip, or a
        different character's LoRA — rides the hardened unload+reload swap
        branch (one heavy model at a time, §3). ``lora`` (an absolute path) is
        supplied by the caller for catalog mode. Refuses while the chat model
        holds the slot — the sequenced swap manager is Stage 6a."""
        with self._lock:
            checkpoint = self.checkpoint_path()
            ip_config = self.ip_adapter_config() if mode == "identity" else None
            lora_path = lora if mode == "catalog" else None
            # Mode preconditions are checked BEFORE the idempotency short-circuit
            # for two reasons: (1) a misconfigured request must always surface,
            # not be masked by a resident backend; (2) ip_config/lora is None in
            # its mode when unconfigured, which would otherwise be mistaken for
            # base mode's None in the load-key below and silently serve the
            # request from a resident BASE backend.
            if mode == "identity":
                if ip_config is None:
                    raise EngineUnavailable(
                        "no IP-Adapter configured — set "
                        "models.image.ip_adapter.dir in data/settings.json to a "
                        "local h94/IP-Adapter mirror (see docs/IMAGE_PIPELINE.md)"
                    )
                if not ip_config.weight_path().is_file():
                    raise EngineUnavailable(
                        f"IP-Adapter weights not found: {ip_config.weight_path()}"
                    )
                if not ip_config.encoder_dir().is_dir():
                    raise EngineUnavailable(
                        f"IP-Adapter image encoder not found: {ip_config.encoder_dir()}"
                    )
            elif mode == "catalog":
                if lora_path is None:
                    raise EngineUnavailable(
                        "no LoRA supplied for catalog generation — train one "
                        "first (Stage 3d)"
                    )
                if not lora_path.is_file():
                    raise EngineUnavailable(f"LoRA weights not found: {lora_path}")
            if self._backend is not None:
                if (
                    checkpoint == self._loaded_checkpoint
                    and ip_config == self._loaded_ip_config
                    and lora_path == self._loaded_lora
                ):
                    return
                self.unload()  # checkpoint/variant/mode changed: swap, don't lie
            active = self._settings.get("models.active")
            if active == "chat":
                raise EngineBusy(
                    "the chat model holds the VRAM slot; it must be unloaded "
                    "before image generation (swap manager lands at Stage 6a)"
                )
            if checkpoint is None:
                raise EngineUnavailable(
                    "no image checkpoint configured — set "
                    "models.image.checkpoint_path in data/settings.json to an "
                    "Illustrious-XL-family .safetensors file "
                    "(see docs/IMAGE_PIPELINE.md)"
                )
            if not checkpoint.is_file():
                raise EngineUnavailable(f"image checkpoint not found: {checkpoint}")
            try:
                backend = self._factory(
                    checkpoint, self.config_dir(), ip_config, lora_path)
            except (EngineBusy, EngineUnavailable):
                raise
            except Exception as exc:
                # A corrupt/truncated checkpoint or a load-time OOM must not
                # leak a raw traceback through the bridge.
                raise EngineUnavailable(
                    f"failed to load the image model: {exc}"
                ) from exc
            # The backend exists — nothing after this point may lose it.
            self._backend = backend
            self._loaded_checkpoint = checkpoint
            self._loaded_ip_config = ip_config
            self._loaded_lora = lora_path
            try:
                self._settings.set("models.active", "image")
            except OSError:
                # Disk-full/AV-locked settings persist. In-memory slot state
                # (what sequencing reads this run) is already correct; a
                # failed persist must not torpedo a loaded multi-GB pipeline.
                pass

    def unload(self) -> None:
        """Free the backend and release the VRAM slot. Best-effort teardown:
        a backend that dies mid-close (device-side assert after a failed
        generation) must still release the slot, and a failed settings
        persist must not surface as a bridge traceback."""
        with self._lock:
            backend, self._backend = self._backend, None
            self._loaded_checkpoint = None
            self._loaded_ip_config = None
            self._loaded_lora = None
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    pass
            if self._settings.get("models.active") == "image":
                try:
                    self._settings.set("models.active", None)
                except OSError:
                    pass

    # -- generation ---------------------------------------------------------------

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """Run one base (3a) generation. Loads on demand; a random seed is
        resolved here so every result is reproducible from its sidecar."""
        request.validate()
        with self._lock:
            self.load(mode="base")
            if request.seed is None:
                request = replace(request, seed=random.randint(0, MAX_SEED))
            try:
                image = self._backend.generate(request)
            except Exception as exc:
                raise GenerationFailed(f"generation failed: {exc}") from exc
            return GenerationResult(image=image, request=request)

    def generate_identity(
        self, request: GenerationRequest, reference: Path
    ) -> GenerationResult:
        """Run one IP-Adapter-steered (3b) generation against ``reference``
        (an absolute, service-validated path). Loads the identity backend on
        demand, swapping from base mode if necessary."""
        request.validate()
        with self._lock:
            self.load(mode="identity")
            if request.seed is None:
                request = replace(request, seed=random.randint(0, MAX_SEED))
            try:
                image = self._backend.generate(request, reference)
            except ReferenceUnreadable:
                # A bad reference image is the caller's problem, not an engine
                # fault — surface it distinctly, do not fold into GenerationFailed.
                raise
            except Exception as exc:
                raise GenerationFailed(f"generation failed: {exc}") from exc
            return GenerationResult(image=image, request=request)

    def generate_catalog(
        self, request: GenerationRequest, lora: Path
    ) -> GenerationResult:
        """Run one LoRA-steered (3e) catalog generation using ``lora`` (an
        absolute, service-validated path to the character's trained LoRA).
        Loads the catalog backend on demand, swapping from another mode if
        necessary."""
        request.validate()
        with self._lock:
            self.load(mode="catalog", lora=lora)
            if request.seed is None:
                request = replace(request, seed=random.randint(0, MAX_SEED))
            try:
                image = self._backend.generate(request)
            except Exception as exc:
                raise GenerationFailed(f"generation failed: {exc}") from exc
            return GenerationResult(image=image, request=request)
