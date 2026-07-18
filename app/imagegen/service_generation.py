"""Status, prompt preview, base generation (3a), and identity reference +
steered generation (3b).

Mixin for ``ImageService`` (see service.py): methods run on the composed
class and share its instance state (``self._store``, ``self._engine``,
``self._settings``, …) plus the shared privates that stay on the base
(``_load_record``, ``_assemble``, ``_delete_quietly``, …) via the MRO.
"""

from __future__ import annotations


from .engine import (
    EngineBusy,
    EngineUnavailable,
    GenerationFailed,
    GenerationRequest,
    ReferenceUnreadable,
    clip_token_counter,
)
from .prompt import AssembledPrompt, token_report
from .service_shared import _coerce_candidate_count


class _GenerationOps:

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
                "tokens": self._token_report(assembled), **assembled.to_dict()}

    def preview_record_prompt(self, record) -> dict:
        """preview_prompt for a TRANSIENT (unsaved) record — the live creator
        panel during create (5.5). Same assembly, same Layer-1 gating, same
        token report; no disk involved. The record comes from
        CreatorService.preview_record, never from caller-shaped input."""
        assembled = self._assemble(record)
        if isinstance(assembled, dict):
            return assembled
        return {"ok": True, "id": None, "preview": True, "has_reference": False,
                "tokens": self._token_report(assembled), **assembled.to_dict()}

    def _token_report(self, assembled: AssembledPrompt) -> dict:
        """CLIP-token accounting for the assembled prompt (Stage 5.5b), backed by
        the model's own tokenizer. Structured ``available: False`` when the
        tokenizer is not on disk (the sandbox) — never a vendored second BPE, and
        never a raise (a tokenizer fault must not break the preview)."""
        count = clip_token_counter(self._settings)
        if count is None:
            return {"available": False,
                    "reason": "the CLIP tokenizer is unavailable here — set "
                              "models.image.pipeline_config_dir to the local "
                              "model config (its tokenizer/ subfolder)"}
        try:
            return token_report(assembled, count)
        except Exception as exc:  # noqa: BLE001 — preview must not raise
            return {"available": False, "reason": f"token counting failed: {exc}"}

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

    def generate_base_candidates(self, character_id: object,
                                 count: object = None) -> dict:
        """Generate several gated base renders (varied seeds) as the avatar
        CANDIDATES for the create-wizard reference step (5.5d, §10 quick-create
        IP-Adapter tier). Sets NOTHING — the UI shows the grid, the user picks
        one, and only then does ``set_reference`` promote it; the character
        saved fine without a reference, this step only invites one.

        Runs as a job (5.5a): the base backend loads once and stays resident
        across the batch (``load`` is idempotent), each frame ticks the
        CancellableEngine progress, and cancellation between frames unwinds
        through the ``finally`` that always frees the §3 VRAM slot. A partial
        batch (engine dies mid-run) still returns the frames already rendered."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        n = _coerce_candidate_count(count)
        assembled = self._assemble(record)
        if isinstance(assembled, dict):
            return assembled  # a blocked record

        gen_settings = self._generation_settings()
        candidates: list[dict] = []
        error = None
        try:
            for _ in range(n):
                request = GenerationRequest(
                    positive=assembled.positive, negative=assembled.negative,
                    seed=None, **gen_settings)
                try:
                    result = self._engine.generate(request)
                except (EngineBusy, EngineUnavailable, GenerationFailed) as exc:
                    error = {"ok": False, "kind": "engine", "error": str(exc)}
                    break
                except ValueError as exc:
                    error = {"ok": False, "kind": "config", "error": str(exc)}
                    break
                try:
                    frame_path, _sidecar = self._persist_frame(
                        record, assembled, result, subdir="reference",
                        prefix="base", kind="base", stage="3a-base")
                except OSError as exc:
                    error = {"ok": False, "kind": "io",
                             "error": f"could not save a candidate frame: {exc}"}
                    break
                self._audit.log("image_generated", stage="3a-base",
                                character_id=record.id, path=str(frame_path),
                                seed=result.request.seed,
                                positive=assembled.positive,
                                negative=assembled.negative,
                                settings=gen_settings)
                candidates.append({"path": str(frame_path),
                                   "seed": result.request.seed})
        finally:
            self._engine.unload()  # §3: free the slot on every path (incl. cancel)
        if not candidates and error is not None:
            # Nothing rendered — surface the reason (engine-unavailable on the
            # sandbox). A partial batch is a success (the user still picks).
            return error
        return {"ok": True, "id": record.id, "candidates": candidates,
                "count": len(candidates), "requested": n}

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

