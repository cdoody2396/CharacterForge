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
from .creator import CreatorService

WEB_DIR = Path(__file__).resolve().parent / "web"


class Api:
    """The JS↔Python bridge. Every method here is callable from the page
    via ``window.pywebview.api``. Keep it small and validated — this is the
    UI's only doorway into the backend."""

    # Settings keys the UI may write, with their validators.
    _SETTABLE: dict[str, Any] = {
        "models.image.variant": ("default", "heavy"),
        "models.chat.variant": ("default", "heavy"),
        "safety.logging_enabled": (True, False),
    }

    def __init__(
        self,
        settings: Settings,
        audit: AuditLog,
        content_filter: Layer1Filter,
        creator: CreatorService,
        images: ImageService,
    ):
        self._settings = settings
        self._audit = audit
        self._filter = content_filter
        self._creator = creator
        self._images = images

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
        # so guard the boolean toggle by type as well.
        if key == "safety.logging_enabled" and not isinstance(value, bool):
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

    # -- image pipeline (Stage 3) ------------------------------------------------

    def image_engine_status(self) -> dict:
        """Engine availability + generation settings — the UI's (and the
        hardware checklist's) structural probe."""
        return self._images.engine_status()

    def image_prompt_preview(self, character_id: Any = None) -> dict:
        """Assemble + gate a saved character's prompt pair without
        generating. Runs everywhere (no GPU needed)."""
        return self._images.preview_prompt(character_id)

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

    def image_clear_cache(self, character_id: Any = None) -> dict:
        """Delete the on-demand cache (frames + mattes + manifest); evicted
        states regenerate on demand (3g, §14)."""
        return self._images.clear_cache(character_id)


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

    api = Api(settings, audit, content_filter, creator, images)
    create_window(api, settings)
    # debug=False: no devtools, no extra windows. private_mode keeps the
    # webview from persisting browser-profile data.
    webview.start(debug=False, private_mode=True)
