"""App entry point: wire settings, audit (Layer 4), the Layer-1 filter, and
the single-window shell."""

from __future__ import annotations

from pathlib import Path

from . import STAGE, __version__
from .audit import AuditLog
from .config import Settings
from .imagegen import ImageService, build_image_service
from .safety import Layer1Filter
from .ui import shell
from .ui.builders import BuilderService, build_builders
from .ui.creator import CreatorService, build_creator
from .ui.library import LibraryService

APP_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = APP_ROOT / "data"


def build_services() -> tuple[
    Settings, AuditLog, Layer1Filter, CreatorService, ImageService,
    LibraryService, BuilderService,
]:
    """Construct the core services. Shared by the app and the test suite."""
    settings = Settings(DATA_DIR / "settings.json")
    if settings.get("models.active") is not None:
        # No process can hold VRAM across a restart — a persisted slot value
        # is always stale after a crash. The Stage-6a swap manager inherits
        # this reset.
        settings.set("models.active", None)
    audit = AuditLog(DATA_DIR / "logs", enabled=bool(settings.get("safety.logging_enabled", True)))
    # Pin the process HF cache to the configured classifier dir BEFORE any
    # heavy import can freeze HF_HOME (hardware-validation catch: the setting
    # was a preflight witness only — imgutils would silently read the user's
    # default cache instead of the configured dir).
    from .imagegen import cull as _cull
    from .imagegen import engine as _engine

    _cull.pin_hf_cache(settings)
    # Same freeze-at-first-heavy-import hazard, offline flavor: with the §2
    # offline config complete (local pipeline_config_dir), pin the hub
    # offline BEFORE the base backend's diffusers import can freeze
    # HF_HUB_OFFLINE=False for the whole process (hardware-validation catch:
    # the cull's cached-model resolutions were making live etag requests).
    _engine.pin_hf_offline(settings)
    content_filter = Layer1Filter()
    creator = build_creator(DATA_DIR, audit)
    # Stage-5 builders (persona/scene/event/scenario) live in a parallel store.
    builders = build_builders(DATA_DIR, audit)
    # The image service reads the creator's live catalog so "Reload options"
    # changes prompt assembly the same instant it changes the form; it also
    # owns scene background generation + compositing, so it takes the builder
    # store + the live SCENE catalog (a builder "Reload options" reaches scene
    # prompt assembly the same instant).
    images = build_image_service(
        creator.store, settings, audit, lambda: creator.catalog,
        builder_store=builders.store,
        scene_catalog_provider=builders.scene_catalog,
    )
    library = LibraryService(
        creator.store, settings, audit,
        images=images, catalog_provider=lambda: creator.catalog,
    )
    return (settings, audit, content_filter, creator, images, library,
            builders)


def run() -> None:
    (settings, audit, content_filter, creator, images, library,
     builders) = build_services()
    audit.log("app_start", version=__version__, stage=STAGE)
    # Startup reconciliation sweeps: hard-kill orphans + the §14 LRU cap
    # (Stage 4), and orphaned scene-background frames (Stage 5). Fail-safe by
    # design; a fault here must never block the launch.
    try:
        library.reconcile()
    except Exception:  # noqa: BLE001 — launch must proceed regardless
        audit.log("library_reconcile_failed")
    try:
        builders.reconcile()
    except Exception:  # noqa: BLE001 — launch must proceed regardless
        audit.log("builder_reconcile_failed")
    try:
        shell.run_shell(settings, audit, content_filter, creator, images,
                        library, builders)
    finally:
        audit.log("app_exit", version=__version__)


if __name__ == "__main__":
    run()
