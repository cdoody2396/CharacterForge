"""Image service (Stage 3a + 3b) — the bridge between the UI and the pipeline.

Mirrors the CreatorService stance: strict shape validation at the doorway,
structured ``{ok: ...}`` results the UI maps onto fields, and the safety
gates living below this layer (the assembler's Layer-1 prompt gate runs on
every path — safety never depends on the UI behaving).

- ``generate_base`` (3a): record → gated prompt → SDXL call → frame +
  reproducibility sidecar under ``characters/<id>/reference/``. Frames land in
  ``reference/`` because a coherent base render is exactly the candidate
  reference image the §6 bootstrap flow starts from.
- ``set_reference`` / ``clear_reference`` / ``reference_status`` (3b): promote
  a chosen in-character frame to ``IdentityAnchor.reference_image_path`` (the
  ONLY record mutation the image pipeline makes), stored char-relative and
  containment-validated both when set and when used.
- ``generate_identity`` (3b): the same gated prompt, IP-Adapter-steered by the
  stored reference for immediate identity consistency (the quick-create path),
  written under ``characters/<id>/identity/`` with an ``ip_adapter`` provenance
  block in the sidecar.

Path safety: a reference path is DUALLY containment-checked — at set-time and
again at use-time — because ``character.json`` is hand-editable, so a stored
``reference_image_path`` is untrusted input at generation time (§11).

Layer 4: every generation (and every refused one) is audited — local review is
what makes boundary-testing visible (§11). 3b adds no pixel/content gating;
the Layer-1 prompt gate + Layer-2 negative age anchors run unchanged on every
identity render, and the reference is itself a frame our own gated pipeline
produced. (The Layer-2 pixel/face classifier attaches at 3c.)
"""

from __future__ import annotations

import json
import math
import os
import shutil
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import __version__
from ..audit import AuditLog
from ..config import Settings
from ..model import (
    AgeError,
    BackgroundEntry,
    BackgroundManifest,
    BootstrapCandidate,
    BootstrapManifest,
    BuilderKindError,
    BuilderNotFound,
    BuilderRecord,
    BuilderStore,
    CatalogEntry,
    CatalogManifest,
    CharacterNotFound,
    CharacterRecord,
    CharacterStore,
    ConsentError,
    ContentBlocked,
    InvalidId,
    LoraManifest,
    OptionCatalog,
    VettedEntry,
    VettedManifest,
    load_builder_catalog,
    resolve_within,
)
from ..model.bootstrap import (
    CONFIRMABLE_STATUSES,
    PHASE_CONFIRMED,
    PHASE_CULLED,
    PHASE_PROPOSED,
    STATUS_CONFIRMED,
    STATUS_KEPT,
    STATUS_PROPOSED,
    STATUS_REJECTED_CONTENT,
    STATUS_REJECTED_ERROR,
)
from ..model.store import atomic_write_json
from . import catalog as catalog_mod
from . import composite as composite_mod
from . import cull as cull_mod
from . import lora as lora_mod
from . import manage as manage_mod
from . import matte as matte_mod
from .cull import (
    ClassifierFactory,
    ContentVerdict,
    CullConfig,
    CullToolkit,
    CullUnavailable,
    ToolkitFactory,
)
from .matte import MatteFactory, MatteUnavailable
from .lora import (
    TrainConfig,
    TrainFailed,
    TrainItem,
    TrainRequest,
    TrainUnavailable,
    TrainerFactory,
)
from .engine import (
    DEFAULT_IP_ADAPTER_SCALE,
    EngineBusy,
    EngineUnavailable,
    GenerationFailed,
    GenerationRequest,
    GenerationResult,
    ImageEngine,
    IPAdapterConfig,
    MAX_SEED,
    ReferenceUnreadable,
)
from .prompt import AssembledPrompt, PromptAssembler, PromptBlocked


@dataclass(frozen=True)
class _BatchFrame:
    """One generated bootstrap candidate before culling."""

    candidate_id: str
    abs_path: Path
    rel_path: str
    seed: int


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Everything a hand-edited JSON artifact can raise through json.loads +
# from_dict, mapped to a structured *_corrupt/io by every loader guard (the
# 3d fix-across-loaders precedent): ValueError/TypeError (bad shapes/values,
# incl. json.JSONDecodeError and InvalidId subclasses), LookupError (missing
# required key), OverflowError (int(Infinity) — json.loads accepts
# Infinity/1e999 as floats), AttributeError (.get on a non-dict nested value
# — review catch, escaped every manifest bridge), RecursionError
# (pathologically nested JSON — red-team catch), OSError (fs faults).
ARTIFACT_LOAD_ERRORS = (
    OSError, json.JSONDecodeError, ValueError, TypeError, LookupError,
    OverflowError, AttributeError, RecursionError, InvalidId,
)


