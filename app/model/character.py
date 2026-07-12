"""Character record schema (DECISIONS.md §5, §6, §10, §12).

A record is a structured prompt + a per-character identity anchor:
  - `selections`  single-choice fields (race, body type, categorical anatomy)
  - `tags`        multi-choice fields (personality traits, kinks, wardrobe)
  - `sliders`     continuous axes reserved to height / weight / muscle (§12)
  - `free_text`   filtered prose (backstory, personality, appearance notes)
  - `age`         an `Age` (>= 20, structurally — see age.py)
  - `identity`    IdentityAnchor (has-LoRA, reference/LoRA paths, footprint)

Invariants enforced at every construction AND mutation path, so a record can
never exist in a blocked or under-age state — including after a hand-edited
file is loaded:
  - `age` is always an `Age`; assigning a raw/under-20 value re-runs the floor
    (Layer 3) and raises. `Age` has no sub-20 representation.
  - `id` is always a safe single path segment (no separators / `..` / drive),
    so a tampered id cannot make the store write or delete outside itself.
  - `name` and every free_text / selection / tag / slider key and every
    free_text / selection / tag value pass the Stage-0 Layer-1 filter, so
    prohibited text cannot be stored on any channel.

The catalog itself is produced later (Stage 3) using the identity anchor;
`CatalogManifest` fixes its shape now and rides alongside the record.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..safety import Layer1Filter, get_filter
from .age import Age

SCHEMA_VERSION = 1


class ContentBlocked(ValueError):
    """A field's text was rejected by the Layer-1 filter."""

    def __init__(self, field_name: str, category: str | None, matched: str | None):
        self.field_name = field_name
        self.category = category
        self.matched = matched
        super().__init__(
            f"field {field_name!r} blocked by content policy "
            f"(category={category}, matched={matched!r})"
        )


class InvalidId(ValueError):
    """A character id is not a safe single path segment."""


def ensure_safe_id(value: object) -> str:
    """Return ``value`` as a string if it is a safe single path segment,
    else raise. Rejects empty, '.'/'..', anything containing a path separator
    or drive marker — so an id from a caller or a hand-edited file cannot make
    the store escape its own directory."""
    s = str(value)
    if (
        not s
        or s in (".", "..")
        or "/" in s
        or "\\" in s
        or ":" in s
        or "\x00" in s
        or os.path.basename(s) != s
    ):
        raise InvalidId(f"unsafe character id: {value!r}")
    return s


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_number(value: object) -> float | int:
    """Numeric slider value normalized so int-valued numbers stay ints
    (keeps save/load idempotent: 172 does not become 172.0)."""
    try:
        f = float(value)  # raises TypeError/ValueError on non-numeric
    except OverflowError:
        # a hand-edited huge integer should fail like any other bad number
        raise ValueError(f"slider value out of range: {value!r}") from None
    return int(f) if f.is_integer() else f


