"""Persistence layer for character records + catalog manifests (Stage 1).

On-disk layout (self-contained app folder, DECISIONS.md §2):

    <root>/characters/<id>/character.json     the record
                          /catalog.json        the catalog manifest (Stage 3)
                          /cache.json          the on-demand cache manifest (3g)
                          /reference/           reference image(s) (Stage 3)
                          /lora/                trained LoRA (Stage 3d)
                          /catalog/             rendered frames (Stage 3e)
                          /cache/               on-demand frames (Stage 3g)

Writes are atomic (temp file + os.replace) so a crash can never leave a
half-written record. Loading a record re-runs the content/age gates, so a
hand-edited file that violates policy fails loudly rather than loading a
prohibited state.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .bootstrap import BootstrapManifest, VettedManifest
from .character import CatalogManifest, CharacterRecord, Footprint, ensure_safe_id
from .lora import LoraManifest


class CharacterNotFound(KeyError):
    """No record exists for the given character id."""


def atomic_write_json(path: Path, data: dict) -> None:
    """Crash-safe JSON write (temp file + os.replace). Shared by the store
    and the Stage-3 pipeline's generation sidecars."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _dir_size(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    return total


class CharacterStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.characters_dir = self.root / "characters"

    # -- paths --------------------------------------------------------------

    def char_dir(self, character_id: str) -> Path:
        # ensure_safe_id rejects separators / '..' / drive markers, so a
        # crafted id (from a caller or a hand-edited file) cannot make save,
        # load, delete, or footprint escape characters_dir. Belt-and-braces:
        # confirm the resolved parent is exactly characters_dir.
        safe = ensure_safe_id(character_id)
        path = self.characters_dir / safe
        if path.resolve().parent != self.characters_dir.resolve():
            raise ValueError(f"character id escapes the store: {character_id!r}")
        return path

    def record_path(self, character_id: str) -> Path:
        return self.char_dir(character_id) / "character.json"

    def catalog_path(self, character_id: str) -> Path:
        return self.char_dir(character_id) / "catalog.json"

    def catalog_frames_dir(self, character_id: str) -> Path:
        return self.char_dir(character_id) / "catalog"

    def matted_dir(self, character_id: str) -> Path:
        """Stage-3f matte output dir. Inside catalog/ so a 3e regeneration
        swap destroys derived mattes with their source frames, footprint
        counts them as catalog_bytes, and clear_catalog removes them free."""
        return self.catalog_frames_dir(character_id) / "matted"

    def clear_catalog(self, character_id: str) -> bool:
        """Remove the seed-catalog frames + manifest (Stage 3e). Confined
        under char_dir; the frames dir and its sibling catalog.json both go."""
        import shutil

        removed = False
        frames = self.catalog_frames_dir(character_id)
        if frames.is_dir():
            shutil.rmtree(frames)
            removed = True
        manifest = self.catalog_path(character_id)
        if manifest.is_file():
            manifest.unlink()
            removed = True
        return removed

    # -- on-demand cache (Stage 3g) paths ------------------------------------

    def cache_path(self, character_id: str) -> Path:
        return self.char_dir(character_id) / "cache.json"

    def cache_frames_dir(self, character_id: str) -> Path:
        return self.char_dir(character_id) / "cache"

    def cache_matted_dir(self, character_id: str) -> Path:
        """Stage-3g matte output dir. Inside cache/ so a cache clear (or a
        Stage-4 LRU eviction of the tree) removes derived mattes with their
        source frames and footprint counts them as cache_bytes."""
        return self.cache_frames_dir(character_id) / "matted"

    def clear_cache(self, character_id: str) -> bool:
        """Remove the on-demand cache frames + manifest (Stage 3g). Confined
        under char_dir; the frames dir and its sibling cache.json both go."""
        import shutil

        removed = False
        frames = self.cache_frames_dir(character_id)
        if frames.is_dir():
            shutil.rmtree(frames)
            removed = True
        manifest = self.cache_path(character_id)
        if manifest.is_file():
            manifest.unlink()
            removed = True
        return removed

    # -- identity bootstrap (Stage 3c) paths --------------------------------

    def bootstrap_dir(self, character_id: str) -> Path:
        return self.char_dir(character_id) / "bootstrap"

    def candidates_dir(self, character_id: str) -> Path:
        return self.bootstrap_dir(character_id) / "candidates"

    def swapped_dir(self, character_id: str) -> Path:
        return self.bootstrap_dir(character_id) / "swapped"

    def bootstrap_path(self, character_id: str) -> Path:
        return self.bootstrap_dir(character_id) / "bootstrap.json"

    def vetted_dir(self, character_id: str) -> Path:
        return self.char_dir(character_id) / "vetted"

    def vetted_path(self, character_id: str) -> Path:
        return self.vetted_dir(character_id) / "vetted.json"

    # -- identity LoRA (Stage 3d) paths -------------------------------------

    def lora_dir(self, character_id: str) -> Path:
        return self.char_dir(character_id) / "lora"

    def lora_dataset_dir(self, character_id: str) -> Path:
        return self.lora_dir(character_id) / "dataset"

    def lora_manifest_path(self, character_id: str) -> Path:
        return self.lora_dir(character_id) / "lora.json"

    # -- records ------------------------------------------------------------

    def save(self, record: CharacterRecord) -> Path:
        path = self.record_path(record.id)
        atomic_write_json(path, record.to_dict())
        return path

    def load(self, character_id: str) -> CharacterRecord:
        path = self.record_path(character_id)
        if not path.is_file():
            raise CharacterNotFound(character_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        return CharacterRecord.from_dict(data)

    def exists(self, character_id: str) -> bool:
        return self.record_path(character_id).is_file()

    def list_ids(self) -> list[str]:
        if not self.characters_dir.is_dir():
            return []
        ids = [
            d.name
            for d in self.characters_dir.iterdir()
            if d.is_dir() and (d / "character.json").is_file()
        ]
        return sorted(ids)

    def load_all(self) -> list[CharacterRecord]:
        records: list[CharacterRecord] = []
        for cid in self.list_ids():
            records.append(self.load(cid))
        return records

    def delete(self, character_id: str) -> bool:
        cdir = self.char_dir(character_id)
        if not cdir.is_dir():
            return False
        # Remove the whole per-character directory (record + catalog + frames).
        import shutil

        shutil.rmtree(cdir)
        return True

    # -- catalog manifests --------------------------------------------------

    def save_catalog(self, manifest: CatalogManifest) -> Path:
        path = self.catalog_path(manifest.character_id)
        atomic_write_json(path, manifest.to_dict())
        return path

    def load_catalog(self, character_id: str) -> CatalogManifest | None:
        path = self.catalog_path(character_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return CatalogManifest.from_dict(data)

    # -- on-demand cache manifests (Stage 3g) --------------------------------

    def save_cache(self, manifest: CatalogManifest) -> Path:
        """The cache manifest reuses the CatalogManifest shape (its entries
        carry on_demand=True + last_used). Routes by the manifest's own id,
        like save_catalog — the service guards the id at load."""
        path = self.cache_path(manifest.character_id)
        atomic_write_json(path, manifest.to_dict())
        return path

    def load_cache(self, character_id: str) -> CatalogManifest | None:
        path = self.cache_path(character_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return CatalogManifest.from_dict(data)

    # -- identity bootstrap manifests (Stage 3c) ----------------------------

    def save_bootstrap(self, manifest: BootstrapManifest) -> Path:
        path = self.bootstrap_path(manifest.character_id)
        atomic_write_json(path, manifest.to_dict())
        return path

    def load_bootstrap(self, character_id: str) -> BootstrapManifest | None:
        path = self.bootstrap_path(character_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return BootstrapManifest.from_dict(data)

    def save_vetted(self, manifest: VettedManifest) -> Path:
        path = self.vetted_path(manifest.character_id)
        atomic_write_json(path, manifest.to_dict())
        return path

    def load_vetted(self, character_id: str) -> VettedManifest | None:
        path = self.vetted_path(character_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return VettedManifest.from_dict(data)

    # -- identity LoRA manifest (Stage 3d) ----------------------------------

    def save_lora_manifest(self, manifest: LoraManifest) -> Path:
        path = self.lora_manifest_path(manifest.character_id)
        atomic_write_json(path, manifest.to_dict())
        return path

    def load_lora_manifest(self, character_id: str) -> LoraManifest | None:
        path = self.lora_manifest_path(character_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return LoraManifest.from_dict(data)

    def clear_lora(self, character_id: str) -> bool:
        """Remove the whole per-character lora/ tree (LoRA + manifest +
        any leftover dataset). Confined under char_dir."""
        import shutil

        target = self.lora_dir(character_id)
        if target.is_dir():
            shutil.rmtree(target)
            return True
        return False

    def clear_bootstrap(self, character_id: str, scope: str = "all") -> bool:
        """Remove the bootstrap and/or vetted trees for a character. ``scope``
        is 'bootstrap' (candidates + swapped + manifest), 'vetted' (the
        confirmed set), or 'all'. Both dirs stay confined under char_dir."""
        import shutil

        removed = False
        targets: list[Path] = []
        if scope in ("bootstrap", "all"):
            targets.append(self.bootstrap_dir(character_id))
        if scope in ("vetted", "all"):
            targets.append(self.vetted_dir(character_id))
        for target in targets:
            if target.is_dir():
                shutil.rmtree(target)
                removed = True
        return removed

    # -- footprint ----------------------------------------------------------

    def measure_footprint(self, character_id: str) -> Footprint:
        """Actual on-disk footprint from the per-character subdirectories
        (§14). LoRA vs catalog vs cache are separated for the management view."""
        cdir = self.char_dir(character_id)
        return Footprint(
            lora_bytes=_dir_size(cdir / "lora"),
            catalog_bytes=_dir_size(cdir / "catalog"),
            cache_bytes=_dir_size(cdir / "cache"),
        )
