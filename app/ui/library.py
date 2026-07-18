"""Library & management service (Stage 4 — DECISIONS.md §14).

The read/manage side of the character library, behind the same bridge stance
as the creator and image services: strict shape validation at the doorway,
structured ``{ok: ...}`` results, and every failure mode mapped to the kind
it actually is (a corrupt or policy-blocked record is a *degraded row the
user can still delete*, never a traceback and never a phantom "no
characters").

- ``list_characters``: one summary row per stored character — identity
  flags, catalog/cache state (incl. §14 staleness), the MEASURED per-
  character footprint (LoRA + catalog + cached frames), and the §14
  deletion recommendation (cache past ``library.recommend_cache_bytes``).
  Sorting/filtering happens client-side over this payload; the row carries
  every axis the UI sorts on.
- ``get_character``: a record serialized back into the creator-form shape,
  for the Stage-4 edit path (the write itself is
  ``CreatorService.update_character``).
- ``delete_character``: removes the whole per-character tree. Deliberately
  does NOT require a loadable record — deletion is the remedy for a corrupt
  or policy-blocked one.
- ``thumbnail``: the identity reference image as a small data URI (the CSP
  allows ``img-src data:`` only — the page can never read arbitrary disk
  paths).
- ``reconcile``: the startup reconciliation sweep (the deferred Stage-4
  item): stale staging/backup dirs (``catalog.old``/``catalog.new``/
  ``cache.new``/``vetted.new`` — all only ever populated mid-run, so at
  startup they are orphans by definition), bootstrap candidates absent from
  ``bootstrap.json``, cache artifacts absent from ``cache.json``, manifest
  entries whose frames are gone, and the §14 LRU cap. Fail-safe stance: an
  unreadable manifest means orphanhood cannot be PROVEN, so that channel is
  skipped and reported — the sweep only ever deletes what a trusted
  manifest says is unrecorded, and only files matching our own artifact
  patterns, directly inside our own artifact dirs.

Layer 4: deletions, sweeps, and evictions are audited.
"""

from __future__ import annotations

import base64
import io
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..audit import AuditLog
from ..config import Settings
from ..imagegen import ImageService
from ..imagegen.manage import coerce_library_config
from ..imagegen.service import ARTIFACT_LOAD_ERRORS
from ..model import (
    AgeError,
    CharacterNotFound,
    CharacterRecord,
    CharacterStore,
    ContentBlocked,
    InvalidId,
    OptionCatalog,
    resolve_within,
)

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# The reconciliation sweep only ever deletes files matching our own artifact
# patterns; anything else (a user's stray notes.txt) is left alone.
_ARTIFACT_SUFFIXES = (".png", ".json", ".png.tmp")

# Staging/backup dirs that are only ever populated mid-run — at startup they
# are orphans of a hard kill (or a rolled-back 3e swap) by definition.
_STAGING_DIRS = ("catalog.old", "catalog.new", "cache.new", "vetted.new")

THUMBNAIL_MAX_PX = 256


def load_record_guarded(
    store: CharacterStore, audit: AuditLog, character_id: object,
    *, context: str = "library.load",
) -> CharacterRecord | dict:
    """Load + re-gate a stored record, mapping every failure mode to its
    structured kind (the ImageService._load_record taxonomy, shared here so
    the creator's edit path and the library agree on the doorway)."""
    cid = str(character_id or "").strip()
    if not cid:
        return {"ok": False, "kind": "invalid",
                "error": "a character id is required"}
    try:
        return store.load(cid)
    except (CharacterNotFound, InvalidId):
        return {"ok": False, "kind": "not_found",
                "error": f"no character with id {cid!r}"}
    except ContentBlocked as exc:
        audit.log(
            "filter_block",
            layer=1,
            category=exc.category,
            context=f"{context}.{exc.field_name}",
            matched=exc.matched,
            character_id=cid,
        )
        return {"ok": False, "kind": "blocked", "source": exc.field_name,
                "category": exc.category,
                "error": f"stored record blocked by the content policy "
                         f"({exc.category})"}
    except AgeError as exc:
        return {"ok": False, "kind": "age", "error": str(exc)}
    except ARTIFACT_LOAD_ERRORS as exc:
        return {"ok": False, "kind": "io",
                "error": f"could not read character {cid!r}: {exc}"}