@dataclass
class Footprint:
    """Per-character disk accounting (§14). Bytes, computed/managed by the
    store and Stage-4 management; the shape lands here."""

    lora_bytes: int = 0
    catalog_bytes: int = 0
    cache_bytes: int = 0

    @property
    def total_bytes(self) -> int:
        return self.lora_bytes + self.catalog_bytes + self.cache_bytes

    def to_dict(self) -> dict:
        return {
            "lora_bytes": self.lora_bytes,
            "catalog_bytes": self.catalog_bytes,
            "cache_bytes": self.cache_bytes,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Footprint":
        data = data or {}
        return cls(
            lora_bytes=int(data.get("lora_bytes", 0)),
            catalog_bytes=int(data.get("catalog_bytes", 0)),
            cache_bytes=int(data.get("cache_bytes", 0)),
        )


@dataclass
class IdentityAnchor:
    """The per-character identity state (§6). Quick-create rides on the
    reference image via IP-Adapter; detailed-create may promote to a trained
    LoRA, flipping `has_lora`."""

    has_lora: bool = False
    reference_image_path: str | None = None
    lora_path: str | None = None
    footprint: Footprint = field(default_factory=Footprint)

    def to_dict(self) -> dict:
        return {
            "has_lora": self.has_lora,
            "reference_image_path": self.reference_image_path,
            "lora_path": self.lora_path,
            "footprint": self.footprint.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "IdentityAnchor":
        data = data or {}
        return cls(
            has_lora=bool(data.get("has_lora", False)),
            reference_image_path=data.get("reference_image_path"),
            lora_path=data.get("lora_path"),
            footprint=Footprint.from_dict(data.get("footprint")),
        )


@dataclass
class CatalogEntry:
    """One rendered frame's manifest row (§7). Stage 3 fills these.
    ``last_used`` is the Stage-4 LRU signal (§14), recorded from Stage 3g on
    for on-demand cache entries (creation + every cache hit); seed-catalog
    entries leave it None. Additive — old manifests load unchanged."""

    frame_id: str
    path: str
    state: dict[str, Any] = field(default_factory=dict)  # expression/pose/outfit
    matted_path: str | None = None
    on_demand: bool = False
    bytes: int = 0
    last_used: str | None = None

    def to_dict(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "path": self.path,
            "state": self.state,
            "matted_path": self.matted_path,
            "on_demand": self.on_demand,
            "bytes": self.bytes,
            "last_used": self.last_used,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CatalogEntry":
        # A non-dict entry (a natural hand-edit: `"entries": [null]`) must
        # raise a loader-guarded type, never AttributeError from .get —
        # review catch: the last_used read reordered evaluation ahead of the
        # ["frame_id"] subscript and turned the guarded TypeError into an
        # unguarded AttributeError through every manifest bridge.
        if not isinstance(data, dict):
            raise ValueError(
                f"catalog entry must be an object, got {type(data).__name__}")
        raw_last = data.get("last_used")
        return cls(
            frame_id=str(data["frame_id"]),
            path=str(data["path"]),
            # str-normalize both sides (the record's __post_init__ stance):
            # our writers only ever store strings, and a hand-edited
            # Infinity/NaN float would otherwise ride this dict verbatim
            # into a bridge payload as invalid JSON (red-team catch).
            state={str(k): str(v)
                   for k, v in dict(data.get("state", {})).items()},
            matted_path=data.get("matted_path"),
            on_demand=bool(data.get("on_demand", False)),
            bytes=int(data.get("bytes", 0)),
            last_used=str(raw_last) if raw_last is not None else None,
        )


@dataclass
class CatalogManifest:
    """The per-character image catalog manifest (§7). Separate artifact from
    the record; produced by Stage 3 using the identity anchor. `stale` marks
    frames as no longer matching an edited record (§14)."""

    character_id: str
    entries: list[CatalogEntry] = field(default_factory=list)
    stale: bool = False
    # Stage-3f matting run provenance (raw dict, like BootstrapManifest.params;
    # None until a matte run writes it). Purely additive — old manifests load
    # with None and no schema_version bump is needed.
    matting: dict | None = None
    updated_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        # A manifest id also drives store paths — keep it confined.
        self.character_id = ensure_safe_id(self.character_id)

    def total_bytes(self) -> int:
        return sum(e.bytes for e in self.entries)

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "character_id": self.character_id,
            "stale": self.stale,
            "matting": self.matting,
            "updated_at": self.updated_at,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CatalogManifest":
        return cls(
            character_id=str(data["character_id"]),
            entries=[CatalogEntry.from_dict(e) for e in data.get("entries", [])],
            stale=bool(data.get("stale", False)),
            matting=data.get("matting"),
            updated_at=str(data.get("updated_at", _now_iso())),
        )


@dataclass
class CharacterRecord:
    name: str
    age: Age
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: int = SCHEMA_VERSION
    selections: dict[str, str] = field(default_factory=dict)
    tags: dict[str, list[str]] = field(default_factory=dict)
    sliders: dict[str, float] = field(default_factory=dict)
    free_text: dict[str, str] = field(default_factory=dict)
    identity: IdentityAnchor = field(default_factory=IdentityAnchor)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    # -- attribute-level invariants ----------------------------------------

    def __setattr__(self, name: str, value: Any) -> None:
        # age is always an Age: a raw/under-20 assignment re-runs the floor.
        if name == "age" and not isinstance(value, Age):
            value = Age.coerce(value)
        # id is always a safe single path segment.
        elif name == "id":
            value = ensure_safe_id(value)
        super().__setattr__(name, value)

    def __post_init__(self) -> None:
        # Single normalization choke point — every construction path (direct,
        # create, from_dict/load) funnels here before the content gates run.
        self.name = str(self.name)
        self.selections = {str(k): str(v) for k, v in self.selections.items()}
        # A bare string tag value is a natural hand-edit; wrap it, never
        # explode it into single characters.
        self.tags = {
            str(k): ([str(v)] if isinstance(v, str) else [str(x) for x in v])
            for k, v in self.tags.items()
        }
        self.sliders = {str(k): _norm_number(v) for k, v in self.sliders.items()}
        self.free_text = {str(k): str(v) for k, v in self.free_text.items()}
        self._run_content_gates(get_filter())

    # -- gates --------------------------------------------------------------

    def _run_content_gates(self, filt: Layer1Filter) -> None:
        self._gate(filt, "name", self.name, context="name")
        for key, value in self.free_text.items():
            self._gate(filt, "free_text.key", key)
            self._gate(filt, f"free_text.{key}", value)
        # Selection/tag values are discrete tokens headed for image-prompt
        # assembly (Stage 3), not prose — proximity logic can never trip on a
        # lone token, so they get the strict prompt context, where contextual
        # terms ("child", "forced", ...) block outright.
        for key, value in self.selections.items():
            self._gate(filt, "selections.key", key)
            self._gate(filt, f"selections.{key}", value, context="prompt")
        for key, values in self.tags.items():
            self._gate(filt, "tags.key", key)
            for value in values:
                self._gate(filt, f"tags.{key}", value, context="prompt")
        for key in self.sliders:
            # values are numbers, but the keys are text and persist too
            self._gate(filt, "sliders.key", key)

    @staticmethod
    def _gate(
        filt: Layer1Filter, field_name: str, text: str, *, context: str = "freetext"
    ) -> None:
        result = filt.check(text, context=context)
        if not result.allowed:
            raise ContentBlocked(field_name, result.category, result.matched)

    # -- convenience factory ------------------------------------------------

    @classmethod
    def create(
        cls,
        name: str,
        age: object,
        *,
        selections: dict[str, str] | None = None,
        tags: dict[str, list[str]] | None = None,
        sliders: dict[str, float] | None = None,
        free_text: dict[str, str] | None = None,
        identity: IdentityAnchor | None = None,
    ) -> "CharacterRecord":
        return cls(
            name=name,
            age=Age.coerce(age),
            selections=dict(selections or {}),
            tags=dict(tags or {}),
            sliders=dict(sliders or {}),
            free_text=dict(free_text or {}),
            identity=identity or IdentityAnchor(),
        )

    def touch(self) -> None:
        self.updated_at = _now_iso()

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "age": int(self.age),
            "selections": dict(self.selections),
            "tags": {k: list(v) for k, v in self.tags.items()},
            "sliders": dict(self.sliders),
            "free_text": dict(self.free_text),
            "identity": self.identity.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CharacterRecord":
        if "name" not in data or "age" not in data:
            raise ValueError("character record requires 'name' and 'age'")
        # Containers pass through raw; __post_init__ normalizes and gates them.
        return cls(
            name=data["name"],
            age=Age.coerce(data["age"]),
            id=str(data.get("id", uuid.uuid4().hex)),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            selections=dict(data.get("selections", {})),
            tags=dict(data.get("tags", {})),
            sliders=dict(data.get("sliders", {})),
            free_text=dict(data.get("free_text", {})),
            identity=IdentityAnchor.from_dict(data.get("identity")),
            created_at=str(data.get("created_at", _now_iso())),
            updated_at=str(data.get("updated_at", _now_iso())),
        )

    # -- validation against a loaded option catalog (soft) ------------------

    def validate_against(self, catalog) -> list[str]:
        """Return a list of human-readable issues: selections/tags/sliders that
        reference unknown groups or options. Soft — the record is still the
        source of truth (options can be added later); this is a lint, not a
        gate. The age and content gates are the hard ones and already ran."""
        issues: list[str] = []
        for gid, val in self.selections.items():
            group = catalog.get(gid)
            if group is None:
                issues.append(f"selection {gid!r}: unknown option group")
            elif not group.has_option(val):
                issues.append(f"selection {gid!r}: unknown option {val!r}")
        for gid, vals in self.tags.items():
            group = catalog.get(gid)
            if group is None:
                issues.append(f"tags {gid!r}: unknown option group")
                continue
            for v in vals:
                if not group.has_option(v):
                    issues.append(f"tags {gid!r}: unknown option {v!r}")
        for gid, val in self.sliders.items():
            group = catalog.get(gid)
            if group is None:
                issues.append(f"slider {gid!r}: unknown option group")
            elif not group.is_numeric:
                issues.append(f"slider {gid!r}: group is not numeric")
        return issues
