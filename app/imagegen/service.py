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
    BootstrapCandidate,
    BootstrapManifest,
    CatalogEntry,
    CatalogManifest,
    CharacterNotFound,
    CharacterRecord,
    CharacterStore,
    ContentBlocked,
    InvalidId,
    LoraManifest,
    OptionCatalog,
    VettedEntry,
    VettedManifest,
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
from . import cull as cull_mod
from . import lora as lora_mod
from . import matte as matte_mod
from .cull import ContentVerdict, CullConfig, CullToolkit, CullUnavailable, ToolkitFactory
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
    ):
        self._store = store
        self._settings = settings
        self._audit = audit
        self._catalog = catalog_provider
        self._engine = engine or ImageEngine(settings)
        self._assembler = assembler or PromptAssembler()
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
        except (OSError, json.JSONDecodeError, ValueError, TypeError, LookupError,
                OverflowError, InvalidId) as exc:
            # LookupError (KeyError) covers a valid-JSON manifest that is
            # missing a required key — a natural hand-edit that from_dict
            # subscripts blindly.
            return {"ok": False, "kind": "bootstrap_corrupt",
                    "error": f"the bootstrap manifest is unreadable: {exc}"}

    def _load_vetted_manifest(self, character_id: str):
        try:
            return self._store.load_vetted(character_id)
        except (OSError, json.JSONDecodeError, ValueError, TypeError, LookupError,
                OverflowError, InvalidId) as exc:
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
        except (OSError, json.JSONDecodeError, ValueError, TypeError, LookupError,
                OverflowError, InvalidId) as exc:
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
        except (OSError, json.JSONDecodeError, ValueError, TypeError, LookupError,
                OverflowError, InvalidId) as exc:
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

    def _catalog_cell_prompts(self, record, cells, trigger):
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
                                context=f"image.catalog.{exc.source}",
                                character_id=record.id)
                continue
            pending.append((cell, assembled))
        return pending

    def _catalog_generate_pass(self, record, lora_abs, pending, config, ref_rel,
                               gen_settings):
        """Generate one frame per pending cell (LoRA image model), ALWAYS
        unloading in the finally. Returns a list of (cell, frame_path, rel,
        seed, assembled) or a structured error dict."""
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
                        record, assembled, result, subdir="catalog.new",
                        prefix="frame", kind="catalog", stage="3e-catalog",
                        reference=ref_rel)
                except OSError as exc:
                    error = {"ok": False, "kind": "io",
                             "error": f"could not save a catalog frame: {exc}"}
                    break
                generated.append((cell, frame_path,
                                  f"catalog/{frame_path.name}",
                                  result.request.seed, assembled))
        finally:
            self._engine.unload()
        # An engine/io error mid-pass is treated as fatal (a persistent OOM /
        # bad LoRA won't be fixed by retrying); bail and keep the prior catalog.
        if error is not None:
            return error
        return generated

    def _catalog_cull_pass(self, record, ref_abs, generated, cull_config):
        """Auto-filter the generated frames with the 3c cull. Returns
        (passed CatalogEntries, set of failed cells) or a structured error."""
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
                        context="image.catalog.frame", character_id=record.id)
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
                    on_demand=False, bytes=frame_bytes))
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
                    # for recovery, and the next successful run cleans it up.
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
            except (OSError, json.JSONDecodeError, ValueError, TypeError,
                    LookupError, OverflowError, InvalidId):
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
        except (OSError, json.JSONDecodeError, ValueError, TypeError,
                LookupError, OverflowError, InvalidId) as exc:
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
        except (OSError, json.JSONDecodeError, ValueError, OverflowError) as exc:
            # Unreadable (AV lock/disk) or corrupt/invalid record file.
            # OverflowError: a hand-edited Infinity in a footprint int field.
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
        provenance block. No absolute path is ever written to the sidecar."""
        out_dir = self._store.char_dir(record.id) / subdir
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        stem = f"{prefix}-{stamp}-{result.request.seed}"
        # Atomic name reservation (O_EXCL), not check-then-save: two
        # same-second same-seed persists must never overwrite each other,
        # even from concurrent bridge threads.
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
            result.image.save(str(frame_path))
        except BaseException:
            try:  # do not leave the zero-byte reservation behind
                os.unlink(frame_path)
            except OSError:
                pass
            raise
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
        sidecar_path = frame_path.with_suffix(".json")
        atomic_write_json(sidecar_path, sidecar)
        return frame_path, sidecar_path


def build_image_service(
    store: CharacterStore,
    settings: Settings,
    audit: AuditLog,
    catalog_provider: Callable[[], OptionCatalog],
) -> ImageService:
    return ImageService(
        store, settings, audit, catalog_provider=catalog_provider
    )
