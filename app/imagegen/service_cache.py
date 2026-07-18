"""On-demand generation + LRU cache (3g) and footprint caching (5.5e).

Mixin for ``ImageService`` (see service.py): methods run on the composed
class and share its instance state (``self._store``, ``self._engine``,
``self._settings``, …) plus the shared privates that stay on the base
(``_load_record``, ``_assemble``, ``_delete_quietly``, …) via the MRO.
"""

from __future__ import annotations


import math
import os
from dataclasses import replace
from pathlib import Path

from ..model import CatalogEntry, CatalogManifest
from . import catalog as catalog_mod
from . import cull as cull_mod
from . import manage as manage_mod
from . import matte as matte_mod
from .matte import MatteUnavailable
from .service_shared import (
    ARTIFACT_LOAD_ERRORS,
    _MatteEscalation,
    _humanize,
    _now_iso,
)


class _CacheOps:

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

        trigger = self._generation_trigger(record)
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
                esc = self._build_escalation(mconfig)  # 5.5g bust escalation
                try:
                    mfinal, matte_status = self._matte_one(
                        toolkit, final, self._store.cache_matted_dir(record.id),
                        mconfig, esc=esc)
                finally:
                    toolkit.close()
                    if esc is not None:
                        esc.close()
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

        self.refresh_footprint(record.id)  # 5.5e: cache bytes grew (net of evict)
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

    def catalog_state_space(self, character_id: object) -> dict:
        """The id-triple space the 5.5d on-demand posing picker offers: the
        editable expression + pose states (``data/catalog_states.json``, §15)
        and the character's own wardrobe outfits (plus the as-is look). Ids
        only — the picker never sends prompt text; every fragment is resolved
        server-side at generation (the on-demand injection-safety stance,
        §15). No models, no GPU."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        expressions, poses = catalog_mod.load_catalog_states()
        catalog = self._catalog()
        outfit_group = catalog.get(catalog_mod.OUTFIT_GROUP)
        outfits = []
        for oid, _prompt in catalog_mod.record_outfits(record, catalog):
            if oid == catalog_mod.ASIS_OUTFIT:
                label = "As defined"
            else:
                opt = outfit_group.get_option(oid) if outfit_group else None
                label = opt.label if opt else _humanize(oid)
            outfits.append({"id": oid, "label": label})
        return {
            "ok": True, "id": record.id,
            "expressions": [{"id": s.id, "label": _humanize(s.id)}
                            for s in expressions],
            "poses": [{"id": s.id, "label": _humanize(s.id)} for s in poses],
            "outfits": outfits,
        }

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
        self.refresh_footprint(record.id)  # 5.5e: cache bytes went to zero
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

    # -- footprint caching (5.5e) -------------------------------------------------

    def refresh_footprint(self, character_id: object) -> None:
        """Recompute the on-disk footprint (§14) and cache it into the record's
        ``IdentityAnchor`` so ``library_list`` reads a stored value instead of
        walking every character's tree on each refresh (5.5e — ~10k stat()s per
        refresh at 200 characters bought nothing). Called after each artifact
        mutation that changes the LoRA / catalog / cache bytes, and from the
        reconcile sweep ("recompute on demand + at reconcile").

        Re-loads the record FRESH (only the footprint field is overwritten) so a
        long-running image job — the catalog run is 287 s, the train 31 min —
        can never clobber a concurrent creator edit with its own stale copy.
        Never raises and does NOT ``touch()``: a derived-artifact change is not
        a record edit and must not reorder the "recently updated" view, and a
        blocked/corrupt/locked record is simply the next reconcile's problem."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return
        try:
            record.identity.footprint = self._store.measure_footprint(record.id)
            self._store.save(record)
        except OSError:
            pass

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
        esc = self._build_escalation(config)  # 5.5g bust escalation; None = off
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
                                             config, esc=esc)
        finally:
            toolkit.close()
            if esc is not None:
                esc.close()
        if mfinal is not None:
            prefix = ("cache/matted" if source == "cache"
                      else "catalog/matted")
            entry.matted_path = f"{prefix}/{mfinal.name}"
        self._audit.log("cache_matted", character_id=record.id, source=source,
                        frame_id=entry.frame_id, status=status)
        return status

    def _build_escalation(self, config):
        """A _MatteEscalation for this run, or None when escalation is unset
        (the byte-for-byte no-op path). 5.5g / 3f residual. NEVER raises —
        coercion is defensive today, and this is called before the primary
        toolkit's close guard, so any future raising path in coercion must
        still degrade to 'escalation off' rather than leak the toolkit."""
        try:
            ec = matte_mod.coerce_escalation_config(self._settings, config)
        except Exception:
            return None
        return (_MatteEscalation(self._matte_factory, self._settings, ec)
                if ec is not None else None)

    def _apply_escalation(self, esc, src_abs, matted_dir, tmp, reading, config):
        """After a PRIMARY matte wrote ``tmp``, optionally re-matte the same
        source with the escalation (BiRefNet) model and return the (tmp, reading)
        to PROMOTE. The losing tmp is deleted. Never raises.

        Passthrough (returns the primary tmp/reading unchanged) when: escalation
        is off (esc None); the primary coverage is non-finite; the primary
        coverage is below the escalation threshold (not a bust signature); or the
        escalation toolkit is unavailable. Otherwise the escalated result is
        PREFERRED iff it is usable by the SAME gate AND keys strictly MORE out
        (lower coverage) than the primary — the never-worse rail, so escalation
        can never ship a matte worse than the primary."""
        if (esc is None
                or not math.isfinite(reading.coverage)
                or reading.coverage < esc.coverage):
            return tmp, reading
        tk = esc.toolkit()
        if tk is None:
            return tmp, reading
        # A distinct temp; still *.png.tmp so both stale-tmp sweeps reap a
        # crashed-run leftover, and it cannot collide with the primary tmp
        # (<stem>.png.tmp) or any final (<stem>.png).
        esc_tmp = matted_dir / f"{src_abs.stem}.esc.png.tmp"
        try:
            esc_reading = tk.matter.matte(src_abs, esc_tmp)
        except Exception:
            self._delete_quietly(esc_tmp)
            return tmp, reading
        if (matte_mod.evaluate_matte(esc_reading, config) is None
                and math.isfinite(esc_reading.coverage)
                and esc_reading.coverage < reading.coverage):
            self._delete_quietly(tmp)
            esc.escalated += 1
            return esc_tmp, esc_reading
        self._delete_quietly(esc_tmp)
        return tmp, reading

    def _matte_one(self, toolkit, src_abs, matted_dir, config, esc=None):
        """Matte ONE source frame (the 3f per-frame steps d-f: temp namespace
        no final can carry -> degenerate coverage gate -> atomic promote).
        ``esc`` (5.5g) optionally re-mattes a bust with BiRefNet before the
        gate. Returns (final_path | None, status). Never raises."""
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
        tmp, reading = self._apply_escalation(esc, src_abs, matted_dir, tmp,
                                              reading, config)
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

