"""Stage-5 builder record model (app/model/builder.py) — the lighter builder
record + the code-anchored consent gate (Layer 3) + the kind gate."""

import pytest

from app.model import (
    APPROVED_CONSENT_FRAMES,
    BackgroundManifest,
    BuilderKindError,
    BuilderRecord,
    ConsentError,
    ContentBlocked,
    approved_consent_frames,
)
from app.model.builder import BUILDER_KINDS


# -- kind gate ---------------------------------------------------------------

@pytest.mark.parametrize("kind", BUILDER_KINDS)
def test_each_kind_constructs(kind):
    consent = "enthusiastic" if kind == "scenario" else None
    rec = BuilderRecord.create("A name", kind, consent=consent)
    assert rec.kind == kind


def test_unknown_kind_is_unconstructable():
    with pytest.raises(BuilderKindError):
        BuilderRecord.create("x", "wombat")


def test_hand_edited_kind_flip_cannot_dodge_consent():
    # A persona hand-edited to kind=scenario must re-gate on load and demand a
    # consent frame — the flip cannot smuggle a consent-less scenario in.
    data = BuilderRecord.create("P", "persona").to_dict()
    data["kind"] = "scenario"
    with pytest.raises(ConsentError):
        BuilderRecord.from_dict(data)


# -- consent gate (Layer 3, code-anchored) -----------------------------------

def test_scenario_requires_consent():
    with pytest.raises(ConsentError):
        BuilderRecord.create("S", "scenario")


@pytest.mark.parametrize("frame", APPROVED_CONSENT_FRAMES)
def test_scenario_accepts_every_approved_frame(frame):
    rec = BuilderRecord.create("S", "scenario", consent=frame)
    assert rec.consent == frame


def test_scenario_rejects_unapproved_frame():
    with pytest.raises(ConsentError):
        BuilderRecord.create("S", "scenario", consent="coerced")


def test_non_scenario_consent_is_dropped():
    rec = BuilderRecord.create("Sc", "scene", consent="enthusiastic")
    assert rec.consent is None


def test_consent_cannot_be_mutated_to_unapproved():
    rec = BuilderRecord.create("S", "scenario", consent="romantic")
    with pytest.raises(ConsentError):
        rec.consent = "reluctant"


def test_approved_frames_advertised_from_code_matches_the_gate():
    advertised = [c["id"] for c in approved_consent_frames()]
    assert tuple(advertised) == APPROVED_CONSENT_FRAMES


# -- content gate (Layer 1) --------------------------------------------------

def test_name_blocked_text_raises():
    with pytest.raises(ContentBlocked):
        BuilderRecord.create("loli", "persona")


def test_free_text_blocked_raises():
    with pytest.raises(ContentBlocked):
        BuilderRecord.create("ok", "scene",
                             free_text={"setting_notes": "a loli sits here"})


def test_selection_token_blocked_in_prompt_context():
    with pytest.raises(ContentBlocked):
        BuilderRecord.create("ok", "scene", tags={"mood": ["shota"]})


# -- round-trip + normalization ----------------------------------------------

def test_round_trip_preserves_everything():
    rec = BuilderRecord.create(
        "Date night", "scenario", consent="romantic",
        selections={"relationship": "partners"},
        tags={"mood": ["romantic", "cozy"]},
        free_text={"situation_notes": "a quiet dinner"})
    back = BuilderRecord.from_dict(rec.to_dict())
    assert back.to_dict() == rec.to_dict()
    assert back.consent == "romantic" and back.kind == "scenario"


def test_bare_string_tag_is_wrapped_not_exploded():
    rec = BuilderRecord.from_dict(
        {"name": "n", "kind": "scene", "tags": {"mood": "romantic"}})
    assert rec.tags["mood"] == ["romantic"]


def test_id_is_confined_to_a_safe_segment():
    from app.model import InvalidId
    with pytest.raises(InvalidId):
        BuilderRecord.from_dict({"name": "n", "kind": "scene", "id": "../evil"})


# -- background manifest ------------------------------------------------------

def test_background_manifest_round_trips():
    m = BackgroundManifest(builder_id="abc")
    from app.model import BackgroundEntry
    m.entries.append(BackgroundEntry(frame_id="bg-1", path="background/bg-1.png",
                                     bytes=100, state={"prompt": "beach"}))
    back = BackgroundManifest.from_dict(m.to_dict())
    assert back.builder_id == "abc" and back.entries[0].frame_id == "bg-1"
    assert back.total_bytes() == 100


def test_background_manifest_id_is_confined():
    from app.model import InvalidId
    with pytest.raises(InvalidId):
        BackgroundManifest(builder_id="../x")
