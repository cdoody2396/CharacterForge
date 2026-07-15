"""Creator service (Stage 2 — DECISIONS.md §10, §12, §15).

The bridge between the creator UI and the Stage-1 model. It does two things:

- ``describe()`` serializes the loaded option catalog for the page, so the
  form is entirely data-driven — a drop-in option file surfaces in the
  creator with no code change (§15). ``reload()`` re-scans the option
  directories so that works without an app restart.
- ``create_character(payload)`` turns a creator payload into a saved
  ``CharacterRecord``.

Validation stance: this is the UI's write path, so it is strict where the
record layer is deliberately soft — every selection/tag must name a loaded
group and a legal option of the right kind, sliders clamp to their group's
bounds, and free text is limited to the fixed field set below. The hard
gates (20+ age, Layer-1 content) live in the record itself and re-run on
every construction; this layer adds *shape* validation on top — safety never
depends on the UI behaving.
"""

from __future__ import annotations

import base64
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..audit import AuditLog
from ..model import (
    AgeError,
    CharacterRecord,
    CharacterStore,
    ContentBlocked,
    IdentityAnchor,
    MAX_AGE,
    MIN_AGE,
    MissingRequiredSelection,
    OptionCatalog,
    OptionGroup,
    derive_widget,
    load_option_catalog,
    resolve_within,
)
from ..model.options import BUNDLED_OPTIONS_DIR

MODES = ("quick", "detailed")
NAME_MAX_LEN = 120
TEXT_MAX_LEN = 20_000

# Option picker thumbnails (5.5c): bounded, CSP-displayable image types only.
_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}
_MAX_THUMB_BYTES = 512 * 1024

# The fixed free-text fields the creator offers (§10: structured tags +
# filtered free text). The *set* of fields is code-defined; their content is
# user text and passes Layer 1 on every path (live check + record gate).
FREE_TEXT_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "appearance_notes",
        "label": "Appearance notes",
        "section": "Appearance",
        "rows": 3,
        "hint": "Specifics the pickers can't express — where the scars sit, "
                "how the hair falls, what the tattoos depict.",
    },
    {
        "key": "personality_notes",
        "label": "Personality notes",
        "section": "Personality",
        "rows": 4,
        "hint": "Voice, quirks, speech patterns, how they treat people.",
    },
    {
        "key": "backstory",
        "label": "Backstory",
        "section": "Backstory",
        "rows": 7,
        "hint": "History, relationships, world context. Tags carry the vibe; "
                "this carries the specifics.",
    },
)
_FREE_TEXT_KEYS = tuple(f["key"] for f in FREE_TEXT_FIELDS)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Invalid(ValueError):
    """A payload shape/reference problem (not a safety gate)."""

    def __init__(self, field: str | None, message: str):
        self.field = field
        super().__init__(message)


def _option_payload(opt, image_resolver) -> dict:
    """One option as the UI consumes it: id + label, plus a swatch ``color``
    and/or a picker-tile ``image`` (a containment-resolved data URI) when the
    option file carried them (5.5c). Generation-side data (prompt/aliases)
    stays backend-side."""
    out: dict[str, Any] = {"id": opt.id, "label": opt.label}
    if opt.color:
        out["color"] = opt.color
    if opt.image:
        src = image_resolver(opt.image)
        if src:
            out["image"] = src
    return out


def _group_payload(group: OptionGroup, image_resolver) -> dict:
    """One group as the UI consumes it. Generation-side data (prompt
    fragments, aliases) stays backend-side on purpose. ``widget`` is the
    resolved creator widget (derivation applied, 5.5c) — the front-end renders
    it verbatim; ``required`` and ``prompt_ranges`` ride along for the required
    marker and the live slider band label."""
    return {
        "id": group.id,
        "label": group.label,
        "kind": group.kind,
        "field": group.field,
        "region": group.region,
        "attribute": group.attribute,
        "order": group.order,
        "section": group.section,
        "quick": group.quick,
        "required": group.required,
        "widget": derive_widget(group),
        "multi": group.multi,
        "options": [_option_payload(o, image_resolver) for o in group.options],
        "min": group.min,
        "max": group.max,
        "step": group.step,
        "default": group.default,
        "unit": group.unit,
        "prompt_ranges": [dict(r) for r in group.prompt_ranges],
    }


