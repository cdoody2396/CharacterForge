"""Persistence for builder records + scene backgrounds (Stage 5 — §13).

On-disk layout (self-contained app folder, DECISIONS.md §2), a parallel tree
to ``characters/`` — builders are character-independent (a scene is reusable
across characters, §6):

    <root>/builders/<id>/builder.json        the record (kind in the record)
                        /background.json       the scene background manifest
                        /background/            generated background frames (scene)

Flat, with ``kind`` in the record (mirrors how ``CharacterStore`` keeps one
tree and the kind rides inside). Writes are atomic (temp file + os.replace);
loading a record re-runs the content + consent gates, so a hand-edited file
that violates policy fails loudly rather than loading a prohibited state.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .builder import BackgroundManifest, BuilderRecord
from .character import ensure_safe_id
from .store import _dir_size, atomic_write_json


class BuilderNotFound(KeyError):
    """No record exists for the given builder id."""


class BuilderStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.builders_dir = self.root / "builders"

    # -- paths --------------------------------------------------------------

    def builder_dir(self, builder_id: str) -> Path:
        # ensure_safe_id rejects separators / '..' / drive markers; belt-and-
        # braces, confirm the resolved parent is exactly builders_dir (the
        # CharacterStore.char_dir stance).
        safe = ensure_safe_id(builder_id)
        path = self.builders_dir / safe
        if path.resolve().parent != self.builders_dir.resolve():
            raise ValueError(f"builder id escapes the store: {builder_id!r}")
        return path

    def record_path(self, builder_id: str) -> Path:
        return self.builder_dir(builder_id) / "builder.json"

    def background_dir(self, builder_id: str) -> Path:
        return self.builder_dir(builder_id) / "background"

    def background_path(self, builder_id: str) -> Path:
        return self.builder_dir(builder_id) / "background.json"

    # -- records ------------------------------------------------------------

    def save(self, record: BuilderRecord) -> Path:
        path = self.record_path(record.id)
        atomic_write_json(path, record.to_dict())
        return path

    def load(self, builder_id: str) -> BuilderRecord:
        path = self.record_path(builder_id)
        if not path.is_file():
            raise BuilderNotFound(builder_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        return BuilderRecord.from_dict(data)

    def exists(self, builder_id: str) -> bool:
        return self.record_path(builder_id).is_file()

    def list_ids(self) -> list[str]:
        if not self.builders_dir.is_dir():
            return []
        ids = [
            d.name
            for d in self.builders_dir.iterdir()
            if d.is_dir() and (d / "builder.json").is_file()
        ]
        return sorted(ids)

    def load_all(self) -> list[BuilderRecord]:
        return [self.load(bid) for bid in self.list_ids()]

    def delete(self, builder_id: str) -> bool:
        bdir = self.builder_dir(builder_id)
        if not bdir.is_dir():
            return False
        # Remove the whole per-builder directory (record + background tree).
        shutil.rmtree(bdir)
        return True

    # -- scene background manifests -----------------------------------------

    def save_background(self, manifest: BackgroundManifest) -> Path:
        path = self.background_path(manifest.builder_id)
        atomic_write_json(path, manifest.to_dict())
        return path

    def load_background(self, builder_id: str) -> BackgroundManifest | None:
        path = self.background_path(builder_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return BackgroundManifest.from_dict(data)

    def clear_background(self, builder_id: str) -> bool:
        """Remove the generated background frames + manifest. Confined under
        builder_dir; the frames dir and its sibling background.json both go."""
        removed = False
        frames = self.background_dir(builder_id)
        if frames.is_dir():
            shutil.rmtree(frames)
            removed = True
        manifest = self.background_path(builder_id)
        if manifest.is_file():
            manifest.unlink()
            removed = True
        return removed

    # -- footprint ----------------------------------------------------------

    def measure_background_bytes(self, builder_id: str) -> int:
        """On-disk background footprint (the only heavy artifact a builder
        owns; personas/events/scenarios without imagery measure 0)."""
        return _dir_size(self.builder_dir(builder_id) / "background")
