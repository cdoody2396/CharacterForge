"""Identity-bootstrap auto-filter (Stage 3c — DECISIONS.md §6, §11).

Turns a seed batch of IP-Adapter-steered candidates into a vetted, on-model
training set by culling drift, low quality, and — the safety-critical part —
policy-violating pixels. Four model-backed steps, each behind a **fakeable
Protocol** so the whole cull is verified in the GPU-less sandbox with injected
fakes; only the real models are [HARDWARE]:

  FaceEmbedder      — ArcFace/InsightFace face detection + 512-d embedding
  QualityScorer     — anime aesthetic rank (soft)
  ContentClassifier — the **Layer-2 pixel gate** (§11): minor-coded/explicit
                      content detection on generated pixels; HARD + fail-closed
  FaceSwapper       — optional inswapper identity-lock (post-cull only)

Design invariants (see docs/IMAGE_PIPELINE.md §10):

- **Content dominates and is audited on every frame.** It runs before the
  quality/similarity gates so a no-face frame that still trips a minor-coded
  tag is caught, and every block feeds the Layer-4 leakage signal.
- **Fail-closed.** A missing/unconfigured classifier raises ``CullUnavailable``
  at preflight (nothing is ever produced unclassified); a per-frame classify
  exception is treated as *blocked*.
- **Swap never precedes the similarity cull** — swapping first collapses every
  ArcFace cosine toward 1.0 and masks drift.
- **Path-in / dataclass-out.** The pure ``score_candidate`` / ``cull_and_rank``
  consume only plain dataclasses, so they import and run with none of
  torch/insightface/onnxruntime/cv2/imgutils installed.

Honest bar (§11): no single pixel check is reliable on stylized anime (the
style renders adults with neotenous features). The value is stacked
independent checks + bias-to-block + Layer-4 review — defense in depth, never
a guarantee. Every threshold is a hardware-tuned default (§16), not a constant.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from ..config import Settings
from .engine import APP_ROOT
from ..model.bootstrap import (
    STATUS_CANDIDATE,
    STATUS_KEPT,
    STATUS_PROPOSED,
    STATUS_REJECTED_CONTENT,
    STATUS_REJECTED_ERROR,
    STATUS_REJECTED_NO_FACE,
    STATUS_REJECTED_QUALITY,
    STATUS_REJECTED_SIMILARITY,
)

DATA_DIR = Path(__file__).resolve().parent / "data"
MINOR_TAGS_FILE = DATA_DIR / "minor_coded_tags.txt"

# WD14 confidence at/above which a minor-coded tag counts as a hit. Bias-to-
# block keeps this low; hardware-tuned (§16).
MINOR_TAG_CONFIDENCE = 0.35

BUFFALO_REQUIRED = ("det_10g.onnx", "w600k_r50.onnx")  # det + recognition


class CullUnavailable(RuntimeError):
    """A required cull model is missing/unconfigured (fail-closed). ``kind`` is
    a structured reason: face_models_missing / classifier_unavailable /
    swap_model_missing."""

    def __init__(self, kind: str, message: str = ""):
        self.kind = kind
        super().__init__(message or kind)


# -- readings (path-in / dataclass-out) --------------------------------------


@dataclass(frozen=True)
class FaceReading:
    found: bool
    face_count: int = 0
    det_score: float = 0.0
    area_fraction: float = 0.0
    sharpness: float = 0.0
    embedding: tuple[float, ...] | None = None  # UNIT 512-d ArcFace vector


@dataclass(frozen=True)
class QualityReading:
    aesthetic: float = 0.0
    label: str = ""


@dataclass(frozen=True)
class ContentVerdict:
    blocked: bool
    category: str | None = None
    matched: str | None = None
    scores: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"blocked": self.blocked, "category": self.category,
                "matched": self.matched}


@dataclass
class CandidateScore:
    """The cull's per-candidate result — status + the numbers behind it."""

    candidate_id: str
    status: str = STATUS_CANDIDATE
    similarity: float = 0.0
    aesthetic: float = 0.0
    det_score: float = 0.0
    face_count: int = 0
    area_fraction: float = 0.0
    sharpness: float = 0.0
    content_blocked: bool = False
    content_category: str | None = None
    content_matched: str | None = None
    rank: int | None = None

    @property
    def rejected(self) -> bool:
        return self.status.startswith("rejected_")

    def quality_dict(self) -> dict:
        return {
            "sharpness": self.sharpness,
            "aesthetic": self.aesthetic,
            "det_score": self.det_score,
            "face_area_fraction": self.area_fraction,
            "face_count": self.face_count,
        }

    def content_dict(self) -> dict:
        return {
            "blocked": self.content_blocked,
            "category": self.content_category,
            "matched": self.content_matched,
        }


