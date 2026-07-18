"""Image service — the bridge between the UI and the image pipeline.

Mirrors the CreatorService stance: strict shape validation at the doorway,
structured ``{ok: ...}`` results the UI maps onto fields, and the safety
gates living below this layer (the assembler's Layer-1 prompt gate runs on
every path — safety never depends on the UI behaving).

``ImageService`` composes one mixin per pipeline stage — each module owns a
stage's public bridge methods AND its private helpers; this module owns
construction, the shared privates every stage uses (``_load_record``,
``_assemble``, ``_persist_image``, the quiet-delete/message helpers), and
the historic import surface (``ARTIFACT_LOAD_ERRORS`` et al. re-exported
from ``service_shared``):

- ``service_generation`` — status, prompt preview, base gen (3a), identity
  reference + IP-Adapter-steered gen (3b)
- ``service_bootstrap``  — identity bootstrap + auto-filter cull (3c)
- ``service_lora``       — identity LoRA promotion (3d)
- ``service_catalog``    — seed catalog generation (3e)
- ``service_matte``      — matting / keyable output (3f)
- ``service_scene``      — scene backgrounds + compositing (Stage 5, §13)
- ``service_cache``      — on-demand generation + LRU cache (3g) + footprint

Path safety: a reference path is DUALLY containment-checked — at set-time and
again at use-time — because ``character.json`` is hand-editable, so a stored
``reference_image_path`` is untrusted input at generation time (§11).

Layer 4: every generation (and every refused one) is audited — local review is
what makes boundary-testing visible (§11). The Layer-1 prompt gate + Layer-2
negative age anchors run unchanged on every render path.
"""

from __future__ import annotations

import json
import math
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import __version__
from ..audit import AuditLog
from ..config import Settings
from ..model import (
    AgeError,
    BuilderStore,
    CharacterNotFound,
    CharacterRecord,
    CharacterStore,
    ContentBlocked,
    InvalidId,
    OptionCatalog,
    load_builder_catalog,
)
from ..model.store import atomic_write_json
from . import cull as cull_mod
from . import lora as lora_mod
from . import matte as matte_mod
from .cull import ClassifierFactory, ToolkitFactory
from .matte import MatteFactory
from .lora import TrainerFactory
from .engine import (
    DEFAULT_IP_ADAPTER_SCALE,
    GenerationResult,
    ImageEngine,
    IPAdapterConfig,
    MAX_SEED,
)
from .prompt import AssembledPrompt, PromptAssembler, PromptBlocked

# Re-exported shared names: this module is the historic import surface
# (creator/library/builders + tests import ARTIFACT_LOAD_ERRORS et al.
# from here).
from .service_shared import ARTIFACT_LOAD_ERRORS, _now_iso
from .service_generation import _GenerationOps
from .service_bootstrap import _BootstrapOps
from .service_lora import _LoraOps
from .service_catalog import _CatalogOps
from .service_matte import _MatteOps
from .service_scene import _SceneOps
from .service_cache import _CacheOps


