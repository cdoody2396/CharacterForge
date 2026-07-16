"""Record → image-prompt assembly (Stage 3a — DECISIONS.md §5, §11).

Turns a ``CharacterRecord`` plus the loaded ``OptionCatalog`` into the
positive/negative prompt pair the SDXL-derived model consumes. The record IS
a structured prompt (§5): every fragment comes from data — option ``prompt``
fragments, slider ``prompt_ranges``, and the filtered ``appearance_notes``
free text — so a drop-in option file changes rendering with no code change
(§15), exactly as it changes the creator.

Assembly order (identity-critical first — CLIP attends early tokens hardest
and truncates around 75; see docs/IMAGE_PIPELINE.md):

  1. quality preamble        (data/positive_quality.txt)
  2. subject anchor          (solo + 1girl/1boy/1other from gender_presentation)
  3. adult anchor + age-range fragment (always asserts adulthood)
  4. option fragments        (5.6b tier order: P0 → P1 → P2 → P3 → untiered
                              buckets, stable (order, id) within each — the
                              V2 first-window contract; an untiered catalog
                              keeps the old flat creator order. Groups with
                              ``render: false`` are chat-side only)
  5. appearance_notes        (the one image-relevant free-text field)

Safety wiring (§11):

- **Layer 1 (image-prompt gate):** every fragment is checked in the strict
  ``prompt`` context *with provenance*, and the final joined string is
  checked again — fragments that are individually clean can form a blocked
  term across a ", " boundary (the joiner fold tolerates 2 separators), and
  option-file ``prompt`` fragments are data no record gate ever saw. A hit
  raises ``PromptBlocked`` naming the offending source; generation refuses.
- **Layer 2 (negative prompts):** every negative prompt carries the
  age-coded steer-away anchors (data/negative_safety.txt) ahead of the
  quality negatives. The negative prompt is deliberately NOT Layer-1 gated:
  it exists to name what generation must avoid.
- The positive prompt always asserts adulthood ("adult" anchor) even if a
  drop-in file strips the age group's prompt_ranges — the anchor is
  structural in code, not data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..model import BuilderRecord, CharacterRecord, OptionCatalog, OptionGroup
from ..safety import Layer1Filter, get_filter

DATA_DIR = Path(__file__).resolve().parent / "data"

# The free-text fields that feed the image prompt. Backstory and personality
# notes are chat-side context (Stage 6d), not visual signal.
IMAGE_FREE_TEXT_KEYS = ("appearance_notes",)

# The builder free-text field that feeds a SCENE background prompt (Stage 5,
# §13). The scenario/persona/event notes are chat-side, never rendered.
SCENE_FREE_TEXT_KEYS = ("setting_notes",)

# Booru-idiomatic anchor for an empty setting: "no humans" is the strongest
# lever to keep a generated background people-free so a matted character
# composites over it cleanly (§13). Paired with negative_scene.txt.
_SCENE_ANCHOR = "scenery, no humans"

# Booru-style subject anchors keyed off the gender_presentation field.
# Generation-side conditioning, not a record property: Illustrious-family
# checkpoints key composition on these tags ("solo" pins a single subject).
_SUBJECT_BY_PRESENTATION = {
    "feminine": "1girl",
    "masculine": "1boy",
    "androgynous": "1other, androgynous",
}
_SOLO = "solo"  # always present: pins a single subject even when the
# gender field is unset (no anchor is invented for an unset field)

# The structural adult anchor (P1). Always present in every positive prompt.
_ADULT_ANCHOR = "adult"

# 5.6b: the prompt-window ordering contract (CHARACTER_VOCABULARY_V2 §1).
# Character assembly walks groups P0 -> P1 -> P2 -> P3 -> untiered, stable by
# (order, id) inside a bucket, so P0+P1 render identity always lands inside
# the first 77-token CLIP window (pooled embeds come from window 0 — see
# engine.encode_chunked). Untiered groups rank last with today's (order, id)
# semantics: an all-untiered catalog (the pre-V2 data files) assembles
# byte-identically to the old flat catalog.groups() pass. `tier` is decoupled
# from `order`, which keeps driving creator form layout.
_TIER_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_UNTIERED_RANK = len(_TIER_RANK)


def _assembly_groups(catalog: OptionCatalog) -> list[OptionGroup]:
    """Catalog groups in prompt-assembly order (tier buckets, then the stable
    (order, id) layout order within each bucket)."""
    return sorted(
        catalog.groups(),
        key=lambda g: (_TIER_RANK.get(g.tier, _UNTIERED_RANK), g.order, g.id),
    )

# Leading/trailing non-word characters, stripped from a fragment before the
# adjacency gate so an author cannot pad a fragment edge with punctuation to
# push a cross-fragment blocked term past the filter's separator tolerance.
_EDGE_PUNCT = re.compile(r"^\W+|\W+$", re.UNICODE)


def _strip_edges(text: str) -> str:
    return _EDGE_PUNCT.sub("", text)


class PromptBlocked(ValueError):
    """A prompt fragment (or the assembled prompt) hit the Layer-1 gate.

    ``source`` names where the text came from ("selections.race",
    "free_text.appearance_notes", "assembled", ...) so the UI can point at
    the field — or at the drop-in option file — responsible.
    """

    def __init__(self, source: str, category: str | None, matched: str | None):
        self.source = source
        self.category = category
        self.matched = matched
        super().__init__(
            f"image prompt blocked by content policy at {source!r} "
            f"(category={category}, matched={matched!r})"
        )


@dataclass(frozen=True)
class PromptPiece:
    """One assembled fragment with provenance, for preview/audit (Layer 4)."""

    source: str
    text: str

    def to_dict(self) -> dict:
        return {"source": self.source, "text": self.text}


@dataclass(frozen=True)
class AssembledPrompt:
    positive: str
    negative: str
    pieces: tuple[PromptPiece, ...]

    def to_dict(self) -> dict:
        return {
            "positive": self.positive,
            "negative": self.negative,
            "pieces": [p.to_dict() for p in self.pieces],
        }


# CLIP encodes 77 slots = BOS + 75 content + EOS. A prompt over the content
# budget was silently truncated pre-5.5b; chunked encoding now carries all of
# it, but the accounting still marks the old 77-boundary so the creator can show
# what USED to be dropped.
CLIP_WINDOW = 77
CLIP_CONTENT_BUDGET = CLIP_WINDOW - 2


def token_report(assembled: AssembledPrompt, count) -> dict:
    """Per-fragment + total CLIP-token accounting for the assembled positive,
    using ``count`` (a ``Callable[[str], int]`` over the model's own tokenizer —
    see :func:`app.imagegen.engine.clip_token_counter`). Reports the total, the
    per-piece marginal cost, and the index of the first piece that overran the
    old single-window 77-boundary (``boundary_index == len(pieces)`` when the
    whole prompt fits). ``count`` counts CONTENT tokens (no BOS/EOS)."""
    per_piece: list[dict] = []
    running = ""
    prev = 0
    boundary = len(assembled.pieces)
    for i, piece in enumerate(assembled.pieces):
        running = piece.text if not running else running + ", " + piece.text
        cumulative = count(running)
        per_piece.append({
            "source": piece.source,
            "text": piece.text,
            "tokens": cumulative - prev,
            "cumulative": cumulative,
        })
        if boundary == len(assembled.pieces) and cumulative > CLIP_CONTENT_BUDGET:
            boundary = i
        prev = cumulative
    total = prev if assembled.pieces else 0
    return {
        "available": True,
        "total": total,
        "window": CLIP_WINDOW,
        "content_budget": CLIP_CONTENT_BUDGET,
        "boundary_index": boundary,
        "within_budget": total <= CLIP_CONTENT_BUDGET,
        "per_piece": per_piece,
    }


def _load_fragments(path: Path) -> tuple[str, ...]:
    """One fragment per line; '#' comments and blank lines ignored."""
    fragments: list[str] = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            fragments.append(line)
    return tuple(fragments)


class PromptAssembler:
    """Stateless record→prompt assembly over the editable prompt data files.

    The data files load once at construction (they are static for a run,
    like the Layer-1 blocklists); the option catalog is passed per call so a
    live "Reload options" is reflected immediately.
    """

    def __init__(
        self,
        *,
        data_dir: Path | str = DATA_DIR,
        content_filter: Layer1Filter | None = None,
    ):
        data_dir = Path(data_dir)
        self._filter = content_filter or get_filter()
        self._positive_quality = _load_fragments(data_dir / "positive_quality.txt")
        self._negative_quality = _load_fragments(data_dir / "negative_quality.txt")
        self._negative_safety = _load_fragments(data_dir / "negative_safety.txt")
        self._negative_scene = _load_fragments(data_dir / "negative_scene.txt")

    # -- public API -----------------------------------------------------------

    def assemble(
        self,
        record: CharacterRecord,
        catalog: OptionCatalog,
        *,
        exclude_groups: frozenset[str] = frozenset(),
        lead: tuple[tuple[str, str], ...] = (),
        extra: tuple[tuple[str, str], ...] = (),
    ) -> AssembledPrompt:
        """Build the gated positive prompt + fixed negative prompt.

        Raises ``PromptBlocked`` on any Layer-1 hit. Groups the record names
        that are missing from the catalog are skipped silently — the record
        stays the source of truth and ``validate_against`` is the lint (§15).

        Stage-3e hooks (all gated + deduped + adjacency-checked like every
        other fragment): ``lead`` (source, text) pairs are inserted right after
        the subject/adult anchors (for the LoRA trigger); ``exclude_groups``
        skips those option-group ids (so the catalog can vary the outfit per
        cell); ``extra`` pairs append after the free text (the cell's
        outfit/expression/pose fragments)."""
        pieces: list[PromptPiece] = []

        def add(source: str, text: str) -> None:
            text = " ".join(str(text).split())  # collapse newlines/whitespace
            if not text:
                return
            self._gate(source, text)
            pieces.append(PromptPiece(source, text))

        for fragment in self._positive_quality:
            add("quality", fragment)
        add("subject", self._subject(record))
        add("age", _ADULT_ANCHOR)
        age_fragment = self._age_fragment(record, catalog)
        if age_fragment != _ADULT_ANCHOR:
            add("age", age_fragment)

        for source, text in lead:
            add(source, text)

        # 5.6b: tier buckets first (P0..P3, then untiered), (order, id) within
        for group in _assembly_groups(catalog):
            if not group.render or group.field == "age" or group.id in exclude_groups:
                continue
            for source, fragment in self._group_fragments(record, group):
                add(source, fragment)

        for key in IMAGE_FREE_TEXT_KEYS:
            add(f"free_text.{key}", record.free_text.get(key, ""))

        for source, text in extra:
            add(source, text)

        kept = self._dedupe_pieces(pieces)
        positive = ", ".join(p.text for p in kept)
        self._gate_adjacency(kept)

        negative = ", ".join(
            self._dedupe(self._negative_safety + self._negative_quality)
        )
        return AssembledPrompt(positive, negative, tuple(kept))

    def assemble_scene(
        self, record: BuilderRecord, catalog: OptionCatalog
    ) -> AssembledPrompt:
        """Build a gated positive scenery prompt for a **scene** builder record
        + the scene negative prompt (Stage 5, §13).

        NO character identity: no subject anchor, no adult anchor, no age — a
        scene renders an empty *setting* the character is later composited over
        (character-over-background, not character-in-scene). It reuses the
        SAME per-fragment gate and the SAME cross-fragment ``_gate_adjacency``
        as character assembly — the scene channel is a §15 attack surface
        (option-file ``prompt`` text is data no record gate saw) and a *named*
        minor-coding surface (CONTENT_POLICY R7: school vocabulary blocks in
        every image prompt, scene backgrounds included), so it must not be a
        separate, weaker path. Layer-1 hits raise ``PromptBlocked``.

        Negatives stack the age-coded safety anchors (``negative_safety.txt``,
        unchanged) ahead of the people-steer (``negative_scene.txt``) and the
        quality negatives.
        """
        pieces: list[PromptPiece] = []

        def add(source: str, text: str) -> None:
            text = " ".join(str(text).split())
            if not text:
                return
            self._gate(source, text)
            pieces.append(PromptPiece(source, text))

        for fragment in self._positive_quality:
            add("quality", fragment)
        add("scene", _SCENE_ANCHOR)

        for group in catalog.groups():  # stable (order, id)
            if not group.render:  # render:false groups are chat-side (tone, ...)
                continue
            for source, fragment in self._scene_group_fragments(record, group):
                add(source, fragment)

        for key in SCENE_FREE_TEXT_KEYS:
            add(f"free_text.{key}", record.free_text.get(key, ""))

        kept = self._dedupe_pieces(pieces)
        positive = ", ".join(p.text for p in kept)
        self._gate_adjacency(kept)

        negative = ", ".join(self._dedupe(
            self._negative_safety + self._negative_scene + self._negative_quality
        ))
        return AssembledPrompt(positive, negative, tuple(kept))

    # -- internals --------------------------------------------------------------

    def _gate(self, source: str, text: str) -> None:
        result = self._filter.check(text, context="prompt")
        if not result.allowed:
            raise PromptBlocked(source, result.category, result.matched)

    def _gate_adjacency(self, kept: list[PromptPiece]) -> None:
        """Catch a blocked term formed ACROSS fragment boundaries — the one
        surface the per-fragment gate cannot see, and the §15 attack surface
        (option-file `prompt` text is data no record gate ever saw).

        Two passes, because the filter's own separator tolerance ({0,2}) is
        exactly what an author overflows:

        1. **Edge-normalized join.** Strip each fragment's leading/trailing
           non-word padding, then join with a single space and gate. No
           amount of edge punctuation ("cute little..." + "girl") can then
           push the boundary past the fold, and because only fragment *edges*
           are stripped (interior prose spacing is preserved) this does not
           concatenate ordinary prose into false positives ("she shot at"
           stays "she shot at", never "shota").
        2. **Zero-separator option pairs.** Concatenate each consecutive pair
           of NON-free-text fragments with no separator and gate, catching a
           single word split across two option fragments ("sho"+"ta"). Prose
           is excluded from this pass (it always trails last), so the
           documented separator-required false positives are not reintroduced.
        """
        normalized = " ".join(
            s for s in (_strip_edges(p.text) for p in kept) if s
        )
        self._gate("assembled", normalized)
        non_prose = [p for p in kept if not p.source.startswith("free_text")]
        for a, b in zip(non_prose, non_prose[1:]):
            self._gate("assembled", _strip_edges(a.text) + _strip_edges(b.text))

    def _subject(self, record: CharacterRecord) -> str:
        presentation = record.selections.get("gender_presentation")
        anchor = _SUBJECT_BY_PRESENTATION.get(presentation or "")
        return f"{_SOLO}, {anchor}" if anchor else _SOLO

    def _age_fragment(self, record: CharacterRecord, catalog: OptionCatalog) -> str:
        """The age group's prompt_ranges refinement ("young adult", "elderly",
        ...) when the catalog provides one; the bare adult anchor otherwise."""
        for group in catalog.groups():
            if group.field == "age" and group.is_numeric:
                fragment = group.prompt_for(int(record.age))
                if fragment:
                    return fragment
        return _ADULT_ANCHOR

    @staticmethod
    def _group_fragments(record: CharacterRecord, group: OptionGroup):
        """(source, fragment) pairs the record's values yield for one group."""
        if group.is_numeric:
            if group.id in record.sliders:
                yield (
                    f"sliders.{group.id}",
                    group.prompt_for(float(record.sliders[group.id])),
                )
            return
        if group.multi:
            for value in record.tags.get(group.id, ()):
                option = group.get_option(value)
                if option is not None:
                    yield (f"tags.{group.id}.{value}", option.prompt)
            return
        value = record.selections.get(group.id)
        if value is not None:
            option = group.get_option(value)
            if option is not None:
                yield (f"selections.{group.id}", option.prompt)

    @staticmethod
    def _scene_group_fragments(record: BuilderRecord, group: OptionGroup):
        """(source, fragment) pairs a scene record yields for one group.
        Selection/tag only — the bundled builder catalogs define no numeric
        groups and a ``BuilderRecord`` has no sliders field, so the numeric
        branch of ``_group_fragments`` is deliberately absent here."""
        if group.multi:
            for value in record.tags.get(group.id, ()):
                option = group.get_option(value)
                if option is not None and option.prompt:
                    yield (f"tags.{group.id}.{value}", option.prompt)
            return
        value = record.selections.get(group.id)
        if value is not None:
            option = group.get_option(value)
            if option is not None and option.prompt:
                yield (f"selections.{group.id}", option.prompt)

    @staticmethod
    def _dedupe(texts) -> list[str]:
        """Drop exact-duplicate fragments, preserving first-seen order."""
        seen: set[str] = set()
        out: list[str] = []
        for text in texts:
            if text and text not in seen:
                seen.add(text)
                out.append(text)
        return out

    @staticmethod
    def _dedupe_pieces(pieces: list[PromptPiece]) -> list[PromptPiece]:
        """Drop exact-duplicate fragment texts (keeping first-seen provenance),
        preserving order."""
        seen: set[str] = set()
        out: list[PromptPiece] = []
        for piece in pieces:
            if piece.text and piece.text not in seen:
                seen.add(piece.text)
                out.append(piece)
        return out
