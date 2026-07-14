"""Builder service (Stage 5 — DECISIONS.md §13, §10, §11).

The write/manage side of the persona / scene / event / scenario builders,
behind the same bridge stance as the creator: strict shape validation at the
doorway, structured ``{ok: ...}`` results, and the hard gates (Layer-1 content,
the Layer-3 consent gate, the kind gate) living in ``BuilderRecord`` and
re-running on every construction — safety never depends on the UI behaving.

It mirrors ``CreatorService`` but for the *lighter* builder record (§13): the
same tags + filtered-free-text mechanism, no age / anatomy / sliders / identity.
Option catalogs are loaded **per kind** (``load_builder_catalog``,
``include_bundled=False``) so a scene form never shows character races, and a
drop-in file under ``<data>/builders/<kind>/`` extends a kind with no rebuild
(§15). §12's numeric reservation still runs at load, so a builder numeric group on
any non-reserved field is a load error; the bundled builder files define none
and a ``BuilderRecord`` has no sliders field regardless — "no sliders" holds
structurally either way.

The approved consent vocabulary is **advertised from code**
(``approved_consent_frames``), never from an option file, so the set a scenario
form offers and the set the record gate accepts are one and the same and a
data file can neither widen nor rename it.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..audit import AuditLog
from ..imagegen.service import ARTIFACT_LOAD_ERRORS
from ..model import (
    BUILDER_KINDS,
    BuilderKindError,
    BuilderNotFound,
    BuilderRecord,
    BuilderStore,
    ConsentError,
    ContentBlocked,
    InvalidId,
    OptionCatalog,
    OptionGroup,
    SCENARIO,
    approved_consent_frames,
    load_builder_catalog,
    resolve_within,
)
from .creator import NAME_MAX_LEN, TEXT_MAX_LEN, _group_payload, _Invalid
from .library import _ARTIFACT_SUFFIXES, _delete_file_quietly

# The fixed free-text fields each kind offers (§10: structured tags + filtered
# free text). Code-defined field SET (content is user text, gated on every
# path). ``setting_notes`` is the one that feeds the scene image prompt (the
# analogue of prompt.IMAGE_FREE_TEXT_KEYS).
BUILDER_FREE_TEXT_FIELDS: dict[str, tuple[dict[str, Any], ...]] = {
    "scene": (
        {"key": "setting_notes", "label": "Setting notes", "rows": 4,
         "hint": "Specifics the pickers can't express — architecture, props, "
                 "colours, what fills the space. Feeds the background image."},
    ),
    "scenario": (
        {"key": "situation_notes", "label": "Situation notes", "rows": 5,
         "hint": "The premise: what's happening, what's at stake, the framing "
                 "the characters step into."},
    ),
    "persona": (
        {"key": "persona_notes", "label": "Persona notes", "rows": 5,
         "hint": "Who this persona is — voice, quirks, how they carry "
                 "themselves, what they want."},
    ),
    "event": (
        {"key": "event_notes", "label": "Event notes", "rows": 5,
         "hint": "What happens and why it matters — the beat this event marks."},
    ),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_builder_guarded(
    store: BuilderStore, audit: AuditLog, builder_id: object,
    *, context: str = "builder.load",
) -> BuilderRecord | dict:
    """Load + re-gate a stored builder, mapping every failure mode to its
    structured kind (the ImageService._load_builder taxonomy, shared so the
    service and this UI agree on the doorway). The consent + kind gates re-run
    on load — a hand-edited builder.json cannot enter through this door."""
    bid = str(builder_id or "").strip()
    if not bid:
        return {"ok": False, "kind": "invalid", "error": "a builder id is required"}
    try:
        return store.load(bid)
    except (BuilderNotFound, InvalidId):
        return {"ok": False, "kind": "not_found",
                "error": f"no builder with id {bid!r}"}
    except ContentBlocked as exc:
        audit.log("filter_block", layer=1, category=exc.category,
                  context=f"{context}.{exc.field_name}", matched=exc.matched,
                  builder_id=bid)
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


class BuilderService:
    """Owns the per-kind option catalogs + the builder store on behalf of the
    Scenes UI (create/edit/list/delete + the scene-background reconcile sweep)."""

    def __init__(self, store: BuilderStore, audit: AuditLog,
                 *, data_dir: Path | str | None = None):
        self._store = store
        self._audit = audit
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._catalogs: dict[str, OptionCatalog] = {}
        self._load_catalogs()

    def _load_catalogs(self) -> None:
        self._catalogs = {
            kind: load_builder_catalog(kind, self._data_dir)
            for kind in BUILDER_KINDS
        }

    @property
    def store(self) -> BuilderStore:
        return self._store

    def catalog(self, kind: str) -> OptionCatalog:
        return self._catalogs.get(kind) or load_builder_catalog(kind, self._data_dir)

    def scene_catalog(self) -> OptionCatalog:
        """The live SCENE catalog — passed to ImageService so a builder
        "Reload options" reaches scene prompt assembly the same instant."""
        return self.catalog("scene")

    # -- catalog description ----------------------------------------------------

    def describe(self, kind: object = None) -> dict:
        """The option catalog + free-text fields for one kind, shaped for the
        UI. For a scenario, the code-advertised approved consent frames ride
        along (the required Layer-3 control). ``kind`` None returns the kind
        list only (the UI's initial pick)."""
        if kind is None:
            return {"ok": True, "kinds": list(BUILDER_KINDS),
                    "name_max_len": NAME_MAX_LEN, "text_max_len": TEXT_MAX_LEN}
        k = str(kind)
        if k not in BUILDER_KINDS:
            return {"ok": False, "kind": "invalid",
                    "error": f"unknown builder kind {k!r}"}
        catalog = self.catalog(k)
        payload = {
            "ok": True,
            "kind": k,
            "kinds": list(BUILDER_KINDS),
            # builder options carry no picker thumbnails — a null image
            # resolver keeps the shared payload shape without a file read
            "groups": [_group_payload(g, lambda _rel: None)
                       for g in catalog.groups()],
            "free_text_fields": [dict(f) for f in BUILDER_FREE_TEXT_FIELDS.get(k, ())],
            "name_max_len": NAME_MAX_LEN,
            "text_max_len": TEXT_MAX_LEN,
            "errors": [{"file": f, "error": e} for f, e in catalog.errors],
        }
        if k == SCENARIO:
            payload["consent_frames"] = approved_consent_frames()
        return payload

    def reload(self, kind: object = None) -> dict:
        """Re-scan the builder option directories (a dropped-in file surfaces
        without an app restart, §15) and return describe(kind)."""
        self._load_catalogs()
        return self.describe(kind)

    # -- listing ----------------------------------------------------------------

    def list(self) -> dict:
        """Every stored builder as a summary row. A record that fails to load
        degrades to an error row (still deletable), never hides."""
        rows = []
        for bid in self._store.list_ids():
            rows.append(self._summary_row(bid))
        return {"ok": True, "builders": rows, "count": len(rows)}

    def _summary_row(self, bid: str) -> dict:
        try:
            bg_bytes = self._store.measure_background_bytes(bid)
        except (InvalidId, ValueError, OSError):
            bg_bytes = 0
        loaded = load_builder_guarded(self._store, self._audit, bid)
        if isinstance(loaded, dict):
            return {"id": bid, "ok": False, "kind": None,
                    "error": loaded.get("error"),
                    "load_kind": loaded.get("kind"), "name": None,
                    "background_bytes": bg_bytes}
        backgrounds = 0
        if loaded.kind == "scene":
            try:
                manifest = self._store.load_background(bid)
                backgrounds = len(manifest.entries) if manifest else 0
            except ARTIFACT_LOAD_ERRORS:
                backgrounds = 0
        return {
            "id": bid, "ok": True, "kind": loaded.kind, "name": loaded.name,
            "consent": loaded.consent,
            "created_at": loaded.created_at, "updated_at": loaded.updated_at,
            "backgrounds": backgrounds, "background_bytes": bg_bytes,
        }

    def get(self, builder_id: object) -> dict:
        """One record serialized back into the builder-form shape, for the edit
        path, + the §15 soft option-catalog lint."""
        loaded = load_builder_guarded(self._store, self._audit, builder_id)
        if isinstance(loaded, dict):
            return loaded
        return {
            "ok": True, "id": loaded.id, "kind": loaded.kind, "name": loaded.name,
            "selections": dict(loaded.selections),
            "tags": {k: list(v) for k, v in loaded.tags.items()},
            "free_text": dict(loaded.free_text),
            "consent": loaded.consent,
            "created_at": loaded.created_at, "updated_at": loaded.updated_at,
            "issues": loaded.validate_against(self.catalog(loaded.kind)),
        }

    # -- create / update --------------------------------------------------------

    def create(self, payload: object) -> dict:
        """Validate a builder payload, build the record (which re-runs the
        content + consent + kind gates), persist it, and report a structured
        result."""
        if not isinstance(payload, dict):
            return {"ok": False, "kind": "invalid", "field": None,
                    "error": "malformed payload"}
        try:
            record = self._build_record(payload)
        except _Invalid as exc:
            return {"ok": False, "kind": "invalid", "field": exc.field,
                    "error": str(exc)}
        except ContentBlocked as exc:
            self._audit.log("filter_block", layer=1, category=exc.category,
                            context=f"builder.{exc.field_name}",
                            matched=exc.matched)
            return {"ok": False, "kind": "blocked", "field": exc.field_name,
                    "category": exc.category,
                    "error": f"blocked by the content policy ({exc.category})"}
        except ConsentError as exc:
            return {"ok": False, "kind": "consent", "field": "consent",
                    "error": str(exc)}
        except BuilderKindError as exc:
            return {"ok": False, "kind": "invalid", "field": "kind",
                    "error": str(exc)}
        issues = record.validate_against(self.catalog(record.kind))
        try:
            self._store.save(record)
        except OSError as exc:
            return {"ok": False, "kind": "io", "field": None,
                    "error": f"could not save the builder: {exc}"}
        self._audit.log("builder_created", id=record.id, builder_kind=record.kind)
        return {"ok": True, "id": record.id, "kind": record.kind,
                "name": record.name, "issues": issues}

    def update(self, builder_id: object, payload: object) -> dict:
        """Apply an edited payload to an existing builder. The record is REBUILT
        (so the content + consent + kind gates re-run — an edit cannot smuggle
        in what creation would refuse), preserving id/created_at. The kind is
        immutable across an edit (a persona cannot become a scenario and shed
        its consent requirement); any ``kind`` in the payload must match."""
        loaded = load_builder_guarded(self._store, self._audit, builder_id)
        if isinstance(loaded, dict):
            return loaded
        if not isinstance(payload, dict):
            return {"ok": False, "kind": "invalid", "field": None,
                    "error": "malformed payload"}
        payload = dict(payload)
        payload["kind"] = loaded.kind  # kind is fixed across an edit
        try:
            record = self._build_record(payload)
        except _Invalid as exc:
            return {"ok": False, "kind": "invalid", "field": exc.field,
                    "error": str(exc)}
        except ContentBlocked as exc:
            self._audit.log("filter_block", layer=1, category=exc.category,
                            context=f"builder.update.{exc.field_name}",
                            matched=exc.matched, builder_id=loaded.id)
            return {"ok": False, "kind": "blocked", "field": exc.field_name,
                    "category": exc.category,
                    "error": f"blocked by the content policy ({exc.category})"}
        except ConsentError as exc:
            return {"ok": False, "kind": "consent", "field": "consent",
                    "error": str(exc)}
        except BuilderKindError as exc:
            return {"ok": False, "kind": "invalid", "field": "kind",
                    "error": str(exc)}
        record.id = loaded.id
        record.created_at = loaded.created_at
        record.touch()
        issues = record.validate_against(self.catalog(record.kind))
        try:
            self._store.save(record)
        except OSError as exc:
            return {"ok": False, "kind": "io", "field": None,
                    "error": f"could not save the builder: {exc}"}
        self._audit.log("builder_updated", id=record.id, builder_kind=record.kind)
        return {"ok": True, "id": record.id, "kind": record.kind,
                "name": record.name, "issues": issues}

    def delete(self, builder_id: object) -> dict:
        """Remove the whole per-builder tree. Requires a valid id only, NOT a
        loadable record — deletion is the remedy for a corrupt/blocked one."""
        bid = str(builder_id or "").strip()
        if not bid:
            return {"ok": False, "kind": "invalid",
                    "error": "a builder id is required"}
        try:
            removed = self._store.delete(bid)
        except (InvalidId, ValueError):
            return {"ok": False, "kind": "not_found",
                    "error": f"no builder with id {bid!r}"}
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not delete builder {bid!r}: {exc}"}
        if removed:
            self._audit.log("builder_deleted", builder_id=bid)
        return {"ok": True, "id": bid, "removed": removed}

    # -- payload validation -----------------------------------------------------

    def _build_record(self, payload: dict) -> BuilderRecord:
        kind = str(payload.get("kind") or "").strip()
        if kind not in BUILDER_KINDS:
            raise _Invalid("kind", f"kind must be one of {BUILDER_KINDS}")
        catalog = self.catalog(kind)
        name = str(payload.get("name") or "").strip()
        if not name:
            raise _Invalid("name", "a name is required")
        if len(name) > NAME_MAX_LEN:
            raise _Invalid("name", f"name is too long (max {NAME_MAX_LEN} characters)")
        consent = None
        if kind == SCENARIO:
            consent = str(payload.get("consent") or "").strip() or None
            if consent is None:
                # A friendly doorway error; the record gate is the hard one.
                raise _Invalid("consent",
                               "a scenario requires an approved consent frame")
        return BuilderRecord.create(
            name=name,
            kind=kind,
            selections=self._check_selections(catalog, payload.get("selections") or {}),
            tags=self._check_tags(catalog, payload.get("tags") or {}),
            free_text=self._check_free_text(kind, payload.get("free_text") or {}),
            consent=consent,  # ConsentError surfaces if not approved
        )

    @staticmethod
    def _group_for(catalog: OptionCatalog, gid: str, channel: str) -> OptionGroup:
        group = catalog.get(gid)
        if group is None:
            raise _Invalid(f"{channel}.{gid}", f"unknown option group {gid!r}")
        return group

    def _check_selections(self, catalog: OptionCatalog, raw: object) -> dict[str, str]:
        if not isinstance(raw, dict):
            raise _Invalid("selections", "selections must be an object")
        out: dict[str, str] = {}
        for gid, value in raw.items():
            gid = str(gid)
            group = self._group_for(catalog, gid, "selections")
            if not group.is_selection or group.multi:
                raise _Invalid(f"selections.{gid}",
                               f"group {gid!r} is not single-select")
            value = str(value).strip()
            if not value:
                continue
            if not group.has_option(value):
                raise _Invalid(f"selections.{gid}",
                               f"unknown option {value!r} for group {gid!r}")
            out[gid] = value
        return out

    def _check_tags(self, catalog: OptionCatalog, raw: object) -> dict[str, list[str]]:
        if not isinstance(raw, dict):
            raise _Invalid("tags", "tags must be an object")
        out: dict[str, list[str]] = {}
        for gid, values in raw.items():
            gid = str(gid)
            group = self._group_for(catalog, gid, "tags")
            if not group.multi:
                raise _Invalid(f"tags.{gid}", f"group {gid!r} is not multi-select")
            if not isinstance(values, (list, tuple)):
                raise _Invalid(f"tags.{gid}", "tag values must be a list")
            seen: set[str] = set()
            picked: list[str] = []
            for value in values:
                value = str(value).strip()
                if not value or value in seen:
                    continue
                if not group.has_option(value):
                    raise _Invalid(f"tags.{gid}",
                                   f"unknown option {value!r} for group {gid!r}")
                seen.add(value)
                picked.append(value)
            if picked:
                out[gid] = picked
        return out

    @staticmethod
    def _check_free_text(kind: str, raw: object) -> dict[str, str]:
        if not isinstance(raw, dict):
            raise _Invalid("free_text", "free_text must be an object")
        allowed = {f["key"] for f in BUILDER_FREE_TEXT_FIELDS.get(kind, ())}
        out: dict[str, str] = {}
        for key, value in raw.items():
            key = str(key)
            if key not in allowed:
                raise _Invalid(f"free_text.{key}",
                               f"unknown free-text field {key!r} for kind {kind!r}")
            value = str(value)
            if len(value) > TEXT_MAX_LEN:
                raise _Invalid(f"free_text.{key}",
                               f"text is too long (max {TEXT_MAX_LEN} characters)")
            value = value.strip()
            if value:
                out[key] = value
        return out

    # -- reconciliation sweep (scene backgrounds) -------------------------------

    def reconcile(self) -> dict:
        """Sweep orphaned scene-background frames — a killed generation leaves a
        frame+sidecar the manifest never recorded (the 3g kill-window analogue).
        Fail-safe vouching model (mirrors LibraryService.reconcile): only ever
        delete our own artifact patterns, directly inside ``background/``, and
        only when a TRUSTED manifest fails to vouch (a corrupt manifest sweeps
        nothing; an absent one vouches for nothing). Idempotent."""
        total_orphans = 0
        total_freed = 0
        details: list[dict] = []
        skipped: list[dict] = []
        for bid in self._store.list_ids():
            try:
                res = self._reconcile_builder(bid)
            except Exception as exc:  # noqa: BLE001 — one fault never aborts the sweep
                self._audit.log("builder_sweep_failed", builder_id=bid,
                                error=str(exc))
                skipped.append({"id": bid, "skipped": "error"})
                continue
            if res.get("skipped"):
                skipped.append(res)
                continue
            total_orphans += res["orphans"]
            total_freed += res["bytes_freed"]
            if res["orphans"] or res.get("notes"):
                details.append(res)
                self._audit.log("builder_swept",
                                **{k: v for k, v in res.items() if k != "id"},
                                builder_id=bid)
        self._audit.log("builders_reconciled", builders=len(details),
                        skipped=len(skipped), orphans=total_orphans,
                        bytes_freed=total_freed)
        return {"ok": True, "orphans": total_orphans, "bytes_freed": total_freed,
                "builders": details, "skipped": skipped}

    def _reconcile_builder(self, bid: str) -> dict:
        out: dict = {"id": bid, "orphans": 0, "bytes_freed": 0, "notes": []}
        try:
            self._store.builder_dir(bid)
        except (InvalidId, ValueError, OSError):
            return {"id": bid, "skipped": "invalid_id"}
        keep = self._background_keep_names(bid, out["notes"])
        if keep is not None:
            orphans, freed = self._sweep_orphans(
                self._store.background_dir(bid), keep)
            out["orphans"] += orphans
            out["bytes_freed"] += freed
        out["notes"] = sorted(set(out["notes"]))
        return out

    def _background_keep_names(self, bid: str,
                               notes: list[str]) -> set[str] | None:
        """Filenames background.json vouches for (frame + same-stem sidecar).
        None (= sweep nothing) when the manifest is unreadable; an ABSENT
        manifest vouches for nothing (every background artifact is an orphan)."""
        try:
            manifest = self._store.load_background(bid)
        except ARTIFACT_LOAD_ERRORS:
            notes.append("background_corrupt")
            return None
        if manifest is not None and manifest.builder_id != bid:
            notes.append("background_corrupt")
            return None
        keep: set[str] = set()
        if manifest is not None:
            for entry in manifest.entries:
                base = os.path.basename(str(entry.path))
                if base:
                    keep.add(base)
                    keep.add(Path(base).stem + ".json")
        return keep

    @staticmethod
    def _sweep_orphans(directory: Path, keep: set[str]) -> tuple[int, int]:
        """Delete files in ``directory`` (non-recursive, our artifact patterns
        only) whose names the manifest does not vouch for."""
        if not directory.is_dir():
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


def build_builders(data_dir: Path | str, audit: AuditLog) -> BuilderService:
    """Assemble the builder service against a runtime data directory: records
    land under ``<data_dir>/builders`` (via BuilderStore); option files come
    from the bundled set plus user drop-ins under ``<data_dir>/builders/<kind>``."""
    data_dir = Path(data_dir)
    return BuilderService(BuilderStore(data_dir), audit, data_dir=data_dir)