@dataclass(frozen=True)
class CullConfig:
    batch: int = 64            # ~3-4x over-generation to net the 15-30 band
    keep_cap: int = 30         # suggested confirmation ceiling (advisory)
    floor: int = 15            # below this, `short` -> UI offers generate-more
    grid_size: int = 12        # the confirmation grid
    similarity_floor: float = 0.50   # same-person cosine (conservative/tight)
    det_score_floor: float = 0.50
    sharpness_floor: float = 100.0   # Laplacian variance
    face_area_min: float = 0.04
    face_area_max: float = 0.90
    face_swap_enabled: bool = False


# -- model abstractions (real impls are [HARDWARE], lazily imported) ---------


@runtime_checkable
class FaceEmbedder(Protocol):
    def embed(self, path: Path) -> FaceReading: ...


@runtime_checkable
class QualityScorer(Protocol):
    def score(self, path: Path) -> QualityReading: ...


@runtime_checkable
class ContentClassifier(Protocol):
    def classify(self, path: Path) -> ContentVerdict: ...


@runtime_checkable
class FaceSwapper(Protocol):
    def swap(self, target_path: Path, source_ref_path: Path, out_path: Path) -> bool: ...


@dataclass
class CullToolkit:
    """The four models + the cached reference reading, built together so the
    reference face is embedded once. ``close()`` frees them best-effort."""

    embedder: FaceEmbedder
    quality: QualityScorer
    classifier: ContentClassifier
    swapper: FaceSwapper | None
    ref_reading: FaceReading
    closer: Callable[[], None] | None = None

    def close(self) -> None:
        if self.closer is not None:
            try:
                self.closer()
            except Exception:
                pass


# A toolkit factory takes (settings, reference_abs|None, need_swap) and returns
# a CullToolkit with the reference embedded (ref_reading.found=False when the
# reference is None — confirm_vetted only needs the classifier), or raises
# CullUnavailable.
ToolkitFactory = Callable[[Settings, "Path | None", bool], CullToolkit]


@dataclass
class ClassifierToolkit:
    """Just the Layer-2 pixel classifier (§11), built alone for callers that
    gate generated pixels but need none of the face-embedding stack — Stage 5
    background generation. ``close()`` frees it best-effort."""

    classifier: ContentClassifier
    closer: Callable[[], None] | None = None

    def close(self) -> None:
        if self.closer is not None:
            try:
                self.closer()
            except Exception:
                pass


# A classifier factory takes (settings) and returns a ClassifierToolkit, or
# raises CullUnavailable. Injected into ImageService like ToolkitFactory so the
# background flow is sandbox-verifiable with a fake.
ClassifierFactory = Callable[[Settings], ClassifierToolkit]


def preflight_classifier(settings: Settings) -> str | None:
    """Cheap, import-free existence check for JUST the Layer-2 classifier
    (mirrors preflight_matte's classifier half). None = ready."""
    cc = content_classifier_dir(settings)
    if cc is None or not cc.is_dir():
        return "classifier_unavailable"
    return None


def _default_classifier_factory(settings: Settings) -> ClassifierToolkit:
    """Build ONLY the Layer-2 classifier, fully offline (the matte factory's
    classifier half). CPU ONNX => zero VRAM; it coexists with a loaded SDXL
    slot (the confirm_vetted / matte precedent), so a background can be
    classified without unloading the engine."""
    kind = preflight_classifier(settings)
    if kind is not None:
        raise CullUnavailable(kind)

    import os

    os.environ.setdefault("HF_HOME", str(content_classifier_dir(settings)))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

    classifier = _ImgutilsContentClassifier(_load_minor_tags())

    def _closer() -> None:
        import gc

        gc.collect()

    return ClassifierToolkit(classifier=classifier, closer=_closer)


