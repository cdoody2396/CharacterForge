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

import math
from pathlib import Path
from typing import Any

from ..audit import AuditLog
from ..model import (
    AgeError,
    CharacterRecord,
    CharacterStore,
    ContentBlocked,
    MAX_AGE,
    MIN_AGE,
    OptionCatalog,
    OptionGroup,
    load_option_catalog,
)

MODES = ("quick", "detailed")
NAME_MAX_LEN = 120
TEXT_MAX_LEN = 20_000

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


class _Invalid(ValueError):
    """A payload shape/reference problem (not a safety gate)."""

    def __init__(self, field: str | None, message: str):
        self.field = field
        super().__init__(message)


def _group_payload(group: OptionGroup) -> dict:
    """One group as the UI consumes it. Generation-side data (prompt
    fragments, aliases) stays backend-side on purpose."""
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
        "multi": group.multi,
        "options": [
            {"id": o.id, "label": o.label, **({"color": o.color} if o.color else {})}
            for o in group.options
        ],
        "min": group.min,
        "max": group.max,
        "step": group.step,
        "default": group.default,
        "unit": group.unit,
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
        return {
            "groups": [_group_payload(g) for g in self._catalog.groups()],
            "free_text_fields": [dict(f) for f in FREE_TEXT_FIELDS],
            "min_age": MIN_AGE,
            "max_age": MAX_AGE,
            "name_max_len": NAME_MAX_LEN,
            "text_max_len": TEXT_MAX_LEN,
            "errors": [{"file": f, "error": e} for f, e in self._catalog.errors],
        }

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
        self._store.save(record)
        self._audit.log("character_created", id=record.id, mode=mode)
        return {"ok": True, "id": record.id, "name": record.name,
                "mode": mode, "issues": issues}

    # -- payload validation ----------------------------------------------------

    def _build_record(self, payload: dict) -> CharacterRecord:
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
        )

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
