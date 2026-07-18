"""Matting / keyable output (3f) over the seed catalog.

Mixin for ``ImageService`` (see service.py): methods run on the composed
class and share its instance state (``self._store``, ``self._engine``,
``self._settings``, …) plus the shared privates that stay on the base
(``_load_record``, ``_assemble``, ``_delete_quietly``, …) via the MRO.
"""

from __future__ import annotations


import math
import os

from .. import __version__
from . import matte as matte_mod
from .matte import MatteUnavailable
from .service_shared import ARTIFACT_LOAD_ERRORS, _now_iso


class _MatteOps:

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

        esc = self._build_escalation(config)  # 5.5g bust escalation; None = off
        catalog_dir = self._store.catalog_frames_dir(record.id).resolve()
        matted_dir = self._store.matted_dir(record.id)
        try:
            matted_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            toolkit.close()
            if esc is not None:
                esc.close()
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
                # (d') 5.5g: a bust (high coverage) is re-matted with BiRefNet;
                # the better-keyed cutout replaces tmp/reading (never worse).
                tmp, reading = self._apply_escalation(esc, src_abs, matted_dir,
                                                      tmp, reading, config)
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
            if esc is not None:  # frees the BiRefNet session; .escalated persists
                esc.close()

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
            if esc is not None:  # 5.5g bust escalation provenance (only if on)
                ep = matte_mod.matting_escalation_model_path(self._settings)
                manifest.matting["escalation_variant"] = esc.config.variant
                manifest.matting["escalation_model"] = ep.name if ep else None
                manifest.matting["escalated"] = esc.escalated
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
        if matted or removed:
            self.refresh_footprint(record.id)  # 5.5e: matted/purged catalog bytes
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


