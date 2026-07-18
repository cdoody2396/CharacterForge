"""Seed catalog generation (3e): the posed/keyable frame grid.

Mixin for ``ImageService`` (see service.py): methods run on the composed
class and share its instance state (``self._store``, ``self._engine``,
``self._settings``, …) plus the shared privates that stay on the base
(``_load_record``, ``_assemble``, ``_delete_quietly``, …) via the MRO.
"""

from __future__ import annotations


import os
from dataclasses import replace

from ..model import CatalogEntry, CatalogManifest
from ..model.bootstrap import STATUS_REJECTED_CONTENT
from . import catalog as catalog_mod
from . import cull as cull_mod
from .cull import CullUnavailable
from .engine import (
    EngineBusy,
    EngineUnavailable,
    GenerationFailed,
    GenerationRequest,
)
from .prompt import PromptBlocked
from .service_shared import ARTIFACT_LOAD_ERRORS


class _CatalogOps:

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

        trigger = self._generation_trigger(record)
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

        self.refresh_footprint(record.id)  # 5.5e: catalog bytes changed
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
        self.refresh_footprint(record.id)  # 5.5e: catalog bytes went to zero
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