class ImageService(_GenerationOps, _BootstrapOps, _LoraOps, _CatalogOps, _MatteOps, _SceneOps, _CacheOps):
    """Owns the engine + assembler on behalf of the UI bridge.

    ``catalog_provider`` returns the *current* option catalog (the creator's,
    so a live "Reload options" changes prompt assembly the same instant it
    changes the form)."""

    def __init__(
        self,
        store: CharacterStore,
        settings: Settings,
        audit: AuditLog,
        *,
        catalog_provider: Callable[[], OptionCatalog],
        engine: ImageEngine | None = None,
        assembler: PromptAssembler | None = None,
        toolkit_factory: ToolkitFactory | None = None,
        trainer_factory: TrainerFactory | None = None,
        matte_factory: MatteFactory | None = None,
        builder_store: BuilderStore | None = None,
        scene_catalog_provider: Callable[[], OptionCatalog] | None = None,
        classifier_factory: ClassifierFactory | None = None,
    ):
        self._store = store
        self._settings = settings
        self._audit = audit
        self._catalog = catalog_provider
        self._engine = engine or ImageEngine(settings)
        self._assembler = assembler or PromptAssembler()
        # Stage-5 scene/compositing (§13). The builder store shares the same
        # data root as the character store; the scene catalog provider returns
        # the live SCENE option catalog (the BuilderService's, so a builder
        # "Reload options" reaches scene prompt assembly). A default is
        # supplied so a standalone ImageService still composes scenes.
        self._builder_store = builder_store or BuilderStore(store.root)
        self._scene_catalog = scene_catalog_provider or (
            lambda: load_builder_catalog("scene"))
        # The Layer-2 classifier-only factory for background generation
        # (§11) — injected like the matte factory so the flow is
        # sandbox-verifiable with a fake classifier.
        self._classifier_factory = (
            classifier_factory or cull_mod._default_classifier_factory)
        # The 3c cull toolkit factory (face embedder / quality / Layer-2
        # classifier / face-swapper). Injected like the engine's backend
        # factory so the whole bootstrap is sandbox-verifiable with fakes.
        self._toolkit_factory = toolkit_factory or cull_mod._default_toolkit_factory
        # The 3d LoRA trainer factory (kohya subprocess by default). Injected
        # so the whole promotion flow is sandbox-verifiable with a fake trainer.
        self._trainer_factory = trainer_factory or lora_mod._default_trainer_factory
        # The 3f matting factory (ISNet/BiRefNet ONNX + the Layer-2
        # classifier). Injected so the whole matte flow is sandbox-verifiable
        # with a fake matter.
        self._matte_factory = matte_factory or matte_mod._default_matte_factory

    @property
    def engine(self) -> ImageEngine:
        return self._engine

    @staticmethod
    def _delete_quietly(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    @staticmethod
    def _delete_tree_quietly(path: Path) -> None:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    # Preflight-missing kind → user-facing pointer (docs/IMAGE_PIPELINE.md is
    # the canonical fix reference). One table for the cull/trainer/matte
    # families — kinds are globally unique; classifier_unavailable is shared
    # by cull and matte with identical text.
    _MISSING_MESSAGES = {
        "face_models_missing": "no face-recognition models — set "
        "models.image.face_recognition_dir to a dir containing "
        "models/buffalo_l/ (see docs/IMAGE_PIPELINE.md)",
        "classifier_unavailable": "the Layer-2 content classifier is "
        "unavailable — set models.image.content_classifier_dir",
        "swap_model_missing": "face-swap is enabled but "
        "models.image.face_swapper_path is missing",
        "trainer_unavailable": "no LoRA trainer configured — set "
        "models.image.lora_trainer_dir to a kohya-ss sd-scripts checkout "
        "(see docs/IMAGE_PIPELINE.md)",
        "matting_model_missing": "no matting model — set "
        "models.image.matting_model_path to a user-placed "
        "isnet-anime.onnx (see docs/IMAGE_PIPELINE.md §16)",
    }

    @classmethod
    def _cull_missing_message(cls, kind: str) -> str:
        return cls._MISSING_MESSAGES.get(kind, f"cull unavailable: {kind}")

    @classmethod
    def _trainer_missing_message(cls, kind: str) -> str:
        return cls._MISSING_MESSAGES.get(kind, f"trainer unavailable: {kind}")

    @classmethod
    def _matte_missing_message(cls, kind: str) -> str:
        return cls._MISSING_MESSAGES.get(kind, f"matting unavailable: {kind}")

    @staticmethod
    def _toolkit_load_error(kind: str, what: str, exc: Exception) -> dict:
        """A non-*Unavailable failure building a model toolkit (missing
        dependency import, corrupt model, undecodable reference) —
        structured, never a bridge traceback (§2)."""
        return {"ok": False, "kind": kind,
                "error": f"the {what} could not be loaded — finish "
                         "`pip install -r requirements-full.txt` on the "
                         f"target machine and place the model files ({exc})"}

    @staticmethod
    def _cull_load_error(exc: Exception) -> dict:
        return ImageService._toolkit_load_error(
            "cull_unavailable", "cull models", exc)

    @staticmethod
    def _matte_load_error(exc: Exception) -> dict:
        return ImageService._toolkit_load_error(
            "matte_unavailable", "matting model", exc)

    def release_engine(self) -> dict:
        """Unload the pipeline and free the VRAM slot (manual until the
        Stage-6a swap manager owns sequencing)."""
        self._engine.unload()
        return {"ok": True, **self._engine.status()}

    # -- internals ---------------------------------------------------------------

    def _load_record(self, character_id: object) -> CharacterRecord | dict:
        """Load + re-gate a stored record, mapping every failure mode to the
        structured kind it actually is. The distinctions matter: a record
        whose stored text is caught by an UPDATED blocklist is a policy block
        (audited, Layer 4), not a phantom "no such character"."""
        cid = str(character_id or "").strip()
        if not cid:
            return {"ok": False, "kind": "invalid", "error": "a character id is required"}
        try:
            return self._store.load(cid)
        except (CharacterNotFound, InvalidId):
            # InvalidId: ensure_safe_id on a crafted id — same outcome: no
            # such character, and no path influence either way.
            return {"ok": False, "kind": "not_found",
                    "error": f"no character with id {cid!r}"}
        except ContentBlocked as exc:
            # Load-time Layer-1 hit (hand-edited file, or a record predating
            # a blocklist tightening) — a block, on the record (Layer 4).
            self._audit.log(
                "filter_block",
                layer=1,
                category=exc.category,
                context=f"image.load.{exc.field_name}",
                matched=exc.matched,
                character_id=cid,
            )
            return {"ok": False, "kind": "blocked", "source": exc.field_name,
                    "category": exc.category,
                    "error": f"stored record blocked by the content policy "
                             f"({exc.category})"}
        except AgeError as exc:
            # Layer 3: a hand-edited record cannot re-enter through this door.
            return {"ok": False, "kind": "age", "error": str(exc)}
        except ARTIFACT_LOAD_ERRORS as exc:
            # Unreadable (AV lock/disk) or corrupt/invalid record file.
            # OverflowError: a hand-edited Infinity in a footprint int field;
            # TypeError/LookupError/AttributeError: non-dict nested values
            # (e.g. `"identity": "x"`) — the same hand-edit class the manifest
            # loaders guard. The typed except clauses above run first, so
            # not_found/blocked/age keep their precise kinds (InvalidId is a
            # ValueError subclass but is caught by the earlier clause).
            return {"ok": False, "kind": "io",
                    "error": f"could not read character {cid!r}: {exc}"}

    def _assemble(self, record: CharacterRecord) -> AssembledPrompt | dict:
        try:
            return self._assembler.assemble(record, self._catalog())
        except PromptBlocked as exc:
            self._audit.log(
                "filter_block",
                layer=1,
                category=exc.category,
                context=f"image.prompt.{exc.source}",
                matched=exc.matched,
                character_id=record.id,
            )
            return {"ok": False, "kind": "blocked", "source": exc.source,
                    "category": exc.category,
                    "error": f"image prompt blocked by the content policy "
                             f"({exc.category})"}

    @staticmethod
    def _parse_seed(seed: object) -> int | None | dict:
        """None passes through (engine rolls one); otherwise an integer in
        [0, MAX_SEED]. The bridge may deliver JSON numbers as floats."""
        if seed is None:
            return None
        if isinstance(seed, bool) or not isinstance(seed, (int, float)):
            return {"ok": False, "kind": "invalid", "error": "seed must be a number"}
        if isinstance(seed, float):
            if not seed.is_integer():
                return {"ok": False, "kind": "invalid",
                        "error": "seed must be a whole number"}
            seed = int(seed)
        if not (0 <= seed <= MAX_SEED):
            return {"ok": False, "kind": "invalid",
                    "error": f"seed must be in [0, {MAX_SEED}]"}
        return seed

    def _resolve_reference(
        self, character_id: str, raw: object, *, allow_absolute: bool
    ) -> tuple[Path, str] | dict:
        """Resolve a reference path to ``(absolute, char-relative-posix)`` iff
        it names an existing file INSIDE the character's own directory, else a
        structured error. This is the security boundary for the identity
        pipeline — the stored ``reference_image_path`` is hand-editable, so it
        is untrusted input every time it is used.

        ``allow_absolute`` is True only at set-time (the UI hands back the
        absolute path of the frame it just generated); at use-time it is False,
        so a stored absolute value is itself a tamper signal.
        """
        text = str(raw or "").strip()
        if not text:
            return {"ok": False, "kind": "reference_invalid",
                    "error": "a reference path is required"}
        if "\x00" in text:
            # A NUL makes Path.resolve()/stat() raise ValueError (NOT OSError);
            # reject it up front so no branch reaches resolve() with one.
            return {"ok": False, "kind": "reference_invalid",
                    "error": "reference path contains a null byte"}
        try:
            char_dir = self._store.char_dir(character_id).resolve()
        except (ValueError, OSError):
            # char_dir applies ensure_safe_id + the resolve().parent check; a
            # bad id here means the same "no such character" as _load_record.
            return {"ok": False, "kind": "not_found",
                    "error": f"no character with id {character_id!r}"}
        candidate = Path(text)
        # Reject traversal / absolute components BEFORE joining: on Windows
        # `char_dir / "C:/x"` discards the base entirely, so an absolute
        # component must be caught here, not left to the containment check.
        if ".." in candidate.parts:
            return {"ok": False, "kind": "reference_invalid",
                    "error": "reference path escapes the character directory"}
        if candidate.is_absolute() or candidate.drive or candidate.anchor:
            if not allow_absolute:
                return {"ok": False, "kind": "reference_invalid",
                        "error": "reference path must be inside the character "
                                 "directory"}
        else:
            candidate = char_dir / candidate
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            # ValueError: an embedded NUL (already rejected above, but a
            # platform may raise it for other malformed paths too) is not an
            # OSError — mirror _load_record / char_dir which catch both.
            return {"ok": False, "kind": "reference_invalid",
                    "error": "reference path could not be resolved"}
        # Authoritative containment after resolve() collapses '..' AND symlinks.
        if not (resolved == char_dir or char_dir in resolved.parents):
            return {"ok": False, "kind": "reference_invalid",
                    "error": "reference path is outside the character directory"}
        if not resolved.is_file():
            return {"ok": False, "kind": "reference_missing",
                    "error": "the reference image no longer exists"}
        return resolved, resolved.relative_to(char_dir).as_posix()

    def _ip_adapter_scale(self, scale: object) -> float | dict:
        """None -> the defensively-coerced settings default (never raises,
        like _generation_settings). Otherwise validate the override to a
        finite float in [0, 1], else a structured 'invalid'."""
        if scale is None:
            try:
                value = float(
                    self._settings.get("image_gen.ip_adapter_scale",
                                       DEFAULT_IP_ADAPTER_SCALE)
                )
            except (TypeError, ValueError, OverflowError):
                return DEFAULT_IP_ADAPTER_SCALE
            if not math.isfinite(value) or not (0.0 <= value <= 1.0):
                return DEFAULT_IP_ADAPTER_SCALE
            return value
        if isinstance(scale, bool) or not isinstance(scale, (int, float)):
            return {"ok": False, "kind": "invalid",
                    "error": "ip_adapter_scale must be a number"}
        value = float(scale)
        if not math.isfinite(value) or not (0.0 <= value <= 1.0):
            return {"ok": False, "kind": "invalid",
                    "error": "ip_adapter_scale must be in [0, 1]"}
        return value

    def _generation_settings(self) -> dict:
        """The image_gen knobs, coerced defensively so this NEVER raises — it
        runs on every generate AND every status probe, both outside the
        request try/except. json.loads accepts Infinity/-Infinity/1e999 as
        floats, and int(inf) raises OverflowError, so a hand-edited
        settings.json must not reach the bridge as a traceback. The request
        re-validates anyway; bad values fall back to defaults."""
        def _int(key: str, default: int) -> int:
            try:
                value = float(self._settings.get(f"image_gen.{key}", default))
            except (TypeError, ValueError, OverflowError):
                return default
            return int(value) if math.isfinite(value) else default

        def _float(key: str, default: float) -> float:
            try:
                value = float(self._settings.get(f"image_gen.{key}", default))
            except (TypeError, ValueError, OverflowError):
                return default
            return value if math.isfinite(value) else default

        sampler = self._settings.get("image_gen.sampler", "euler_a")
        return {
            "width": _int("width", 832),
            "height": _int("height", 1216),
            "steps": _int("steps", 28),
            "cfg_scale": _float("cfg_scale", 5.5),
            "sampler": str(sampler) if sampler else "euler_a",
            # 5.5b: default True; only an explicit `false` disables chunking
            # (the A/B baseline). A missing/junk value is truthy-coerced to the
            # safe full-prompt path.
            "chunked": self._settings.get("image_gen.encode_chunked", True)
            is not False,
        }

    def _persist_image(
        self, out_dir: Path, prefix: str, seed: object, image, sidecar: dict
    ) -> tuple[Path, Path]:
        """Atomically reserve a unique ``<prefix>-<stamp>-<seed>.png`` (O_EXCL,
        not check-then-save — two same-second same-seed persists must never
        overwrite each other, even from concurrent bridge threads), save the
        image, and write its reproducibility sidecar. Shared by character-frame
        persistence (3a/3b) and Stage-5 scene-background persistence so both get
        the identical atomicity and no-half-write guarantees. The caller builds
        the sidecar dict (no absolute path is ever written to it)."""
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        stem = f"{prefix}-{stamp}-{seed}"
        counter = 1
        while True:
            name = stem if counter == 1 else f"{stem}-{counter}"
            frame_path = out_dir / f"{name}.png"
            try:
                os.close(os.open(frame_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                break
            except FileExistsError:
                counter += 1
        try:
            image.save(str(frame_path))
        except BaseException:
            try:  # do not leave the zero-byte reservation behind
                os.unlink(frame_path)
            except OSError:
                pass
            raise
        sidecar_path = frame_path.with_suffix(".json")
        atomic_write_json(sidecar_path, sidecar)
        return frame_path, sidecar_path

    def _persist_frame(
        self,
        record: CharacterRecord,
        assembled: AssembledPrompt,
        result: GenerationResult,
        *,
        subdir: str,
        prefix: str,
        kind: str,
        stage: str,
        reference: str | None = None,
        ip_adapter: IPAdapterConfig | None = None,
    ) -> tuple[Path, Path]:
        """Write a frame + reproducibility sidecar under ``characters/<id>/
        <subdir>/``. Shared by base (3a) and identity (3b) generation; the 3b
        call adds the char-relative ``reference`` and an ``ip_adapter``
        provenance block. Delegates the atomic write to ``_persist_image``."""
        # The checkpoint the live backend was BUILT from — settings may have
        # changed since load; the sidecar must not lie about provenance.
        checkpoint = self._engine.loaded_checkpoint
        try:
            checkpoint_bytes = checkpoint.stat().st_size if checkpoint else None
        except OSError:
            checkpoint_bytes = None
        sidecar: dict = {
            "kind": kind,
            "stage": stage,
            "character_id": record.id,
            "record_updated_at": record.updated_at,
            "created_at": _now_iso(),
            "app_version": __version__,
            "checkpoint": checkpoint.name if checkpoint else None,
            "checkpoint_bytes": checkpoint_bytes,
            "request": result.request.to_dict(),
            "pieces": [p.to_dict() for p in assembled.pieces],
        }
        if reference is not None:
            sidecar["reference"] = reference  # char-relative, never absolute
        if ip_adapter is not None:
            sidecar["ip_adapter"] = {
                "dir": ip_adapter.dir.name,  # basename only (provenance, not a path)
                "variant": ip_adapter.variant,
                "weight_name": ip_adapter.weight_name,
                "image_encoder_folder": ip_adapter.image_encoder_folder,
                "scale": result.request.ip_adapter_scale,
            }
        out_dir = self._store.char_dir(record.id) / subdir
        return self._persist_image(
            out_dir, prefix, result.request.seed, result.image, sidecar)


def build_image_service(
    store: CharacterStore,
    settings: Settings,
    audit: AuditLog,
    catalog_provider: Callable[[], OptionCatalog],
    *,
    builder_store: BuilderStore | None = None,
    scene_catalog_provider: Callable[[], OptionCatalog] | None = None,
) -> ImageService:
    # Wrap the engine so the byte-frozen per-frame loops become cooperatively
    # cancellable + progress-reporting under a running job (Stage 5.5a). The
    # proxy is a pure pass-through when no job is active (current_token() is
    # None) — the synchronous / test / hardware-harness path is byte-identical.
    from ..jobs import CancellableEngine

    return ImageService(
        store, settings, audit, catalog_provider=catalog_provider,
        engine=CancellableEngine(ImageEngine(settings)),
        builder_store=builder_store,
        scene_catalog_provider=scene_catalog_provider,
    )
