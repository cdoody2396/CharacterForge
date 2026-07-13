"""Stage-5 scene prompt assembly (PromptAssembler.assemble_scene) — a SCENE
channel that reuses the same Layer-1 per-fragment + cross-fragment adjacency
gate as character assembly (the HIGH-severity bypass surface), with no
character identity anchors."""

import pytest

from app.imagegen.prompt import PromptAssembler, PromptBlocked
from app.model import (
    BuilderRecord,
    OptionCatalog,
    OptionGroup,
    OptionItem,
    load_builder_catalog,
)


@pytest.fixture(scope="module")
def asm():
    return PromptAssembler()


@pytest.fixture(scope="module")
def scene_catalog():
    return load_builder_catalog("scene")


def test_scene_prompt_has_scenery_anchor_and_no_identity(asm, scene_catalog):
    rec = BuilderRecord.create("Beach", "scene",
                               selections={"location": "beach", "lighting": "warm"})
    ap = asm.assemble_scene(rec, scene_catalog)
    assert "no humans" in ap.positive
    assert "beach" in ap.positive
    # NO character identity anchors
    assert "solo" not in ap.positive
    assert "1girl" not in ap.positive
    assert "adult" not in ap.positive.split(", ")


def test_scene_negatives_stack_safety_then_people_steer(asm, scene_catalog):
    rec = BuilderRecord.create("S", "scene", selections={"location": "park"})
    ap = asm.assemble_scene(rec, scene_catalog)
    # age-coded safety anchors LEAD, then the people-steer
    assert ap.negative.split(", ")[0] == "child"
    assert "1girl" in ap.negative and "person" in ap.negative


def test_scene_setting_notes_feed_the_prompt(asm, scene_catalog):
    rec = BuilderRecord.create("S", "scene", selections={"location": "cafe"},
                               free_text={"setting_notes": "warm string lights across the ceiling"})
    ap = asm.assemble_scene(rec, scene_catalog)
    assert "warm string lights across the ceiling" in ap.positive


def test_scene_channel_blocks_school_vocabulary(asm, scene_catalog):
    # CONTENT_POLICY R7: school vocabulary blocks in every image prompt,
    # scene backgrounds included — the scene channel must enforce it too.
    rec = BuilderRecord.create("S", "scene", selections={"location": "library"})
    with pytest.raises(Exception):
        # a scene note that trips R7 (the record gate blocks it first, but the
        # assembler is the second line for option-file fragments)
        bad = BuilderRecord.create("S", "scene",
                                   free_text={"setting_notes": "a classroom with a chalkboard"})
        asm.assemble_scene(bad, scene_catalog)


def test_scene_channel_gates_option_file_prompt_fragments(asm):
    # The §15 attack surface: an option-file `prompt` fragment is data no record
    # gate ever saw. assemble_scene must Layer-1 it — a blocked fragment raises.
    grp = OptionGroup(id="x", label="X", kind="single", field="x", render=True,
                      options=[OptionItem(id="bad", label="Bad", prompt="loli")])
    catalog = OptionCatalog({"x": grp})
    rec = BuilderRecord.create("s", "scene", selections={"x": "bad"})
    with pytest.raises(PromptBlocked):
        asm.assemble_scene(rec, catalog)


def test_scene_channel_adjacency_gate_catches_cross_fragment(asm):
    # Two individually-clean option fragments that concatenate into a blocked
    # term must be caught by the cross-fragment adjacency pass (the same gate
    # that closed the 3a separator-overflow bypass), on the scene channel.
    grp = OptionGroup(id="p", label="P", kind="single", field="p", render=True,
                      order=1, options=[OptionItem(id="a", label="A", prompt="sho")])
    grp2 = OptionGroup(id="q", label="Q", kind="single", field="q", render=True,
                       order=2, options=[OptionItem(id="b", label="B", prompt="ta")])
    catalog = OptionCatalog({"p": grp, "q": grp2})
    rec = BuilderRecord.create("s", "scene", selections={"p": "a", "q": "b"})
    with pytest.raises(PromptBlocked):
        asm.assemble_scene(rec, catalog)


def test_render_false_groups_are_excluded(asm, scene_catalog):
    # `tone` is render:false (chat-side) — its fragment must never reach a
    # scene image prompt even if the record carries it.
    rec = BuilderRecord.create("S", "scene", selections={"location": "bar"},
                               tags={"tone": ["dramatic"]})
    ap = asm.assemble_scene(rec, scene_catalog)
    assert "dramatic tone" not in ap.positive