def resolve_contained(
    store: CharacterStore, character_id: str, raw: object
) -> Path | None:
    """Resolve a stored char-relative path iff it names an existing file
    INSIDE the character's own directory, else None. Mirrors the use-time
    rules of ImageService._resolve_reference (NUL reject, no '..'/absolute/
    drive components, containment after resolve() collapses symlinks) —
    stored paths are hand-editable, so they are untrusted every time. The
    containment rule itself lives in ``store.resolve_within`` (shared with the
    Stage-5 builder store)."""
    try:
        char_dir = store.char_dir(character_id)
    except (InvalidId, ValueError, OSError):
        return None
    return resolve_within(char_dir, raw)


def _tree_size(path: Path) -> int:
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


def _delete_file_quietly(path: Path) -> int:
    """Best-effort unlink; returns the bytes freed (0 if it was already
    gone or is locked — a locked file is simply retried next startup)."""
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    try:
        os.unlink(path)
    except OSError:
        return 0
    return size


class LibraryService:
    """Owns the management view over the character store (§14)."""

    def __init__(
        self,
        store: CharacterStore,
        settings: Settings,
        audit: AuditLog,
        *,
        images: ImageService,
        catalog_provider: Callable[[], OptionCatalog],
    ):
        self._store = store
        self._settings = settings
        self._audit = audit
        self._images = images
        self._catalog = catalog_provider

    # -- listing ---------------------------------------------------------------

    def list_characters(self) -> dict:
        """Every stored character as a summary row. A record that fails to
        load degrades to an error row (still deletable) rather than hiding
        or failing the whole list."""
        config = coerce_library_config(self._settings)
        rows = []
        for cid in self._store.list_ids():
            rows.append(self._summary_row(cid, config))
        return {
            "ok": True,
            "characters": rows,
            "count": len(rows),
            "recommend_cache_bytes": config.recommend_cache_bytes,
            "cache_cap_bytes": config.cache_cap_bytes,
            # 5.7 filter capabilities: the genitalia filter exists exactly
            # while the content gate is open — structural, the catalog either
            # has the group or it doesn't (§11 Layer 3; nothing to sniff).
            "filters": {"genitalia": self._catalog().get("genitalia") is not None},
        }

    def _summary_row(self, cid: str, config) -> dict:
        loaded = load_record_guarded(self._store, self._audit, cid)
        if isinstance(loaded, dict):
            # Degraded row: the id is real (it came from list_ids), the record
            # is not usable — surface why, keep delete available. A broken
            # record has no cached footprint to read, so MEASURE it directly
            # here (5.5e: the disk walk stays off the hot path — this branch is
            # for the rare corrupt/blocked record, not the common list).
            try:
                footprint = self._store.measure_footprint(cid).to_dict()
                footprint["total_bytes"] = sum(footprint.values())
            except (InvalidId, ValueError, OSError):
                footprint = None
            recommend = bool(
                footprint
                and footprint["cache_bytes"] > config.recommend_cache_bytes
            )
            return {"id": cid, "ok": False, "kind": loaded.get("kind"),
                    "error": loaded.get("error"), "name": None,
                    "footprint": footprint, "recommend_delete": recommend}
        # 5.5e: read the CACHED footprint off the record instead of walking the
        # tree per row (~10k stat()s at 200 characters bought nothing). The
        # artifact ops (train/catalog/matte/on-demand/clear) refresh it on
        # change and the reconcile sweep recomputes it, so this stays current;
        # a record predating the cache reads 0s until the next reconcile.
        footprint = loaded.identity.footprint.to_dict()
        footprint["total_bytes"] = sum(footprint.values())
        recommend = footprint["cache_bytes"] > config.recommend_cache_bytes
        has_reference = (
            resolve_contained(self._store, cid,
                              loaded.identity.reference_image_path)
            is not None
        )
        return {
            "id": cid,
            "ok": True,
            "name": loaded.name,
            "age": int(loaded.age),
            "created_at": loaded.created_at,
            "updated_at": loaded.updated_at,
            "has_lora": bool(loaded.identity.has_lora
                             and loaded.identity.lora_path),
            "has_reference": has_reference,
            "tags": self._tag_labels(loaded),
            "labels": list(loaded.labels),  # 5.7 free-form labels
            # 5.7 attribute filters (sex / species / genitalia). Genitalia is
            # gate-degrading: with the gate closed the catalog lacks the
            # group, _single_label reads None, and the row carries no value —
            # nothing leaks into an ungated listing.
            "presentation": self._single_label(loaded, "gender_presentation"),
            "race": self._single_label(loaded, "race"),
            "race_classes": self._race_classes(loaded),
            "genitalia": self._single_label(loaded, "genitalia"),
            "catalog": self._manifest_summary(cid, "catalog"),
            "cache": self._manifest_summary(cid, "cache"),
            "footprint": footprint,
            "recommend_delete": bool(recommend),
        }

    def _single_label(self, record: CharacterRecord, gid: str) -> str | None:
        """A single-select value resolved to its option label (5.7 filters).
        None when unset; None when the group is absent from the live catalog
        (retired — or gated and the gate is closed); the raw id when the
        OPTION left the catalog (§15 source-of-truth, same as _tag_labels)."""
        value = record.selections.get(gid)
        if not value:
            return None
        group = self._catalog().get(gid)
        if group is None:
            return None
        option = group.get_option(value)
        return option.label if option else str(value)

    def _race_classes(self, record: CharacterRecord) -> list[str]:
        """The selected race option's class taxonomy (5.7 species filter —
        10 classes beat a 112-race dropdown)."""
        value = record.selections.get("race")
        group = self._catalog().get("race")
        option = group.get_option(value) if (group and value) else None
        return list(option.classes) if option else []

    def _tag_labels(self, record: CharacterRecord) -> list[str]:
        """The character's multi-select tag values (archetype / distinctive
        features / traits / wardrobe) resolved to human labels, for the 5.5e
        tag filter. Deduped, order-stable; an option id no longer in the
        catalog falls back to the raw id (the record stays the source of
        truth, §15). Chat-only groups (traits, render:false) are included —
        they are still identity the user filters by."""
        catalog = self._catalog()
        labels: list[str] = []
        seen: set[str] = set()
        for gid, opt_ids in record.tags.items():
            group = catalog.get(gid)
            for oid in opt_ids:
                option = group.get_option(oid) if group else None
                label = option.label if option else str(oid)
                if label not in seen:
                    seen.add(label)
                    labels.append(label)
        return labels

    def _manifest_summary(self, cid: str, channel: str) -> dict:
        loader = (self._store.load_catalog if channel == "catalog"
                  else self._store.load_cache)
        try:
            manifest = loader(cid)
        except ARTIFACT_LOAD_ERRORS:
            return {"error": f"{channel}_corrupt", "frames": 0, "stale": False}
        if manifest is None:
            return {"frames": 0, "stale": False}
        return {"frames": len(manifest.entries), "stale": bool(manifest.stale)}

    # -- read one (the edit-form payload) ---------------------------------------

    def get_character(self, character_id: object) -> dict:
        """A record serialized back into the creator-form shape, plus the
        identity state and the soft option-catalog lint (§15: options may
        have been removed since the record was written — the record stays
        the source of truth; issues are informational)."""
        loaded = load_record_guarded(self._store, self._audit, character_id)
        if isinstance(loaded, dict):
            return loaded
        has_reference = (
            resolve_contained(self._store, loaded.id,
                              loaded.identity.reference_image_path)
            is not None
        )
        footprint = loaded.identity.footprint.to_dict()
        footprint["total_bytes"] = sum(footprint.values())
        return {
            "ok": True,
            "id": loaded.id,
            "name": loaded.name,
            "age": int(loaded.age),
            "selections": dict(loaded.selections),
            "tags": {k: list(v) for k, v in loaded.tags.items()},
            "sliders": dict(loaded.sliders),
            "free_text": dict(loaded.free_text),
            "labels": list(loaded.labels),
            "created_at": loaded.created_at,
            "updated_at": loaded.updated_at,
            "identity": {
                "has_lora": bool(loaded.identity.has_lora
                                 and loaded.identity.lora_path),
                "has_reference": has_reference,
            },
            # 5.5d profile header reads the cached footprint (5.5e) here.
            "footprint": footprint,
            "issues": loaded.validate_against(self._catalog()),
        }

    # -- delete ------------------------------------------------------------------

    def delete_character(self, character_id: object) -> dict:
        """Remove the whole per-character tree (record + reference + LoRA +
        catalog + cache). Requires only a valid id, NOT a loadable record —
        deletion must stay available for exactly the records that no longer
        load (corrupt, policy-blocked, under-age hand-edits)."""
        cid = str(character_id or "").strip()
        if not cid:
            return {"ok": False, "kind": "invalid",
                    "error": "a character id is required"}
        try:
            removed = self._store.delete(cid)
        except (InvalidId, ValueError):
            return {"ok": False, "kind": "not_found",
                    "error": f"no character with id {cid!r}"}
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not delete character {cid!r}: {exc}"}
        if removed:
            self._audit.log("character_deleted", character_id=cid)
        return {"ok": True, "id": cid, "removed": removed}

    # -- thumbnail ----------------------------------------------------------------

    def thumbnail(self, character_id: object) -> dict:
        """The identity reference image, downscaled to a small JPEG data URI
        (bounded by THUMBNAIL_MAX_PX). ``thumbnail: None`` when there is no
        usable reference — a missing/corrupt/escaped image is a None, never
        a traceback (the row still renders)."""
        loaded = load_record_guarded(self._store, self._audit, character_id)
        if isinstance(loaded, dict):
            return loaded
        resolved = resolve_contained(self._store, loaded.id,
                                     loaded.identity.reference_image_path)
        if resolved is None:
            return {"ok": True, "id": loaded.id, "thumbnail": None}
        try:
            from PIL import Image
        except Exception:  # noqa: BLE001 — optional on a bare sandbox
            return {"ok": True, "id": loaded.id, "thumbnail": None}
        try:
            with Image.open(resolved) as im:
                im = im.convert("RGB")
                im.thumbnail((THUMBNAIL_MAX_PX, THUMBNAIL_MAX_PX))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=82)
        except Exception:  # noqa: BLE001 — undecodable/oversized image
            return {"ok": True, "id": loaded.id, "thumbnail": None}
        data = base64.b64encode(buf.getvalue()).decode("ascii")
        return {"ok": True, "id": loaded.id,
                "thumbnail": "data:image/jpeg;base64," + data}

    # -- reconciliation sweep -------------------------------------------------------

    def reconcile(self) -> dict:
        """The startup reconciliation sweep (the deferred Stage-4 item; see
        the module docstring for the fail-safe stance). Also runs the §14
        LRU cap per character. Callable from the UI as well as at startup;
        idempotent — a second run finds nothing."""
        totals = {"staging_dirs": 0, "bootstrap_orphans": 0,
                  "cache_orphans": 0, "catalog_entries_dropped": 0,
                  "cache_entries_dropped": 0, "cache_evicted": 0,
                  "bytes_freed": 0}
        details: list[dict] = []
        skipped: list[dict] = []
        for cid in self._store.list_ids():
            try:
                result = self._reconcile_character(cid)
            except Exception as exc:  # noqa: BLE001
                # A deep-fs fault on one character must never abort the whole
                # sweep nor escape the bridge (never-raise contract). Record
                # it as skipped and move on; the next launch retries.
                self._audit.log("library_sweep_failed", character_id=cid,
                                error=str(exc))
                skipped.append({"id": cid, "skipped": "error"})
                continue
            if result.get("skipped"):
                skipped.append(result)
                continue
            acted = any(v for k, v in result.items()
                        if k not in ("id", "notes") and v)
            for key in totals:
                totals[key] += result.get(key, 0)
            if acted or result.get("notes"):
                details.append(result)
                self._audit.log("library_swept", character_id=cid,
                                **{k: v for k, v in result.items()
                                   if k != "id"})
        self._audit.log("library_reconciled", characters=len(details),
                        skipped=len(skipped), **totals)
        return {"ok": True, **totals, "characters": details,
                "skipped": skipped}

    def _reconcile_character(self, cid: str) -> dict:
        out: dict = {"id": cid, "staging_dirs": 0, "bootstrap_orphans": 0,
                     "cache_orphans": 0, "catalog_entries_dropped": 0,
                     "cache_entries_dropped": 0, "cache_evicted": 0,
                     "bytes_freed": 0, "notes": []}
        try:
            cdir = self._store.char_dir(cid)
        except (InvalidId, ValueError, OSError):
            # A directory whose name fails the id rules is not ours to touch.
            return {"id": cid, "skipped": "invalid_id"}

        # 1) Stale staging/backup dirs — populated mid-run only; at startup
        #    they are hard-kill leftovers by definition. Count only bytes
        #    actually reclaimed (before minus what survives a locked
        #    rmtree), so a failed removal never over-reports (review catch).
        for name in _STAGING_DIRS:
            target = cdir / name
            if target.is_dir():
                before = _tree_size(target)
                shutil.rmtree(target, ignore_errors=True)
                if not target.is_dir():
                    out["staging_dirs"] += 1
                    out["bytes_freed"] += before
                else:
                    out["bytes_freed"] += before - _tree_size(target)

        # 2) Bootstrap candidates absent from bootstrap.json (a mid-batch
        #    kill leaves frame+sidecar pairs the manifest never recorded).
        out["bootstrap_orphans"], freed = self._sweep_orphans(
            cid, self._store.candidates_dir(cid),
            self._bootstrap_keep_names(cid, out["notes"]))
        out["bytes_freed"] += freed

        # 3) Catalog manifest verification: drop entries whose frames are
        #    gone; clear matted_path pointers whose mattes are gone.
        out["catalog_entries_dropped"] = self._verify_manifest(
            cid, "catalog", out["notes"])

        # 4) Cache: verify entries, then sweep unrecorded artifacts (a hard
        #    kill between survivor-move and manifest-save leaves an
        #    unrecorded frame+sidecar(+matte) pair — the named 3g orphan).
        out["cache_entries_dropped"] = self._verify_manifest(
            cid, "cache", out["notes"])
        keep = self._cache_keep_names(cid, out["notes"])
        if keep is not None:
            frames_keep, matted_keep = keep
            orphans, freed = self._sweep_orphans(
                cid, self._store.cache_frames_dir(cid), frames_keep)
            out["cache_orphans"] += orphans
            out["bytes_freed"] += freed
            orphans, freed = self._sweep_orphans(
                cid, self._store.cache_matted_dir(cid), matted_keep)
            out["cache_orphans"] += orphans
            out["bytes_freed"] += freed

        # 5) §14 LRU cap (best-effort — a blocked/corrupt record skips it;
        #    its cache cannot grow either).
        capped = self._images.enforce_cache_cap(cid)
        if capped.get("ok"):
            out["cache_evicted"] = capped.get("evicted", 0)
            out["bytes_freed"] += capped.get("freed_bytes", 0)

        # 6) 5.5e: recompute + cache the footprint. The sweeps above just
        #    changed the on-disk bytes (staging removed, orphans swept,
        #    evictions), and this is also the migration path for records
        #    written before the cache existed (their stored footprint is 0s
        #    until now). Best-effort — a blocked/corrupt record simply keeps
        #    the degraded-row measure path in list_characters.
        self._images.refresh_footprint(cid)

        # A channel can be flagged corrupt by both its keep-set build and its
        # verify pass — one note per distinct reason.
        out["notes"] = sorted(set(out["notes"]))
        return out

    def _bootstrap_keep_names(self, cid: str,
                              notes: list[str]) -> set[str] | None:
        """Filenames bootstrap.json vouches for (frame + same-stem sidecar).
        None (= sweep nothing) when the manifest is unreadable: orphanhood
        cannot be proven against a corrupt witness. An ABSENT manifest
        vouches for nothing — every candidate artifact is an orphan."""
        try:
            manifest = self._store.load_bootstrap(cid)
        except ARTIFACT_LOAD_ERRORS:
            notes.append("bootstrap_corrupt")
            return None
        if manifest is None:
            return set()
        keep: set[str] = set()
        for cand in manifest.candidates:
            base = os.path.basename(str(cand.path))
            if base:
                keep.add(base)
                keep.add(Path(base).stem + ".json")
        return keep

    def _cache_keep_names(
        self, cid: str, notes: list[str]
    ) -> tuple[set[str], set[str]] | None:
        """(cache/ names, cache/matted/ names) the cache manifest vouches
        for. A recorded frame keeps its sidecar and canonical-stem matte (an
        unrecorded matte for a recorded frame re-links on the next hit —
        deleting it would only force a pointless re-matte)."""
        try:
            manifest = self._store.load_cache(cid)
        except ARTIFACT_LOAD_ERRORS:
            notes.append("cache_corrupt")
            return None
        if manifest is not None and manifest.character_id != cid:
            notes.append("cache_corrupt")
            return None
        frames_keep: set[str] = set()
        matted_keep: set[str] = set()
        if manifest is not None:
            for entry in manifest.entries:
                base = os.path.basename(str(entry.path))
                if base:
                    frames_keep.add(base)
                    frames_keep.add(Path(base).stem + ".json")
                    matted_keep.add(Path(base).stem + ".png")
                if entry.matted_path:
                    mbase = os.path.basename(str(entry.matted_path))
                    if mbase:
                        matted_keep.add(mbase)
        return frames_keep, matted_keep

    def _sweep_orphans(self, cid: str, directory: Path,
                       keep: set[str] | None) -> tuple[int, int]:
        """Delete files in ``directory`` (non-recursive, our artifact
        patterns only) whose names the manifest does not vouch for. keep is
        None => the manifest was unreadable => sweep nothing."""
        if keep is None or not directory.is_dir():
            return 0, 0
        removed = 0
        freed = 0
        try:
            entries = list(directory.iterdir())
        except OSError:
            return 0, 0
        for item in entries:
            if not item.is_file():
                continue
            name = item.name
            if not name.endswith(_ARTIFACT_SUFFIXES):
                continue
            if name in keep:
                continue
            size = _delete_file_quietly(item)
            if size or not item.exists():
                removed += 1
                freed += size
        return removed, freed

    def _verify_manifest(self, cid: str, channel: str,
                         notes: list[str]) -> int:
        """Drop manifest entries whose frames no longer exist on disk (the
        deferred item's "verify manifest frames exist"); clear matted_path
        pointers whose mattes are gone (they heal/re-matte later). Saves
        only when something changed; an unreadable manifest is skipped."""
        loader = (self._store.load_catalog if channel == "catalog"
                  else self._store.load_cache)
        saver = (self._store.save_catalog if channel == "catalog"
                 else self._store.save_cache)
        try:
            manifest = loader(cid)
        except ARTIFACT_LOAD_ERRORS:
            notes.append(f"{channel}_corrupt")
            return 0
        if manifest is None:
            return 0
        if manifest.character_id != cid:
            notes.append(f"{channel}_corrupt")
            return 0
        dropped = 0
        changed = False
        for entry in list(manifest.entries):
            if resolve_contained(self._store, cid, entry.path) is None:
                manifest.entries.remove(entry)
                dropped += 1
                changed = True
                continue
            if entry.matted_path and resolve_contained(
                    self._store, cid, entry.matted_path) is None:
                entry.matted_path = None
                changed = True
        if changed:
            manifest.updated_at = _now_iso()
            try:
                saver(manifest)
            except OSError:
                notes.append(f"{channel}_save_io")
                return 0
        return dropped