# -- pure cull logic (sandbox-verifiable; no heavy deps) ---------------------


def _cosine(a: tuple[float, ...] | None, b: tuple[float, ...] | None) -> float:
    """Dot product of two unit vectors == cosine similarity. 0.0 if either is
    missing or degenerate."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    if not math.isfinite(dot):
        return 0.0
    return float(dot)


def score_candidate(
    toolkit: CullToolkit,
    ref_reading: FaceReading,
    candidate_id: str,
    path: Path,
    config: CullConfig,
) -> CandidateScore:
    """Score ONE candidate through the canonical, short-circuiting gate.

    Order (see module docstring): decode+detect (informational) -> CONTENT
    (hard, fail-closed, audited by the caller) -> quality floor -> similarity
    -> aesthetic (soft rank). Never raises: a decode error is rejected_error,
    a classify error is a *block*."""
    score = CandidateScore(candidate_id=candidate_id)

    # (1)+(2) decode + face detection + model-free cv2 signals
    try:
        reading = toolkit.embedder.embed(path)
    except Exception:
        score.status = STATUS_REJECTED_ERROR
        return score
    score.face_count = reading.face_count
    score.det_score = reading.det_score
    score.area_fraction = reading.area_fraction
    score.sharpness = reading.sharpness

    # (3) CONTENT — the Layer-2 pixel gate: hard, fail-closed, runs on EVERY
    # frame (even a no-face one can trip a minor-coded whole-image tag).
    try:
        verdict = toolkit.classifier.classify(path)
    except Exception:
        verdict = ContentVerdict(blocked=True, category="classifier_error",
                                 matched="classify_exception")
    score.content_blocked = verdict.blocked
    score.content_category = verdict.category
    score.content_matched = verdict.matched
    if verdict.blocked:
        score.status = STATUS_REJECTED_CONTENT
        return score

    # (4) quality floor (free from the detection pass)
    if not reading.found or reading.face_count == 0:
        score.status = STATUS_REJECTED_NO_FACE
        return score
    if (
        reading.face_count != 1
        or reading.det_score < config.det_score_floor
        or not (config.face_area_min <= reading.area_fraction <= config.face_area_max)
        or reading.sharpness < config.sharpness_floor
    ):
        score.status = STATUS_REJECTED_QUALITY
        return score

    # (5) identity similarity to the reference
    similarity = _cosine(ref_reading.embedding, reading.embedding)
    score.similarity = similarity
    if similarity < config.similarity_floor:
        score.status = STATUS_REJECTED_SIMILARITY
        return score

    # (6) aesthetic (soft — rank only; a failure must not reject an on-model frame)
    try:
        score.aesthetic = toolkit.quality.score(path).aesthetic
    except Exception:
        score.aesthetic = 0.0
    score.status = STATUS_KEPT
    return score


def cull_and_rank(
    scores: list[CandidateScore], config: CullConfig
) -> tuple[list[CandidateScore], bool]:
    """Rank the survivors (kept) by (similarity DESC, aesthetic DESC); the top
    ``grid_size`` become PROPOSED (the confirmation grid), the rest stay KEPT
    (still confirmable). Returns (survivors, short) where ``short`` means fewer
    on-model survivors than the floor — the UI should offer generate-more."""
    survivors = [s for s in scores if s.status == STATUS_KEPT]
    survivors.sort(key=lambda s: (s.similarity, s.aesthetic), reverse=True)
    for i, score in enumerate(survivors):
        score.rank = i + 1
        if i < config.grid_size:
            score.status = STATUS_PROPOSED
        else:
            score.status = STATUS_KEPT
    short = len(survivors) < config.floor
    return survivors, short


# -- settings resolution -----------------------------------------------------


def _resolve(raw: object) -> Path | None:
    """A settings path value (app-root-relative if not absolute), or None."""
    if raw is None or not str(raw).strip():
        return None
    path = Path(str(raw))
    return path if path.is_absolute() else APP_ROOT / path


def face_recognition_dir(settings: Settings) -> Path | None:
    return _resolve(settings.get("models.image.face_recognition_dir"))


def content_classifier_dir(settings: Settings) -> Path | None:
    return _resolve(settings.get("models.image.content_classifier_dir"))


def face_swapper_path(settings: Settings) -> Path | None:
    return _resolve(settings.get("models.image.face_swapper_path"))


def onnx_providers(settings: Settings) -> list[str]:
    raw = settings.get("models.image.onnx_providers")
    if isinstance(raw, (list, tuple)) and raw:
        return [str(p) for p in raw]
    return ["CPUExecutionProvider"]


def pin_hf_cache(settings: Settings) -> None:
    """Point the process's Hugging Face cache at ``content_classifier_dir``
    (the setting's documented meaning: "imgutils HF cache"). MUST run at app
    startup, before any heavy import: huggingface_hub freezes HF_HOME at
    import time, and in the normal flow the ENGINE (diffusers) imports it
    long before the first cull — an env pin at toolkit-build time would be
    silently ineffective. Hard-set, not setdefault: the app setting is
    authoritative for this app's process. No-op when the setting is unset
    (preflight fails closed as classifier_unavailable anyway)."""
    import os

    cc = content_classifier_dir(settings)
    if cc is not None:
        os.environ["HF_HOME"] = str(cc)


def preflight_cull(settings: Settings, need_swap: bool) -> str | None:
    """Cheap, import-free existence check of the required cull models, run
    BEFORE burning a batch. Returns a CullUnavailable kind or None. The full
    factory re-guards defensively (fail-closed). The identity embedder (CCIP
    + anime face detection) loads from the imgutils HF cache — the classifier
    dir witnesses all of it; buffalo_l is needed only by the optional
    face-swap (§10 embedder swap, 2026-07-12)."""
    cc = content_classifier_dir(settings)
    if cc is None or not cc.is_dir():
        return "classifier_unavailable"
    if need_swap:
        fr = face_recognition_dir(settings)
        if fr is None:
            return "face_models_missing"
        pack = fr / "models" / "buffalo_l"
        if not all((pack / f).is_file() for f in BUFFALO_REQUIRED):
            return "face_models_missing"
        sw = face_swapper_path(settings)
        if sw is None or not sw.is_file():
            return "swap_model_missing"
    return None


def coerce_cull_config(settings: Settings) -> CullConfig:
    """Build a CullConfig from image_gen.bootstrap.*, coerced defensively so a
    hand-edited Infinity/NaN/string never reaches the bridge as a traceback
    (mirrors ImageService._generation_settings). Bad values -> code defaults."""
    d = CullConfig()

    def _int(key: str, default: int) -> int:
        try:
            v = float(settings.get(f"image_gen.bootstrap.{key}", default))
        except (TypeError, ValueError, OverflowError):
            return default
        return int(v) if math.isfinite(v) and v > 0 else default

    def _float(key: str, default: float) -> float:
        try:
            v = float(settings.get(f"image_gen.bootstrap.{key}", default))
        except (TypeError, ValueError, OverflowError):
            return default
        return v if math.isfinite(v) else default

    swap = settings.get("image_gen.bootstrap.face_swap_enabled", d.face_swap_enabled)
    # batch is the one knob with no downstream per-request re-validation (it
    # drives the generate loop directly), so clamp it here — a hand-edited
    # 1e9 must not launch a billion renders. Clamp, don't error (degrade).
    return CullConfig(
        batch=min(256, max(1, _int("batch", d.batch))),
        keep_cap=_int("keep_cap", d.keep_cap),
        floor=_int("floor", d.floor),
        grid_size=_int("grid_size", d.grid_size),
        similarity_floor=_float("similarity_floor", d.similarity_floor),
        det_score_floor=_float("det_score_floor", d.det_score_floor),
        sharpness_floor=_float("sharpness_floor", d.sharpness_floor),
        face_area_min=_float("face_area_min", d.face_area_min),
        face_area_max=_float("face_area_max", d.face_area_max),
        face_swap_enabled=bool(swap),
    )


def detector_threshold(settings: Settings) -> float:
    """The detector-level confidence gate — the CONFIGURED det_score_floor,
    [0,1]-clamped at this use site (the coercion only finite-guards it — as
    a comparison it needs no more, but the detectors consume it). Feeds the
    CCIP embedder's ``detect_faces(conf_threshold=…)`` and, swap-only, the
    insightface ``prepare(det_thresh=…)``. Mirroring the floor matters:
    a detector's own default would otherwise drop faces BEFORE
    ``score_candidate``'s floor ever saw them, making a tuned floor a dead
    knob (hardware-validation catch, 2026-07-12, found on insightface's 0.5
    default vs anime faces in the 0.2-0.5 band). The pure cull still applies
    the floor to whatever the detector returns, so this only WIDENS what the
    configured floor can see, never loosens the floor itself."""
    return min(1.0, max(0.0, coerce_cull_config(settings).det_score_floor))


def _load_minor_tags() -> frozenset[str]:
    tags: set[str] = set()
    for raw_line in MINOR_TAGS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            tags.add(line.replace(" ", "_").lower())
    return frozenset(tags)


# ===========================================================================
# [HARDWARE] real backends — every heavy import is lazy and inside a method,
# so this module imports clean on the GPU-less sandbox. Structurally complete
# per docs/IMAGE_PIPELINE.md §10 api_verdict; validated on the 16 GB target.
# ===========================================================================


class _CcipEmbedder:
    """imgutils CCIP character identity + anime-trained face detection (the
    3c embedder, swapped from buffalo_l/ArcFace after the 2026-07-12 hardware
    calibration — photo-trained ArcFace sat at its margin on the anime style,
    §10). The embedding is the L2-normalized whole-image CCIP feature, so the
    pure cull's cosine similarity IS CCIP's own metric in disguise
    (``ccip_difference == (1 - cos) / 2``, verified exactly on hardware);
    det_score/area/count come from the anime face detector, whose
    conf_threshold mirrors the configured floor (``detector_threshold``)."""

    def __init__(self, det_thresh: float):
        self._det_thresh = det_thresh

    def embed(self, path: Path) -> FaceReading:
        import cv2
        import numpy as np
        from imgutils.detect import detect_faces
        from imgutils.metrics import ccip_extract_feature

        bgr = cv2.imread(str(path))  # BGR uint8 (NOT PIL RGB)
        if bgr is None:
            raise ValueError(f"could not decode image: {path}")
        faces = detect_faces(str(path), conf_threshold=self._det_thresh)
        if not faces:
            return FaceReading(found=False, face_count=0)
        # Primary face = largest bbox (the ArcFace backend's rule, kept).
        (x0, y0, x1, y1), _, det_score = max(
            faces, key=lambda f: (f[0][2] - f[0][0]) * (f[0][3] - f[0][1])
        )
        height, width = bgr.shape[:2]
        area = (x1 - x0) * (y1 - y0)
        area_frac = float(area) / float(width * height) if width and height else 0.0
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        feat = np.asarray(ccip_extract_feature(str(path)), dtype=float).ravel()
        norm = float(np.linalg.norm(feat))
        if not norm > 0.0:  # degenerate feature -> no usable identity signal
            return FaceReading(found=False, face_count=len(faces))
        embedding = tuple(float(x) for x in (feat / norm).tolist())
        return FaceReading(
            found=True,
            face_count=len(faces),
            det_score=float(det_score),
            area_fraction=area_frac,
            sharpness=sharpness,
            embedding=embedding,
        )


class _ImgutilsQualityScorer:
    """Anime aesthetic percentile (imgutils, ONNX — no torch). Rank only."""

    def score(self, path: Path) -> QualityReading:
        from imgutils.metrics import anime_dbaesthetic

        result = anime_dbaesthetic(str(path))
        label, aesthetic = "", 0.0
        # Version-tolerant unpack: (label, percentile) or (label, percentile, ...).
        if isinstance(result, (tuple, list)) and result:
            label = str(result[0])
            if len(result) > 1:
                try:
                    aesthetic = float(result[1])
                except (TypeError, ValueError):
                    aesthetic = 0.0
        return QualityReading(aesthetic=aesthetic, label=label)


class _ImgutilsContentClassifier:
    """Layer-2 pixel gate (§11). WD14 minor-coded tags (primary) + an
    explicitness escalator. Errs to block; a load/inference error is raised so
    the caller fails closed."""

    def __init__(self, minor_tags: frozenset[str]):
        self._minor_tags = minor_tags

    def classify(self, path: Path) -> ContentVerdict:
        from imgutils.tagging import get_wd14_tags

        rating, features, _chars = get_wd14_tags(str(path))
        for tag, confidence in features.items():
            norm = str(tag).replace(" ", "_").lower()
            if norm in self._minor_tags and float(confidence) >= MINOR_TAG_CONFIDENCE:
                return ContentVerdict(
                    blocked=True, category="minors", matched=norm,
                    scores={"confidence": float(confidence)},
                )
        return ContentVerdict(blocked=False, scores={"rating": rating})


class _InSwapper:
    """inswapper_128 identity lock. Shares the FaceAnalysis app for detection.
    Argument order is (img, target, source) — target BEFORE source."""

    def __init__(self, swapper_path: Path, app: Any, providers: list[str]):
        import insightface

        self._swapper = insightface.model_zoo.get_model(
            str(swapper_path), download=False, download_zip=False, providers=providers
        )
        self._app = app

    def _primary(self, bgr: Any) -> Any:
        faces = self._app.get(bgr, max_num=0)
        if not faces:
            return None
        return max(
            faces,
            key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]),
        )

    def swap(self, target_path: Path, source_ref_path: Path, out_path: Path) -> bool:
        import cv2
        from PIL import Image

        frame = cv2.imread(str(target_path))
        donor = cv2.imread(str(source_ref_path))
        if frame is None or donor is None:
            return False
        target = self._primary(frame)
        source = self._primary(donor)
        if target is None or source is None:
            return False
        out_bgr = self._swapper.get(frame, target, source, paste_back=True)
        Image.fromarray(out_bgr[:, :, ::-1]).save(str(out_path))  # BGR->RGB
        return True


def _default_toolkit_factory(
    settings: Settings, reference_abs: Path | None, need_swap: bool
) -> CullToolkit:
    """Build the real [HARDWARE] toolkit, fully offline. Re-guards existence
    (fail-closed) and embeds the reference once (skipped when reference_abs is
    None — confirm_vetted only needs the classifier). Called ONLY after the
    image engine is unloaded (§3), so its ONNX models never contend for the
    SDXL slot."""
    kind = preflight_cull(settings, need_swap)
    if kind is not None:
        raise CullUnavailable(kind)

    import os

    # Offline: imgutils pulls its ONNX heads from the HF cache; pin it offline
    # before the first imgutils import (extends engine.py's HF posture).
    # HF_HOME backstop for direct-construction flows — the authoritative pin
    # is pin_hf_cache() at app startup (env is frozen at first hub import).
    os.environ.setdefault("HF_HOME", str(content_classifier_dir(settings)))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

    providers = onnx_providers(settings)
    det_thresh = detector_threshold(settings)

    embedder = _CcipEmbedder(det_thresh)
    quality = _ImgutilsQualityScorer()
    classifier = _ImgutilsContentClassifier(_load_minor_tags())
    app = None
    if need_swap:
        # buffalo_l is only the SWAPPER's face stack now (inswapper needs its
        # detection + ArcFace source faces); the identity embedder is CCIP.
        from insightface.app import FaceAnalysis

        app = FaceAnalysis(
            name="buffalo_l",
            root=str(face_recognition_dir(settings)),
            allowed_modules=["detection", "recognition"],  # skip ~150MB landmark/attr
            providers=providers,
        )
        app.prepare(  # ctx_id=-1 forces CPU
            ctx_id=-1, det_size=(640, 640), det_thresh=det_thresh
        )
    swapper = _InSwapper(face_swapper_path(settings), app, providers) if need_swap else None
    ref_reading = embedder.embed(reference_abs) if reference_abs is not None else FaceReading(found=False)

    def _closer() -> None:
        import gc

        nonlocal app
        app = None
        gc.collect()

    return CullToolkit(
        embedder=embedder,
        quality=quality,
        classifier=classifier,
        swapper=swapper,
        ref_reading=ref_reading,
        closer=_closer,
    )
