"""Scene builders (Stage 5, §13): background generation + compositing.

Mixin for ``ImageService`` (see service.py): methods run on the composed
class and share its instance state (``self._store``, ``self._engine``,
``self._settings``, …) plus the shared privates that stay on the base
(``_load_record``, ``_assemble``, ``_delete_quietly``, …) via the MRO.
"""

from __future__ import annotations


import base64
import io
import math
from dataclasses import replace

from .. import __version__
from ..model import (
    BackgroundEntry,
    BackgroundManifest,
    BuilderKindError,
    BuilderNotFound,
    BuilderRecord,
    ConsentError,
    ContentBlocked,
    InvalidId,
    resolve_within,
)
from . import composite as composite_mod
from . import cull as cull_mod
from .cull import CullUnavailable
from .engine import (
    EngineBusy,
    EngineUnavailable,
    GenerationFailed,
    GenerationRequest,
)
from .prompt import PromptBlocked
from .service_shared import ARTIFACT_LOAD_ERRORS, _coerce_thumb_px, _now_iso


class _SceneOps:

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

    def frame_thumbnail(self, character_id: object, frame_path: object,
                        max_px: object = None) -> dict:
        """A downscaled JPEG data URI for ANY frame a character owns —
        base/identity/bootstrap/catalog/cache renders — for the 5.5d profile
        UI (the page CSP allows ``img-src data:`` only, so disk paths under
        ``characters/<id>/`` can never be shown directly; this is the visual
        surface for every generated frame). ``frame_path`` is the absolute
        path a generate_* result handed the UI (or a stored char-relative
        one); it is containment-resolved inside the character's own directory
        every call — the same untrusted-path stance as ``library.thumbnail``.
        A missing / corrupt / oversized / escaped path yields
        ``thumbnail: None``, never a traceback (the tile just shows a
        placeholder). No GPU."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        resolved = self._resolve_reference(record.id, frame_path,
                                           allow_absolute=True)
        if isinstance(resolved, dict):
            # invalid/escaped/missing — a None thumbnail, not the error dict
            # (the profile shows a placeholder tile, never a broken bridge).
            return {"ok": True, "id": record.id, "thumbnail": None}
        abs_path, rel = resolved
        px = _coerce_thumb_px(max_px)
        try:
            from PIL import Image
        except Exception:  # noqa: BLE001 — optional on a bare sandbox
            return {"ok": True, "id": record.id, "thumbnail": None}
        try:
            with Image.open(abs_path) as im:
                im = im.convert("RGB")  # flattens matted RGBA over black; the
                #                          compositing studio previews alpha
                im.thumbnail((px, px))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=82)
        except Exception:  # noqa: BLE001 — undecodable/oversized/DecompressionBomb
            return {"ok": True, "id": record.id, "thumbnail": None}
        data = base64.b64encode(buf.getvalue()).decode("ascii")
        return {"ok": True, "id": record.id, "path": rel,
                "thumbnail": "data:image/jpeg;base64," + data}

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

