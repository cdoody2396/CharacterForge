"""Option-definition data-file format + loader (DECISIONS.md §15).

The creator's choices — races, outfits, traits, categorical anatomy, reserved
sliders — are defined in structured JSON files read at startup, not baked into
code. Drop a new definition file into an options directory and its choices
appear; no rebuild. An in-app editor (later) is only a friendlier way to
author the same files.

File format (one JSON object per file):

    {
      "groups": [
        {
          "id": "race",                # unique group id
          "label": "Race",
          "kind": "single",            # single | multi | tags | slider | number
          "field": "race",             # record field it populates (default: id)
          "region": null,              # body region, for anatomy grouping / UI
          "attribute": null,           # sub-attribute label within a region
          "order": 10,                 # sort hint for the creator
          "section": "Identity",       # creator page section (non-anatomy)
          "quick": true,               # include in the quick-create path
          "render": true,              # feed this group's prompt fragments
                                       # into image prompts (default true;
                                       # false for non-visual groups such as
                                       # personality/voice — Stage 3)
          "options": [                 # for single/multi/tags
            {"id": "human", "label": "Human", "prompt": "human",
             "aliases": ["person"], "tags": ["mammal"],
             "color": "#c99b6f"}       # optional swatch hint for the UI
          ],
          "min": 140, "max": 220,      # for slider/number
          "step": 1, "default": 170, "unit": "cm",
          "prompt_ranges": [           # optional slider->prompt mapping
            {"max": 155, "prompt": "petite"},
            {"min": 186, "prompt": "very tall"}
          ]
        }
      ]
    }

Extensibility rule: a later file may reuse an existing group ``id`` to extend
it — its ``options`` append (deduped by option id) and its scalar properties
override. This is what lets a drop-in file add a few new races to the existing
race group without redefining the whole group.

Numeric reservation (DECISIONS.md §12, structural): continuous sliders are
reserved to the whole-body axes the model honors continuously — height,
weight, muscle (plus the record's age bounds). A numeric kind (slider/number)
on any other field, or on a regioned (anatomy) group, is a format error —
enforced at load on both the new-group and merge paths, so a pseudo-precise
anatomy slider is unrepresentable, not merely unrendered.

Files apply atomically: a malformed file is skipped as a whole (its error is
recorded on the catalog), never half-applied — a bad group cannot leave an
earlier group from the same file behind, and a bad merge fragment cannot
leave a bundled group half-mutated.

Loading order: bundled definitions (``app/data/options``) first, then any
user directories (e.g. the runtime ``data/options``) so user drop-ins extend
or override the bundled set.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

BUNDLED_OPTIONS_DIR = Path(__file__).resolve().parent.parent / "data" / "options"

SELECTION_KINDS = ("single", "multi", "tags")
NUMERIC_KINDS = ("slider", "number")
VALID_KINDS = SELECTION_KINDS + NUMERIC_KINDS

# §12 closed list: the only fields a numeric group may target. Everything
# else — anatomy above all — is categorical by frozen decision.
RESERVED_NUMERIC_FIELDS = ("height", "weight", "muscle", "age")


class OptionFormatError(ValueError):
    """A data file does not conform to the option-definition format."""


def _str_tuple(value, group_id: str, option_id: str, field_name: str) -> tuple[str, ...]:
    """Normalize an aliases/tags field to a tuple of strings. A bare string
    (a natural authoring slip) becomes a single-element tuple rather than
    being exploded into characters; a non-list/str raises a clear error."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    raise OptionFormatError(
        f"group {group_id!r} option {option_id!r}: {field_name!r} must be "
        f"a list of strings"
    )


def _coerce_number(data: dict, key: str, source: str, group_id: str) -> float | None:
    """Coerce a numeric scalar (min/max/step/default) to float, or None if
    absent. A non-numeric or non-finite value raises at load time rather than
    crashing later inside clamp()/prompt_for() — "inf"/"nan" strings would
    otherwise skew clamp bounds and write non-spec JSON into records."""
    if key not in data or data[key] is None:
        return None
    try:
        value = float(data[key])
    except (TypeError, ValueError, OverflowError):
        raise OptionFormatError(
            f"{source}: group {group_id!r} field {key!r} must be numeric, "
            f"got {data[key]!r}"
        ) from None
    if not math.isfinite(value):
        raise OptionFormatError(
            f"{source}: group {group_id!r} field {key!r} must be finite, "
            f"got {data[key]!r}"
        )
    return value