class ImageService:
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

    # -- status -----------------------------------------------------------------

    def engine_status(self) -> dict:
        """Engine availability + the generation settings in force (incl. the
        3b IP-Adapter steer strength)."""
        generation = {
            **self._generation_settings(),
            "ip_adapter_scale": self._ip_adapter_scale(None),
        }
        return {**self._engine.status(), "generation": generation}

    # -- prompt preview ------------------------------------------------------------

    def preview_prompt(self, character_id: object) -> dict:
        """Assemble (and gate) the prompt pair without generating. Structural
        verification path: exercises everything but the model call, so it
        runs in the build sandbox and in the UI on any machine."""
        loaded = self._load_record(character_id)
        if isinstance(loaded, dict):
            return loaded
        assembled = self._assemble(loaded)
        if isinstance(assembled, dict):
            return assembled
        # has_reference lets the UI enable the identity control without a
        # second call; a broken/absent reference reads as False (never raises).
        raw = loaded.identity.reference_image_path
        has_reference = bool(
            raw
            and not isinstance(
                self._resolve_reference(loaded.id, raw, allow_absolute=False), dict
            )
        )
        return {"ok": True, "id": loaded.id, "has_reference": has_reference,
                **assembled.to_dict()}

    # -- base generation (3a) -------------------------------------------------------

    def generate_base(self, character_id: object, seed: object = None) -> dict:
        """One gated base render for a saved record. Returns the frame path +
        sidecar path + resolved seed, or a structured refusal."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        parsed_seed = self._parse_seed(seed)
        if isinstance(parsed_seed, dict):
            return parsed_seed
        assembled = self._assemble(record)
        if isinstance(assembled, dict):
            return assembled

        request = GenerationRequest(
            positive=assembled.positive,
            negative=assembled.negative,
            seed=parsed_seed,
            **self._generation_settings(),
        )
        try:
            result = self._engine.generate(request)
        except (EngineBusy, EngineUnavailable, GenerationFailed) as exc:
            return {"ok": False, "kind": "engine", "error": str(exc)}
        except ValueError as exc:
            # GenerationRequest.validate: a hand-edited settings file fed the
            # engine an impossible shape — report, don't crash the bridge.
            return {"ok": False, "kind": "config", "error": str(exc)}

        try:
            frame_path, sidecar_path = self._persist_frame(
                record, assembled, result,
                subdir="reference", prefix="base", kind="base", stage="3a-base",
            )
        except OSError as exc:
            # Disk full / AV lock: the frame is lost but the bridge contract
            # holds — structured error, no leaked traceback.
            return {"ok": False, "kind": "io",
                    "error": f"could not save the generated frame: {exc}"}
        self._audit.log(
            "image_generated",
            stage="3a-base",
            character_id=record.id,
            path=str(frame_path),
            seed=result.request.seed,
            positive=assembled.positive,
            negative=assembled.negative,
            settings=self._generation_settings(),
        )
        return {
            "ok": True,
            "id": record.id,
            "path": str(frame_path),
            "sidecar": str(sidecar_path),
            "seed": result.request.seed,
            "positive": assembled.positive,
            "negative": assembled.negative,
        }

    # -- identity reference + steered generation (3b) ---------------------------

    def set_reference(self, character_id: object, frame_path: object) -> dict:
        """Promote a frame the character already owns (e.g. a 3a base render)
        to its identity reference. Stores the char-relative path on the
        anchor. ``frame_path`` may be the absolute path the UI holds; it must
        resolve inside the character's own directory."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        resolved = self._resolve_reference(record.id, frame_path, allow_absolute=True)
        if isinstance(resolved, dict):
            return resolved
        _abs, rel = resolved
        record.identity.reference_image_path = rel
        record.touch()
        try:
            self._store.save(record)
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not save the reference: {exc}"}
        self._audit.log(
            "identity_reference_set", character_id=record.id, reference=rel
        )
        return {"ok": True, "id": record.id, "reference": rel}

    def clear_reference(self, character_id: object) -> dict:
        """Unset the identity reference. Does not touch any catalog (none
        exists at 3b; stale-marking is Stage 4)."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        record.identity.reference_image_path = None
        record.touch()
        try:
            self._store.save(record)
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not clear the reference: {exc}"}
        self._audit.log("identity_reference_cleared", character_id=record.id)
        return {"ok": True, "id": record.id}

    def reference_status(self, character_id: object) -> dict:
        """Whether the character has a usable identity reference — runs the
        full use-time resolver (containment + existence) WITHOUT generating,
        so the UI can enable/disable the identity control with a precise
        reason. No GPU / no PIL needed."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        raw = record.identity.reference_image_path
        if not raw:
            return {"ok": True, "id": record.id, "has_reference": False,
                    "reference": None}
        resolved = self._resolve_reference(record.id, raw, allow_absolute=False)
        if isinstance(resolved, dict):
            return resolved  # reference_invalid / reference_missing
        return {"ok": True, "id": record.id, "has_reference": True,
                "reference": resolved[1]}

    def generate_identity(
        self, character_id: object, seed: object = None, scale: object = None
    ) -> dict:
        """One IP-Adapter-steered render using the character's stored
        reference. Requires a reference to be set; re-validates its
        containment at use-time (the stored path is untrusted)."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        parsed_seed = self._parse_seed(seed)
        if isinstance(parsed_seed, dict):
            return parsed_seed
        resolved_scale = self._ip_adapter_scale(scale)
        if isinstance(resolved_scale, dict):
            return resolved_scale
        raw = record.identity.reference_image_path
        if not raw:
            return {"ok": False, "kind": "no_reference",
                    "error": "this character has no identity reference set — "
                             "generate a base image and set_reference first"}
        resolved = self._resolve_reference(record.id, raw, allow_absolute=False)
        if isinstance(resolved, dict):
            return resolved  # reference_invalid / reference_missing (no gen)
        ref_abs, ref_rel = resolved
        assembled = self._assemble(record)
        if isinstance(assembled, dict):
            return assembled

        request = GenerationRequest(
            positive=assembled.positive,
            negative=assembled.negative,
            seed=parsed_seed,
            ip_adapter_scale=resolved_scale,
            **self._generation_settings(),
        )
        try:
            result = self._engine.generate_identity(request, ref_abs)
        except (EngineBusy, EngineUnavailable, GenerationFailed) as exc:
            return {"ok": False, "kind": "engine", "error": str(exc)}
        except ReferenceUnreadable as exc:
            return {"ok": False, "kind": "reference_unreadable", "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "kind": "config", "error": str(exc)}

        try:
            frame_path, sidecar_path = self._persist_frame(
                record, assembled, result,
                subdir="identity", prefix="identity", kind="identity",
                stage="3b-identity",
                reference=ref_rel, ip_adapter=self._engine.loaded_ip_config,
            )
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not save the generated frame: {exc}"}
        self._audit.log(
            "identity_generated",
            stage="3b-identity",
            character_id=record.id,
            path=str(frame_path),
            seed=result.request.seed,
            scale=result.request.ip_adapter_scale,
            reference=ref_rel,
            positive=assembled.positive,
            negative=assembled.negative,
            settings=self._generation_settings(),
        )
        return {
            "ok": True,
            "id": record.id,
            "path": str(frame_path),
            "sidecar": str(sidecar_path),
            "seed": result.request.seed,
            "scale": result.request.ip_adapter_scale,
            "reference": ref_rel,
            "positive": assembled.positive,
            "negative": assembled.negative,
        }

    # -- identity bootstrap + auto-filter (3c) ----------------------------------

    def bootstrap_generate(
        self, character_id: object, batch: object = None, more: object = False
    ) -> dict:
        """Generate a seed batch steered by the character's reference, then
        auto-filter it (content-classify -> similarity cull -> quality rank ->
        optional face-swap) into a machine-vetted grid. ``more=True`` appends a
        fresh-seed batch to an existing bootstrap and re-culls the union.

        VRAM (§3): the image model generates the whole batch, is UNLOADED in a
        finally (always freeing the slot), and ONLY THEN are the light CPU cull
        models built — one heavy model at a time."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        ref = self._resolve_record_reference(record)
        if isinstance(ref, dict):
            return ref
        ref_abs, ref_rel = ref
        assembled = self._assemble(record)
        if isinstance(assembled, dict):
            return assembled

        config = self._bootstrap_config()
        batch_n = self._parse_batch(batch, config.batch)
        if isinstance(batch_n, dict):
            return batch_n

        # Preflight the cull models BEFORE burning a batch (fail-closed).
        missing = cull_mod.preflight_cull(self._settings, config.face_swap_enabled)
        if missing is not None:
            return {"ok": False, "kind": missing,
                    "error": self._cull_missing_message(missing)}

        want_more = bool(more)
        if not want_more:
            # A fresh bootstrap replaces any prior candidate set.
            self._store.clear_bootstrap(record.id, scope="bootstrap")

        scale = self._ip_adapter_scale(None)
        assert not isinstance(scale, dict)  # None -> always the coerced default
        batch = self._generate_batch(record, assembled, ref_abs, ref_rel, scale, batch_n)
        if isinstance(batch, dict):
            return batch
        new_frames, checkpoint_name, checkpoint_bytes = batch

        # Merge with prior candidates on a `more` run (append-only on disk).
        prior = None
        if want_more:
            prior = self._load_bootstrap(record.id)
            if isinstance(prior, dict):
                return prior
        prior_frames = self._prior_frames(record, prior) if prior else []
        all_frames = prior_frames + new_frames
        if not all_frames:
            return {"ok": False, "kind": "engine",
                    "error": "no candidates were generated"}

        params = {
            "batch_n": batch_n,
            "checkpoint": checkpoint_name,
            "checkpoint_bytes": checkpoint_bytes,
            "ip_adapter": self._loaded_ip_adapter_params(),
            "thresholds": self._config_dict(config),
            "face_swap_enabled": config.face_swap_enabled,
        }
        return self._cull_to_manifest(record, ref_abs, ref_rel, all_frames, config,
                                      params, event="bootstrap_generated")

    def bootstrap_recull(self, character_id: object, overrides: object = None) -> dict:
        """Re-cull the already-generated candidates with (optionally) adjusted
        thresholds — NO image model, no regeneration (§6 'adjust without
        regenerating'). Requires the reference to still exist (similarity)."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        manifest = self._load_bootstrap(record.id)
        if isinstance(manifest, dict):
            return manifest
        if manifest is None:
            return {"ok": False, "kind": "no_bootstrap",
                    "error": "no bootstrap candidates to re-cull"}
        ref = self._resolve_record_reference(record)
        if isinstance(ref, dict):
            return ref
        ref_abs, ref_rel = ref
        config = self._apply_overrides(self._bootstrap_config(), overrides)
        if isinstance(config, dict):
            return config
        frames = self._prior_frames(record, manifest)
        params = dict(manifest.params)
        params["thresholds"] = self._config_dict(config)
        params["face_swap_enabled"] = config.face_swap_enabled
        return self._cull_to_manifest(record, ref_abs, ref_rel, frames, config,
                                      params, event="bootstrap_reculled")

    def bootstrap_status(self, character_id: object) -> dict:
        """Bootstrap phase / counts / proposed grid / vetted state — no models,
        no GPU, runs anywhere."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        vetted = self._load_vetted_manifest(record.id)
        if isinstance(vetted, dict):
            return vetted
        has_vetted = vetted is not None
        vetted_count = vetted.count if vetted else 0
        manifest = self._load_bootstrap(record.id)
        if isinstance(manifest, dict):
            return manifest
        if manifest is None:
            return {"ok": True, "id": record.id, "phase": None, "counts": {},
                    "proposed": [], "short": True, "has_reference": bool(
                        record.identity.reference_image_path),
                    "has_vetted": has_vetted, "vetted_count": vetted_count}
        proposed = sorted(
            [c for c in manifest.candidates if c.status == STATUS_PROPOSED],
            key=lambda c: (c.rank if c.rank is not None else 1_000_000),
        )
        config = self._bootstrap_config()
        kept = sum(1 for c in manifest.candidates
                   if c.status in (STATUS_KEPT, STATUS_PROPOSED))
        return {
            "ok": True, "id": record.id, "phase": manifest.phase,
            "counts": manifest.counts_by_status(),
            "proposed": [
                {"candidate_id": c.candidate_id, "path": c.final_path(),
                 "similarity": c.similarity, "rank": c.rank}
                for c in proposed
            ],
            "short": kept < config.floor,
            "has_reference": bool(record.identity.reference_image_path),
            "has_vetted": has_vetted, "vetted_count": vetted_count,
        }

    def confirm_vetted(self, character_id: object, candidate_ids: object) -> dict:
        """Promote a user-selected subset of machine-vetted candidates into the
        training set. The selection is validated against the TRUSTED manifest
        (membership + status), the pixels are taken from the manifest (never
        caller input) and re-resolved for containment, and the FINAL pixels are
        re-classified fail-closed — so no forged id, escaped path, or
        content-blocked frame can enter the 3d training set."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        manifest = self._load_bootstrap(record.id)
        if isinstance(manifest, dict):
            return manifest
        if manifest is None:
            return {"ok": False, "kind": "no_bootstrap",
                    "error": "no bootstrap to confirm from"}
        if not isinstance(candidate_ids, (list, tuple)) or not candidate_ids:
            return {"ok": False, "kind": "invalid",
                    "error": "select at least one candidate"}

        selected: list[BootstrapCandidate] = []
        seen: set[str] = set()
        for raw_id in candidate_ids:
            cid = str(raw_id)
            if cid in seen:
                continue
            seen.add(cid)
            cand = manifest.get(cid)
            if cand is None or cand.status not in CONFIRMABLE_STATUSES:
                return {"ok": False, "kind": "invalid_selection",
                        "error": f"{cid!r} is not a vetted candidate"}
            selected.append(cand)

        # Build a classifier-only toolkit (reference optional; fail-closed).
        ref_abs = None
        ref = self._resolve_record_reference(record)
        if not isinstance(ref, dict):
            ref_abs = ref[0]
        try:
            toolkit = self._toolkit_factory(self._settings, ref_abs, False)
        except CullUnavailable as exc:
            return {"ok": False, "kind": exc.kind,
                    "error": self._cull_missing_message(exc.kind)}
        except Exception as exc:
            # A missing cull dependency / corrupt model / undecodable reference
            # must not escape the bridge (§2).
            return self._cull_load_error(exc)

        try:
            resolved: list[tuple[BootstrapCandidate, Path, ContentVerdict]] = []
            for cand in selected:
                fr = self._resolve_reference(record.id, cand.final_path(),
                                             allow_absolute=False)
                if isinstance(fr, dict):
                    return {"ok": False, "kind": "invalid_selection",
                            "error": f"candidate {cand.candidate_id!r} pixels "
                                     f"are unavailable"}
                final_abs, _ = fr
                verdict = self._classify(toolkit, final_abs)
                if verdict.blocked:
                    self._audit.log(
                        "filter_block", layer=2, category=verdict.category,
                        matched=verdict.matched,
                        context="image.confirm_vetted",
                        character_id=record.id,
                    )
                    return {"ok": False, "kind": "blocked",
                            "source": cand.candidate_id,
                            "category": verdict.category,
                            "error": "a selected image was blocked by the "
                                     "content policy on re-check"}
                resolved.append((cand, final_abs, verdict))
        finally:
            toolkit.close()

        # Build the new set fully in a staging dir, THEN swap it over the old
        # one — a mid-copy OSError must not destroy the prior confirmed set
        # (mirrors the repo's temp-then-replace discipline).
        char_dir = self._store.char_dir(record.id)
        staging = char_dir / "vetted.new"
        self._delete_tree_quietly(staging)
        entries: list[VettedEntry] = []
        try:
            staging.mkdir(parents=True, exist_ok=True)
            for i, (cand, final_abs, verdict) in enumerate(resolved, start=1):
                dest_name = f"vetted-{i:02d}.png"
                shutil.copyfile(final_abs, staging / dest_name)
                entries.append(VettedEntry(
                    path=f"vetted/{dest_name}",
                    source_candidate_id=cand.candidate_id,
                    seed=cand.seed,
                    similarity=cand.similarity,
                    aesthetic=float(cand.quality.get("aesthetic", 0.0)),
                    face_swapped=bool(cand.swapped_path),
                    content_verdict=verdict.to_dict(),
                    reference=manifest.reference,
                    checkpoint=manifest.params.get("checkpoint"),
                    checkpoint_bytes=manifest.params.get("checkpoint_bytes"),
                ))
            atomic_write_json(
                staging / "vetted.json",
                VettedManifest(character_id=record.id, entries=entries).to_dict())
            # New set is complete — now (and only now) replace the old one.
            vetted_dir = self._store.vetted_dir(record.id)
            self._delete_tree_quietly(vetted_dir)
            os.replace(staging, vetted_dir)
        except OSError as exc:
            self._delete_tree_quietly(staging)
            return {"ok": False, "kind": "io",
                    "error": f"could not write the vetted set: {exc}"}
        for cand in selected:
            cand.status = STATUS_CONFIRMED
        manifest.phase = PHASE_CONFIRMED
        manifest.touch()
        self._store.save_bootstrap(manifest)
        self._audit.log("vetted_confirmed", character_id=record.id,
                        count=len(entries))
        below_floor = len(entries) < self._bootstrap_config().floor
        return {"ok": True, "id": record.id, "count": len(entries),
                "vetted": [e.path for e in entries], "below_floor": below_floor}

    def clear_bootstrap(self, character_id: object, scope: object = "all") -> dict:
        """Delete the bootstrap and/or vetted artifacts (the only destructive
        bootstrap op)."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        scope_str = str(scope) if scope in ("all", "bootstrap", "vetted") else "all"
        try:
            removed = self._store.clear_bootstrap(record.id, scope=scope_str)
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not clear the bootstrap: {exc}"}
        self._audit.log("bootstrap_cleared", character_id=record.id, scope=scope_str)
        return {"ok": True, "id": record.id, "scope": scope_str, "removed": removed}

    # -- bootstrap internals ----------------------------------------------------

    def _load_bootstrap(self, character_id: str):
        """load_bootstrap, but a corrupt/hand-edited manifest is a structured
        `bootstrap_corrupt` (never a raise through the bridge). Returns the
        manifest, None (absent), or an error dict."""
        try:
            return self._store.load_bootstrap(character_id)
        except ARTIFACT_LOAD_ERRORS as exc:
            # LookupError (KeyError) covers a valid-JSON manifest that is
            # missing a required key — a natural hand-edit that from_dict
            # subscripts blindly.
            return {"ok": False, "kind": "bootstrap_corrupt",
                    "error": f"the bootstrap manifest is unreadable: {exc}"}

    def _load_vetted_manifest(self, character_id: str):
        try:
            return self._store.load_vetted(character_id)
        except ARTIFACT_LOAD_ERRORS as exc:
            return {"ok": False, "kind": "bootstrap_corrupt",
                    "error": f"the vetted manifest is unreadable: {exc}"}

    def _resolve_record_reference(self, record: CharacterRecord):
        """The character's stored reference resolved use-time-strict, or a
        structured error (no_reference / reference_invalid / reference_missing)."""
        raw = record.identity.reference_image_path
        if not raw:
            return {"ok": False, "kind": "no_reference",
                    "error": "this character has no identity reference set — "
                             "set_reference first (Stage 3b)"}
        return self._resolve_reference(record.id, raw, allow_absolute=False)

    def _bootstrap_config(self) -> CullConfig:
        return cull_mod.coerce_cull_config(self._settings)

    @staticmethod
    def _config_dict(config: CullConfig) -> dict:
        return {
            "batch": config.batch, "keep_cap": config.keep_cap,
            "floor": config.floor, "grid_size": config.grid_size,
            "similarity_floor": config.similarity_floor,
            "det_score_floor": config.det_score_floor,
            "sharpness_floor": config.sharpness_floor,
            "face_area_min": config.face_area_min,
            "face_area_max": config.face_area_max,
            "face_swap_enabled": config.face_swap_enabled,
        }

    @staticmethod
    def _parse_batch(batch: object, default: int) -> int | dict:
        if batch is None:
            return default
        if isinstance(batch, bool) or not isinstance(batch, (int, float)):
            return {"ok": False, "kind": "invalid", "error": "batch must be a number"}
        if isinstance(batch, float):
            if not batch.is_integer():
                return {"ok": False, "kind": "invalid",
                        "error": "batch must be a whole number"}
            batch = int(batch)
        if not (1 <= batch <= 256):
            return {"ok": False, "kind": "invalid",
                    "error": "batch must be in [1, 256]"}
        return batch

    def _apply_overrides(self, config: CullConfig, overrides: object):
        if overrides is None:
            return config
        if not isinstance(overrides, dict):
            return {"ok": False, "kind": "invalid",
                    "error": "overrides must be an object"}
        current = self._config_dict(config)
        for key, value in overrides.items():
            if key not in current:
                return {"ok": False, "kind": "invalid",
                        "error": f"unknown threshold {key!r}"}
            if key == "face_swap_enabled":
                current[key] = bool(value)
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return {"ok": False, "kind": "invalid",
                        "error": f"{key} must be a number"}
            if not math.isfinite(float(value)):
                return {"ok": False, "kind": "invalid",
                        "error": f"{key} must be finite"}
            current[key] = value
        int_keys = ("batch", "keep_cap", "floor", "grid_size")
        return CullConfig(
            **{k: (int(current[k]) if k in int_keys else current[k])
               for k in current}
        )

    @staticmethod
    def _cull_missing_message(kind: str) -> str:
        return {
            "face_models_missing": "no face-recognition models — set "
            "models.image.face_recognition_dir to a dir containing "
            "models/buffalo_l/ (see docs/IMAGE_PIPELINE.md)",
            "classifier_unavailable": "the Layer-2 content classifier is "
            "unavailable — set models.image.content_classifier_dir",
            "swap_model_missing": "face-swap is enabled but "
            "models.image.face_swapper_path is missing",
        }.get(kind, f"cull unavailable: {kind}")

    def _loaded_ip_adapter_params(self) -> dict | None:
        ip = self._engine.loaded_ip_config
        if ip is None:
            return None
        return {"variant": ip.variant, "weight_name": ip.weight_name}

    def _generate_batch(self, record, assembled, ref_abs, ref_rel, scale, batch_n):
        """Generate ``batch_n`` steered frames (varying only the seed), persist
        each under bootstrap/candidates/, and ALWAYS unload the image model
        (finally). Returns (frames, checkpoint_name, checkpoint_bytes) or a
        structured error dict."""
        frames: list[_BatchFrame] = []
        checkpoint_name: str | None = None
        checkpoint_bytes: int | None = None
        error: dict | None = None
        try:
            for _ in range(batch_n):
                request = GenerationRequest(
                    positive=assembled.positive,
                    negative=assembled.negative,
                    seed=None,
                    ip_adapter_scale=scale,
                    **self._generation_settings(),
                )
                try:
                    result = self._engine.generate_identity(request, ref_abs)
                except (EngineBusy, EngineUnavailable, GenerationFailed) as exc:
                    error = {"ok": False, "kind": "engine", "error": str(exc)}
                    break
                except ReferenceUnreadable as exc:
                    error = {"ok": False, "kind": "reference_unreadable",
                             "error": str(exc)}
                    break
                except ValueError as exc:
                    error = {"ok": False, "kind": "config", "error": str(exc)}
                    break
                if checkpoint_name is None:
                    ckpt = self._engine.loaded_checkpoint
                    checkpoint_name = ckpt.name if ckpt else None
                    try:
                        checkpoint_bytes = ckpt.stat().st_size if ckpt else None
                    except OSError:
                        checkpoint_bytes = None
                try:
                    frame_path, _ = self._persist_frame(
                        record, assembled, result,
                        subdir="bootstrap/candidates", prefix="cand",
                        kind="bootstrap-candidate", stage="3c-bootstrap",
                        reference=ref_rel, ip_adapter=self._engine.loaded_ip_config,
                    )
                except OSError as exc:
                    error = {"ok": False, "kind": "io",
                             "error": f"could not save a candidate: {exc}"}
                    break
                frames.append(_BatchFrame(
                    candidate_id=frame_path.stem,
                    abs_path=frame_path,
                    rel_path=f"bootstrap/candidates/{frame_path.name}",
                    seed=result.request.seed,
                ))
        finally:
            self._engine.unload()  # ALWAYS free the ~10-12GB slot (§3)
        if error is not None and not frames:
            return error
        return frames, checkpoint_name, checkpoint_bytes

    def _prior_frames(self, record, manifest) -> list[_BatchFrame]:
        """Re-resolve the candidate pixels an existing manifest references,
        dropping any whose file no longer exists (append-only, but a content
        reject was deleted)."""
        frames: list[_BatchFrame] = []
        for cand in manifest.candidates:
            resolved = self._resolve_reference(record.id, cand.path,
                                               allow_absolute=False)
            if isinstance(resolved, dict):
                continue
            frames.append(_BatchFrame(
                candidate_id=cand.candidate_id, abs_path=resolved[0],
                rel_path=cand.path, seed=cand.seed))
        return frames

    def _cull_to_manifest(self, record, ref_abs, ref_rel, frames, config, params, *,
                          event):
        """Build the toolkit (after the image model is unloaded), score + rank
        every frame, run the optional post-cull face-swap, and persist the
        BootstrapManifest. Fail-closed on the classifier."""
        try:
            toolkit = self._toolkit_factory(self._settings, ref_abs, config.face_swap_enabled)
        except CullUnavailable as exc:
            return {"ok": False, "kind": exc.kind,
                    "error": self._cull_missing_message(exc.kind)}
        except Exception as exc:
            return self._cull_load_error(exc)
        if not toolkit.ref_reading.found:
            toolkit.close()
            return {"ok": False, "kind": "no_faces",
                    "error": "the reference image has no detectable face"}

        try:
            by_id = {f.candidate_id: f for f in frames}
            scores = []
            for frame in frames:
                score = cull_mod.score_candidate(
                    toolkit, toolkit.ref_reading, frame.candidate_id,
                    frame.abs_path, config)
                if score.status == STATUS_REJECTED_CONTENT:
                    self._audit.log(
                        "filter_block", layer=2, category=score.content_category,
                        matched=score.content_matched,
                        context="image.bootstrap.candidate",
                        character_id=record.id, candidate_id=frame.candidate_id)
                if score.status in (STATUS_REJECTED_CONTENT, STATUS_REJECTED_ERROR):
                    # Do not keep policy-violating or unusable pixels on disk.
                    self._delete_quietly(frame.abs_path)
                scores.append(score)
            survivors, short = cull_mod.cull_and_rank(scores, config)
            swapped_paths = self._apply_face_swap(
                record, toolkit, config, ref_abs, survivors, by_id)
        finally:
            toolkit.close()

        manifest = BootstrapManifest(
            character_id=record.id, phase=PHASE_PROPOSED, reference=ref_rel,
            params=params,
            candidates=[
                BootstrapCandidate(
                    candidate_id=s.candidate_id,
                    path=by_id[s.candidate_id].rel_path,
                    seed=by_id[s.candidate_id].seed,
                    status=s.status,
                    swapped_path=swapped_paths.get(s.candidate_id),
                    similarity=s.similarity,
                    quality=s.quality_dict(),
                    content=s.content_dict(),
                    rank=s.rank,
                )
                for s in scores
            ],
        )
        if not any(c.status == STATUS_PROPOSED for c in manifest.candidates):
            manifest.phase = PHASE_CULLED
        try:
            self._store.save_bootstrap(manifest)
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not save the bootstrap manifest: {exc}"}
        counts = manifest.counts_by_status()
        self._audit.log(event, character_id=record.id,
                        generated=len(frames), counts=counts, short=short)
        return {
            "ok": True, "id": record.id, "phase": manifest.phase,
            "generated": len(frames), "counts": counts, "short": short,
            "proposed": [
                {"candidate_id": c.candidate_id, "path": c.final_path(),
                 "similarity": c.similarity, "rank": c.rank}
                for c in sorted(
                    (c for c in manifest.candidates if c.status == STATUS_PROPOSED),
                    key=lambda c: (c.rank if c.rank is not None else 1_000_000))
            ],
            "has_vetted": self._store.load_vetted(record.id) is not None,
        }

    def _apply_face_swap(self, record, toolkit, config, ref_abs, survivors, by_id):
        """OPTIONAL identity-lock pass, post-cull, on survivors only. Swapped
        pixels are re-classified + re-similarity-checked; a swap that fails
        falls back to the original (swapped_path stays unset)."""
        swapped: dict[str, str] = {}
        if not (config.face_swap_enabled and toolkit.swapper is not None):
            return swapped
        swap_dir = self._store.swapped_dir(record.id)
        swap_dir.mkdir(parents=True, exist_ok=True)
        for score in survivors:
            frame = by_id[score.candidate_id]
            out_name = f"{frame.candidate_id}-swap.png"
            # Defense in depth: candidate_id is already ensure_safe_id'd at
            # manifest load, but never let the swap output escape swap_dir.
            if os.path.basename(out_name) != out_name:
                continue
            out_abs = swap_dir / out_name
            try:
                ok = toolkit.swapper.swap(frame.abs_path, ref_abs, out_abs)
            except Exception:
                ok = False
            if not ok or not out_abs.is_file():
                self._delete_quietly(out_abs)
                continue
            # Re-check the NEW pixels: content (fail-closed) + similarity floor.
            verdict = self._classify(toolkit, out_abs)
            if verdict.blocked:
                self._audit.log(
                    "filter_block", layer=2, category=verdict.category,
                    matched=verdict.matched, context="image.bootstrap.swapped",
                    character_id=record.id, candidate_id=frame.candidate_id)
                self._delete_quietly(out_abs)
                continue
            try:
                reading = toolkit.embedder.embed(out_abs)
                sim = cull_mod._cosine(toolkit.ref_reading.embedding,
                                       reading.embedding)
            except Exception:
                sim = 0.0
            if sim < config.similarity_floor:
                self._delete_quietly(out_abs)
                continue
            swapped[frame.candidate_id] = f"bootstrap/swapped/{out_name}"
        if swapped:
            self._audit.log("bootstrap_faceswapped", character_id=record.id,
                            count=len(swapped))
        return swapped

    def _classify(self, toolkit, path) -> ContentVerdict:
        """Run the Layer-2 classifier fail-closed: any exception is a block."""
        try:
            return toolkit.classifier.classify(path)
        except Exception:
            return ContentVerdict(blocked=True, category="classifier_error",
                                  matched="classify_exception")

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

    @staticmethod
    def _cull_load_error(exc: Exception) -> dict:
        """A non-CullUnavailable failure building the cull toolkit (missing
        dependency import, corrupt model, undecodable reference) — structured,
        never a bridge traceback (§2)."""
        return {"ok": False, "kind": "cull_unavailable",
                "error": "the cull models could not be loaded — finish "
                         "`pip install -r requirements-full.txt` on the target "
                         f"machine and place the model files ({exc})"}

    # -- identity LoRA promotion (3d) -------------------------------------------

    def train_lora(self, character_id: object) -> dict:
        """Train a per-character identity LoRA on the confirmed vetted set (3c),
        store it, and flip the record's identity anchor (``has_lora`` +
        ``lora_path`` — the first record mutation the image pipeline makes since
        the 3b reference). Heaviest op: the image model is unloaded first so the
        trainer subprocess gets the whole GPU (§3), and the prior LoRA (if any)
        survives a failed re-train."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        vetted = self._load_vetted_manifest(record.id)
        if isinstance(vetted, dict):
            return vetted
        if vetted is None or vetted.count == 0:
            return {"ok": False, "kind": "no_vetted",
                    "error": "no confirmed vetted set to train on — run the "
                             "bootstrap and confirm a grid first (Stage 3c)"}
        checkpoint = self._engine.checkpoint_path()
        if checkpoint is None or not checkpoint.is_file():
            return {"ok": False, "kind": "engine",
                    "error": "no image checkpoint configured to train against"}
        missing = lora_mod.preflight_train(self._settings)
        if missing is not None:
            return {"ok": False, "kind": missing,
                    "error": self._trainer_missing_message(missing)}

        assembled = self._assemble(record)
        if isinstance(assembled, dict):
            return assembled
        trigger = self._lora_trigger(record)
        caption = self._lora_caption(trigger, assembled)

        # Resolve every vetted image under the char dir (containment) — a
        # hand-edited manifest path cannot pull a foreign file into training,
        # and a vetted entry must actually live under vetted/ (so a tampered
        # manifest can't feed e.g. character.json in as a training frame).
        items: list[TrainItem] = []
        vetted_dir = self._store.vetted_dir(record.id).resolve()
        for entry in vetted.entries:
            resolved = self._resolve_reference(record.id, entry.path,
                                               allow_absolute=False)
            if isinstance(resolved, dict):
                continue  # a missing/escaped vetted image is skipped
            abs_path = resolved[0]
            if vetted_dir != abs_path.parent and vetted_dir not in abs_path.parents:
                continue  # a vetted entry must be under vetted/
            items.append(TrainItem(image_path=abs_path, caption=caption))
        if not items:
            return {"ok": False, "kind": "no_vetted",
                    "error": "the vetted images are missing on disk"}

        config = lora_mod.coerce_train_config(self._settings)
        dataset_dir = self._store.lora_dataset_dir(record.id)
        output_dir = self._store.lora_dir(record.id) / "output"
        self._delete_tree_quietly(dataset_dir)
        self._delete_tree_quietly(output_dir)
        try:
            lora_mod.build_dataset(dataset_dir, items, config)
        except OSError as exc:
            self._delete_tree_quietly(dataset_dir)
            return {"ok": False, "kind": "io",
                    "error": f"could not prepare the training dataset: {exc}"}

        # VRAM (§3): free the image slot for the trainer subprocess, then mark
        # the slot busy for the duration; ALWAYS reset in the finally.
        self._engine.unload()
        produced: Path | None = None
        error: dict | None = None
        try:
            self._settings.set("models.active", "image")
        except OSError:
            pass
        try:
            trainer = self._trainer_factory(self._settings)
            request = TrainRequest(
                dataset_dir=dataset_dir, output_dir=output_dir,
                output_name="identity", base_checkpoint=checkpoint,
                trigger=trigger, config=config)
            produced = trainer.train(request)
        except TrainUnavailable as exc:
            error = {"ok": False, "kind": exc.kind,
                     "error": self._trainer_missing_message(exc.kind)}
        except TrainFailed as exc:
            error = {"ok": False, "kind": "train_failed", "error": str(exc)}
        except Exception as exc:
            error = {"ok": False, "kind": "train_failed",
                     "error": f"training failed: {exc}"}
        finally:
            try:
                if self._settings.get("models.active") == "image":
                    self._settings.set("models.active", None)
            except OSError:
                pass
            self._delete_tree_quietly(dataset_dir)

        if error is not None:
            self._delete_tree_quietly(output_dir)
            return error

        # Promote: move the LoRA to its final home, flip the record, write the
        # provenance manifest. The prior LoRA is only overwritten now (on success).
        lora_dir = self._store.lora_dir(record.id)
        final = lora_dir / "identity.safetensors"
        try:
            lora_dir.mkdir(parents=True, exist_ok=True)
            os.replace(produced, final)
        except OSError as exc:
            self._delete_tree_quietly(output_dir)
            return {"ok": False, "kind": "io",
                    "error": f"could not store the trained LoRA: {exc}"}
        self._delete_tree_quietly(output_dir)

        try:
            lora_bytes = final.stat().st_size
        except OSError:
            lora_bytes = 0
        lora_rel = "lora/identity.safetensors"
        try:
            ckpt_bytes = checkpoint.stat().st_size
        except OSError:
            ckpt_bytes = None
        # Write the provenance manifest FIRST (guarded) so a promotion always
        # has provenance and the footprint below counts lora.json too; a disk
        # failure here fails the promotion cleanly (the record isn't flipped —
        # the orphan .safetensors reads has_lora=False and a retrain overwrites).
        try:
            self._store.save_lora_manifest(LoraManifest(
                character_id=record.id, trigger=trigger, lora_file=lora_rel,
                base_checkpoint=checkpoint.name, base_checkpoint_bytes=ckpt_bytes,
                network_dim=config.network_dim, network_alpha=config.network_alpha,
                steps=config.max_train_steps, resolution=config.resolution,
                learning_rate=config.learning_rate, dataset_size=len(items),
                lora_bytes=lora_bytes))
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not save the LoRA provenance: {exc}"}
        record.identity.has_lora = True
        record.identity.lora_path = lora_rel
        record.identity.footprint = self._store.measure_footprint(record.id)
        record.touch()
        try:
            self._store.save(record)
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not save the promoted record: {exc}"}
        self._audit.log("lora_trained", character_id=record.id, trigger=trigger,
                        dataset_size=len(items), steps=config.max_train_steps,
                        lora_bytes=lora_bytes)
        return {"ok": True, "id": record.id, "lora_path": lora_rel,
                "trigger": trigger, "steps": config.max_train_steps,
                "network_dim": config.network_dim, "dataset_size": len(items),
                "lora_bytes": lora_bytes}

    def lora_status(self, character_id: object) -> dict:
        """Whether the character has a trained LoRA + its provenance. No GPU."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        anchor = record.identity
        manifest = self._load_lora_manifest(record.id)
        if isinstance(manifest, dict):
            return manifest
        # A valid promotion needs both the flag AND the file present.
        has_lora = bool(anchor.has_lora and anchor.lora_path)
        lora_ok = False
        if has_lora:
            resolved = self._resolve_reference(record.id, anchor.lora_path,
                                               allow_absolute=False)
            lora_ok = not isinstance(resolved, dict)
        return {
            "ok": True, "id": record.id,
            "has_lora": has_lora and lora_ok,
            "lora_path": anchor.lora_path if lora_ok else None,
            "trigger": manifest.trigger if manifest else None,
            "provenance": manifest.to_dict() if manifest else None,
            "footprint": anchor.footprint.to_dict(),
        }

    def clear_lora(self, character_id: object) -> dict:
        """Delete the LoRA + its provenance and un-promote the record."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        try:
            removed = self._store.clear_lora(record.id)
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not clear the LoRA: {exc}"}
        record.identity.has_lora = False
        record.identity.lora_path = None
        record.identity.footprint = self._store.measure_footprint(record.id)
        record.touch()
        try:
            self._store.save(record)
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not save the record: {exc}"}
        self._audit.log("lora_cleared", character_id=record.id)
        return {"ok": True, "id": record.id, "removed": removed}

    # -- LoRA internals ---------------------------------------------------------

    def _load_lora_manifest(self, character_id: str):
        try:
            return self._store.load_lora_manifest(character_id)
        except ARTIFACT_LOAD_ERRORS as exc:
            return {"ok": False, "kind": "lora_corrupt",
                    "error": f"the LoRA manifest is unreadable: {exc}"}

    @staticmethod
    def _lora_trigger(record: CharacterRecord) -> str:
        """A stable single-token identity trigger. Derived from a HASH of the
        id so it is provably ``[a-z0-9]`` even for a hand-edited id (the id is
        path-safe but not content-gated), and won't collide on a short prefix."""
        import hashlib

        digest = hashlib.sha1(str(record.id).encode("utf-8")).hexdigest()
        return "cfid" + digest[:12]

    @staticmethod
    def _lora_caption(trigger: str, assembled: AssembledPrompt) -> str:
        """The training caption: the trigger + the record's gated identity
        description (dropping the booru composition anchors — quality/subject —
        which are generation-time, not identity)."""
        fragments = [trigger]
        for piece in assembled.pieces:
            if piece.source in ("quality", "subject"):
                continue
            fragments.append(piece.text)
        return ", ".join(fragments)

    @staticmethod
    def _trainer_missing_message(kind: str) -> str:
        return {
            "trainer_unavailable": "no LoRA trainer configured — set "
            "models.image.lora_trainer_dir to a kohya-ss sd-scripts checkout "
            "(see docs/IMAGE_PIPELINE.md)",
        }.get(kind, f"trainer unavailable: {kind}")

    # -- seed catalog generation (3e) -------------------------------------------

    def generate_catalog(self, character_id: object) -> dict:
        """Render the core-matrix seed catalog (expressions × poses × wardrobe)
        LoRA-steered, auto-filtered by the same 3c cull ("same filter as
        training", §7), into `catalog/` + a `CatalogManifest`. Requires a
        trained LoRA (3d), the reference (3c, for the similarity cull), and the
        cull models. Rejected cells are regenerated up to `max_attempts`.

        VRAM (§3): each pass generates with the LoRA image model, unloads it,
        then culls on the CPU toolkit — one heavy model at a time. The prior
        catalog is replaced only on success (a staged `catalog.new/` swap)."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        anchor = record.identity
        if not (anchor.has_lora and anchor.lora_path):
            return {"ok": False, "kind": "no_lora",
                    "error": "this character has no trained LoRA — train one "
                             "first (Stage 3d)"}
        lora_resolved = self._resolve_reference(record.id, anchor.lora_path,
                                                allow_absolute=False)
        if isinstance(lora_resolved, dict):
            return {"ok": False, "kind": "lora_missing",
                    "error": "the trained LoRA file is missing on disk"}
        lora_abs = lora_resolved[0]
        checkpoint = self._engine.checkpoint_path()
        if checkpoint is None or not checkpoint.is_file():
            return {"ok": False, "kind": "engine",
                    "error": "no image checkpoint configured for catalog generation"}
        ref = self._resolve_record_reference(record)
        if isinstance(ref, dict):
            return ref  # no_reference / reference_invalid / reference_missing
        ref_abs, ref_rel = ref
        missing = cull_mod.preflight_cull(self._settings, False)
        if missing is not None:
            return {"ok": False, "kind": missing,
                    "error": self._cull_missing_message(missing)}
        base = self._assemble(record)
        if isinstance(base, dict):
            return base  # a blocked record

        config = catalog_mod.coerce_catalog_config(self._settings)
        expressions, poses = catalog_mod.load_catalog_states()
        cells = catalog_mod.build_cells(record, self._catalog(), expressions,
                                        poses, config)
        if not cells:
            return {"ok": False, "kind": "no_states",
                    "error": "no catalog states to render"}

        trigger = self._lora_trigger(record)
        pending = self._catalog_cell_prompts(record, cells, trigger)
        if not pending:
            return {"ok": False, "kind": "blocked",
                    "error": "every catalog cell was blocked by the content policy"}

        # Reuse the 3c cull ("same filter as training", §7) but relax ONLY the
        # face-area floor for the catalog's deliberately pose-varied (small-
        # face) frames; the Layer-2 content gate + similarity stay unchanged.
        cull_config = replace(cull_mod.coerce_cull_config(self._settings),
                              face_area_min=config.face_area_min)
        gen_settings = self._generation_settings()
        staging = self._store.char_dir(record.id) / "catalog.new"
        self._delete_tree_quietly(staging)

        kept: list[CatalogEntry] = []
        attempt = 0
        while pending and attempt < config.max_attempts:
            attempt += 1
            generated = self._catalog_generate_pass(
                record, lora_abs, pending, config, ref_rel, gen_settings)
            if isinstance(generated, dict):
                self._delete_tree_quietly(staging)
                return generated  # engine/config/io — bail, prior catalog intact
            culled = self._catalog_cull_pass(record, ref_abs, generated, cull_config)
            if isinstance(culled, dict):
                self._delete_tree_quietly(staging)
                return culled  # cull_unavailable / no_faces
            passed, failed_cells = culled
            kept.extend(passed)
            pending = [(c, a) for (c, a) in pending if c in failed_cells]

        if not kept:
            # No frame survived the auto-filter — the LoRA likely needs
            # retuning. Keep the prior catalog rather than wiping it.
            self._delete_tree_quietly(staging)
            return {"ok": False, "kind": "catalog_empty",
                    "error": "no catalog frame passed the auto-filter — the "
                             "trained LoRA may need retuning (Stage 3d)"}
        # Swap the staged frames over the old catalog (only now, on success).
        try:
            self._finalize_catalog(record, staging, kept)
        except OSError as exc:
            self._delete_tree_quietly(staging)
            return {"ok": False, "kind": "io",
                    "error": f"could not store the catalog: {exc}"}

        self._audit.log("catalog_generated", character_id=record.id,
                        frames=len(kept), requested=len(cells),
                        incomplete=len(pending))
        return {
            "ok": True, "id": record.id, "frames": len(kept),
            "requested": len(cells), "incomplete": len(pending),
            "entries": [{"frame_id": e.frame_id, "path": e.path, "state": e.state}
                        for e in kept],
        }

    def catalog_status(self, character_id: object) -> dict:
        """Catalog frame count / states / staleness — no GPU."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        try:
            manifest = self._store.load_catalog(record.id)
        except ARTIFACT_LOAD_ERRORS as exc:
            return {"ok": False, "kind": "catalog_corrupt",
                    "error": f"the catalog manifest is unreadable: {exc}"}
        if manifest is None:
            return {"ok": True, "id": record.id, "has_catalog": False,
                    "frames": 0, "stale": False, "states": []}
        return {
            "ok": True, "id": record.id, "has_catalog": bool(manifest.entries),
            "frames": len(manifest.entries), "stale": manifest.stale,
            "states": [e.state for e in manifest.entries],
        }

    def clear_catalog(self, character_id: object) -> dict:
        """Delete the seed catalog (frames + manifest)."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        try:
            removed = self._store.clear_catalog(record.id)
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not clear the catalog: {exc}"}
        self._audit.log("catalog_cleared", character_id=record.id)
        return {"ok": True, "id": record.id, "removed": removed}

    # -- catalog internals ------------------------------------------------------

    def _catalog_cell_prompts(self, record, cells, trigger, *,
                              context_prefix="image.catalog"):
        """Pre-assemble + gate every cell's prompt (identity minus wardrobe +
        the trigger + the cell's outfit/expression/pose). A cell whose state
        fragments trip the gate is skipped + audited, not fatal."""
        catalog = self._catalog()
        exclude = frozenset({catalog_mod.OUTFIT_GROUP})
        lead = (("trigger", trigger),)
        pending = []
        for cell in cells:
            try:
                assembled = self._assembler.assemble(
                    record, catalog, exclude_groups=exclude, lead=lead,
                    extra=cell.extra())
            except PromptBlocked as exc:
                self._audit.log("filter_block", layer=1, category=exc.category,
                                matched=exc.matched,
                                context=f"{context_prefix}.{exc.source}",
                                character_id=record.id)
                continue
            pending.append((cell, assembled))
        return pending

    def _catalog_generate_pass(self, record, lora_abs, pending, config, ref_rel,
                               gen_settings, *, subdir="catalog.new",
                               rel_prefix="catalog", stage="3e-catalog",
                               kind="catalog"):
        """Generate one frame per pending cell (LoRA image model), ALWAYS
        unloading in the finally. Returns a list of (cell, frame_path, rel,
        seed, assembled) or a structured error dict. The 3g on-demand path
        reuses this with cache-staging parameters; ``rel_prefix`` is the
        FINAL char-relative dir the frame will live in (3e swaps its staging
        dir whole; 3g moves the single kept frame)."""
        generated = []
        error = None
        try:
            for cell, assembled in pending:
                request = GenerationRequest(
                    positive=assembled.positive, negative=assembled.negative,
                    seed=None, lora_scale=config.lora_scale, **gen_settings)
                try:
                    result = self._engine.generate_catalog(request, lora_abs)
                except (EngineBusy, EngineUnavailable, GenerationFailed) as exc:
                    error = {"ok": False, "kind": "engine", "error": str(exc)}
                    break
                except ValueError as exc:
                    error = {"ok": False, "kind": "config", "error": str(exc)}
                    break
                try:
                    frame_path, _ = self._persist_frame(
                        record, assembled, result, subdir=subdir,
                        prefix="frame", kind=kind, stage=stage,
                        reference=ref_rel)
                except OSError as exc:
                    error = {"ok": False, "kind": "io",
                             "error": f"could not save a catalog frame: {exc}"}
                    break
                generated.append((cell, frame_path,
                                  f"{rel_prefix}/{frame_path.name}",
                                  result.request.seed, assembled))
        finally:
            self._engine.unload()
        # An engine/io error mid-pass is treated as fatal (a persistent OOM /
        # bad LoRA won't be fixed by retrying); bail and keep the prior catalog.
        if error is not None:
            return error
        return generated

    def _catalog_cull_pass(self, record, ref_abs, generated, cull_config, *,
                           on_demand=False, context="image.catalog.frame"):
        """Auto-filter the generated frames with the 3c cull. Returns
        (passed CatalogEntries, set of failed cells) or a structured error.
        The 3g on-demand path reuses this with on_demand=True + its own
        audit context."""
        try:
            toolkit = self._toolkit_factory(self._settings, ref_abs, False)
        except CullUnavailable as exc:
            return {"ok": False, "kind": exc.kind,
                    "error": self._cull_missing_message(exc.kind)}
        except Exception as exc:
            return self._cull_load_error(exc)
        if not toolkit.ref_reading.found:
            toolkit.close()
            return {"ok": False, "kind": "no_faces",
                    "error": "the reference image has no detectable face"}
        passed: list[CatalogEntry] = []
        failed_cells = set()
        try:
            for cell, frame_path, rel, seed, _assembled in generated:
                score = cull_mod.score_candidate(
                    toolkit, toolkit.ref_reading, frame_path.stem, frame_path,
                    cull_config)
                if score.status == STATUS_REJECTED_CONTENT:
                    self._audit.log(
                        "filter_block", layer=2, category=score.content_category,
                        matched=score.content_matched,
                        context=context, character_id=record.id)
                if score.rejected:
                    # A rejected catalog frame is not shown — drop it and retry
                    # the cell (§7: regenerate rather than show malformed).
                    self._delete_quietly(frame_path)
                    self._delete_quietly(frame_path.with_suffix(".json"))
                    failed_cells.add(cell)
                    continue
                try:
                    frame_bytes = frame_path.stat().st_size
                except OSError:
                    frame_bytes = 0
                passed.append(CatalogEntry(
                    frame_id=frame_path.stem, path=rel, state=cell.state(),
                    on_demand=on_demand, bytes=frame_bytes))
        finally:
            toolkit.close()
        return passed, failed_cells

    def _finalize_catalog(self, record, staging, kept):
        """Swap the staged frames over the live catalog and write the manifest
        (kept is non-empty). Rollback-safe: the prior catalog is renamed aside
        and RESTORED on any failure, so the manifest never ends up disagreeing
        with the frames on disk. Raises OSError on failure (caller cleans
        staging); the prior catalog is preserved on every failure path."""
        frames_dir = self._store.catalog_frames_dir(record.id)
        backup = frames_dir.with_name("catalog.old")
        self._delete_tree_quietly(backup)
        had_prior = frames_dir.exists()
        if had_prior:
            os.replace(frames_dir, backup)  # move the prior frames aside (atomic)
        try:
            os.replace(staging, frames_dir)  # new frames into place
            # Manifest last: on failure the prior catalog.json is untouched
            # (atomic_write_json = temp+replace), and we roll the frames back.
            self._store.save_catalog(CatalogManifest(
                character_id=record.id, entries=kept, stale=False))
        except OSError:
            self._delete_tree_quietly(frames_dir)  # drop the half-applied new set
            if had_prior:
                try:
                    os.replace(backup, frames_dir)  # restore the prior frames
                except OSError:
                    # Double fault (can't restore either) — drop the now-dangling
                    # manifest so catalog_status reports NO catalog (consistent),
                    # not phantom frames. The prior frames remain in catalog.old
                    # for recovery until either the next successful generate or
                    # the Stage-4 startup reconciliation sweep reclaims it
                    # (catalog.old is a documented staging-dir orphan there).
                    self._delete_quietly(self._store.catalog_path(record.id))
            raise
        self._delete_tree_quietly(backup)

    # -- matting / keyable output (3f) -------------------------------------------

    def matte_catalog(self, character_id: object, force: object = False) -> dict:
        """Background-remove every seed-catalog frame into a keyable RGBA
        cutout under ``catalog/matted/``, filling ``CatalogEntry.matted_path``
        (§7, §13 — Stage 5 composites these). Every source frame is
        re-screened by the Layer-2 classifier (fail-closed) BEFORE the skip
        check: ``catalog.json`` and the frames are hand-editable, so the
        pixels at ``entry.path`` are untrusted on every run, and classifier
        drift must catch previously-passed frames. A blocked frame is purged
        (pixels + sidecar + prior matte + manifest entry) and audited.

        ``force`` re-mattes frames that already have a valid matte. Per-frame
        failures never abort the run. No GPU: the matting model runs on the
        CPU ONNX providers by default and the image engine is never touched
        (the 3c confirm_vetted posture)."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        manifest = self._load_catalog_manifest(record.id)
        if isinstance(manifest, dict):
            return manifest
        if manifest is None or not manifest.entries:
            return {"ok": False, "kind": "no_catalog",
                    "error": "no seed catalog to matte — generate one first "
                             "(Stage 3e)"}
        token = manifest.updated_at  # optimistic-concurrency token

        missing = matte_mod.preflight_matte(self._settings)
        if missing is not None:
            return {"ok": False, "kind": missing,
                    "error": self._matte_missing_message(missing)}
        config = matte_mod.coerce_matte_config(self._settings)
        try:
            toolkit = self._matte_factory(self._settings, config)
        except MatteUnavailable as exc:
            return {"ok": False, "kind": exc.kind,
                    "error": self._matte_missing_message(exc.kind)}
        except Exception as exc:
            # A missing dependency import / corrupt model must not escape
            # the bridge (§2) — mirrors _cull_load_error.
            return self._matte_load_error(exc)

        catalog_dir = self._store.catalog_frames_dir(record.id).resolve()
        matted_dir = self._store.matted_dir(record.id)
        try:
            matted_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            toolkit.close()
            return {"ok": False, "kind": "io",
                    "error": f"could not create the matte directory: {exc}"}
        matted_dir = matted_dir.resolve()
        # Crashed-run leftovers. The temp namespace (*.png.tmp) cannot overlap
        # any promoted final (always <stem>.png) — with a *.tmp.png suffix, a
        # hand-placed source named x.tmp.png promoted to a final this sweep
        # would then destroy on the next run.
        for stale in matted_dir.glob("*.png.tmp"):
            self._delete_quietly(stale)

        results: list[dict] = []
        matted = skipped = removed = 0
        redo = bool(force)
        try:
            for entry in list(manifest.entries):  # list(): we may remove
                row: dict = {"frame_id": entry.frame_id}
                results.append(row)
                # (a) The stored source path is untrusted (hand-editable
                # manifest): containment-resolve, then require the frame to
                # be a DIRECT child of catalog/ (never reference/, matted/,
                # or a subdir — the train_lora vetted/ discipline, tightened).
                # Paths are never built from frame_id (report-only field).
                resolved = self._resolve_reference(record.id, entry.path,
                                                   allow_absolute=False)
                if isinstance(resolved, dict):
                    row["status"] = ("missing"
                                     if resolved.get("kind") == "reference_missing"
                                     else "invalid_path")
                    continue
                src_abs, _ = resolved
                if src_abs.parent != catalog_dir or src_abs.suffix != ".png":
                    # .png-only: 3e emits only *.png, and keying the matte
                    # output by the source STEM means same-stem sources with
                    # different extensions (hand-placed shot.png + shot.jpeg)
                    # would collide onto one matte file — silently swapping
                    # one entry's cutout for another's pixels, or letting a
                    # blocked entry's purge delete its neighbour's matte.
                    # Same-dir same-extension names cannot collide.
                    row["status"] = "invalid_path"
                    continue
                # (b) Layer-2 gate — ALWAYS, before the skip check
                # (re-screen semantics; a classify exception is a block).
                verdict = self._classify(toolkit, src_abs)
                if verdict.blocked:
                    self._audit.log(
                        "filter_block", layer=2, category=verdict.category,
                        matched=verdict.matched, context="image.matte.frame",
                        character_id=record.id, frame_id=entry.frame_id)
                    # Do not keep policy-violating pixels on disk (3c/3e
                    # discipline): frame + sidecar + any prior matte go, and
                    # the entry leaves the manifest (manifest mutation only —
                    # never record mutation).
                    self._delete_quietly(src_abs)
                    self._delete_quietly(src_abs.with_suffix(".json"))
                    self._delete_quietly(matted_dir / f"{src_abs.stem}.png")
                    # The purge must honor the SAME trust as the skip check:
                    # any recorded matted_path resolving into matted/ is a
                    # live matte of these now-blocked pixels (a hand-renamed
                    # matte would otherwise survive the purge).
                    if entry.matted_path:
                        prior = self._resolve_reference(
                            record.id, entry.matted_path, allow_absolute=False)
                        if not isinstance(prior, dict) and prior[0].parent == matted_dir:
                            self._delete_quietly(prior[0])
                    manifest.entries.remove(entry)
                    removed += 1
                    row["status"] = "blocked"
                    continue
                # (c) Skip (idempotent resume) — only a matted_path that
                # containment-resolves INTO matted/ is trusted; a dangling /
                # escaped / non-canonical value falls through and is
                # re-matted (overwritten with the canonical value).
                if not redo and entry.matted_path:
                    prior = self._resolve_reference(record.id, entry.matted_path,
                                                    allow_absolute=False)
                    if not isinstance(prior, dict) and prior[0].parent == matted_dir:
                        skipped += 1
                        row["status"] = "skipped"
                        continue
                # (d) Matte to a temp path the run owns; promote only a
                # gate-passing result, so a failure never destroys a prior
                # good matte (the 3d/3e prior-artifact discipline).
                final = matted_dir / f"{src_abs.stem}.png"
                tmp = matted_dir / f"{src_abs.stem}.png.tmp"
                try:
                    reading = toolkit.matter.matte(src_abs, tmp)
                except Exception as exc:
                    self._delete_quietly(tmp)
                    row["status"] = "matte_failed"
                    row["error"] = str(exc)
                    continue
                # (e) degenerate gate (empty / keyed-nothing-out masks)
                status = matte_mod.evaluate_matte(reading, config)
                if status is not None:
                    self._delete_quietly(tmp)
                    row["status"] = status
                    # A non-finite reading must not ship NaN/Infinity into the
                    # bridge payload (json.dumps would emit invalid JSON and
                    # hang the JS promise on JSON.parse).
                    row["coverage"] = (round(reading.coverage, 4)
                                       if math.isfinite(reading.coverage) else None)
                    continue
                # (f) promote (atomic) + record the char-relative path
                try:
                    os.replace(tmp, final)
                except OSError as exc:
                    self._delete_quietly(tmp)
                    row["status"] = "matte_failed"
                    row["error"] = str(exc)
                    continue
                entry.matted_path = f"catalog/matted/{final.name}"
                matted += 1
                row["status"] = "matted"
                row["matted_path"] = entry.matted_path
                row["coverage"] = round(reading.coverage, 4)
        finally:
            toolkit.close()

        blocked = removed
        failed = len(results) - matted - skipped - blocked
        tallies = {"frames": len(results), "matted": matted, "skipped": skipped,
                   "blocked": blocked, "failed": failed}
        model_path = matte_mod.matting_model_path(self._settings)
        model_name = model_path.name if model_path else None
        if matted or removed:
            # Optimistic concurrency (best-effort, not a lock): a concurrent
            # 3e regeneration swapped in a FRESH manifest mid-matte — never
            # clobber it with our stale in-memory copy. Mattes written into
            # the dead dir died with the swap; a rerun re-links idempotently.
            try:
                current = self._store.load_catalog(record.id)
            except ARTIFACT_LOAD_ERRORS:
                current = None
            if current is None or current.updated_at != token:
                # The run still did real work (purges audited per-frame as
                # they happened) — leave a run-level trail on the abort too.
                self._audit.log("catalog_matted", character_id=record.id,
                                aborted="catalog_changed", **tallies,
                                variant=config.variant, model=model_name)
                return {"ok": False, "kind": "catalog_changed",
                        "error": "the catalog changed during matting — rerun",
                        **tallies, "results": results}
            try:
                model_bytes = model_path.stat().st_size if model_path else None
            except OSError:
                model_bytes = None
            manifest.matting = {
                "variant": config.variant,
                "model": model_name,  # basename only (provenance, not a path)
                "model_bytes": model_bytes,
                "providers": matte_mod.onnx_providers(self._settings),
                "erode_px": config.erode_px,
                "feather_px": config.feather_px,
                "coverage_min": config.coverage_min,
                "coverage_max": config.coverage_max,
                "matted": matted,
                "complete": all(e.matted_path for e in manifest.entries),
                "matted_at": _now_iso(),
                "app_version": __version__,
            }
            manifest.updated_at = _now_iso()  # CatalogManifest has no touch()
            try:
                self._store.save_catalog(manifest)
            except OSError as exc:
                # Harmless drift: matted_path unrecorded, but the matte files
                # sit at deterministic names — a rerun re-links idempotently.
                self._audit.log("catalog_matted", character_id=record.id,
                                aborted="io", **tallies,
                                variant=config.variant, model=model_name)
                return {"ok": False, "kind": "io",
                        "error": f"could not save the catalog manifest: {exc}",
                        **tallies, "results": results}
        # (an all-skipped / all-non-destructive-failure run saves NOTHING:
        # a true no-op — no updated_at churn, no concurrency exposure)
        self._audit.log("catalog_matted", character_id=record.id, **tallies,
                        variant=config.variant, model=model_name)
        if matted == 0 and skipped == 0:
            # A run that produced nothing usable and hit at least one backend
            # failure is a systemic signal (e.g. the wrong .onnx pointed at),
            # not N per-frame rows. After save+audit, so blocked purges are
            # never lost; the tallies ride along like results.
            first = next((r for r in results if r["status"] == "matte_failed"),
                         None)
            if first is not None:
                return {"ok": False, "kind": "matte_failed",
                        "error": first.get("error")
                        or "matting failed on every frame",
                        **tallies, "results": results}
        return {"ok": True, "id": record.id, **tallies, "results": results}

    def matte_status(self, character_id: object) -> dict:
        """Matted/unmatted counts + matting readiness + run provenance (3f) —
        no models, no GPU, runs anywhere. A matted_path only counts when it
        containment-resolves to an existing file inside catalog/matted/ (a
        hand-edited value pointing elsewhere counts UNMATTED)."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        manifest = self._load_catalog_manifest(record.id)
        if isinstance(manifest, dict):
            return manifest
        missing = matte_mod.preflight_matte(self._settings)
        ready = missing is None
        if manifest is None:
            return {"ok": True, "id": record.id, "has_catalog": False,
                    "frames": 0, "matted": 0, "unmatted": 0, "stale": False,
                    "matting": None, "ready": ready, "missing": missing}
        matted_dir = self._store.matted_dir(record.id).resolve()
        matted = 0
        for entry in manifest.entries:
            if not entry.matted_path:
                continue
            resolved = self._resolve_reference(record.id, entry.matted_path,
                                               allow_absolute=False)
            if not isinstance(resolved, dict) and resolved[0].parent == matted_dir:
                matted += 1
        frames = len(manifest.entries)
        return {"ok": True, "id": record.id,
                "has_catalog": bool(manifest.entries), "frames": frames,
                "matted": matted, "unmatted": frames - matted,
                "stale": manifest.stale, "matting": manifest.matting,
                "ready": ready, "missing": missing}

    # -- matting internals --------------------------------------------------------

    def _load_catalog_manifest(self, character_id: str):
        """store.load_catalog with corrupt/hand-edited manifests mapped to a
        structured 'catalog_corrupt' (mirrors _load_bootstrap). Returns the
        manifest, None (absent), or an error dict. A manifest whose
        character_id != the requested id is CORRUPT: 3f is the first flow
        that loads, mutates, and re-saves catalog.json, and save_catalog
        routes by manifest.character_id — a hand-edited id would write this
        manifest onto ANOTHER character."""
        try:
            manifest = self._store.load_catalog(character_id)
        except ARTIFACT_LOAD_ERRORS as exc:
            # OverflowError: json.loads accepts Infinity/1e999 as floats and
            # a from_dict int() on one raises it (NOT a ValueError) — the
            # _generation_settings hazard, on the manifest channel.
            return {"ok": False, "kind": "catalog_corrupt",
                    "error": f"the catalog manifest is unreadable: {exc}"}
        if manifest is not None and manifest.character_id != character_id:
            return {"ok": False, "kind": "catalog_corrupt",
                    "error": "the catalog manifest belongs to a different "
                             "character"}
        return manifest

    @staticmethod
    def _matte_missing_message(kind: str) -> str:
        return {
            "matting_model_missing": "no matting model — set "
            "models.image.matting_model_path to a user-placed "
            "isnet-anime.onnx (see docs/IMAGE_PIPELINE.md §16)",
            "classifier_unavailable": "the Layer-2 content classifier is "
            "unavailable — set models.image.content_classifier_dir",
        }.get(kind, f"matting unavailable: {kind}")

    @staticmethod
    def _matte_load_error(exc: Exception) -> dict:
        """A non-MatteUnavailable failure building the matte toolkit (missing
        dependency import, corrupt model) — structured, never a bridge
        traceback (§2). Mirrors _cull_load_error."""
        return {"ok": False, "kind": "matte_unavailable",
                "error": "the matting model could not be loaded — finish "
                         "`pip install -r requirements-full.txt` on the target "
                         f"machine and place the model file ({exc})"}

    # -- scene builders: background generation + compositing (Stage 5, §13) -----

    def generate_background(self, scene_id: object, seed: object = None) -> dict:
        """Generate a background for a SCENE builder (§13, [HARDWARE]). Assembles
        a gated scenery prompt (NO character identity — no subject/adult anchor,
        no LoRA, no IP-Adapter), renders it on the plain base backend, screens
        the generated pixels with the Layer-2 classifier (fail-closed — a new
        requirement over generate_base), and persists the frame + a
        BackgroundManifest entry under the scene. A blocked frame is purged and
        audited; nothing policy-violating stays on disk. Engine-unavailable in
        the build sandbox returns a structured 'engine' error."""
        loaded = self._load_builder(scene_id)
        if isinstance(loaded, dict):
            return loaded
        if loaded.kind != "scene":
            return {"ok": False, "kind": "not_scene",
                    "error": "only scene builders generate backgrounds"}
        parsed_seed = self._parse_seed(seed)
        if isinstance(parsed_seed, dict):
            return parsed_seed
        assembled = self._assemble_scene(loaded)
        if isinstance(assembled, dict):
            return assembled

        # Layer-2 preflight up front — fail fast before burning a render.
        missing = cull_mod.preflight_classifier(self._settings)
        if missing is not None:
            return {"ok": False, "kind": missing,
                    "error": self._matte_missing_message(missing)}

        request = GenerationRequest(
            positive=assembled.positive,
            negative=assembled.negative,
            seed=parsed_seed,
            **self._generation_settings(),
        )
        try:
            result = self._engine.generate(request)
        except (EngineBusy, EngineUnavailable, GenerationFailed) as exc:
            return {"ok": False, "kind": "engine", "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "kind": "config", "error": str(exc)}

        checkpoint = self._engine.loaded_checkpoint
        try:
            checkpoint_bytes = checkpoint.stat().st_size if checkpoint else None
        except OSError:
            checkpoint_bytes = None
        sidecar = {
            "kind": "scene-background",
            "stage": "5-background",
            "builder_id": loaded.id,
            "record_updated_at": loaded.updated_at,
            "created_at": _now_iso(),
            "app_version": __version__,
            "checkpoint": checkpoint.name if checkpoint else None,
            "checkpoint_bytes": checkpoint_bytes,
            "request": result.request.to_dict(),
            "pieces": [p.to_dict() for p in assembled.pieces],
        }
        try:
            frame_path, sidecar_path = self._persist_image(
                self._builder_store.background_dir(loaded.id), "bg",
                result.request.seed, result.image, sidecar)
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not save the background: {exc}"}

        # Layer-2 gate: build the classifier (CPU ONNX — coexists with the
        # loaded engine, the matte/confirm_vetted precedent) and screen the
        # generated pixels fail-closed. A block purges the frame.
        try:
            toolkit = self._classifier_factory(self._settings)
        except CullUnavailable as exc:
            self._delete_quietly(frame_path)
            self._delete_quietly(sidecar_path)
            return {"ok": False, "kind": exc.kind,
                    "error": self._matte_missing_message(exc.kind)}
        except Exception as exc:
            self._delete_quietly(frame_path)
            self._delete_quietly(sidecar_path)
            return self._cull_load_error(exc)
        try:
            verdict = self._classify(toolkit, frame_path)
        finally:
            toolkit.close()
        if verdict.blocked:
            self._audit.log("filter_block", layer=2, category=verdict.category,
                            matched=verdict.matched, context="image.background",
                            builder_id=loaded.id)
            self._delete_quietly(frame_path)
            self._delete_quietly(sidecar_path)
            return {"ok": False, "kind": "blocked", "category": verdict.category,
                    "error": f"the generated background was blocked by the "
                             f"content policy ({verdict.category})"}

        self._append_background_entry(loaded.id, frame_path, assembled)
        self._audit.log("image_generated", stage="5-background",
                        builder_id=loaded.id, path=str(frame_path),
                        seed=result.request.seed, positive=assembled.positive,
                        negative=assembled.negative)
        return {"ok": True, "id": loaded.id,
                "path": str(frame_path), "sidecar": str(sidecar_path),
                "seed": result.request.seed, "positive": assembled.positive,
                "negative": assembled.negative}

    def composite_frame(self, character_id: object, frame_ref: object,
                        scene_id: object = None, background_ref: object = None,
                        overrides: object = None) -> dict:
        """Composite a matted character frame over a scene background, or —
        with no scene — return the matted cutout as a transparent-passthrough
        preview (§13 background on/off toggle). All-[HERE]: PIL runs in the
        sandbox. Returns a PNG data-URI preview (the CSP allows img-src data:
        only, like library.thumbnail); persists NOTHING — avatar-frame caching
        is Stage 6e."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        # The character's matted frame is untrusted (hand-editable manifest /
        # UI-supplied path): containment-resolve inside the character dir.
        resolved = self._resolve_reference(record.id, frame_ref, allow_absolute=True)
        if isinstance(resolved, dict):
            return resolved
        fg_abs, fg_rel = resolved
        config = self._composite_config(overrides)
        if isinstance(config, dict):
            return config
        try:
            fg = composite_mod.load_rgba_matted(fg_abs)
        except composite_mod.NotMatted as exc:
            return {"ok": False, "kind": "not_matted", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — never raise out of the bridge (§2)
            # A hand-placed oversized/crafted frame makes PIL raise
            # DecompressionBombError — a BARE Exception subclass, NOT
            # OSError/ValueError — which would otherwise hang the UI under
            # pythonw. Mirror library.thumbnail's "undecodable/oversized image"
            # stance: any decode failure on this untrusted path is structured io.
            return {"ok": False, "kind": "io",
                    "error": f"could not read the character frame: {exc}"}

        if scene_id is None or not str(scene_id).strip():
            # Background OFF: transparent passthrough (serve the matted cutout).
            try:
                preview = composite_mod.encode_png_data_uri(fg)
            except Exception as exc:  # noqa: BLE001 — never raise out of the bridge
                return {"ok": False, "kind": "io",
                        "error": f"could not encode the preview: {exc}"}
            self._audit.log("image_composited", character_id=record.id,
                            frame=fg_rel, background=None)
            return {"ok": True, "id": record.id, "background": False,
                    "frame": fg_rel, "preview": preview,
                    "width": fg.width, "height": fg.height}

        scene = self._load_builder(scene_id)
        if isinstance(scene, dict):
            return scene
        if scene.kind != "scene":
            return {"ok": False, "kind": "not_scene",
                    "error": "the background must be a scene builder"}
        bg_rel = self._resolve_background_ref(scene.id, background_ref)
        if isinstance(bg_rel, dict):
            return bg_rel
        bg_abs = resolve_within(self._builder_store.builder_dir(scene.id), bg_rel)
        if bg_abs is None:
            return {"ok": False, "kind": "no_background",
                    "error": "this scene has no usable generated background — "
                             "generate one first"}
        try:
            from PIL import Image

            with Image.open(bg_abs) as opened:
                bg = opened.convert("RGB")
            out = composite_mod.composite_over(bg, fg, config)
            preview = composite_mod.encode_png_data_uri(out)
        except Exception as exc:  # noqa: BLE001 — never raise out of the bridge (§2)
            # Same DecompressionBombError hazard on the untrusted background
            # path, plus any encode/compose failure — all structured, never a
            # bridge traceback.
            return {"ok": False, "kind": "io", "error": f"could not composite: {exc}"}
        self._audit.log("image_composited", character_id=record.id,
                        frame=fg_rel, background=bg_rel, scene_id=scene.id)
        return {"ok": True, "id": record.id, "background": True,
                "scene_id": scene.id, "frame": fg_rel, "background_frame": bg_rel,
                "preview": preview, "width": out.width, "height": out.height,
                "placement": {"anchor": config.anchor, "scale": config.scale,
                              "edge_choke": config.edge_choke,
                              "feather_px": config.feather_px,
                              "alpha_floor": config.alpha_floor}}

    def matted_frames(self, character_id: object) -> dict:
        """Every matted (keyable RGBA) frame a character owns across its
        catalog + cache — the compositing UI's source list. Each row is the
        char-relative ``matted_path`` (containment-checked to still exist) +
        its state + channel. No GPU."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        try:
            base = self._store.char_dir(record.id)
        except (InvalidId, ValueError, OSError):
            return {"ok": True, "id": record.id, "frames": [], "count": 0}
        frames: list[dict] = []
        for channel, loader in (("catalog", self._store.load_catalog),
                                ("cache", self._store.load_cache)):
            try:
                manifest = loader(record.id)
            except ARTIFACT_LOAD_ERRORS:
                continue
            if manifest is None:
                continue
            for e in manifest.entries:
                if e.matted_path and resolve_within(base, e.matted_path) is not None:
                    frames.append({"frame_id": e.frame_id,
                                   "matted_path": e.matted_path,
                                   "state": e.state, "source": channel})
        return {"ok": True, "id": record.id, "frames": frames,
                "count": len(frames)}

    def background_status(self, scene_id: object) -> dict:
        """A scene's generated backgrounds (frame list + existence) + Layer-2
        readiness. No GPU — a status probe for the UI."""
        loaded = self._load_builder(scene_id)
        if isinstance(loaded, dict):
            return loaded
        manifest = self._load_background_manifest(loaded.id)
        if isinstance(manifest, dict):
            return manifest
        frames = []
        base = self._builder_store.builder_dir(loaded.id)
        if manifest is not None:
            for e in manifest.entries:
                frames.append({
                    "frame_id": e.frame_id, "path": e.path,
                    "exists": resolve_within(base, e.path) is not None,
                    "state": e.state, "bytes": e.bytes})
        return {"ok": True, "id": loaded.id, "kind": loaded.kind,
                "is_scene": loaded.kind == "scene",
                "frames": frames, "count": len(frames),
                "classifier_ready":
                    cull_mod.preflight_classifier(self._settings) is None}

    def clear_background(self, scene_id: object) -> dict:
        """Delete a scene's generated backgrounds (frames + manifest)."""
        loaded = self._load_builder(scene_id)
        if isinstance(loaded, dict):
            return loaded
        try:
            removed = self._builder_store.clear_background(loaded.id)
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not clear the background: {exc}"}
        self._audit.log("scene_background_cleared", builder_id=loaded.id,
                        removed=removed)
        return {"ok": True, "id": loaded.id, "removed": removed}

    # -- scene/compositing helpers ----------------------------------------------

    def _load_builder(self, builder_id: object):
        """Load + re-gate a builder record, mapping every failure mode to the
        structured kind it is (the _load_record taxonomy, for the builder
        store). The consent + kind gates re-run on load, so a hand-edited
        builder.json cannot enter through this door."""
        bid = str(builder_id or "").strip()
        if not bid:
            return {"ok": False, "kind": "invalid",
                    "error": "a builder id is required"}
        try:
            return self._builder_store.load(bid)
        except (BuilderNotFound, InvalidId):
            return {"ok": False, "kind": "not_found",
                    "error": f"no builder with id {bid!r}"}
        except ContentBlocked as exc:
            self._audit.log("filter_block", layer=1, category=exc.category,
                            context=f"image.builder.{exc.field_name}",
                            matched=exc.matched, builder_id=bid)
            return {"ok": False, "kind": "blocked", "source": exc.field_name,
                    "category": exc.category,
                    "error": f"stored builder blocked by the content policy "
                             f"({exc.category})"}
        except ConsentError as exc:
            return {"ok": False, "kind": "consent", "error": str(exc)}
        except BuilderKindError as exc:
            return {"ok": False, "kind": "invalid", "error": str(exc)}
        except ARTIFACT_LOAD_ERRORS as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not read builder {bid!r}: {exc}"}

    def _assemble_scene(self, record: BuilderRecord):
        try:
            return self._assembler.assemble_scene(record, self._scene_catalog())
        except PromptBlocked as exc:
            self._audit.log("filter_block", layer=1, category=exc.category,
                            context=f"image.scene.{exc.source}",
                            matched=exc.matched, builder_id=record.id)
            return {"ok": False, "kind": "blocked", "source": exc.source,
                    "category": exc.category,
                    "error": f"scene prompt blocked by the content policy "
                             f"({exc.category})"}

    def _load_background_manifest(self, builder_id: str):
        """store.load_background with corrupt/hand-edited manifests mapped to a
        structured 'background_corrupt' (mirrors _load_catalog_manifest)."""
        try:
            manifest = self._builder_store.load_background(builder_id)
        except ARTIFACT_LOAD_ERRORS as exc:
            return {"ok": False, "kind": "background_corrupt",
                    "error": f"the background manifest is unreadable: {exc}"}
        if manifest is not None and manifest.builder_id != builder_id:
            return {"ok": False, "kind": "background_corrupt",
                    "error": "the background manifest belongs to a different "
                             "builder"}
        return manifest

    def _append_background_entry(self, builder_id, frame_path, assembled) -> None:
        """Append the persisted frame to background.json (best-effort — a save
        failure leaves the frame as an orphan the builder reconcile sweep
        removes, the character cache kill-window model)."""
        try:
            manifest = self._builder_store.load_background(builder_id)
        except ARTIFACT_LOAD_ERRORS:
            manifest = None
        if manifest is None or manifest.builder_id != builder_id:
            manifest = BackgroundManifest(builder_id=builder_id)
        try:
            rel = frame_path.relative_to(
                self._builder_store.builder_dir(builder_id)).as_posix()
        except ValueError:
            return
        try:
            size = frame_path.stat().st_size
        except OSError:
            size = 0
        manifest.entries.append(BackgroundEntry(
            frame_id=frame_path.stem, path=rel, bytes=size,
            state={"prompt": assembled.positive[:200]}))
        manifest.updated_at = _now_iso()
        try:
            self._builder_store.save_background(manifest)
        except OSError:
            pass

    def _resolve_background_ref(self, scene_id: str, background_ref: object):
        """The specific background frame to composite: an explicit
        containment-checked ref, else the most recent still-present manifest
        entry. Returns a char-relative path str or a structured error."""
        base = self._builder_store.builder_dir(scene_id)
        if background_ref is not None and str(background_ref).strip():
            rel = str(background_ref).strip()
            if resolve_within(base, rel) is None:
                return {"ok": False, "kind": "no_background",
                        "error": "the requested background frame is not available"}
            return rel
        manifest = self._load_background_manifest(scene_id)
        if isinstance(manifest, dict):
            return manifest
        if manifest is None or not manifest.entries:
            return {"ok": False, "kind": "no_background",
                    "error": "this scene has no generated background yet"}
        for entry in reversed(manifest.entries):
            if resolve_within(base, entry.path) is not None:
                return entry.path
        return {"ok": False, "kind": "no_background",
                "error": "this scene's background frames are missing"}

    def _composite_config(self, overrides):
        """The settings composite config + optional per-call overrides
        (anchor/scale/margin/edge_choke/feather_px/alpha_floor), clamped the
        same way (a bad override value is ignored, not fatal). A non-dict
        overrides object is a structured 'invalid'."""
        config = composite_mod.coerce_composite_config(self._settings)
        if overrides is None:
            return config
        if not isinstance(overrides, dict):
            return {"ok": False, "kind": "invalid",
                    "error": "composite overrides must be an object"}
        changes: dict = {}
        anchor = overrides.get("anchor")
        if isinstance(anchor, str) and anchor in composite_mod.ANCHORS:
            changes["anchor"] = anchor

        def _num(key: str, lo: float, hi: float, integer: bool) -> None:
            if key not in overrides:
                return
            try:
                v = float(overrides[key])
            except (TypeError, ValueError, OverflowError):
                return
            if not math.isfinite(v):
                return
            v = min(hi, max(lo, v))
            changes[key] = int(v) if integer else v

        _num("scale", 0.05, 1.0, False)
        _num("margin", 0.0, 0.5, False)
        _num("edge_choke", 0, 8, True)
        _num("feather_px", 0, 8, True)
        _num("alpha_floor", 0, 254, True)
        return replace(config, **changes)

    # -- on-demand generation + cache (3g) ----------------------------------------

    def generate_on_demand(self, character_id: object, state: object,
                           force: object = False) -> dict:
        """Resolve a requested state (expression/pose/outfit ids) to a frame —
        the "grow" of §7's seed-plus-grow. A state already covered by a valid
        seed-catalog or cache frame is served instantly (no models, no GPU);
        a novel state generates LoRA-steered, runs the SAME 3c auto-filter as
        3e ("same filter as training", §7), is matted best-effort via the 3f
        Matter, and caches under ``cache/`` with ``on_demand=True``.

        The caller only picks ids — every prompt fragment comes from the
        editable states file / option catalog, and the Layer-1 gate re-runs on
        the assembled cell regardless. ``force=True`` skips the lookup and
        regenerates, replacing any same-state cache frame (the seed catalog is
        never touched — a fresh cache frame shadows it, lookup order
        cache-then-catalog).

        Serving a hit is a read: pixels are re-screened at the next
        *processing* boundary (the 3f re-screen; the heal path below), not on
        every read — the 3c/3f stance. The one write a hit can make is
        bookkeeping (``last_used`` — the §14 LRU signal — and a healed
        ``matted_path``), saved best-effort behind the 3f optimistic token so
        it can never clobber a concurrently regenerated manifest and never
        fails the hit.

        VRAM (§3): generation reuses the 3e passes — the LoRA image model
        renders, is unloaded in a finally, and ONLY THEN the CPU cull runs;
        matting is CPU ONNX (zero VRAM). Zero record mutation."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        expressions, poses = catalog_mod.load_catalog_states()
        cell = catalog_mod.resolve_cell(record, self._catalog(), expressions,
                                        poses, state)
        if isinstance(cell, tuple):
            kind, message = cell
            return {"ok": False, "kind": kind, "error": message}
        triple = cell.state()

        cache_manifest = self._load_cache_manifest(record.id)
        if isinstance(cache_manifest, dict):
            return cache_manifest
        catalog_manifest = self._load_catalog_manifest(record.id)
        if isinstance(catalog_manifest, dict):
            return catalog_manifest

        if not bool(force):
            found = self._find_state_frame(record.id, triple, cache_manifest,
                                           catalog_manifest)
            if found is not None:
                source, manifest, entry, src_abs = found
                token = manifest.updated_at
                served = self._serve_cached(record, source, manifest, entry,
                                            src_abs, token)
                if served is not None:
                    return served
                # blocked on the heal re-screen: the frame was purged — the
                # state is novel again; fall through and regenerate fresh.

        # -- novel state -> generate (mirrors the 3e preconditions) ----------
        anchor = record.identity
        if not (anchor.has_lora and anchor.lora_path):
            return {"ok": False, "kind": "no_lora",
                    "error": "this character has no trained LoRA — train one "
                             "first (Stage 3d)"}
        lora_resolved = self._resolve_reference(record.id, anchor.lora_path,
                                                allow_absolute=False)
        if isinstance(lora_resolved, dict):
            return {"ok": False, "kind": "lora_missing",
                    "error": "the trained LoRA file is missing on disk"}
        lora_abs = lora_resolved[0]
        checkpoint = self._engine.checkpoint_path()
        if checkpoint is None or not checkpoint.is_file():
            return {"ok": False, "kind": "engine",
                    "error": "no image checkpoint configured for on-demand "
                             "generation"}
        ref = self._resolve_record_reference(record)
        if isinstance(ref, dict):
            return ref  # no_reference / reference_invalid / reference_missing
        ref_abs, ref_rel = ref
        missing = cull_mod.preflight_cull(self._settings, False)
        if missing is not None:
            return {"ok": False, "kind": missing,
                    "error": self._cull_missing_message(missing)}

        trigger = self._lora_trigger(record)
        pending = self._catalog_cell_prompts(record, [cell], trigger,
                                             context_prefix="image.cache")
        if not pending:
            return {"ok": False, "kind": "blocked",
                    "error": "the requested state was blocked by the content "
                             "policy"}

        # Reuse the 3e knobs verbatim (§7: the cache is the catalog, grown) —
        # lora_scale, max_attempts, and the pose-varied face-area relaxation.
        config = catalog_mod.coerce_catalog_config(self._settings)
        cull_config = replace(cull_mod.coerce_cull_config(self._settings),
                              face_area_min=config.face_area_min)
        gen_settings = self._generation_settings()
        # Stage into cache.new/ so an in-process failure leaves ZERO orphans
        # (the 3e staging discipline); only the culled survivor moves into
        # cache/. A hard kill leaves cache.new/, swept here on the next run.
        staging = self._store.char_dir(record.id) / "cache.new"
        self._delete_tree_quietly(staging)

        kept: list[CatalogEntry] = []
        attempt = 0
        while not kept and attempt < config.max_attempts:
            attempt += 1
            generated = self._catalog_generate_pass(
                record, lora_abs, pending, config, ref_rel, gen_settings,
                subdir="cache.new", rel_prefix="cache", stage="3g-cache",
                kind="cache")
            if isinstance(generated, dict):
                self._delete_tree_quietly(staging)
                return generated  # engine/config/io — nothing cached
            culled = self._catalog_cull_pass(record, ref_abs, generated,
                                             cull_config, on_demand=True,
                                             context="image.cache.frame")
            if isinstance(culled, dict):
                self._delete_tree_quietly(staging)
                return culled  # cull_unavailable / no_faces
            passed, _failed = culled
            kept.extend(passed)

        if not kept:
            self._delete_tree_quietly(staging)
            return {"ok": False, "kind": "frame_rejected",
                    "error": "no on-demand frame passed the auto-filter — "
                             "try again, or retune the LoRA (Stage 3d)"}
        entry = kept[0]

        # Move the survivor (frame + sidecar) from staging into cache/.
        frames_dir = self._store.cache_frames_dir(record.id)
        try:
            frames_dir.mkdir(parents=True, exist_ok=True)
            final = self._move_unique(staging / f"{entry.frame_id}.png",
                                      frames_dir)
            sidecar = staging / f"{entry.frame_id}.json"
            if sidecar.is_file():
                os.replace(sidecar, final.with_suffix(".json"))
        except OSError as exc:
            self._delete_tree_quietly(staging)
            return {"ok": False, "kind": "io",
                    "error": f"could not store the cached frame: {exc}"}
        self._delete_tree_quietly(staging)
        entry.frame_id = final.stem
        entry.path = f"cache/{final.name}"
        entry.last_used = _now_iso()

        # Matte best-effort via the 3f Matter (CPU). The pixels were classified
        # seconds ago by this run's own cull (content-first, fail-closed), so
        # the fresh frame is NOT re-classified here — unlike the heal path,
        # where the pixels' age is unbounded. A matte failure never discards
        # the culled frame; the next hit heals the gap.
        matte_status = matte_mod.preflight_matte(self._settings)
        if matte_status is None:
            mconfig = matte_mod.coerce_matte_config(self._settings)
            try:
                toolkit = self._matte_factory(self._settings, mconfig)
            except MatteUnavailable as exc:
                matte_status = exc.kind
            except Exception:
                matte_status = "matte_unavailable"
            else:
                try:
                    mfinal, matte_status = self._matte_one(
                        toolkit, final, self._store.cache_matted_dir(record.id),
                        mconfig)
                finally:
                    toolkit.close()
                if mfinal is not None:
                    entry.matted_path = f"cache/matted/{mfinal.name}"

        # Record it — RE-load the manifest (fresh, minimal read-modify-write
        # window), replace any prior same-state cache entries, append.
        manifest = self._load_cache_manifest(record.id)
        if isinstance(manifest, dict):
            self._audit.log("cache_generated", character_id=record.id,
                            aborted="cache_corrupt", frame_id=entry.frame_id,
                            state=triple)
            return manifest  # frame on disk unrecorded; Stage-4 sweep territory
        if manifest is None:
            manifest = CatalogManifest(character_id=record.id, entries=[])
        replaced = self._purge_state_entries(record.id, manifest, triple)
        manifest.entries.append(entry)
        manifest.updated_at = _now_iso()
        try:
            self._store.save_cache(manifest)
        except OSError as exc:
            self._audit.log("cache_generated", character_id=record.id,
                            aborted="io", frame_id=entry.frame_id, state=triple)
            return {"ok": False, "kind": "io",
                    "error": f"could not save the cache manifest: {exc}"}

        self._audit.log("cache_generated", character_id=record.id,
                        frame_id=entry.frame_id, state=triple,
                        attempts=attempt, replaced=replaced,
                        matted=entry.matted_path is not None,
                        matte_status=matte_status, bytes=entry.bytes)

        # §14 backstop: bring the grown cache back under the LRU cap. Best-
        # effort — the frame is generated, cached, and recorded; a cap fault
        # must never fail the request. The fresh frame is pinned explicitly
        # (protect_frame_id) — its last_used is newest, but the stamp has
        # one-second resolution and a same-second tie must not evict it.
        evicted = 0
        try:
            capped = self.enforce_cache_cap(record.id,
                                            protect_frame_id=entry.frame_id)
            if capped.get("ok"):
                evicted = capped.get("evicted", 0)
        except Exception:  # noqa: BLE001 — un-failable by contract
            pass

        return {"ok": True, "id": record.id, "cached": False,
                "source": "generated", "frame_id": entry.frame_id,
                "path": entry.path, "abs_path": str(final),
                "matted_path": entry.matted_path,
                "matte_status": matte_status, "state": triple,
                "attempts": attempt, "replaced": replaced,
                "evicted": evicted}

    def cache_status(self, character_id: object) -> dict:
        """Cache frame count / per-state rows (incl. the §14 last_used LRU
        signal) / matte coverage + readiness — no models, no GPU. A
        matted_path only counts when it containment-resolves into
        cache/matted/ (the matte_status stance)."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        manifest = self._load_cache_manifest(record.id)
        if isinstance(manifest, dict):
            return manifest
        missing = matte_mod.preflight_matte(self._settings)
        ready = missing is None
        if manifest is None:
            return {"ok": True, "id": record.id, "has_cache": False,
                    "frames": 0, "matted": 0, "unmatted": 0, "bytes": 0,
                    "stale": False, "states": [], "matte_ready": ready,
                    "matte_missing": missing}
        matted_dir = self._store.cache_matted_dir(record.id).resolve()
        matted = 0
        states: list[dict] = []
        for entry in manifest.entries:
            ok_matte = False
            if entry.matted_path:
                resolved = self._resolve_reference(record.id, entry.matted_path,
                                                   allow_absolute=False)
                if not isinstance(resolved, dict) and resolved[0].parent == matted_dir:
                    ok_matte = True
            if ok_matte:
                matted += 1
            states.append({"frame_id": entry.frame_id, "state": entry.state,
                           "matted": ok_matte, "last_used": entry.last_used,
                           "bytes": entry.bytes})
        frames = len(manifest.entries)
        return {"ok": True, "id": record.id, "has_cache": bool(manifest.entries),
                "frames": frames, "matted": matted, "unmatted": frames - matted,
                "bytes": manifest.total_bytes(), "stale": manifest.stale,
                "states": states, "matte_ready": ready, "matte_missing": missing}

    def clear_cache(self, character_id: object) -> dict:
        """Delete the on-demand cache (frames + mattes + manifest). Evicted
        states simply regenerate on demand if asked for again (§14)."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        try:
            removed = self._store.clear_cache(record.id)
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not clear the cache: {exc}"}
        self._audit.log("cache_cleared", character_id=record.id)
        return {"ok": True, "id": record.id, "removed": removed}

    def enforce_cache_cap(self, character_id: object,
                          protect_frame_id: str | None = None) -> dict:
        """§14 automatic per-character LRU cap on the on-demand cache — the
        backstop that keeps a never-cleaned character from growing unbounded.
        Compares the RECORDED artifacts' measured bytes (frame + sidecar +
        matte per manifest entry, trust-rule-resolved) against
        ``library.cache_cap_bytes`` and evicts least-recently-used entries
        (by the 3g ``last_used`` signal) until back under the cap, purging
        each entry's artifacts under the same trust rules as every other
        cache purge. Unrecorded orphan bytes in the tree are deliberately
        NOT counted — eviction cannot free them, and evicting good frames to
        pay for them would strip the cache while the tree stayed over cap;
        orphans are the reconciliation sweep's job (review catch). Evicted
        states simply regenerate on demand (§14). The most-recently-used
        entry is never evicted (a cap below one frame's cost must not
        thrash), and ``protect_frame_id`` pins the just-inserted frame
        against same-second ``last_used`` ties. Runs after every on-demand
        cache insert and from the Stage-4 reconciliation pass; no models,
        no GPU."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        manifest = self._load_cache_manifest(record.id)
        if isinstance(manifest, dict):
            return manifest
        config = manage_mod.coerce_library_config(self._settings)
        cap = config.cache_cap_bytes
        if manifest is None or not manifest.entries:
            return {"ok": True, "id": record.id, "evicted": 0,
                    "freed_bytes": 0, "cap_bytes": cap, "cache_bytes": 0,
                    "remaining": 0}
        frames_dir = self._store.cache_frames_dir(record.id).resolve()
        matted_dir = self._store.cache_matted_dir(record.id).resolve()
        pairs = [
            (entry, self._entry_disk_cost(record.id, entry, frames_dir,
                                          matted_dir))
            for entry in manifest.entries
        ]
        total = sum(cost for _entry, cost in pairs)
        evict = manage_mod.select_evictions(pairs, total, cap,
                                            protect_id=protect_frame_id)
        if not evict:
            return {"ok": True, "id": record.id, "evicted": 0,
                    "freed_bytes": 0, "cap_bytes": cap, "cache_bytes": total,
                    "remaining": len(manifest.entries)}
        costs = {id(entry): cost for entry, cost in pairs}
        freed = 0
        for entry in evict:
            freed += costs.get(id(entry), 0)
            self._purge_entry_artifacts(record.id, entry, frames_dir,
                                        matted_dir)
            manifest.entries.remove(entry)
        manifest.updated_at = _now_iso()
        try:
            self._store.save_cache(manifest)
        except OSError as exc:
            # Artifacts are gone but the manifest write failed: the dangling
            # entries read as novel on lookup and the reconcile pass drops
            # them — report it rather than pretend the eviction is recorded.
            self._audit.log("cache_evicted", character_id=record.id,
                            evicted=len(evict), freed_bytes=freed,
                            cap_bytes=cap, aborted="io")
            return {"ok": False, "kind": "io",
                    "error": f"could not save the cache manifest after "
                             f"eviction: {exc}"}
        self._audit.log("cache_evicted", character_id=record.id,
                        evicted=len(evict), freed_bytes=freed, cap_bytes=cap,
                        remaining=len(manifest.entries))
        return {"ok": True, "id": record.id, "evicted": len(evict),
                "freed_bytes": freed, "cap_bytes": cap,
                "cache_bytes": total - freed,
                "remaining": len(manifest.entries)}

    # -- on-demand cache internals ------------------------------------------------

    def _load_cache_manifest(self, character_id: str):
        """store.load_cache with corrupt/hand-edited manifests mapped to a
        structured 'cache_corrupt' (mirrors _load_catalog_manifest, including
        the character_id-mismatch guard — save_cache routes by the manifest's
        own id)."""
        try:
            manifest = self._store.load_cache(character_id)
        except ARTIFACT_LOAD_ERRORS as exc:
            return {"ok": False, "kind": "cache_corrupt",
                    "error": f"the cache manifest is unreadable: {exc}"}
        if manifest is not None and manifest.character_id != character_id:
            return {"ok": False, "kind": "cache_corrupt",
                    "error": "the cache manifest belongs to a different "
                             "character"}
        return manifest

    @staticmethod
    def _state_matches(entry: CatalogEntry, triple: dict) -> bool:
        state = entry.state if isinstance(entry.state, dict) else {}
        return all(str(state.get(k)) == v for k, v in triple.items())

    def _find_state_frame(self, record_id, triple, cache_manifest,
                          catalog_manifest):
        """The first entry matching the state triple whose pixels pass the 3f
        residency rule (containment-resolved direct *.png child of its own
        frames dir), cache before catalog (a forced regeneration shadows the
        seed frame). A dangling/escaped/hand-edited entry is silently NOT a
        hit — the state reads as novel. Returns (source, manifest, entry,
        src_abs) or None."""
        for source, manifest in (("cache", cache_manifest),
                                 ("catalog", catalog_manifest)):
            if manifest is None:
                continue
            frames_dir = (self._store.cache_frames_dir(record_id)
                          if source == "cache"
                          else self._store.catalog_frames_dir(record_id)).resolve()
            for entry in manifest.entries:
                if not self._state_matches(entry, triple):
                    continue
                resolved = self._resolve_reference(record_id, entry.path,
                                                   allow_absolute=False)
                if isinstance(resolved, dict):
                    continue
                src_abs = resolved[0]
                if src_abs.parent != frames_dir or src_abs.suffix != ".png":
                    continue
                return source, manifest, entry, src_abs
        return None

    def _serve_cached(self, record, source, manifest, entry, src_abs, token):
        """Serve a state hit. Returns the response dict, or None when the
        heal re-screen blocked the pixels (frame purged; caller regenerates).
        Bookkeeping writes (last_used, healed matted_path) are best-effort —
        they never fail the hit."""
        matted_dir = (self._store.cache_matted_dir(record.id)
                      if source == "cache"
                      else self._store.matted_dir(record.id)).resolve()
        matte_status = None
        matte_ok = False
        if entry.matted_path:
            prior = self._resolve_reference(record.id, entry.matted_path,
                                            allow_absolute=False)
            if not isinstance(prior, dict) and prior[0].parent == matted_dir:
                matte_ok = True
        dirty = False
        if matte_ok:
            matte_status = "matted"
        else:
            healed = self._heal_matte(record, source, manifest, entry,
                                      src_abs, matted_dir, token)
            if healed is None:
                return None  # blocked + purged
            matte_status = healed
            matte_ok = matte_status == "matted"
            dirty = matte_ok
        if source == "cache":
            entry.last_used = _now_iso()  # the §14 LRU access signal
            dirty = True
        if dirty:
            self._save_manifest_quietly(record.id, source, manifest, token)
        return {"ok": True, "id": record.id, "cached": True, "source": source,
                "frame_id": entry.frame_id, "path": entry.path,
                "abs_path": str(src_abs),
                "matted_path": entry.matted_path if matte_ok else None,
                "matte_status": matte_status, "state": entry.state,
                "last_used": entry.last_used}

    def _heal_matte(self, record, source, manifest, entry, src_abs,
                    matted_dir, token):
        """Fill a missing/invalid matte on an already-cached frame. This IS a
        processing boundary (3f): the pixels' age is unbounded and the
        manifest is hand-editable, so the source is re-classified fail-closed
        first — a blocked frame is purged (pixels + sidecar + matte + entry)
        + audited, and None is returned so the caller regenerates. Otherwise
        returns the matte status ('matted' sets entry.matted_path; a
        missing-model kind or per-frame failure serves the frame unmatted)."""
        missing = matte_mod.preflight_matte(self._settings)
        if missing is not None:
            return missing
        config = matte_mod.coerce_matte_config(self._settings)
        try:
            toolkit = self._matte_factory(self._settings, config)
        except MatteUnavailable as exc:
            return exc.kind
        except Exception:
            return "matte_unavailable"
        try:
            verdict = self._classify(toolkit, src_abs)
            if verdict.blocked:
                self._audit.log(
                    "filter_block", layer=2, category=verdict.category,
                    matched=verdict.matched, context="image.cache.heal",
                    character_id=record.id, frame_id=entry.frame_id)
                self._delete_quietly(src_abs)
                self._delete_quietly(src_abs.with_suffix(".json"))
                self._delete_quietly(matted_dir / f"{src_abs.stem}.png")
                # Purge honors the same trust rule as the skip/serve check
                # (the 3f purge fix): any recorded matted_path resolving into
                # matted/ is a live matte of these now-blocked pixels.
                if entry.matted_path:
                    prior = self._resolve_reference(record.id, entry.matted_path,
                                                    allow_absolute=False)
                    if not isinstance(prior, dict) and prior[0].parent == matted_dir:
                        self._delete_quietly(prior[0])
                if entry in manifest.entries:
                    manifest.entries.remove(entry)
                self._save_manifest_quietly(record.id, source, manifest, token)
                return None
            mfinal, status = self._matte_one(toolkit, src_abs, matted_dir,
                                             config)
        finally:
            toolkit.close()
        if mfinal is not None:
            prefix = ("cache/matted" if source == "cache"
                      else "catalog/matted")
            entry.matted_path = f"{prefix}/{mfinal.name}"
        self._audit.log("cache_matted", character_id=record.id, source=source,
                        frame_id=entry.frame_id, status=status)
        return status

    def _matte_one(self, toolkit, src_abs, matted_dir, config):
        """Matte ONE source frame (the 3f per-frame steps d-f: temp namespace
        no final can carry -> degenerate coverage gate -> atomic promote).
        Returns (final_path | None, status). Never raises."""
        try:
            matted_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None, "matte_failed"
        for stale in matted_dir.glob("*.png.tmp"):  # crashed-run leftovers
            self._delete_quietly(stale)
        final = matted_dir / f"{src_abs.stem}.png"
        tmp = matted_dir / f"{src_abs.stem}.png.tmp"
        try:
            reading = toolkit.matter.matte(src_abs, tmp)
        except Exception:
            self._delete_quietly(tmp)
            return None, "matte_failed"
        status = matte_mod.evaluate_matte(reading, config)
        if status is not None:
            self._delete_quietly(tmp)
            return None, status
        try:
            os.replace(tmp, final)
        except OSError:
            self._delete_quietly(tmp)
            return None, "matte_failed"
        return final, "matted"

    def _cache_entry_paths(self, record_id, entry, frames_dir,
                           matted_dir) -> list[Path]:
        """A cache entry's on-disk artifacts under the purge trust rules:
        only paths that containment-resolve into cache/ resp. cache/matted/
        count (frame + sidecar + canonical-stem matte + the recorded
        matted_path when it differs)."""
        paths: list[Path] = []
        resolved = self._resolve_reference(record_id, entry.path,
                                           allow_absolute=False)
        if not isinstance(resolved, dict) and resolved[0].parent == frames_dir:
            paths.append(resolved[0])
            paths.append(resolved[0].with_suffix(".json"))
            paths.append(matted_dir / f"{resolved[0].stem}.png")
        if entry.matted_path:
            prior = self._resolve_reference(record_id, entry.matted_path,
                                            allow_absolute=False)
            if (not isinstance(prior, dict) and prior[0].parent == matted_dir
                    and prior[0] not in paths):
                paths.append(prior[0])
        return paths

    def _purge_entry_artifacts(self, record_id, entry, frames_dir,
                               matted_dir) -> None:
        """Delete one cache entry's artifacts (trust rules above)."""
        for path in self._cache_entry_paths(record_id, entry, frames_dir,
                                            matted_dir):
            self._delete_quietly(path)

    def _entry_disk_cost(self, record_id, entry, frames_dir,
                         matted_dir) -> int:
        """Measured bytes an entry's artifacts occupy (the §14 eviction
        arithmetic). A dangling entry costs ~0 — evicting it only drops the
        row."""
        total = 0
        for path in self._cache_entry_paths(record_id, entry, frames_dir,
                                            matted_dir):
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return total

    def _purge_state_entries(self, record_id, manifest, triple) -> int:
        """Drop every CACHE entry matching the state triple (the new frame
        replaces it), deleting its artifacts under the same trust rules as
        the 3f purge (only paths that containment-resolve into cache/ resp.
        cache/matted/ are touched). Returns the number removed."""
        frames_dir = self._store.cache_frames_dir(record_id).resolve()
        matted_dir = self._store.cache_matted_dir(record_id).resolve()
        removed = 0
        for entry in list(manifest.entries):
            if not self._state_matches(entry, triple):
                continue
            self._purge_entry_artifacts(record_id, entry, frames_dir,
                                        matted_dir)
            manifest.entries.remove(entry)
            removed += 1
        return removed

    def _save_manifest_quietly(self, record_id, source, manifest, token) -> bool:
        """Best-effort bookkeeping save for the serve/heal path — never fails
        the hit. The 3f optimistic token protects a concurrently swapped
        manifest from being clobbered by our stale copy; on mismatch or an
        unreadable/absent current manifest, nothing is written (the pixels on
        disk are authoritative; the next access re-links idempotently)."""
        loader = (self._store.load_cache if source == "cache"
                  else self._store.load_catalog)
        saver = (self._store.save_cache if source == "cache"
                 else self._store.save_catalog)
        try:
            current = loader(record_id)
        except ARTIFACT_LOAD_ERRORS:
            return False
        if current is None or current.updated_at != token:
            return False
        manifest.updated_at = _now_iso()
        try:
            saver(manifest)
        except OSError:
            return False
        return True

    @staticmethod
    def _move_unique(src: Path, dest_dir: Path) -> Path:
        """Move ``src`` into ``dest_dir`` without ever clobbering an existing
        file — the _persist_frame O_EXCL discipline applied to a move:
        reserve a free name atomically, then replace the reservation."""
        counter = 1
        while True:
            name = (src.name if counter == 1
                    else f"{src.stem}-{counter}{src.suffix}")
            dest = dest_dir / name
            try:
                os.close(os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                break
            except FileExistsError:
                counter += 1
        try:
            os.replace(src, dest)
        except BaseException:
            try:  # do not leave the zero-byte reservation behind
                os.unlink(dest)
            except OSError:
                pass
            raise
        return dest

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
    return ImageService(
        store, settings, audit, catalog_provider=catalog_provider,
        builder_store=builder_store,
        scene_catalog_provider=scene_catalog_provider,
    )
