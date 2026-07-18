"""Identity bootstrap + auto-filter (3c) and its cull internals.

Mixin for ``ImageService`` (see service.py): methods run on the composed
class and share its instance state (``self._store``, ``self._engine``,
``self._settings``, …) plus the shared privates that stay on the base
(``_load_record``, ``_assemble``, ``_delete_quietly``, …) via the MRO.
"""

from __future__ import annotations


import math
import os
import shutil
from dataclasses import replace
from pathlib import Path

from ..model import (
    BootstrapCandidate,
    BootstrapManifest,
    CharacterRecord,
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
from . import cull as cull_mod
from .cull import ContentVerdict, CullConfig, CullUnavailable
from .engine import (
    EngineBusy,
    EngineUnavailable,
    GenerationFailed,
    GenerationRequest,
    ReferenceUnreadable,
)
from .service_shared import ARTIFACT_LOAD_ERRORS, _BatchFrame


class _BootstrapOps:

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
        # CONFIRMED candidates ride the grid too (flagged) — visible,
        # pre-checked, and re-confirmable, so a later confirm keeps them by
        # default instead of silently shrinking the vetted set (5.5 F1).
        proposed = sorted(
            [c for c in manifest.candidates
             if c.status in (STATUS_PROPOSED, STATUS_CONFIRMED)],
            key=lambda c: (c.rank if c.rank is not None else 1_000_000),
        )
        config = self._bootstrap_config()
        kept = sum(1 for c in manifest.candidates
                   if c.status in (STATUS_KEPT, STATUS_PROPOSED))
        # Per-gate rejection tally from the persisted readings (5.5
        # diagnosability — the UI names the gate, not just "quality").
        reasons: dict[str, int] = {}
        for c in manifest.candidates:
            reason = (c.quality or {}).get("reason")
            if reason and c.status.startswith("rejected_"):
                reasons[str(reason)] = reasons.get(str(reason), 0) + 1
        return {
            "ok": True, "id": record.id, "phase": manifest.phase,
            "counts": manifest.counts_by_status(),
            "reasons": reasons,
            "proposed": [
                {"candidate_id": c.candidate_id, "path": c.final_path(),
                 "similarity": c.similarity, "rank": c.rank,
                 "confirmed": c.status == STATUS_CONFIRMED}
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
        built = CullConfig(
            **{k: (int(current[k]) if k in int_keys else current[k])
               for k in current}
        )
        # The override path must honor the same contradiction guards as the
        # settings coercion (session-5 red-team: a recull override of
        # keep_cap=0 proposed NOTHING, resurrecting the grid<floor deadlock
        # through the bridge). Non-positive counts fall back to the incoming
        # config; the cap never sits below the floor.
        floor = built.floor if built.floor > 0 else config.floor
        keep_cap = built.keep_cap if built.keep_cap > 0 else config.keep_cap
        return replace(built, floor=floor, keep_cap=max(keep_cap, floor))

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

        # 5.5: the CPU cull is minutes on a big batch — make it cooperative so
        # a job cancel lands between candidates rather than after the whole
        # pass. current_token() is None outside a job (main thread, tests,
        # harness) -> pure pass-through, byte-identical behavior.
        from ..jobs import current_token

        token = current_token()
        try:
            by_id = {f.candidate_id: f for f in frames}
            scores = []
            for frame in frames:
                if token is not None:
                    # JobCancelled unwinds to the worker; scored-but-unsaved
                    # candidates stay on disk and the Stage-4 reconcile sweep
                    # owns any orphans (same posture as a mid-generation kill).
                    token.raise_if_cancelled()
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
            # A union re-cull (more=True after a confirm) must not demote the
            # already-vetted candidates back to KEPT/PROPOSED — the vetted
            # manifest is the confirmation's source of truth, so its members
            # keep CONFIRMED in the bootstrap grid (5.5 acceptance fix).
            try:
                vetted = self._store.load_vetted(record.id)
            except ARTIFACT_LOAD_ERRORS:
                # A corrupt/hand-edited vetted.json must not abort the cull —
                # preservation degrades to "nothing preserved" (the re-cull
                # itself is unaffected; confirm_vetted's own guarded loader
                # reports the corruption on its path).
                vetted = None
            vetted_ids = ({e.source_candidate_id for e in vetted.entries}
                          if vetted is not None else set())
            if vetted_ids:
                for score in scores:
                    if score.candidate_id in vetted_ids and not score.rejected:
                        score.status = STATUS_CONFIRMED
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
        # Per-gate rejection tally (5.5 diagnosability: "rejected_quality: 53"
        # hid a face_area miscalibration; name the gate so floors are tunable).
        reasons: dict[str, int] = {}
        for s in scores:
            if s.reason:
                reasons[s.reason] = reasons.get(s.reason, 0) + 1
        self._audit.log(event, character_id=record.id,
                        generated=len(frames), counts=counts, reasons=reasons,
                        short=short)
        return {
            "ok": True, "id": record.id, "phase": manifest.phase,
            "generated": len(frames), "counts": counts, "reasons": reasons,
            "short": short,
            "proposed": [
                {"candidate_id": c.candidate_id, "path": c.final_path(),
                 "similarity": c.similarity, "rank": c.rank,
                 "confirmed": c.status == STATUS_CONFIRMED}
                for c in sorted(
                    (c for c in manifest.candidates
                     if c.status in (STATUS_PROPOSED, STATUS_CONFIRMED)),
                    key=lambda c: (c.rank if c.rank is not None else 1_000_000))
            ],
            "has_vetted": vetted is not None,
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


