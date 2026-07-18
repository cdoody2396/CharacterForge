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
          "required": true,            # (5.5c) a character cannot be
                                       # constructed without a value; a
                                       # required group MUST be quick, else a
                                       # load-time format error
          "widget": "picker",          # (5.5c) explicit creator-widget
                                       # override (segmented|chips|swatch|
                                       # picker|slider); omit to derive
          "render": true,              # feed this group's prompt fragments
                                       # into image prompts (default true;
                                       # false for non-visual groups such as
                                       # personality/voice — Stage 3)
          "tier": "P1",                # (5.6a) prompt-window ordering tier
                                       # (P0|P1|P2|P3, closed vocabulary;
                                       # unknown -> load-time format error).
                                       # Decoupled from `order`, which keeps
                                       # driving form layout — the assembler
                                       # buckets P0..P3 then untiered so
                                       # P0+P1 render-identity stays inside
                                       # the first 77-token CLIP window.
          "visible_when": {            # (5.6a) data-driven conditionality,
            "group": "race",           # evaluated FRONT-END against live
            "class": "beastfolk-mammal"  # selections, and (5.7) BACK-END by
          },                           # the construction gate. Exactly one
                                       # predicate:
                                       #   "any": true    — group has a value
                                       #   "in": [ids]    — selection in list
                                       #   "not_in": [ids]— (5.7) selection
                                       #                   NOT in list; an
                                       #                   EMPTY selection
                                       #                   reads VISIBLE
                                       #   "class": "c"   — selected option
                                       #                   carries class c
                                       # Absent or unparsable -> the group is
                                       # ALWAYS VISIBLE (degrade, never a
                                       # format error) — the V2 doc's
                                       # fallback semantics. (5.7) A
                                       # `required` group MAY be conditional:
                                       # required-when-visible — the record
                                       # gate requires it only while its
                                       # condition holds (evaluated via
                                       # OptionCatalog.visible_now).
          "hint": "How this choice     # (5.7) optional plain-language UI
            shapes renders",           # help text (group- or option-level);
                                       # display-only, never enters prompts
          "options": [                 # for single/multi/tags
            {"id": "human", "label": "Human", "prompt": "human",
             "aliases": ["person"], "tags": ["mammal"],
             "class": ["near-human"],  # (5.6a) class metadata visible_when
                                       # conditions read (string or list)
             "color": "#c99b6f",       # optional swatch hint for the UI
             "image": "thumbs/human.png"}  # (5.5c) optional picker thumbnail,
                                       # relative to the option directory
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

Gated options (5.6a, §11 Layer-3 pattern): adult-only entries live in
separate gated directories (bundled ``app/data/options_gated`` + the runtime
``data/options_gated``), passed to :func:`load_option_catalog` only while the
content gate is open — the ungated catalog *structurally lacks* the entries
(and the prompt assembler, which consumes the same catalog, never sees them);
nothing filters. Gated files should only append groups/options, not carry
scalar overrides (they load after every ungated directory).
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

BUNDLED_OPTIONS_DIR = Path(__file__).resolve().parent.parent / "data" / "options"
# 5.6a: bundled adult-only option definitions, loaded only while the content
# gate is open (§11 Layer-3 — the ungated catalog structurally lacks them).
BUNDLED_GATED_OPTIONS_DIR = (
    Path(__file__).resolve().parent.parent / "data" / "options_gated"
)
# Stage-5 builder option definitions, per-kind subdirs + a _shared dir
# (DECISIONS.md §13). Loaded with include_bundled=False so the character
# options (races, anatomy) never leak into a builder form.
BUNDLED_BUILDERS_DIR = Path(__file__).resolve().parent.parent / "data" / "builders"

SELECTION_KINDS = ("single", "multi", "tags")
NUMERIC_KINDS = ("slider", "number")
VALID_KINDS = SELECTION_KINDS + NUMERIC_KINDS

# §12 closed list: the only fields a numeric group may target. Everything
# else — anatomy above all — is categorical by frozen decision.
RESERVED_NUMERIC_FIELDS = ("height", "weight", "muscle", "age")

# 5.5c §15 delta: the closed set of explicit widget overrides an option file
# may name. Anything else is a load-time format error (an author typo must not
# silently fall back to a derived widget). ``None`` means "derive" — see
# :func:`derive_widget`.
VALID_WIDGETS = ("segmented", "chips", "swatch", "picker", "slider")

# 5.6a §15 delta: the closed set of prompt-window ordering tiers. Like the
# widget enum, an unknown value is a load-time format error (an author typo
# must not silently demote a group to untiered). ``None`` means untiered —
# the group keeps today's flat position semantics in the assembler.
VALID_TIERS = ("P0", "P1", "P2", "P3")


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
    # 5.5c §15 delta: an optional per-option thumbnail, a path relative to the
    # option directory it was authored in. Containment-resolved to a data URI
    # at describe() time (creator.py) so a picker tile can show it; the raw
    # string is stored here untrusted, never opened by the model layer.
    image: str | None = None
    # 5.6a §15 delta: class metadata (JSON key "class" — a Python keyword, so
    # the internal name is ``classes``). The key ``visible_when`` conditions
    # read: e.g. a race option carrying "beastfolk-mammal" makes fur groups
    # conditioned on that class appear. Shipped to the browser in the option
    # payload (creator.py) so conditions evaluate against live selections.
    classes: tuple[str, ...] = ()
    # 5.7 §15 delta: optional plain-language help text for the UI (tooltip /
    # info popover). Display-only — never enters prompts or records.
    hint: str | None = None

    @classmethod
    def from_dict(cls, data: dict, *, group_id: str) -> "OptionItem":
        if not isinstance(data, dict):
            raise OptionFormatError(f"group {group_id!r}: an option must be an object")
        if "id" not in data:
            raise OptionFormatError(f"group {group_id!r}: an option is missing 'id'")
        oid = str(data["id"])
        color = data.get("color")
        image = data.get("image")
        return cls(
            id=oid,
            label=str(data.get("label", oid)),
            prompt=str(data.get("prompt", "")),
            aliases=_str_tuple(data.get("aliases", ()), group_id, oid, "aliases"),
            tags=_str_tuple(data.get("tags", ()), group_id, oid, "tags"),
            color=str(color) if color is not None else None,
            image=str(image) if image is not None else None,
            classes=_str_tuple(data.get("class", ()), group_id, oid, "class"),
            hint=_opt_str(data.get("hint")),
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
        if self.image:
            out["image"] = self.image
        if self.classes:
            out["class"] = list(self.classes)
        if self.hint:
            out["hint"] = self.hint
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
    # 5.5c §15 delta:
    required: bool = False  # a character cannot be constructed without a value
    widget: str | None = None  # explicit creator-widget override; None = derive
    # 5.6a §15 delta:
    tier: str | None = None  # prompt-window ordering (P0..P3); None = untiered
    # Normalized visibility condition ({"group": id} + one predicate) or None
    # for always-visible. Evaluated FRONT-END against live selections for
    # form layout, and (5.7) BACK-END by ``OptionCatalog.visible_now`` for
    # the required-when-visible construction gate and hidden-value drop.
    visible_when: dict | None = None
    # 5.7 §15 delta: optional plain-language help text for the UI (tooltip /
    # info popover). Display-only — never enters prompts or records.
    hint: str | None = None
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

    @property
    def has_colors(self) -> bool:
        return any(o.color for o in self.options)

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


def derive_widget(group: OptionGroup) -> str:
    """The creator widget for a group (5.5c). An explicit ``group.widget``
    override wins; otherwise derive from kind / colors / option count. This is
    the sole widget authority — the front-end renders whatever this returns, so
    ``<select>`` (the old ``length > 8 && !colors`` heuristic) is gone: a large
    colorless list becomes a searchable ``picker`` instead.

        explicit widget            -> that widget
        kind slider|number         -> slider
        any option carries color   -> swatch
        kind single, <= 5 options  -> segmented
        kind single|multi|tags, <=12 -> chips
        otherwise                  -> picker
    """
    if group.widget:
        return group.widget
    if group.is_numeric:
        return "slider"
    if group.has_colors:
        return "swatch"
    n = len(group.options)
    if group.kind == "single" and n <= 5:
        return "segmented"
    if n <= 12:
        return "chips"
    return "picker"


def _coerce_widget(data: dict, source: str, group_id: str) -> str | None:
    """Validate an explicit ``widget`` override at load time. Absent/None means
    "derive"; anything outside the closed enum is a format error so an author
    typo surfaces at load instead of silently falling back to derivation."""
    if "widget" not in data or data["widget"] is None:
        return None
    widget = str(data["widget"])
    if widget not in VALID_WIDGETS:
        raise OptionFormatError(
            f"{source}: group {group_id!r} has invalid widget {widget!r}; "
            f"expected one of {VALID_WIDGETS}"
        )
    return widget


def _check_required_quick(group: OptionGroup, source: str) -> None:
    """5.5c structural rule: a ``required`` group MUST be a ``quick`` group.
    The required set is the render-identity minimum, and quick-create is the
    minimal path — a required group outside it would make quick-create
    unsatisfiable. Runs after every assembly/merge so neither a new file nor an
    extension fragment can flip one on without the other."""
    if group.required and not group.quick:
        raise OptionFormatError(
            f"{source}: group {group.id!r} is required but not quick; a "
            f"required group must be in the quick-create set (§15/5.5c) — "
            f"quick-create would otherwise be unsatisfiable"
        )


def _coerce_tier(data: dict, source: str, group_id: str) -> str | None:
    """Validate an explicit ``tier`` at load time (5.6a). Absent/None means
    untiered; anything outside the closed enum is a format error so an author
    typo surfaces at load instead of silently demoting the group out of the
    first-window ordering contract."""
    if "tier" not in data or data["tier"] is None:
        return None
    tier = str(data["tier"])
    if tier not in VALID_TIERS:
        raise OptionFormatError(
            f"{source}: group {group_id!r} has invalid tier {tier!r}; "
            f"expected one of {VALID_TIERS}"
        )
    return tier


# The predicates a visible_when condition may carry — exactly one per rule.
_VISIBLE_WHEN_PREDICATES = ("any", "in", "not_in", "class")


def _coerce_visible_when(value: object) -> dict | None:
    """Normalize a ``visible_when`` condition (5.6a), degrading — never
    raising. The V2 doc's fallback semantics: an absent or unparsable
    condition means ALWAYS VISIBLE, so hand-edited junk can only make a group
    more visible, never hide it or brick the load. A well-formed condition is
    ``{"group": "<id>"}`` plus exactly one predicate:

        "any": true     -> the referenced group has any selection
        "in": [ids]     -> the selection is (or intersects) the listed ids
        "not_in": [ids] -> (5.7) NO selection matches the listed ids; an
                           empty selection reads VISIBLE — required-when-
                           visible depends on this polarity: quick mode may
                           not show the referenced group at all (e.g.
                           hair_length), and hiding a required group on "no
                           selection yet" would make quick-create
                           unsatisfiable
        "class": "c"    -> a selected option in the group carries class c

    Returns the canonical plain-JSON dict (bridge-safe: str/list/bool only)
    or None for always-visible."""
    if not isinstance(value, dict):
        return None
    group = value.get("group")
    if not isinstance(group, str) or not group:
        return None
    preds = [k for k in _VISIBLE_WHEN_PREDICATES if k in value]
    if len(preds) != 1:
        return None
    pred = preds[0]
    if pred == "any":
        if value["any"] is not True:
            return None
        return {"group": group, "any": True}
    if pred in ("in", "not_in"):
        ids = value[pred]
        if not isinstance(ids, (list, tuple)) or not ids:
            return None
        if not all(isinstance(v, str) and v for v in ids):
            return None
        return {"group": group, pred: list(ids)}
    # pred == "class"
    cls = value["class"]
    if not isinstance(cls, str) or not cls:
        return None
    return {"group": group, "class": cls}


# 5.7: the 5.6a "required groups must be unconditionally visible" load rule is
# deleted — required-when-visible replaces it. The construction gate now takes
# the selection-aware set from ``required_group_ids_for``, so a condition-
# hidden required group is simply not required while hidden (and the negative
# predicates' empty-selection-is-visible polarity keeps quick-create
# satisfiable when the referenced group is not on the quick path).


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
                    value = float(entry[key])
                except (TypeError, ValueError, OverflowError):
                    raise OptionFormatError(
                        f"{source}: group {group_id!r} prompt_ranges {key!r} "
                        f"must be numeric, got {entry[key]!r}"
                    ) from None
                if not math.isfinite(value):
                    # json.loads accepts Infinity/NaN; a non-finite bound rode
                    # verbatim into the creator_catalog() payload once 5.5c
                    # surfaced prompt_ranges for the slider band label — invalid
                    # strict JSON that bricks the whole creator (the documented
                    # non-finite-into-bridge hazard, matching _coerce_number).
                    raise OptionFormatError(
                        f"{source}: group {group_id!r} prompt_ranges {key!r} "
                        f"must be finite, got {entry[key]!r}"
                    )
                rng[key] = value
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
    if "required" in data:
        existing.required = bool(data["required"])
    if "widget" in data:
        existing.widget = _coerce_widget(data, source, gid)
    if "tier" in data:
        existing.tier = _coerce_tier(data, source, gid)
    if "visible_when" in data:
        # replace wholesale; an explicit null (or junk) clears to
        # always-visible — the doc's degrade semantics
        existing.visible_when = _coerce_visible_when(data["visible_when"])
    if "hint" in data:
        existing.hint = _opt_str(data["hint"])
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
    _check_required_quick(existing, source)


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
        required=bool(data.get("required", False)),
        widget=_coerce_widget(data, source, gid),
        tier=_coerce_tier(data, source, gid),
        visible_when=_coerce_visible_when(data.get("visible_when")),
        hint=_opt_str(data.get("hint")),
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
    _check_required_quick(group, source)
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

    def required_group_ids(self) -> tuple[str, ...]:
        """The ids of ALL ``required`` groups (5.5c), visibility-blind. Every
        one is guaranteed ``quick`` by the load-time check, so the quick path
        can always satisfy them. The UI needs this full static set (it
        evaluates visibility live); the construction gate uses the
        selection-aware :meth:`required_group_ids_for` instead (5.7)."""
        return tuple(g.id for g in self.groups() if g.required)

    def required_group_ids_for(
        self, selections: dict, tags: dict
    ) -> tuple[str, ...]:
        """The required set GIVEN a payload's live values (5.7): required and
        currently visible. Required-when-visible — a condition-hidden
        required group (skin_tone on a metal-chassis surface, hair_style on
        bald) is not required while hidden."""
        return tuple(
            g.id for g in self.groups()
            if g.required and self.visible_now(g.id, selections, tags)
        )

    def visible_now(self, group_id: str, selections: dict, tags: dict) -> bool:
        """Server-side twin of the front-end ``visibleNow`` (creator.js) —
        the semantics must stay byte-matching (5.7): no/unparsable condition
        -> visible; unknown or numeric referenced group -> visible (degrade:
        bad data may only ever make a group MORE visible); positive
        predicates (any/in/class) need a chosen match; ``not_in`` reads
        visible unless a chosen id matches — so an EMPTY selection is
        visible. Non-recursive: a chain of conditions is evaluated one hop
        deep, exactly like the client."""
        group = self._groups.get(group_id)
        if group is None:
            return True
        cond = group.visible_when
        if not isinstance(cond, dict):
            return True
        ref = self._groups.get(cond.get("group"))
        if ref is None or ref.is_numeric:
            return True
        if ref.multi:
            raw = (tags or {}).get(ref.id) or []
            chosen = [str(v) for v in raw] if isinstance(raw, (list, tuple)) else []
        else:
            value = (selections or {}).get(ref.id)
            chosen = [str(value)] if value not in (None, "") else []
        if cond.get("any") is True:
            return bool(chosen)
        if isinstance(cond.get("in"), list):
            return any(c in cond["in"] for c in chosen)
        if isinstance(cond.get("not_in"), list):
            return not any(c in cond["not_in"] for c in chosen)
        if isinstance(cond.get("class"), str):
            want = cond["class"]
            return any(
                (opt := ref.get_option(c)) is not None and want in opt.classes
                for c in chosen
            )
        return True

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


def load_builder_catalog(
    kind: str, data_dir: Path | str | None = None, *, strict: bool = False
) -> OptionCatalog:
    """Load the option catalog for one builder kind (Stage 5, §13): the bundled
    ``_shared`` groups + the bundled per-kind groups, then the same two under a
    runtime ``<data_dir>/builders/`` so drop-in files extend a kind with no
    rebuild (§15). ``include_bundled=False`` — the character catalog (races,
    anatomy) must never leak into a builder form; §12's numeric reservation
    still runs at load, so a builder numeric group on any non-reserved field is
    a load error (the bundled builder files define none, and a ``BuilderRecord``
    has no sliders field regardless — "no sliders" holds structurally)."""
    dirs: list[Path] = [BUNDLED_BUILDERS_DIR / "_shared", BUNDLED_BUILDERS_DIR / kind]
    if data_dir is not None:
        base = Path(data_dir) / "builders"
        dirs += [base / "_shared", base / kind]
    return load_option_catalog(dirs, include_bundled=False, strict=strict)
