"""Builder record schema — personas / scenes / events / scenarios
(DECISIONS.md §13, §10, §11).

A *lighter* structured record than the character engine (§13): the same
tags + filtered-free-text mechanism, but **no** age / anatomy / sliders /
identity anchor / LoRA. One dataclass with a ``kind`` discriminator carries
all four builder types; they differ structurally in only two ways:

  - **scenario** carries a required, code-anchored **consent frame** (Layer 3).
  - **scene** owns generated background imagery (a ``BackgroundManifest``,
    produced by Stage 5's scene image pipeline — the record fixes its shape
    now and rides alongside, exactly as ``CatalogManifest`` does for a
    character).

Invariants enforced at every construction AND mutation path, so a record can
never exist in a blocked or unconstrained state — including after a hand-edited
``builder.json`` is loaded (load re-runs ``__post_init__``):

  - ``id`` is a safe single path segment (``ensure_safe_id``) — a tampered id
    cannot make the store write or delete outside itself.
  - ``kind`` is one of ``BUILDER_KINDS``; an unknown/hand-edited kind raises,
    so a kind cannot be flipped to dodge the consent gate.
  - ``consent`` is always ``None`` or one of ``APPROVED_CONSENT_FRAMES`` (the
    set is a **code constant**, the single source of truth — a drop-in option
    file only *advertises* the ids to the UI and can never widen the gate,
    verbatim the ``age.py`` stance). A ``scenario`` with no approved consent
    frame is **unconstructable** — non-consent is not "caught," it cannot be
    represented. (Non-consent *phrasing* in free text remains Layer 1's job on
    every kind; the two together are the defense.)
  - ``name`` and every free_text / selection / tag key and value pass the
    Stage-0 Layer-1 filter, so prohibited text cannot be stored on any channel.
    The 20+ protection needs no ``Age`` field here: a sub-20 assertion in any
    builder text is blocked by Layer 1 (freetext / strict prompt context).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..safety import Layer1Filter, get_filter
from .character import ContentBlocked, ensure_safe_id

SCHEMA_VERSION = 1

# The four builder kinds (§13). Closed set — construction gates against it.
BUILDER_KINDS = ("persona", "scene", "event", "scenario")

# The kinds that carry structural extras: scene owns generated background
# imagery; scenario owns the consent gate (§13, §11).
SCENE = "scene"
SCENARIO = "scenario"

# The frozen Layer-3 affirmative-consent vocabulary (user-approved). A
# scenario MUST carry one of these; there is no representation for non-consent.
# This is the single source of truth — the scenario option data file only
# advertises the same ids to the creator UI (mirrors MIN_AGE vs the age file).
APPROVED_CONSENT_FRAMES = (
    "enthusiastic",
    "established_relationship",
    "negotiated_scene",
    "romantic",
)

# UI labels for the approved frames — advertised from CODE (not a drop-in
# option file), so the set the creator can offer and the set the record gate
# accepts are one and the same and a data file can neither widen nor rename it.
CONSENT_FRAME_LABELS: dict[str, str] = {
    "enthusiastic": "Enthusiastic — clear, eager mutual consent",
    "established_relationship": "Established relationship — ongoing partners",
    "negotiated_scene": "Negotiated scene — pre-agreed boundaries (BDSM/D-s)",
    "romantic": "Romantic — courtship / dating framing",
}


def approved_consent_frames() -> list[dict]:
    """The approved consent vocabulary as ``[{id, label}]`` for the UI —
    built from the code constant so it can never drift from the gate."""
    return [{"id": cid, "label": CONSENT_FRAME_LABELS.get(cid, cid)}
            for cid in APPROVED_CONSENT_FRAMES]


class BuilderKindError(ValueError):
    """A builder kind is not one of ``BUILDER_KINDS``."""


class ConsentError(ValueError):
    """A scenario lacks an approved consent frame, or a consent value is not
    in ``APPROVED_CONSENT_FRAMES`` (Layer 3 — the state is unconstructable)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Scene background imagery manifest (§13). Light witness — separate from the
# character-flavored CatalogManifest. Produced by Stage-5 scene generation;
# the shape lands here and the reconcile sweep vouches against it.
# ---------------------------------------------------------------------------


@dataclass
class BackgroundEntry:
    """One generated background frame's manifest row."""

    frame_id: str
    path: str  # builder-relative
    state: dict[str, Any] = field(default_factory=dict)  # scene state tokens
    bytes: int = 0
    created_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "path": self.path,
            "state": self.state,
            "bytes": self.bytes,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BackgroundEntry":
        if not isinstance(data, dict):
            raise ValueError(
                f"background entry must be an object, got {type(data).__name__}")
        return cls(
            frame_id=str(data["frame_id"]),
            path=str(data["path"]),
            state={str(k): str(v)
                   for k, v in dict(data.get("state", {})).items()},
            bytes=int(data.get("bytes", 0)),
            created_at=str(data.get("created_at", _now_iso())),
        )


