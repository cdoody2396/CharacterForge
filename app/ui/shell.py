"""Single-window UI shell (DECISIONS.md §2).

One pywebview window hosting an HTML/JS front-end with a JS↔Python bridge.
No console (the launcher runs under pythonw), no child windows, no HTTP
server — the page loads from disk and everything stays offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import STAGE, __version__
from ..audit import AuditLog
from ..config import Settings
from ..imagegen import ImageService
from ..safety import Layer1Filter
from .builders import BuilderService
from .creator import CreatorService
from .library import LibraryService

WEB_DIR = Path(__file__).resolve().parent / "web"


class Api:
    """The JS↔Python bridge. Every method here is callable from the page
    via ``window.pywebview.api``. Keep it small and validated — this is the
    UI's only doorway into the backend."""

    # Settings keys the UI may write, with their validators. Boolean keys are
    # additionally type-guarded in set_setting (True == 1 in Python).
    _SETTABLE: dict[str, Any] = {
        "models.image.variant": ("default", "heavy"),
        "models.chat.variant": ("default", "heavy"),
        "safety.logging_enabled": (True, False),
        # 5.6a content gate: the Settings toggle writes it, then the UI
        # reloads the options so the gated dirs (dis)appear from the catalog.
        "content.gate_open": (True, False),
    }
    _BOOL_KEYS = ("safety.logging_enabled", "content.gate_open")

    def __init__(
        self,
        settings: Settings,
        audit: AuditLog,
        content_filter: Layer1Filter,
        creator: CreatorService,
        images: ImageService,
        library: LibraryService,
        builders: BuilderService,
        jobs: Any = None,
    ):
        self._settings = settings
        self._audit = audit
        self._filter = content_filter
        self._creator = creator
        self._images = images
        self._library = library
        self._builders = builders
        # The long-running-job runner (Stage 5.5a). The app builds it in
        # main.run() (so its reap sweep runs at startup); a default is built
        # here for headless tests, rooted beside the settings file.
        if jobs is None:
            from ..jobs import JobRunner

            jobs = JobRunner(
                Path(self._settings.path).parent / "jobs", audit=audit,
                queue_size=self._settings.get("jobs.queue_size", 16),  # runner coerces
                retain_seconds=self._settings.get("jobs.retain_seconds", 604800),
                release=self._images.engine.unload,
            )
        self._jobs = jobs

    # -- diagnostics ----------------------------------------------------------

    def ping(self) -> str:
        return "pong"

    def app_info(self) -> dict:
        return {
            "version": __version__,
            "stage": STAGE,
            "settings_path": str(self._settings.path),
        }

    # -- settings -------------------------------------------------------------

    def get_settings(self) -> dict:
        return self._settings.as_dict()

    def set_setting(self, key: str, value: Any) -> dict:
        allowed = self._SETTABLE.get(key)
        if allowed is None:
            return {"ok": False, "error": f"setting not writable from UI: {key}"}
        # Strict membership (identity/equality) — note Python treats True==1,
        # so guard the boolean toggles by type as well.
        if key in self._BOOL_KEYS and not isinstance(value, bool):
            return {"ok": False, "error": f"invalid value for {key}: {value!r}"}
        if value not in allowed:
            return {"ok": False, "error": f"invalid value for {key}: {value!r}"}
        try:
            self._settings.set(key, value)
        except OSError as exc:
            # Persistence failed (disk full / file locked). Reload so
            # in-memory state matches disk, and report the failure to the UI
            # instead of leaking a promise rejection.
            self._settings.load()
            return {"ok": False, "error": f"could not save setting: {exc}"}
        if key == "safety.logging_enabled":
            if value:
                # Enable BEFORE logging so the re-enable event is recorded;
                # otherwise AuditLog.log() early-returns and the trail shows
                # only disables.
                self._audit.enabled = True
                self._audit.log("setting_changed", key=key, value=value)
            else:
                # Log the disable while still enabled, then honor it — turning
                # logging off is itself always on the record.
                self._audit.log("setting_changed", key=key, value=value)
                self._audit.enabled = False
        else:
            self._audit.log("setting_changed", key=key, value=value)
        return {"ok": True, "error": None}

    # -- safety ---------------------------------------------------------------

    def check_text(self, text: str, context: str = "freetext") -> dict:
        """Layer-1 gate exposed to the UI. Later stages call this for every
        free-text field; the Stage-0 page uses it for the filter test panel."""
        try:
            result = self._filter.check(text, context)
        except ValueError as exc:
            return {"allowed": False, "category": "error", "matched": None,
                    "context": context, "message": str(exc)}
        if not result.allowed:
            self._audit.log(
                "filter_block",
                layer=1,
                category=result.category,
                context=result.context,
                matched=result.matched,
            )
        return result.to_dict()

    # -- creator (Stage 2) ------------------------------------------------------

    def creator_catalog(self) -> dict:
        """The option catalog + free-text field set the creator renders from."""
        return self._creator.describe()

    def creator_reload_options(self) -> dict:
        """Re-scan the option data files so a freshly dropped-in file surfaces
        in the creator without an app restart (§15)."""
        return self._creator.reload()

    def create_character(self, payload: Any = None) -> dict:
        """Validate + persist a creator payload. Non-dict payloads are
        rejected inside the service with a structured error."""
        return self._creator.create_character(payload)

    # -- library & management (Stage 4) -------------------------------------------

    def library_list(self) -> dict:
        """Every stored character as a summary row (identity flags, catalog/
        cache state incl. staleness, measured footprint, §14 deletion
        recommendation). The UI sorts/filters client-side over this."""
        return self._library.list_characters()

    def library_get(self, character_id: Any = None) -> dict:
        """One record serialized back into the creator-form shape, for the
        edit path."""
        return self._library.get_character(character_id)

    def library_update(self, character_id: Any = None,
                       payload: Any = None) -> dict:
        """Apply an edited creator payload to an existing record: same
        strict validation + gates as creation, identity anchor preserved;
        render-relevant edits mark the catalog + cache stale (§14 — the UI
        then OFFERS regeneration, never forces it)."""
        return self._creator.update_character(character_id, payload)

    def library_delete(self, character_id: Any = None) -> dict:
        """Delete the whole per-character tree. Works on records that no
        longer load (corrupt/blocked) — deletion is their remedy."""
        return self._library.delete_character(character_id)

    def library_thumbnail(self, character_id: Any = None) -> dict:
        """The identity reference image as a small data URI (or None)."""
        return self._library.thumbnail(character_id)

    def library_reconcile(self) -> dict:
        """The startup reconciliation sweep, callable from the UI: stale
        staging dirs, manifest-orphaned artifacts, dangling manifest
        entries, and the §14 LRU cache cap."""
        return self._library.reconcile()

    # -- image pipeline (Stage 3) ------------------------------------------------

    def image_engine_status(self) -> dict:
        """Engine availability + generation settings — the UI's (and the
        hardware checklist's) structural probe."""
        return self._images.engine_status()

    def image_prompt_preview(self, character_id: Any = None) -> dict:
        """Assemble + gate a saved character's prompt pair without
        generating. Runs everywhere (no GPU needed)."""
        return self._images.preview_prompt(character_id)

    def creator_prompt_preview(self, payload: Any = None) -> dict:
        """Live prompt preview for the IN-PROGRESS creator form (5.5): builds
        a transient record from the payload (nothing persisted; partial forms
        allowed; the age + Layer-1 gates still run) and assembles it exactly
        like image_prompt_preview. Runs everywhere (no GPU needed)."""
        record = self._creator.preview_record(payload)
        if isinstance(record, dict):
            return record
        return self._images.preview_record_prompt(record)

    def image_generate_base(self, character_id: Any = None, seed: Any = None) -> dict:
        """One gated base render (3a). On this build sandbox it returns a
        structured engine-unavailable error; on the target machine it
        produces a frame + sidecar under the character's reference dir."""
        return self._images.generate_base(character_id, seed)

    def image_engine_release(self) -> dict:
        """Unload the image model / free the VRAM slot (§3 scaffold)."""
        return self._images.release_engine()

    # -- identity reference + steered generation (Stage 3b) --------------------

    def image_set_reference(self, character_id: Any = None,
                            frame_path: Any = None) -> dict:
        """Promote an in-character frame to the identity reference (3b)."""
        return self._images.set_reference(character_id, frame_path)

    def image_clear_reference(self, character_id: Any = None) -> dict:
        """Unset the identity reference (3b)."""
        return self._images.clear_reference(character_id)

    def image_reference_status(self, character_id: Any = None) -> dict:
        """Whether the character has a usable identity reference (3b) — no
        generation, so it runs on any machine."""
        return self._images.reference_status(character_id)

    def image_generate_identity(self, character_id: Any = None, seed: Any = None,
                                scale: Any = None) -> dict:
        """One IP-Adapter-steered render using the stored reference (3b). On
        this build sandbox it returns a structured engine/no-reference error;
        on the target machine it produces a steered frame under identity/."""
        return self._images.generate_identity(character_id, seed, scale)

    # -- identity bootstrap + auto-filter (Stage 3c) ---------------------------

    def image_bootstrap_generate(self, character_id: Any = None, batch: Any = None,
                                 more: Any = False) -> dict:
        """Generate a seed batch from the reference and auto-filter it into a
        machine-vetted grid (3c). Structured engine/model errors on the sandbox."""
        return self._images.bootstrap_generate(character_id, batch, more)

    def image_bootstrap_recull(self, character_id: Any = None,
                               overrides: Any = None) -> dict:
        """Re-cull the persisted candidates with adjusted thresholds — no image
        model, no regeneration (3c)."""
        return self._images.bootstrap_recull(character_id, overrides)

    def image_bootstrap_status(self, character_id: Any = None) -> dict:
        """Bootstrap phase / counts / proposed grid / vetted state (3c) — no GPU."""
        return self._images.bootstrap_status(character_id)

    def image_confirm_vetted(self, character_id: Any = None,
                             candidate_ids: Any = None) -> dict:
        """Promote a selected subset of machine-vetted candidates into the 3d
        training set (3c), validated against the trusted manifest + re-classified."""
        return self._images.confirm_vetted(character_id, candidate_ids)

    def image_clear_bootstrap(self, character_id: Any = None,
                              scope: Any = "all") -> dict:
        """Delete the bootstrap and/or vetted artifacts (3c)."""
        return self._images.clear_bootstrap(character_id, scope)

    # -- identity LoRA promotion (Stage 3d) ------------------------------------

    def image_train_lora(self, character_id: Any = None) -> dict:
        """Train + promote the identity LoRA on the vetted set (3d). Structured
        no_vetted/trainer_unavailable/engine errors on the sandbox."""
        return self._images.train_lora(character_id)

    def image_lora_status(self, character_id: Any = None) -> dict:
        """Whether the character has a trained LoRA + provenance (3d). No GPU."""
        return self._images.lora_status(character_id)

    def image_clear_lora(self, character_id: Any = None) -> dict:
        """Delete the trained LoRA and un-promote the record (3d)."""
        return self._images.clear_lora(character_id)

    # -- seed catalog (Stage 3e) -----------------------------------------------

    def image_generate_catalog(self, character_id: Any = None) -> dict:
        """Render the LoRA-steered seed catalog (expressions × poses × wardrobe),
        auto-filtered (3e). Structured no_lora/engine/model errors on the sandbox."""
        return self._images.generate_catalog(character_id)

    def image_catalog_status(self, character_id: Any = None) -> dict:
        """Catalog frame count / states / staleness (3e). No GPU."""
        return self._images.catalog_status(character_id)

    def image_clear_catalog(self, character_id: Any = None) -> dict:
        """Delete the seed catalog frames + manifest (3e). Removes the 3f
        mattes with them (they live inside catalog/)."""
        return self._images.clear_catalog(character_id)

    # -- matting / keyable output (Stage 3f) ------------------------------------

    def image_matte_catalog(self, character_id: Any = None,
                            force: Any = False) -> dict:
        """Background-remove the seed-catalog frames into keyable RGBA
        cutouts (3f). CPU ONNX — no GPU; structured matting_model_missing /
        no_catalog errors on the sandbox."""
        return self._images.matte_catalog(character_id, force)

    def image_matte_status(self, character_id: Any = None) -> dict:
        """Matted/unmatted counts + matting readiness + provenance (3f). No
        GPU."""
        return self._images.matte_status(character_id)

    # -- on-demand generation + cache (Stage 3g) ---------------------------------

    def image_generate_on_demand(self, character_id: Any = None,
                                 state: Any = None, force: Any = False) -> dict:
        """Resolve a state (expression/pose/outfit ids) to a frame (3g): a
        covered state serves instantly from the catalog/cache; a novel state
        generates LoRA-steered, auto-filters, mattes, and caches. Structured
        no_lora/engine/model errors on the sandbox."""
        return self._images.generate_on_demand(character_id, state, force)

    def image_cache_status(self, character_id: Any = None) -> dict:
        """Cache frame count / states / last_used / matte coverage (3g). No
        GPU."""
        return self._images.cache_status(character_id)

    def image_catalog_states(self, character_id: Any = None) -> dict:
        """The id-triple space (expressions / poses / wardrobe outfits) the
        on-demand posing picker offers (5.5d). Ids only; the picker never
        sends prompt text. No GPU."""
        return self._images.catalog_state_space(character_id)

    def image_clear_cache(self, character_id: Any = None) -> dict:
        """Delete the on-demand cache (frames + mattes + manifest); evicted
        states regenerate on demand (3g, §14)."""
        return self._images.clear_cache(character_id)

    # -- builders: persona / scene / event / scenario (Stage 5) -------------------

    def builder_describe(self, kind: Any = None) -> dict:
        """The per-kind option catalog + free-text fields the builder renders
        from (+ the code-advertised consent frames for a scenario). ``kind``
        None returns the kind list."""
        return self._builders.describe(kind)

    def builder_reload_options(self, kind: Any = None) -> dict:
        """Re-scan the builder option data files so a freshly dropped-in file
        surfaces without an app restart (§15)."""
        return self._builders.reload(kind)

    def builder_create(self, payload: Any = None) -> dict:
        """Validate + persist a builder payload (all four kinds). The record
        re-runs the Layer-1 content + Layer-3 consent + kind gates."""
        return self._builders.create(payload)

    def builder_update(self, builder_id: Any = None, payload: Any = None) -> dict:
        """Apply an edited payload to an existing builder (kind fixed; same
        gates re-run as creation)."""
        return self._builders.update(builder_id, payload)

    def builder_list(self) -> dict:
        """Every stored builder as a summary row. Unloadable records degrade to
        deletable error rows, never hide."""
        return self._builders.list()

    def builder_get(self, builder_id: Any = None) -> dict:
        """One builder serialized back into the form shape, for the edit path."""
        return self._builders.get(builder_id)

    def builder_delete(self, builder_id: Any = None) -> dict:
        """Delete the whole per-builder tree (works on unloadable records)."""
        return self._builders.delete(builder_id)

    def builder_reconcile(self) -> dict:
        """Sweep orphaned scene-background frames (the killed-generation
        kill-window); fail-safe vouching model. Also runs at startup."""
        return self._builders.reconcile()

    # -- scene imagery + compositing (Stage 5) -----------------------------------

    def scene_generate_background(self, scene_id: Any = None,
                                  seed: Any = None) -> dict:
        """Generate a background for a scene builder (§13, [HARDWARE]): gated
        scenery prompt -> plain SDXL render -> Layer-2 pixel gate -> persist.
        Structured engine/classifier errors on the sandbox."""
        return self._images.generate_background(scene_id, seed)

    def scene_background_status(self, scene_id: Any = None) -> dict:
        """A scene's generated backgrounds + Layer-2 readiness. No GPU."""
        return self._images.background_status(scene_id)

    def image_matted_frames(self, character_id: Any = None) -> dict:
        """A character's matted (keyable RGBA) frames — the compositing UI's
        source list (catalog + cache). No GPU."""
        return self._images.matted_frames(character_id)

    def image_frame_thumbnail(self, character_id: Any = None,
                              frame_path: Any = None, max_px: Any = None) -> dict:
        """A downscaled JPEG data URI for any frame the character owns
        (base/identity/bootstrap/catalog/cache) — the 5.5d profile's visual
        surface, since the page CSP forbids reading disk paths directly. A
        missing/escaped path returns thumbnail:None. No GPU."""
        return self._images.frame_thumbnail(character_id, frame_path, max_px)

    def scene_clear_background(self, scene_id: Any = None) -> dict:
        """Delete a scene's generated backgrounds (frames + manifest)."""
        return self._images.clear_background(scene_id)

    def image_composite(self, character_id: Any = None, frame_ref: Any = None,
                        scene_id: Any = None, background_ref: Any = None,
                        overrides: Any = None) -> dict:
        """Composite a matted character frame over a scene background (§13), or
        — with no scene — return the matted cutout as a transparent-passthrough
        preview (background on/off toggle). Returns a PNG data-URI preview.
        All-[HERE]: runs in the sandbox."""
        return self._images.composite_frame(
            character_id, frame_ref, scene_id, background_ref, overrides)

    # -- long-running jobs (Stage 5.5a) ------------------------------------------
    #
    # The slow image operations (train 31.5 min, bootstrap ~15 min, catalog
    # 287 s) run as background jobs the UI polls at ~1 Hz. The synchronous
    # image_*/scene_* bridges above are UNCHANGED (922 tests + every hardware
    # harness call them) — these wrap the same methods so the window never
    # blocks. `kind` picks the operation; `options` carries per-kind args.

    def job_submit(self, kind: Any = None, target_id: Any = None,
                   options: Any = None) -> dict:
        """Start a heavy image operation in the background. Returns a job_id;
        poll job_status(job_id) for progress + the eventual result."""
        opts = options if isinstance(options, dict) else {}
        built = self._build_job(kind, target_id, opts)
        if isinstance(built, dict):
            return built  # a structured {ok: False, kind: "job"} rejection
        fn, total = built
        return self._jobs.submit(str(kind), fn, target_id=_job_target(target_id),
                                 total=total)

    def job_status(self, job_id: Any = None) -> dict:
        """Non-blocking status (phase, progress {done,total}, result|error).
        The UI polls this ~1 Hz; the poll never blocks on a running generation."""
        return self._jobs.status(job_id)

    def job_cancel(self, job_id: Any = None) -> dict:
        """Request cancellation: cooperative for the in-process loops (bootstrap
        / catalog / on-demand, between frames) and Popen.terminate for a train
        subprocess. The VRAM slot is released and a cancelled train preserves
        the prior LoRA."""
        return self._jobs.cancel(job_id)

    def job_list(self) -> dict:
        """Every job this session has seen (queued/running/terminal)."""
        return self._jobs.list_jobs()

    def _build_job(self, kind: Any, target_id: Any, opts: dict):
        """Map a job kind onto the matching synchronous ImageService method,
        returning ``(zero_arg_fn, total)`` or a structured rejection. `total`
        is the progress denominator (frame count) where cheaply known, else
        None — the token still counts completed frames either way."""
        images = self._images
        tid = target_id
        explicit_total = opts.get("total")
        total = int(explicit_total) if isinstance(explicit_total, int) else None
        if kind == "avatar":
            # The create-wizard reference step: N base candidates, varied
            # seeds, the user picks one (5.5d). total = the candidate count so
            # the progress bar reads done/N as the batch renders.
            count = opts.get("count")
            n = count if isinstance(count, int) and count > 0 else 4
            return (lambda: images.generate_base_candidates(tid, count),
                    total if total is not None else min(n, 8))
        if kind == "identity":
            # One IP-Adapter-steered render (5.5d identity panel). It loads the
            # image model, so it runs as a job — never on the bridge thread.
            seed, scale = opts.get("seed"), opts.get("scale")
            return (lambda: images.generate_identity(tid, seed, scale),
                    total if total is not None else 1)
        if kind == "bootstrap":
            batch, more = opts.get("batch"), bool(opts.get("more", False))
            return (lambda: images.bootstrap_generate(tid, batch, more), total)
        if kind == "train":
            return (lambda: images.train_lora(tid), total)
        if kind == "catalog":
            return (lambda: images.generate_catalog(tid), total)
        if kind == "on_demand":
            state, force = opts.get("state"), bool(opts.get("force", False))
            return (lambda: images.generate_on_demand(tid, state, force),
                    total if total is not None else 1)
        if kind == "matte":
            force = bool(opts.get("force", False))
            return (lambda: images.matte_catalog(tid, force), total)
        if kind == "background":
            seed = opts.get("seed")
            return (lambda: images.generate_background(tid, seed),
                    total if total is not None else 1)
        return {"ok": False, "kind": "job", "reason": "invalid",
                "error": f"unknown job kind: {kind!r}"}


