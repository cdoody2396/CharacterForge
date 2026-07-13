import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.audit import AuditLog  # noqa: E402
from app.config import Settings  # noqa: E402
from app.imagegen import ImageService, build_image_service  # noqa: E402
from app.safety import Layer1Filter  # noqa: E402
from app.ui.creator import CreatorService, build_creator  # noqa: E402
from app.ui.library import LibraryService  # noqa: E402


@pytest.fixture(scope="session")
def content_filter() -> Layer1Filter:
    """One compiled filter for the whole run — the data files are static."""
    return Layer1Filter()


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(tmp_path / "settings.json")


@pytest.fixture()
def audit(tmp_path) -> AuditLog:
    return AuditLog(tmp_path / "logs", enabled=True)


@pytest.fixture()
def creator(tmp_path, audit) -> CreatorService:
    """A creator over the bundled option catalog, persisting to tmp_path/data
    (with tmp_path/data/options as the user drop-in directory)."""
    return build_creator(tmp_path / "data", audit)


@pytest.fixture()
def images(creator, settings, audit) -> ImageService:
    """The image service wired exactly as main.build_services wires it:
    over the creator's store and live catalog."""
    return build_image_service(
        creator.store, settings, audit, lambda: creator.catalog
    )


@pytest.fixture()
def library(creator, settings, audit, images) -> LibraryService:
    """The library service wired exactly as main.build_services wires it."""
    return LibraryService(
        creator.store, settings, audit,
        images=images, catalog_provider=lambda: creator.catalog,
    )