class CreatorService:
    """Owns the option catalog + character store on behalf of the creator UI."""

    def __init__(
        self,
        store: CharacterStore,
        audit: AuditLog,
        *,
        option_dirs: tuple[Path, ...] | list[Path] = (),
        include_bundled: bool = True,
    ):
        self._store = store
        self._audit = audit
        self._option_dirs = tuple(Path(d) for d in option_dirs)
        self._include_bundled = include_bundled
        self._catalog = self._load_catalog()
        # Lazy Stage-4 render-change detector (see _render_changed). Kept
        # None until the first edit so importing the creator stays light.
        self._prompt_assembler = None

    def _load_catalog(self) -> OptionCatalog:
        return load_option_catalog(
            self._option_dirs, include_bundled=self._include_bundled
        )

    @property
    def catalog(self) -> OptionCatalog:
        return self._catalog

    @property
    def store(self) -> CharacterStore:
        return self._store

    # -- catalog description --------------------------------------------------

    def describe(self) -> dict:
        """The option catalog + fixed free-text fields, shaped for the UI."""
        resolve = self._resolve_option_image
        return {
            "groups": [_group_payload(g, resolve) for g in self._catalog.groups()],
            "free_text_fields": [dict(f) for f in FREE_TEXT_FIELDS],
            "required_groups": list(self._catalog.required_group_ids()),
            "min_age": MIN_AGE,
            "max_age": MAX_AGE,
            "name_max_len": NAME_MAX_LEN,
            "text_max_len": TEXT_MAX_LEN,
            "errors": [{"file": f, "error": e} for f, e in self._catalog.errors],
        }

    def _resolve_option_image(self, rel: str) -> str | None:
        """Resolve an option's ``image`` (a path relative to an option
        directory) to an inline ``data:`` URI, or None. Every option directory
        (bundled + user drop-ins) is tried through ``resolve_within`` — the
        same containment rule the stores use — so a hand-authored ``..`` /
        absolute / symlink-escape path can never read outside the option tree.
        Bounded in size (a thumbnail, not a full render) and restricted to
        image types the CSP's ``img-src 'self' data:`` will display; anything
        larger, missing, unreadable, or non-image reads as no thumbnail rather
        than raising — a bad path must never break the whole catalog."""
        for base in (*self._option_dirs, BUNDLED_OPTIONS_DIR):
            resolved = resolve_within(base, rel)
            if resolved is None:
                continue
            suffix = resolved.suffix.lower()
            mime = _IMAGE_MIME.get(suffix)
            if mime is None:
                return None
            try:
                if resolved.stat().st_size > _MAX_THUMB_BYTES:
                    return None
                raw = resolved.read_bytes()
            except OSError:
                return None
            return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        return None

    def _required_group_ids(self) -> tuple[str, ...]:
        """The catalog's required-selection set, passed to record construction
        so a new/edited character is gated on the render-identity minimum."""
        return self._catalog.required_group_ids()

    def reload(self) -> dict:
        """Re-scan the option directories (a freshly dropped-in data file
        surfaces without restarting the app, §15) and return describe()."""
        self._catalog = self._load_catalog()
        return self.describe()

    # -- record creation -------------------------------------------------------

    def create_character(self, payload: object) -> dict:
        """Validate a creator payload, build the record (which re-runs the
        age + Layer-1 gates), persist it, and report a structured result the
        UI can map back onto its fields."""
        if not isinstance(payload, dict):
            return {"ok": False, "kind": "invalid", "field": None,
                    "error": "malformed payload"}
        mode = payload.get("mode")
        try:
            if mode not in MODES:
                raise _Invalid("mode", f"mode must be one of {MODES}")
            record = self._build_record(payload)
        except _Invalid as exc:
            return {"ok": False, "kind": "invalid", "field": exc.field,
                    "error": str(exc)}
        except MissingRequiredSelection as exc:
            return {"ok": False, "kind": "required",
                    "field": f"selections.{exc.group_id}",
                    "error": f"{exc.group_id} is required"}
        except ContentBlocked as exc:
            # Layer-4 trail for a Layer-1 block on the record path (the live
            # check_text path audits separately in the shell Api).
            self._audit.log(
                "filter_block",
                layer=1,
                category=exc.category,
                context=f"creator.{exc.field_name}",
                matched=exc.matched,
            )
            return {"ok": False, "kind": "blocked", "field": exc.field_name,
                    "category": exc.category,
                    "error": f"blocked by the content policy ({exc.category})"}
        except AgeError as exc:
            return {"ok": False, "kind": "age", "field": "age", "error": str(exc)}

        # Soft lint — empty by construction (everything above validated
        # against the same catalog); reported so a discrepancy is visible.
        issues = record.validate_against(self._catalog)
        try:
            self._store.save(record)
        except OSError as exc:
            # Disk full / AV lock: report it structured, never a raw bridge
            # rejection (the set_setting stance).
            return {"ok": False, "kind": "io", "field": None,
                    "error": f"could not save the character: {exc}"}
        self._audit.log("character_created", id=record.id, mode=mode)
        return {"ok": True, "id": record.id, "name": record.name,
                "mode": mode, "issues": issues}

    # -- record editing (Stage 4, §14) ------------------------------------------

    def update_character(self, character_id: object, payload: object) -> dict:
        """Apply an edited creator payload to an existing record. The payload
        passes the same strict shape validation as creation and the record is
        REBUILT (so the age + Layer-1 gates re-run — an edit cannot smuggle
        in what creation would refuse), preserving the immutable parts: id,
        created_at, and the identity anchor (reference/LoRA state — §14
        editing never touches trained identity).

        When the edit changes what the character *renders* as, the catalog
        and cache manifests are marked stale (§14): the user is told frames
        no longer match, and regeneration is offered — never forced — by the
        UI. A personality-notes edit does not invalidate frames. Any ``mode``
        key in the payload is ignored (create-path concern).

        §15 source-of-truth: values referencing an option group NOT in the
        current catalog (a drop-in file removed or failing to load) are
        carried forward from the stored record — the form can neither show
        nor round-trip them and the strict payload check would reject them,
        so without this an unrelated edit (fixing a name typo) would
        silently strip them. They came from an already-gated record, so
        they are safe; ``get_character`` surfaces them as soft issues."""
        from .library import load_record_guarded  # local: avoid import cycle

        loaded = load_record_guarded(self._store, self._audit, character_id,
                                     context="creator.update")
        if isinstance(loaded, dict):
            return loaded
        if not isinstance(payload, dict):
            return {"ok": False, "kind": "invalid", "field": None,
                    "error": "malformed payload"}
        try:
            record = self._build_record(payload, identity=loaded.identity)
        except _Invalid as exc:
            return {"ok": False, "kind": "invalid", "field": exc.field,
                    "error": str(exc)}
        except MissingRequiredSelection as exc:
            return {"ok": False, "kind": "required",
                    "field": f"selections.{exc.group_id}",
                    "error": f"{exc.group_id} is required"}
        except ContentBlocked as exc:
            self._audit.log(
                "filter_block",
                layer=1,
                category=exc.category,
                context=f"creator.update.{exc.field_name}",
                matched=exc.matched,
                character_id=loaded.id,
            )
            return {"ok": False, "kind": "blocked", "field": exc.field_name,
                    "category": exc.category,
                    "error": f"blocked by the content policy ({exc.category})"}
        except AgeError as exc:
            return {"ok": False, "kind": "age", "field": "age",
                    "error": str(exc)}

        record.id = loaded.id
        record.created_at = loaded.created_at
        self._carry_unknown_group_values(loaded, record)
        record.touch()
        render_changed = self._render_changed(loaded, record)
        issues = record.validate_against(self._catalog)
        try:
            self._store.save(record)
        except OSError as exc:
            return {"ok": False, "kind": "io", "field": None,
                    "error": f"could not save the character: {exc}"}
        stale_marked = {"catalog": False, "cache": False}
        if render_changed:
            stale_marked = self._mark_stale(record.id)
        self._audit.log("character_updated", id=record.id,
                        render_changed=render_changed,
                        stale_catalog=stale_marked["catalog"],
                        stale_cache=stale_marked["cache"])
        return {"ok": True, "id": record.id, "name": record.name,
                "render_changed": render_changed,
                "stale_marked": stale_marked, "issues": issues}

    def _carry_unknown_group_values(
        self, old: CharacterRecord, new: CharacterRecord
    ) -> None:
        """Merge into ``new`` any selection/tag/slider values from ``old``
        whose option group is absent from the current catalog (§15 source-
        of-truth — see update_character). Only groups the payload could not
        legitimately carry are restored, and only when the edited payload
        did not itself re-set that group. The values are already-gated
        (they rode a valid loaded record); the dicts are mutated after
        construction, which is safe because they are a subset of a record
        that passed the gates on load."""
        catalog = self._catalog
        for gid, val in old.selections.items():
            if catalog.get(gid) is None and gid not in new.selections:
                new.selections[gid] = val
        for gid, vals in old.tags.items():
            if catalog.get(gid) is None and gid not in new.tags:
                new.tags[gid] = list(vals)
        for gid, val in old.sliders.items():
            if catalog.get(gid) is None and gid not in new.sliders:
                new.sliders[gid] = val

    def _render_changed(self, old: CharacterRecord,
                        new: CharacterRecord) -> bool:
        """Whether an edit changes what the character renders as — compared
        on the assembled positive prompt, the single source of render truth
        (option fragments, slider prompt_ranges, appearance notes, the age
        fragment; ``render: false`` groups like personality/voice never
        appear in it). Any failure to assemble or even build the assembler
        (a blocklist tightened since creation → PromptBlocked; the prompt
        data files vanishing/locking mid-run → OSError building it) is
        inconclusive and conservatively reads as changed — it must never
        raise back through the bridge nor abort the pending save (review
        catch: the lazy ``PromptAssembler()`` read was unguarded)."""
        from ..imagegen.prompt import PromptBlocked

        try:
            if self._prompt_assembler is None:
                from ..imagegen.prompt import PromptAssembler
                self._prompt_assembler = PromptAssembler()
            before = self._prompt_assembler.assemble(old, self._catalog)
            after = self._prompt_assembler.assemble(new, self._catalog)
        except PromptBlocked:
            return True
        except OSError:
            return True
        return before.positive != after.positive

    def _mark_stale(self, character_id: str) -> dict:
        """Best-effort §14 staleness on the catalog + cache manifests (when
        they exist and have entries). Refreshing ``updated_at`` deliberately
        invalidates the 3f/3g optimistic tokens — the manifest changed. A
        corrupt or unwritable manifest reads as unmarked, never fatal (the
        record save already succeeded); an already-stale manifest counts as
        marked without a rewrite."""
        from ..imagegen.service import ARTIFACT_LOAD_ERRORS

        marked = {"catalog": False, "cache": False}
        for channel, loader, saver in (
            ("catalog", self._store.load_catalog, self._store.save_catalog),
            ("cache", self._store.load_cache, self._store.save_cache),
        ):
            try:
                manifest = loader(character_id)
            except ARTIFACT_LOAD_ERRORS:
                continue
            if (manifest is None or not manifest.entries
                    or manifest.character_id != character_id):
                continue
            if manifest.stale:
                marked[channel] = True
                continue
            manifest.stale = True
            manifest.updated_at = _now_iso()
            try:
                saver(manifest)
            except OSError:
                continue
            marked[channel] = True
        return marked

    # -- payload validation ----------------------------------------------------

    def _build_record(
        self, payload: dict, *, identity: IdentityAnchor | None = None,
        required_groups: frozenset[str] | tuple | None = None,
    ) -> CharacterRecord:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise _Invalid("name", "a name is required")
        if len(name) > NAME_MAX_LEN:
            raise _Invalid("name", f"name is too long (max {NAME_MAX_LEN} characters)")
        age = payload.get("age")
        if age is None or (isinstance(age, str) and not age.strip()):
            raise _Invalid("age", "an age is required")
        return CharacterRecord.create(
            name=name,
            age=age,  # Age.coerce inside — AgeError surfaces as kind "age"
            selections=self._check_selections(payload.get("selections") or {}),
            tags=self._check_tags(payload.get("tags") or {}),
            sliders=self._check_sliders(payload.get("sliders") or {}),
            free_text=self._check_free_text(payload.get("free_text") or {}),
            identity=identity,  # Stage-4 edit: the anchor survives the edit
            # 5.5c render-identity minimum: a character cannot be constructed
            # (created OR edited) without every required group. Catalog-driven,
            # so a drop-in file that marks a new group required is enforced with
            # no code change. The preview path passes () — a partial form must
            # preview (the gate stays on every PERSISTING path).
            required_groups=(self._required_group_ids()
                             if required_groups is None else required_groups),
        )

    def preview_record(self, payload: object) -> CharacterRecord | dict:
        """Build a TRANSIENT record from an in-progress creator form for the
        live prompt preview (5.5 acceptance: the panel was dead during create
        because image_prompt_preview needs a SAVED id). Nothing is persisted;
        a partial form previews (no required-selection gate; a placeholder
        name when empty) — but every OTHER gate still runs: strict payload
        shape, the age hard gate (Layer 3), and the Layer-1 content gates.
        Returns the record, or the same structured error dict create returns."""
        if not isinstance(payload, dict):
            return {"ok": False, "kind": "invalid", "field": None,
                    "error": "malformed payload"}
        preview = dict(payload)
        if not str(preview.get("name") or "").strip():
            preview["name"] = "Preview"  # display-only; never persisted
        try:
            return self._build_record(preview, required_groups=())
        except _Invalid as exc:
            return {"ok": False, "kind": "invalid", "field": exc.field,
                    "error": str(exc)}
        except ContentBlocked as exc:
            self._audit.log(
                "filter_block",
                layer=1,
                category=exc.category,
                context=f"creator.preview.{exc.field_name}",
                matched=exc.matched,
            )
            return {"ok": False, "kind": "blocked", "field": exc.field_name,
                    "category": exc.category,
                    "error": f"blocked by the content policy ({exc.category})"}
        except AgeError as exc:
            return {"ok": False, "kind": "age", "field": "age",
                    "error": str(exc)}

    def _group_for(self, gid: str, channel: str) -> OptionGroup:
        group = self._catalog.get(gid)
        if group is None:
            raise _Invalid(f"{channel}.{gid}", f"unknown option group {gid!r}")
        if group.field == "age":
            # The age group exists to advertise bounds to the UI; the value
            # itself lives on the record's typed age field (Layer 3), never
            # in selections/sliders.
            raise _Invalid(f"{channel}.{gid}", "age is set via the age field")
        return group

    def _check_selections(self, raw: object) -> dict[str, str]:
        if not isinstance(raw, dict):
            raise _Invalid("selections", "selections must be an object")
        out: dict[str, str] = {}
        for gid, value in raw.items():
            gid = str(gid)
            group = self._group_for(gid, "selections")
            if not group.is_selection or group.multi:
                raise _Invalid(f"selections.{gid}",
                               f"group {gid!r} is not single-select")
            value = str(value).strip()
            if not value:
                continue  # unset
            if not group.has_option(value):
                raise _Invalid(f"selections.{gid}",
                               f"unknown option {value!r} for group {gid!r}")
            out[gid] = value
        return out

    def _check_tags(self, raw: object) -> dict[str, list[str]]:
        if not isinstance(raw, dict):
            raise _Invalid("tags", "tags must be an object")
        out: dict[str, list[str]] = {}
        for gid, values in raw.items():
            gid = str(gid)
            group = self._group_for(gid, "tags")
            if not group.multi:
                raise _Invalid(f"tags.{gid}",
                               f"group {gid!r} is not multi-select")
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

    def _check_sliders(self, raw: object) -> dict[str, float]:
        if not isinstance(raw, dict):
            raise _Invalid("sliders", "sliders must be an object")
        out: dict[str, float] = {}
        for gid, value in raw.items():
            gid = str(gid)
            group = self._group_for(gid, "sliders")
            if not group.is_numeric:
                raise _Invalid(f"sliders.{gid}",
                               f"group {gid!r} is not a numeric group")
            if isinstance(value, bool):
                raise _Invalid(f"sliders.{gid}", "slider value must be a number")
            try:
                # OverflowError: a JSON integer literal can exceed float range
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                raise _Invalid(f"sliders.{gid}",
                               "slider value must be a number") from None
            if not math.isfinite(number):
                # NaN/inf slip past clamp() and NaN even survives json.dumps
                raise _Invalid(f"sliders.{gid}", "slider value must be finite")
            number = group.clamp(number)
            if not math.isfinite(number):
                # belt-and-braces: bounds are load-validated finite, but a
                # non-finite clamp result must never reach disk
                raise _Invalid(f"sliders.{gid}", "slider bounds are not finite")
            out[gid] = int(number) if number.is_integer() else number
        return out

    def _check_free_text(self, raw: object) -> dict[str, str]:
        if not isinstance(raw, dict):
            raise _Invalid("free_text", "free_text must be an object")
        out: dict[str, str] = {}
        for key, value in raw.items():
            key = str(key)
            if key not in _FREE_TEXT_KEYS:
                raise _Invalid(f"free_text.{key}",
                               f"unknown free-text field {key!r}")
            value = str(value)
            if len(value) > TEXT_MAX_LEN:
                raise _Invalid(f"free_text.{key}",
                               f"text is too long (max {TEXT_MAX_LEN} characters)")
            value = value.strip()
            if value:
                out[key] = value
        return out


def build_creator(data_dir: Path | str, audit: AuditLog) -> CreatorService:
    """Assemble the creator against a runtime data directory: bundled option
    files plus user drop-ins in ``<data_dir>/options``; records land under
    ``<data_dir>/characters`` (via CharacterStore)."""
    data_dir = Path(data_dir)
    return CreatorService(
        store=CharacterStore(data_dir),
        audit=audit,
        option_dirs=(data_dir / "options",),
    )