def _job_target(target_id: Any):
    return target_id if isinstance(target_id, str) else None


def _safe_int(value: Any, default: int) -> int:
    """Persisted geometry may be hand-edited to a non-number. Because the app
    runs under pythonw with no console, an int() crash here would kill the
    launch silently — fall back instead."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def create_window(api: Api, settings: Settings):
    """Create the single app window. Import of pywebview is deferred so the
    backend stays importable (and testable) without a GUI stack."""
    import webview

    title = settings.get("window.title", "CharacterForge")
    if not isinstance(title, str) or not title:
        title = "CharacterForge"
    return webview.create_window(
        title=title,
        url=(WEB_DIR / "index.html").as_uri(),
        js_api=api,
        width=_safe_int(settings.get("window.width", 1280), 1280),
        height=_safe_int(settings.get("window.height", 800), 800),
        min_size=(960, 600),
        text_select=False,
    )


def run_shell(
    settings: Settings,
    audit: AuditLog,
    content_filter: Layer1Filter,
    creator: CreatorService,
    images: ImageService,
    library: LibraryService,
    builders: BuilderService,
    jobs: Any = None,
) -> None:
    """Open the window and block until it closes."""
    import webview

    # Enforce §2 one-window/offline at the shell: never hand a link off to the
    # system browser (that would open a second window and hit the network).
    try:
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
        webview.settings["ALLOW_DOWNLOADS"] = False
    except (AttributeError, TypeError):
        pass  # older pywebview without the settings dict

    api = Api(settings, audit, content_filter, creator, images, library,
              builders, jobs=jobs)
    create_window(api, settings)
    # debug=False: no devtools, no extra windows. private_mode keeps the
    # webview from persisting browser-profile data.
    webview.start(debug=False, private_mode=True)