@dataclass
class BackgroundManifest:
    """Per-scene generated-background manifest (§13). Separate artifact from
    the builder record; produced by Stage 5's scene image pipeline."""

    builder_id: str
    entries: list[BackgroundEntry] = field(default_factory=list)
    # Generation-run provenance (raw dict, like CatalogManifest.matting).
    params: dict | None = None
    updated_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        # A manifest id also drives store paths — keep it confined.
        self.builder_id = ensure_safe_id(self.builder_id)

    def total_bytes(self) -> int:
        return sum(e.bytes for e in self.entries)

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "builder_id": self.builder_id,
            "params": self.params,
            "updated_at": self.updated_at,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BackgroundManifest":
        return cls(
            builder_id=str(data["builder_id"]),
            entries=[BackgroundEntry.from_dict(e)
                     for e in data.get("entries", [])],
            params=data.get("params"),
            updated_at=str(data.get("updated_at", _now_iso())),
        )


# ---------------------------------------------------------------------------
# The builder record.
# ---------------------------------------------------------------------------


@dataclass
class BuilderRecord:
    name: str
    kind: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: int = SCHEMA_VERSION
    selections: dict[str, str] = field(default_factory=dict)
    tags: dict[str, list[str]] = field(default_factory=dict)
    free_text: dict[str, str] = field(default_factory=dict)
    # None on every kind except scenario; always None or an approved frame.
    consent: str | None = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    # -- attribute-level invariants ----------------------------------------

    def __setattr__(self, name: str, value: Any) -> None:
        # id is always a safe single path segment.
        if name == "id":
            value = ensure_safe_id(value)
        # kind is always one of the closed set — a hand-edit cannot invent one.
        elif name == "kind":
            value = str(value)
            if value not in BUILDER_KINDS:
                raise BuilderKindError(
                    f"unknown builder kind {value!r}; expected one of "
                    f"{BUILDER_KINDS}")
        # consent is always None or an approved frame — no non-approved value
        # can ever sit on the field, on any kind, even post-construction
        # (the age.py "cannot be mutated below the floor" stance).
        elif name == "consent" and value is not None:
            value = str(value)
            if value not in APPROVED_CONSENT_FRAMES:
                raise ConsentError(
                    f"consent frame {value!r} is not approved; expected one of "
                    f"{APPROVED_CONSENT_FRAMES}")
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
        self.free_text = {str(k): str(v) for k, v in self.free_text.items()}
        # Consent is a scenario-only concept: a non-scenario carries none, and
        # a scenario MUST carry an approved one (structural — Layer 3).
        if self.kind != SCENARIO:
            self.consent = None
        elif self.consent is None:
            raise ConsentError(
                "a scenario must carry an approved consent frame "
                f"(one of {APPROVED_CONSENT_FRAMES}); a consent-less scenario "
                "is unconstructable")
        self._run_content_gates(get_filter())

    # -- gates --------------------------------------------------------------

    def _run_content_gates(self, filt: Layer1Filter) -> None:
        self._gate(filt, "name", self.name, context="name")
        for key, value in self.free_text.items():
            self._gate(filt, "free_text.key", key)
            self._gate(filt, f"free_text.{key}", value)
        # Selection/tag values are discrete tokens headed for scene image-prompt
        # assembly (Stage 5), not prose — they get the strict prompt context,
        # where contextual terms ("child", "forced", ...) block outright.
        for key, value in self.selections.items():
            self._gate(filt, "selections.key", key)
            self._gate(filt, f"selections.{key}", value, context="prompt")
        for key, values in self.tags.items():
            self._gate(filt, "tags.key", key)
            for value in values:
                self._gate(filt, f"tags.{key}", value, context="prompt")

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
        kind: str,
        *,
        selections: dict[str, str] | None = None,
        tags: dict[str, list[str]] | None = None,
        free_text: dict[str, str] | None = None,
        consent: str | None = None,
    ) -> "BuilderRecord":
        return cls(
            name=name,
            kind=kind,
            selections=dict(selections or {}),
            tags=dict(tags or {}),
            free_text=dict(free_text or {}),
            consent=consent,
        )

    def touch(self) -> None:
        self.updated_at = _now_iso()

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "selections": dict(self.selections),
            "tags": {k: list(v) for k, v in self.tags.items()},
            "free_text": dict(self.free_text),
            "consent": self.consent,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BuilderRecord":
        if "name" not in data or "kind" not in data:
            raise ValueError("builder record requires 'name' and 'kind'")
        # Containers pass through raw; __post_init__ normalizes and gates them.
        return cls(
            name=data["name"],
            kind=data["kind"],
            id=str(data.get("id", uuid.uuid4().hex)),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            selections=dict(data.get("selections", {})),
            tags=dict(data.get("tags", {})),
            free_text=dict(data.get("free_text", {})),
            consent=data.get("consent"),
            created_at=str(data.get("created_at", _now_iso())),
            updated_at=str(data.get("updated_at", _now_iso())),
        )

    # -- validation against a loaded option catalog (soft) ------------------

    def validate_against(self, catalog) -> list[str]:
        """Return human-readable issues: selections/tags referencing unknown
        groups or options. Soft — the record is the source of truth (options
        can be added later, §15); this is a lint, not a gate. The content and
        consent gates are the hard ones and already ran."""
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
        return issues
