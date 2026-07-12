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
from .ui.creator import CreatorService, build_creator

APP_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = APP_ROOT / "data"


def build_services() -> tuple[
    Settings, AuditLog, Layer1Filter, CreatorService, ImageService
]:
    """Construct the core services. Shared by the app and the test suite."""
    settings = Settings(DATA_DIR / "settings.json")
    if settings.get("models.active") is not None:
        # No process can hold VRAM across a restart — a persisted slot value
        # is always stale after a crash. The Stage-6a swap manager inherits
        # this reset.
        settings.set("models.active", None)
    audit = AuditLog(DATA_DIR / "logs", enabled=bool(settings.get("safety.logging_enabled", True)))
    content_filter = Layer1Filter()
    creator = build_creator(DATA_DIR, audit)
    # The image service reads the creator's live catalog so "Reload options"
    # changes prompt assembly the same instant it changes the form.
    images = build_image_service(
        creator.store, settings, audit, lambda: creator.catalog
    )
    return settings, audit, content_filter, creator, images


def run() -> None:
    settings, audit, content_filter, creator, images = build_services()
    audit.log("app_start", version=__version__, stage=STAGE)
    try:
        shell.run_shell(settings, audit, content_filter, creator, images)
    finally:
        audit.log("app_exit", version=__version__)


if __name__ == "__main__":
    run()
