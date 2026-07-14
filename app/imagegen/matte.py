"""Matting / keyable output (Stage 3f — DECISIONS.md §7, §13).

Background-removes the Stage-3e seed-catalog frames into keyable RGBA cutouts
(straight alpha, ORIGINAL RGB preserved) so Stage 5 can composite characters
over generated backgrounds. One model-backed step behind a **fakeable
Protocol**, so the whole flow is verified in the GPU-less sandbox with an
injected fake; only the real ONNX backend is [HARDWARE]:

  Matter — ISNet/BiRefNet salient-object matting on the already-installed
           onnxruntime stack (a user-placed .onnx, like the checkpoint)

Method pick (the deferred spec item, resolved here): a direct-ONNX
reimplementation of rembg's ISNet pipeline (rembg is MIT — the pre/post below
reproduces rembg/sessions/base.py + dis_anime.py / dis_general_use.py /
birefnet_general.py verbatim). rembg itself is NOT installed: its historical
opencv-python-headless dependency is gone upstream (~2.0.72), but it still
hard-depends on pymatting/scikit-image/scipy, floors numpy>=2.3 and
pillow>=12.1, forces onnxruntime>=1.23.2 via its [cpu] extra, and ships a
runtime model downloader (pooch) — none of which this ~30-line recipe needs.
transparent-background is ruled out (hard dep on opencv-python, a second cv2
distribution). Keyable-background *generation* is ruled out: 3f's input is
the already-generated, already-culled 3e catalog — regenerating on a flat key
would discard the vetting those exact pixels passed and re-roll identity, and
SDXL does not render trustworthy flat keys (spill, key-colored hair/costumes).

Two rembg quirks are deliberately reproduced for parity (hardware checklist
diffs against rembg itself): the input is scaled by the image's own max pixel
value (NOT /255), and the predicted mask is per-image min-max stretched. Two
deviations: the stretch is epsilon-guarded (upstream divides by zero on a
constant output), and the alpha is applied putalpha-style (original RGB kept)
instead of rembg's naive_cutout (which blends edge RGB toward black and gives
dark fringes when re-composited).

Design invariants (mirrors cull.py):

- **Fail-closed.** A missing/unconfigured matting model or Layer-2 classifier
  raises ``MatteUnavailable`` at preflight; nothing is ever matted
  unclassified (the service re-screens every source frame per run).
- **Path-in / dataclass-out.** The pure ``evaluate_matte`` gate and the config
  coercion import and run with none of numpy/PIL/onnxruntime installed.
- Every threshold is a hardware-tuned default (§16), not a constant.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from ..config import Settings
from .cull import (  # private cross-module reuse has repo precedent
    ContentClassifier,  # (service.py uses cull_mod._default_toolkit_factory,
    _ImgutilsContentClassifier,  # cull_mod._cosine)
    _load_minor_tags,
    _resolve,
    content_classifier_dir,
    onnx_providers,
)

DEFAULT_VARIANT = "isnet_anime"
MASK_EPSILON = 1e-6  # min-max stretch guard (rembg has none — div-by-zero hazard)
SOLID_ALPHA = 128    # coverage = fraction of pixels with alpha >= this


class MatteUnavailable(RuntimeError):
    """A required matting model is missing/unconfigured (fail-closed).
    ``kind`` is a structured reason: matting_model_missing /
    classifier_unavailable."""

    def __init__(self, kind: str, message: str = ""):
        self.kind = kind
        super().__init__(message or kind)


@dataclass(frozen=True)
class VariantSpec:
    """One model family's pre/post constants — the ONLY per-model differences
    (verified verbatim from rembg sessions @ 2.0.76)."""

    size: int                          # square model input
    mean: tuple[float, float, float]
    std: tuple[float, float, float]
    sigmoid: bool                      # applied in PYTHON after the graph


VARIANTS: dict[str, VariantSpec] = {
    "isnet_anime":   VariantSpec(1024, (0.485, 0.456, 0.406), (1.0, 1.0, 1.0), False),
    "isnet_general": VariantSpec(1024, (0.5, 0.5, 0.5), (1.0, 1.0, 1.0), False),
    "birefnet":      VariantSpec(1024, (0.485, 0.456, 0.406),
                                 (0.229, 0.224, 0.225), True),
}


@dataclass(frozen=True)
class MatteConfig:
    variant: str = DEFAULT_VARIANT  # isnet_anime | isnet_general | birefnet
    erode_px: int = 0               # halo choke: N passes of MinFilter(3); 0 = rembg parity
    feather_px: int = 0             # Gaussian re-soften after erode; 0 = off
    coverage_min: float = 0.02      # degenerate floor (model found no subject)
    coverage_max: float = 0.98      # degenerate ceiling (model keyed nothing out)
    # 5.5g escalation seam: an explicit model file to build the session from.
    # None (every primary config) => matting_model_path(settings) unchanged;
    # set only on the escalation config so the factory loads the BiRefNet .onnx.
    model_path: str | None = None


@dataclass(frozen=True)
class MatteReading:
    """Backend success measurement, computed on the FINAL post-processed mask
    (what would ship). Failure is signalled by raising, never by a reading."""

    coverage: float    # fraction of pixels with alpha >= SOLID_ALPHA
    mean_alpha: float  # mean(alpha)/255 — diagnostic for threshold tuning


@runtime_checkable
class Matter(Protocol):
    def matte(self, src: Path, out: Path) -> MatteReading:
        """Write an RGBA cutout of ``src`` to ``out`` (a caller-owned TEMP
        path; the caller promotes/deletes it, and deletes it on any raise).
        May raise."""
        ...


@dataclass
class MatteToolkit:
    """The matter + the Layer-2 classifier, built together so a matte run can
    re-screen every source frame. ``close()`` frees the session best-effort."""

    matter: Matter
    classifier: ContentClassifier
    closer: Callable[[], None] | None = None

    def close(self) -> None:
        if self.closer is not None:
            try:
                self.closer()
            except Exception:
                pass


# A matte factory takes (settings, config) and returns a MatteToolkit, or
# raises MatteUnavailable. Injected into ImageService like ToolkitFactory.
MatteFactory = Callable[[Settings, MatteConfig], MatteToolkit]


# -- pure gate logic (sandbox-verifiable; no heavy deps) ----------------------


def evaluate_matte(reading: MatteReading, config: MatteConfig) -> str | None:
    """None = usable; else the per-frame failure status. A non-finite
    coverage is a nonsense reading -> matte_failed (not a model judgment)."""
    if not math.isfinite(reading.coverage):
        return "matte_failed"
    if reading.coverage < config.coverage_min:
        return "matte_empty"
    if reading.coverage > config.coverage_max:
        return "matte_full"
    return None


# -- settings resolution -------------------------------------------------------


def matting_model_path(settings: Settings) -> Path | None:
    return _resolve(settings.get("models.image.matting_model_path"))


def preflight_matte(settings: Settings) -> str | None:
    """Cheap, import-free existence check BEFORE building anything. Matting
    needs the matting model + the Layer-2 classifier — NOT the face models
    (unlike the cull preflight). The factory re-guards (fail-closed)."""
    path = matting_model_path(settings)
    if path is None or not path.is_file():
        return "matting_model_missing"
    cc = content_classifier_dir(settings)
    if cc is None or not cc.is_dir():
        return "classifier_unavailable"
    return None


def coerce_matte_config(settings: Settings) -> MatteConfig:
    """Build a MatteConfig from image_gen.matting.*, coerced defensively so a
    hand-edited Infinity/NaN/string never reaches the bridge as a traceback
    (mirrors coerce_cull_config). Bad values -> code defaults; clamped."""
    d = MatteConfig()

    def _int(key: str, default: int, *, lo: int = 0, hi: int = 8) -> int:
        try:
            v = float(settings.get(f"image_gen.matting.{key}", default))
        except (TypeError, ValueError, OverflowError):
            return default
        if not math.isfinite(v):
            return default
        return int(min(hi, max(lo, v)))

    def _float(key: str, default: float) -> float:
        try:
            v = float(settings.get(f"image_gen.matting.{key}", default))
        except (TypeError, ValueError, OverflowError):
            return default
        if not math.isfinite(v):
            return default
        return min(1.0, max(0.0, v))

    raw_variant = settings.get("image_gen.matting.variant", d.variant)
    # isinstance FIRST: `in` on the dict with an unhashable raw (list) raises.
    variant = raw_variant if (isinstance(raw_variant, str)
                              and raw_variant in VARIANTS) else d.variant
    coverage_min = _float("coverage_min", d.coverage_min)
    coverage_max = _float("coverage_max", d.coverage_max)
    if coverage_min > coverage_max:  # nonsense band -> both back to defaults
        coverage_min, coverage_max = d.coverage_min, d.coverage_max
    return MatteConfig(
        variant=variant,
        erode_px=_int("erode_px", d.erode_px),
        feather_px=_int("feather_px", d.feather_px),
        coverage_min=coverage_min,
        coverage_max=coverage_max,
    )


# -- 5.5g close-up-bust escalation (3f residual) ------------------------------

DEFAULT_ESCALATION_VARIANT = "birefnet"
DEFAULT_ESCALATION_COVERAGE = 0.85  # clean ceiling (~0.28) < this < coverage_max


@dataclass(frozen=True)
class EscalationConfig:
    """The second-model re-matte config for tight busts. ``config`` shares the
    primary's gate/knobs but points at the escalation model + variant; a frame
    whose PRIMARY solid-alpha coverage >= ``coverage`` is re-matted with it."""

    config: MatteConfig
    coverage: float


def matting_escalation_model_path(settings: Settings) -> Path | None:
    return _resolve(settings.get("models.image.matting_escalation_model_path"))


def coerce_escalation_config(
    settings: Settings, primary: MatteConfig
) -> EscalationConfig | None:
    """Build the escalation config from image_gen.matting.escalation_* + the
    escalation model path, coerced defensively (bad hand-edit -> default).

    Returns ``None`` **only** when the escalation model path is UNSET — that is
    the byte-for-byte no-op guarantee (no second session, no factory call, no
    manifest keys). A path that is SET but points at a missing file still yields
    a config; it degrades to disabled at build time in the service, so "unset"
    (feature off) stays distinguishable from "misconfigured" (feature on, model
    absent). The escalation inherits the primary's gate + halo knobs so the
    escalated result is judged by the SAME band."""
    path = matting_escalation_model_path(settings)
    if path is None:
        return None

    raw_variant = settings.get("image_gen.matting.escalation_variant",
                               DEFAULT_ESCALATION_VARIANT)
    variant = (raw_variant if (isinstance(raw_variant, str)
                               and raw_variant in VARIANTS)
               else DEFAULT_ESCALATION_VARIANT)
    try:
        cov = float(settings.get("image_gen.matting.escalation_coverage",
                                 DEFAULT_ESCALATION_COVERAGE))
    except (TypeError, ValueError, OverflowError):
        cov = DEFAULT_ESCALATION_COVERAGE
    if not math.isfinite(cov):
        cov = DEFAULT_ESCALATION_COVERAGE
    cov = min(1.0, max(0.0, cov))

    esc = MatteConfig(
        variant=variant,
        erode_px=primary.erode_px,
        feather_px=primary.feather_px,
        coverage_min=primary.coverage_min,
        coverage_max=primary.coverage_max,
        model_path=str(path),
    )
    return EscalationConfig(config=esc, coverage=cov)


# ===========================================================================
# [HARDWARE] real backend — every heavy import is lazy and inside a method,
# so this module imports clean on the GPU-less sandbox. Pre/post reproduces
# rembg (MIT) sessions/base.py + dis_anime.py / dis_general_use.py /
# birefnet_general.py verbatim, except: (a) an epsilon on the min-max
# stretch, (b) putalpha instead of naive_cutout, (c) optional erode/feather
# (both default off). Validated on the 16 GB target (§17 checklist).
# ===========================================================================


class _OnnxMatter:
    """ISNet/BiRefNet matting on onnxruntime. One session reused across
    frames; the input tensor name is read dynamically from the graph (never
    hardcoded — "input.1"/"img" vary per export; rembg's own approach)."""

    def __init__(self, session: Any, spec: VariantSpec, config: MatteConfig):
        self._session = session
        self._input_name = session.get_inputs()[0].name
        self._spec = spec
        self._erode_px = config.erode_px
        self._feather_px = config.feather_px

    def close(self) -> None:
        # Drop the ONLY strong ref this object holds — without this, a
        # factory-local `session = None` frees nothing (the matter still
        # pins the InferenceSession until the toolkit itself is dropped).
        self._session = None

    def matte(self, src: Path, out: Path) -> MatteReading:
        import numpy as np
        from PIL import Image, ImageFilter

        with Image.open(src) as opened:  # close the handle (Windows AV hygiene)
            rgb = opened.convert("RGB")  # convert returns a copy
        w, h = rgb.size
        # Aspect-distorted square inference, per rembg (mask is resized back).
        small = rgb.resize((self._spec.size, self._spec.size),
                           Image.Resampling.LANCZOS)
        ary = np.array(small)
        # rembg quirk: scale by the image's own max pixel, NOT /255.
        ary = ary / max(float(np.max(ary)), 1e-6)
        tmp = np.zeros((self._spec.size, self._spec.size, 3))
        for c in range(3):
            tmp[:, :, c] = (ary[:, :, c] - self._spec.mean[c]) / self._spec.std[c]
        x = np.expand_dims(tmp.transpose((2, 0, 1)), 0).astype(np.float32)
        pred = self._session.run(None, {self._input_name: x})[0][:, 0, :, :]
        if self._spec.sigmoid:  # BiRefNet graphs emit logits
            pred = 1.0 / (1.0 + np.exp(-pred))
        mi, ma = float(np.min(pred)), float(np.max(pred))
        pred = (pred - mi) / max(ma - mi, MASK_EPSILON)  # per-image stretch, GUARDED
        mask = Image.fromarray((np.squeeze(pred) * 255).astype("uint8"), mode="L")
        mask = mask.resize((w, h), Image.Resampling.LANCZOS)
        for _ in range(self._erode_px):
            mask = mask.filter(ImageFilter.MinFilter(3))  # 1px grayscale choke
        if self._feather_px:
            mask = mask.filter(ImageFilter.GaussianBlur(self._feather_px))
        rgb.putalpha(mask)  # straight alpha, ORIGINAL RGB kept (keyable)
        rgb.save(str(out), format="PNG")  # explicit: out is *.png.tmp
        m = np.asarray(mask)  # FINAL shipped mask stats
        return MatteReading(coverage=float((m >= SOLID_ALPHA).mean()),
                            mean_alpha=float(m.mean()) / 255.0)


def _default_matte_factory(settings: Settings, config: MatteConfig) -> MatteToolkit:
    """Build the real [HARDWARE] toolkit, fully offline. Re-guards preflight
    (fail-closed) and builds ONE ONNX session shared across the run. CPU
    providers by default => zero VRAM; the image engine is never touched
    (confirm_vetted precedent: light ONNX coexists with a loaded engine)."""
    kind = preflight_matte(settings)
    if kind is not None:
        raise MatteUnavailable(kind)

    import os

    # Offline: imgutils pulls its ONNX heads from the HF cache; pin it offline
    # before the first imgutils import (same trio as the cull factory).
    # HF_HOME backstop for direct-construction flows — the authoritative pin
    # is cull.pin_hf_cache() at app startup (env freezes at first hub import).
    os.environ.setdefault("HF_HOME", str(content_classifier_dir(settings)))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

    import onnxruntime as ort

    # 5.5g seam: an escalation config carries an explicit model_path (the
    # BiRefNet .onnx); every primary config leaves it None -> the primary
    # matting_model_path, byte-for-byte unchanged.
    model_path = (_resolve(config.model_path) if config.model_path
                  else matting_model_path(settings))
    session = ort.InferenceSession(
        str(model_path), providers=onnx_providers(settings)
    )
    matter = _OnnxMatter(session, VARIANTS[config.variant], config)
    classifier = _ImgutilsContentClassifier(_load_minor_tags())

    def _closer() -> None:
        import gc

        nonlocal session
        matter.close()  # the matter holds the live ref — clear it first
        session = None
        gc.collect()

    return MatteToolkit(matter=matter, classifier=classifier, closer=_closer)