@dataclass(frozen=True)
class OptionItem:
    id: str
    label: str
    prompt: str = ""
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    color: str | None = None  # optional swatch hint for the creator UI

    @classmethod
    def from_dict(cls, data: dict, *, group_id: str) -> "OptionItem":
        if not isinstance(data, dict):
            raise OptionFormatError(f"group {group_id!r}: an option must be an object")
        if "id" not in data:
            raise OptionFormatError(f"group {group_id!r}: an option is missing 'id'")
        oid = str(data["id"])
        color = data.get("color")
        return cls(
            id=oid,
            label=str(data.get("label", oid)),
            prompt=str(data.get("prompt", "")),
            aliases=_str_tuple(data.get("aliases", ()), group_id, oid, "aliases"),
            tags=_str_tuple(data.get("tags", ()), group_id, oid, "tags"),
            color=str(color) if color is not None else None,
        )

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"id": self.id, "label": self.label}
        if self.prompt:
            out["prompt"] = self.prompt
        if self.aliases:
            out["aliases"] = list(self.aliases)
        if self.tags:
            out["tags"] = list(self.tags)
        if self.color:
            out["color"] = self.color
        return out


@dataclass
class OptionGroup:
    id: str
    label: str
    kind: str
    field: str
    region: str | None = None
    attribute: str | None = None
    order: int = 1000
    section: str | None = None  # creator page section (non-anatomy groups)
    quick: bool = False  # include in the quick-create path
    render: bool = True  # fragments feed image prompts (False = chat-only)
    options: list[OptionItem] = field(default_factory=list)
    # numeric (slider/number) properties
    min: float | None = None
    max: float | None = None
    step: float | None = None
    default: float | None = None
    unit: str | None = None
    prompt_ranges: list[dict] = field(default_factory=list)
    # provenance: files this group was assembled from
    sources: list[str] = field(default_factory=list)

    @property
    def is_numeric(self) -> bool:
        return self.kind in NUMERIC_KINDS

    @property
    def is_selection(self) -> bool:
        return self.kind in SELECTION_KINDS

    @property
    def multi(self) -> bool:
        return self.kind in ("multi", "tags")

    def option_ids(self) -> list[str]:
        return [o.id for o in self.options]

    def get_option(self, option_id: str) -> OptionItem | None:
        for opt in self.options:
            if opt.id == option_id:
                return opt
        return None

    def has_option(self, option_id: str) -> bool:
        return self.get_option(option_id) is not None

    def clamp(self, value: float) -> float:
        """Bound a numeric value to [min, max] when both are set."""
        if self.min is not None:
            value = max(self.min, value)
        if self.max is not None:
            value = min(self.max, value)
        return value

    def prompt_for(self, value: float) -> str:
        """Prompt fragment for a numeric value from prompt_ranges (first
        matching range wins), else ''."""
        for rng in self.prompt_ranges:
            lo = rng.get("min")
            hi = rng.get("max")
            if (lo is None or value >= lo) and (hi is None or value <= hi):
                return str(rng.get("prompt", ""))
        return ""


def _validate_group_dict(data: dict, source: str, *, is_new: bool) -> None:
    """Structural checks common to new and merged groups. `kind` is required
    (and validated) only for a NEW group — an extension fragment that just
    appends options may omit it (§15)."""
    if "id" not in data:
        raise OptionFormatError(f"{source}: a group is missing 'id'")
    gid = data["id"]
    if is_new:
        kind = data.get("kind")
        if kind not in VALID_KINDS:
            raise OptionFormatError(
                f"{source}: group {gid!r} has invalid kind {kind!r}; "
                f"expected one of {VALID_KINDS}"
            )
    if "options" in data and not isinstance(data["options"], list):
        raise OptionFormatError(f"{source}: group {gid!r} 'options' must be a list")
    if "order" in data and data["order"] is not None:
        try:
            int(data["order"])
        except (TypeError, ValueError):
            raise OptionFormatError(
                f"{source}: group {gid!r} 'order' must be an integer, "
                f"got {data['order']!r}"
            ) from None


def _clamp_default(group: OptionGroup) -> None:
    """Pull a numeric group's default inside [min, max] if it strayed."""
    if group.is_numeric and group.default is not None:
        group.default = group.clamp(group.default)


def _check_numeric_reservation(group: OptionGroup, source: str) -> None:
    """Structural §12 rule, both halves. Anatomy (regioned) groups must be
    categorical, and numeric kinds are a closed list — only the §12-reserved
    whole-body axes (plus the record's age bounds) may be continuous. Runs
    after every group assembly/merge, so neither a new file nor an extension
    fragment can introduce a pseudo-precise slider."""
    if not group.is_numeric:
        return
    if group.region is not None:
        raise OptionFormatError(
            f"{source}: group {group.id!r} is numeric but has region "
            f"{group.region!r}; anatomy is categorical (§12) — continuous "
            f"sliders are reserved for whole-body axes"
        )
    if group.field not in RESERVED_NUMERIC_FIELDS:
        raise OptionFormatError(
            f"{source}: group {group.id!r} is numeric with field "
            f"{group.field!r}; continuous sliders are reserved to "
            f"{RESERVED_NUMERIC_FIELDS} (§12) — use categorical options"
        )


def _opt_str(value: object) -> str | None:
    """Normalize an optional string property (region/attribute/unit/section):
    None stays None (explicit null clears), anything else becomes str."""
    return None if value is None else str(value)


def _coerce_prompt_ranges(value: object, source: str, group_id: str) -> list[dict]:
    """Validate/coerce a prompt_ranges list at load time so prompt_for()
    can never crash on string bounds or non-object entries."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise OptionFormatError(
            f"{source}: group {group_id!r} 'prompt_ranges' must be a list"
        )
    out: list[dict] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise OptionFormatError(
                f"{source}: group {group_id!r} prompt_ranges entries must be objects"
            )
        rng: dict[str, Any] = {}
        for key in ("min", "max"):
            if key in entry and entry[key] is not None:
                try:
                    rng[key] = float(entry[key])
                except (TypeError, ValueError):
                    raise OptionFormatError(
                        f"{source}: group {group_id!r} prompt_ranges {key!r} "
                        f"must be numeric, got {entry[key]!r}"
                    ) from None
        rng["prompt"] = str(entry.get("prompt", ""))
        out.append(rng)
    return out


def _merge_group(existing: OptionGroup, data: dict, source: str) -> None:
    """Extend an existing group in place from a later file's definition.

    Override semantics: string properties override (an explicit null clears
    them); numeric properties (min/max/step/default) override only with a
    number — a null cannot unset them, supply a new value instead. Callers
    provide atomicity: _apply_file stages a copy, so a raise here never
    leaves a half-merged group in the loaded catalog."""
    # kind, if given on an extension, must stay consistent
    new_kind = data.get("kind", existing.kind)
    if new_kind != existing.kind:
        raise OptionFormatError(
            f"{source}: group {existing.id!r} redefined with kind {new_kind!r} "
            f"(was {existing.kind!r}); a group's kind is fixed"
        )
    gid = existing.id
    for key in ("label", "field"):
        if key in data:
            setattr(existing, key, str(data[key]))
    for key in ("region", "attribute", "unit", "section"):
        if key in data:
            setattr(existing, key, _opt_str(data[key]))
    if "quick" in data:
        existing.quick = bool(data["quick"])
    if "render" in data:
        existing.render = bool(data["render"])
    if "order" in data and data["order"] is not None:
        existing.order = int(data["order"])
    for key in ("min", "max", "step", "default"):
        coerced = _coerce_number(data, key, source, gid)
        if coerced is not None:
            setattr(existing, key, coerced)
    if "prompt_ranges" in data:
        existing.prompt_ranges = _coerce_prompt_ranges(
            data["prompt_ranges"], source, gid)
    seen = {o.id for o in existing.options}
    for opt_data in data.get("options", []):
        opt = OptionItem.from_dict(opt_data, group_id=gid)
        if opt.id in seen:
            # replace in place (later file wins) without reordering
            existing.options = [
                opt if o.id == opt.id else o for o in existing.options
            ]
        else:
            existing.options.append(opt)
            seen.add(opt.id)
    existing.sources.append(source)
    _clamp_default(existing)
    _check_numeric_reservation(existing, source)


def _new_group(data: dict, source: str) -> OptionGroup:
    gid = str(data["id"])
    group = OptionGroup(
        id=gid,
        label=str(data.get("label", gid)),
        kind=str(data["kind"]),
        field=str(data.get("field", gid)),
        region=_opt_str(data.get("region")),
        attribute=_opt_str(data.get("attribute")),
        order=int(data.get("order", 1000)),
        section=_opt_str(data.get("section")),
        quick=bool(data.get("quick", False)),
        render=bool(data.get("render", True)),
        min=_coerce_number(data, "min", source, gid),
        max=_coerce_number(data, "max", source, gid),
        step=_coerce_number(data, "step", source, gid),
        default=_coerce_number(data, "default", source, gid),
        unit=_opt_str(data.get("unit")),
        prompt_ranges=_coerce_prompt_ranges(
            data.get("prompt_ranges"), source, gid),
        sources=[source],
    )
    for opt_data in data.get("options", []):
        group.options.append(OptionItem.from_dict(opt_data, group_id=gid))
    _clamp_default(group)
    _check_numeric_reservation(group, source)
    return group


class OptionCatalog:
    """All loaded option groups, enumerable and queryable. `errors` holds
    (filename, message) for any drop-in file that was skipped during a
    resilient load."""

    def __init__(
        self,
        groups: dict[str, OptionGroup],
        errors: list[tuple[str, str]] | None = None,
    ):
        self._groups = groups
        self.errors: list[tuple[str, str]] = errors or []

    # -- enumeration --------------------------------------------------------

    def __len__(self) -> int:
        return len(self._groups)

    def __contains__(self, group_id: object) -> bool:
        return group_id in self._groups

    def __iter__(self):
        return iter(self.groups())

    def group_ids(self) -> list[str]:
        return [g.id for g in self.groups()]

    def groups(self) -> list[OptionGroup]:
        """All groups sorted by (order, id) for stable creator layout."""
        return sorted(self._groups.values(), key=lambda g: (g.order, g.id))

    def get(self, group_id: str) -> OptionGroup | None:
        return self._groups.get(group_id)

    def by_region(self) -> dict[str | None, list[OptionGroup]]:
        """Groups bucketed by body region for progressive disclosure (§12).
        Groups without a region bucket under None."""
        buckets: dict[str | None, list[OptionGroup]] = {}
        for group in self.groups():
            buckets.setdefault(group.region, []).append(group)
        return buckets

    # -- validation ---------------------------------------------------------

    def validate_selection(self, group_id: str, value: object) -> bool:
        """True if ``value`` is a legal choice for the group. Used by the
        record layer as a soft check (unknown option -> reportable issue)."""
        group = self._groups.get(group_id)
        if group is None:
            return False
        if group.is_numeric:
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if group.multi:
            if not isinstance(value, (list, tuple)):
                return False
            return all(group.has_option(str(v)) for v in value)
        return group.has_option(str(value))


def _read_json_file(path: Path) -> dict:
    # utf-8-sig transparently strips a BOM (Windows Notepad and many editors
    # emit one) so a BOM-prefixed but otherwise valid file still loads.
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise OptionFormatError(f"{path.name}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict) or "groups" not in data:
        raise OptionFormatError(f"{path.name}: expected an object with a 'groups' list")
    if not isinstance(data["groups"], list):
        raise OptionFormatError(f"{path.name}: 'groups' must be a list")
    return data


def _apply_file(path: Path, groups: dict[str, OptionGroup]) -> None:
    """Parse one file and merge all its groups into ``groups``, atomically:
    every change lands on a staged copy that replaces ``groups`` only if the
    whole file applies cleanly. A raise therefore means the file had no
    effect at all — a malformed second group cannot leave the first behind,
    and a bad merge fragment cannot leave a bundled group half-mutated."""
    data = _read_json_file(path)
    staged = copy.deepcopy(groups)
    for group_data in data["groups"]:
        if not isinstance(group_data, dict):
            raise OptionFormatError(f"{path.name}: each group must be an object")
        gid = str(group_data.get("id", "")) if "id" in group_data else None
        is_new = gid is None or gid not in staged
        _validate_group_dict(group_data, path.name, is_new=is_new)
        gid = str(group_data["id"])
        if gid in staged:
            _merge_group(staged[gid], group_data, path.name)
        else:
            staged[gid] = _new_group(group_data, path.name)
    groups.clear()
    groups.update(staged)


def load_option_catalog(
    dirs: Iterable[Path | str] | None = None,
    *,
    include_bundled: bool = True,
    strict: bool = False,
) -> OptionCatalog:
    """Load and merge every ``*.json`` option file from the given directories.

    Files are read in (directory order, then filename order) so a later file
    extends or overrides an earlier one. Missing directories are skipped.

    By default the load is **resilient**: a malformed file is skipped and its
    error recorded on ``catalog.errors`` so one bad drop-in file cannot brick
    the whole creator (§15). Pass ``strict=True`` to re-raise the first error
    instead (used by tests and for debugging authoring).
    """
    search: list[Path] = []
    if include_bundled:
        search.append(BUNDLED_OPTIONS_DIR)
    for d in dirs or ():
        search.append(Path(d))

    groups: dict[str, OptionGroup] = {}
    errors: list[tuple[str, str]] = []
    for directory in search:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            if not path.is_file():
                continue  # a directory named *.json is not an option file
            try:
                _apply_file(path, groups)
            except Exception as exc:
                # The resilient contract is that no drop-in can brick the
                # creator — that includes OSError (unreadable file) and
                # RecursionError (absurdly nested JSON), so catch broadly.
                if strict:
                    raise
                errors.append((path.name, str(exc)))
    return OptionCatalog(groups, errors)
