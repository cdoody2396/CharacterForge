"""Character record schema + gates (DECISIONS.md §5, §10, §11).
Round-trip, structural age gate, name slur-block, free-text filtering."""

import pytest

from app.model import (
    Age,
    CharacterRecord,
    ContentBlocked,
    IdentityAnchor,
    MissingRequiredSelection,
    load_option_catalog,
)
from app.model.age import AgeError


def make_record(**overrides):
    # Catalog-clean against the 5.6c bundled (ungated) vocabulary: no sliders
    # (deleted, V2 flag 3), no gated options (lingerie lives behind the gate),
    # hair_style ids are pure shapes now. Legacy-shaped records are exercised
    # explicitly via overrides / tests/test_vocabulary_data.py.
    base = dict(
        name="Seraphina Vale",
        age=27,
        selections={"race": "elf", "gender_presentation": "feminine",
                    "skin_tone": "fair", "hair_color": "silver",
                    "hair_style": "wavy", "eye_color": "violet",
                    "body_type": "athletic", "chest_size": "medium"},
        tags={"traits": ["confident", "witty"], "outfit": ["gown"]},
        free_text={
            "backstory": "A ranger from the northern reach who lost her clan to war.",
            "personality": "Guarded with strangers, fiercely loyal once trust is earned.",
        },
    )
    base.update(overrides)
    return CharacterRecord.create(**base)


# -- round trip ---------------------------------------------------------------


def test_record_round_trips_to_dict_and_back():
    # sliders passed explicitly: the record schema still round-trips them
    # even though no bundled slider group remains (lenient-load contract)
    original = make_record(sliders={"height": 172})
    data = original.to_dict()
    restored = CharacterRecord.from_dict(data)
    assert restored.to_dict() == data
    assert restored.name == original.name
    assert int(restored.age) == 27
    assert restored.selections["race"] == "elf"
    assert restored.tags["traits"] == ["confident", "witty"]
    assert restored.sliders["height"] == 172


def test_age_is_an_age_object_after_construction():
    record = make_record(age=30)
    assert isinstance(record.age, Age)
    assert int(record.age) == 30


def test_identity_anchor_defaults_and_round_trip():
    record = make_record()
    assert record.identity.has_lora is False
    record.identity = IdentityAnchor(
        has_lora=True, lora_path="lora/seraphina.safetensors"
    )
    restored = CharacterRecord.from_dict(record.to_dict())
    assert restored.identity.has_lora is True
    assert restored.identity.lora_path == "lora/seraphina.safetensors"


# -- structural age gate ------------------------------------------------------


def test_cannot_create_under_20():
    with pytest.raises(AgeError):
        make_record(age=17)


def test_cannot_load_under_20_from_disk_shaped_dict():
    data = make_record().to_dict()
    data["age"] = 16  # hand-edited file
    with pytest.raises(AgeError):
        CharacterRecord.from_dict(data)


def test_boundary_age_20_allowed():
    assert int(make_record(age=20).age) == 20


def test_post_construction_age_mutation_is_gated():
    record = make_record(age=25)
    with pytest.raises(AgeError):
        record.age = 15
    with pytest.raises(AgeError):
        record.age = True  # bool must not slip through as 1
    # a legitimate reassignment still works and stays an Age
    record.age = 40
    assert isinstance(record.age, Age) and int(record.age) == 40


def test_mutated_age_never_reaches_disk_shape():
    record = make_record(age=25)
    try:
        record.age = 16
    except AgeError:
        pass
    # the record still holds the valid age; to_dict emits a legal value
    assert record.to_dict()["age"] == 25


# -- name slur block (Layer 1) ------------------------------------------------


def test_slur_name_blocked_at_construction():
    with pytest.raises(ContentBlocked) as exc:
        make_record(name="nigger")
    assert exc.value.category == "slurs"
    assert exc.value.field_name == "name"


def test_clean_name_allowed():
    assert make_record(name="Kaelith Dawnbringer").name == "Kaelith Dawnbringer"


def test_name_block_survives_round_trip_attempt():
    data = make_record().to_dict()
    data["name"] = "faggot"
    with pytest.raises(ContentBlocked):
        CharacterRecord.from_dict(data)


# -- free-text filtering ------------------------------------------------------


def test_prohibited_free_text_blocked():
    with pytest.raises(ContentBlocked) as exc:
        make_record(free_text={"backstory": "she is 15 years old"})
    assert exc.value.category == "minors"
    assert exc.value.field_name == "free_text.backstory"


def test_explicit_adult_free_text_allowed():
    record = make_record(
        free_text={"backstory": "Two adults share an explicit, passionate night."}
    )
    assert "explicit" in record.free_text["backstory"]


def test_prohibited_free_text_KEY_blocked():
    with pytest.raises(ContentBlocked):
        make_record(free_text={"rape fantasy notes": "clean value"})


def test_prohibited_selection_value_blocked():
    with pytest.raises(ContentBlocked):
        make_record(selections={"kink": "rape"})


def test_prohibited_tag_value_blocked():
    with pytest.raises(ContentBlocked):
        make_record(tags={"kinks": ["loli", "shota"]})


def test_non_string_free_text_value_does_not_crash_raw():
    # a non-string value is coerced, then gated — no raw AttributeError
    record = make_record(free_text={"lucky_number": 7})
    assert record.free_text["lucky_number"] == "7"


def test_bare_string_tag_value_is_wrapped_not_exploded():
    record = make_record(tags={"outfit": "gown"})
    assert record.tags["outfit"] == ["gown"]  # not ['g','o','w','n']


# -- id safety (path-traversal defense) ---------------------------------------


def test_unsafe_id_rejected_at_construction():
    from app.model import InvalidId

    for bad in ("../../escape", "a/b", "a\\b", "..", ".", "", "C:\\x"):
        with pytest.raises(InvalidId):
            make_record().__setattr__("id", bad)


def test_unsafe_id_rejected_from_dict():
    from app.model import InvalidId

    data = make_record().to_dict()
    data["id"] = "../../escaped"
    with pytest.raises(InvalidId):
        CharacterRecord.from_dict(data)


def test_sliders_stay_integral_after_round_trip():
    record = make_record(sliders={"height": 172})
    assert record.sliders["height"] == 172
    assert isinstance(record.sliders["height"], int)
    restored = CharacterRecord.from_dict(record.to_dict())
    assert restored.to_dict() == record.to_dict()  # idempotent, no 172 -> 172.0


# -- soft validation against the option catalog -------------------------------


def test_validate_against_catalog_clean():
    catalog = load_option_catalog()
    issues = make_record().validate_against(catalog)
    assert issues == [], issues


# -- 5.5c required-selection construction gate --------------------------------


def test_create_gated_on_required_groups():
    catalog = load_option_catalog()
    req = catalog.required_group_ids()
    full = {g: catalog.get(g).options[0].id for g in req}
    # complete -> constructs
    rec = CharacterRecord.create("Whole", 25, selections=full,
                                 required_groups=req)
    assert set(rec.selections) >= set(req)
    # drop one -> unconstructable, and the exception names the missing group
    partial = dict(full)
    del partial["eye_color"]
    with pytest.raises(MissingRequiredSelection) as exc:
        CharacterRecord.create("Partial", 25, selections=partial,
                               required_groups=req)
    assert exc.value.group_id == "eye_color"


def test_raw_construction_and_load_are_not_gated():
    # the gate is on deliberate creation; a bare .create() (no required set)
    # and from_dict (load) must stay ungated so legacy records still load
    rec = CharacterRecord.create("Bare", 25)          # no required_groups
    assert rec.selections == {}
    restored = CharacterRecord.from_dict(rec.to_dict())  # load path
    assert restored.selections == {}


def test_validate_against_flags_missing_required():
    catalog = load_option_catalog()
    # a record missing the render-identity minimum lints it (the soft path a
    # loaded legacy record travels)
    rec = CharacterRecord.create("Loaded", 25, selections={"race": "human"})
    issues = " ".join(rec.validate_against(catalog))
    assert "required" in issues and "eye_color" in issues


def _conditional_catalog(tmp_path):
    """A minimal 5.7 catalog: required tone gated by a required surface."""
    import json as _json

    opts = tmp_path / "opts"
    opts.mkdir()
    (opts / "10_surface.json").write_text(_json.dumps({"groups": [
        {"id": "skin_type", "kind": "single", "quick": True, "required": True,
         "options": [{"id": "bare_skin"}, {"id": "metal_chassis"}]},
        {"id": "skin_tone", "kind": "single", "quick": True, "required": True,
         "visible_when": {"group": "skin_type", "in": ["bare_skin"]},
         "options": [{"id": "fair"}]},
    ]}), encoding="utf-8")
    return load_option_catalog(dirs=[opts], include_bundled=False, strict=True)


def test_validate_against_required_is_selection_aware(tmp_path):
    # 5.7 required-when-visible: a legacy/hand-edited record with a hidden
    # required group missing lints clean; visible-and-missing still lints.
    catalog = _conditional_catalog(tmp_path)
    chassis = CharacterRecord.create(
        "Unit-7", 25, selections={"skin_type": "metal_chassis"})
    assert chassis.validate_against(catalog) == []
    bare = CharacterRecord.create(
        "Skin", 25, selections={"skin_type": "bare_skin"})
    issues = " ".join(bare.validate_against(catalog))
    assert "required" in issues and "skin_tone" in issues


def test_validate_against_lints_hidden_group_values(tmp_path):
    # A hand-edited record holding a value for a condition-hidden group stays
    # loadable and renders what it holds (§15 source-of-truth) — but lints.
    catalog = _conditional_catalog(tmp_path)
    rec = CharacterRecord.create(
        "Edited", 25,
        selections={"skin_type": "metal_chassis", "skin_tone": "fair"})
    issues = " ".join(rec.validate_against(catalog))
    assert "skin_tone" in issues and "hidden" in issues


def test_validate_against_catalog_flags_unknown_options():
    catalog = load_option_catalog()
    record = make_record(
        selections={"race": "griffon"},          # unknown option
        tags={"traits": ["confident", "bogus"]},  # one unknown
        sliders={"height": 170},
    )
    issues = record.validate_against(catalog)
    joined = " ".join(issues)
    assert "griffon" in joined
    assert "bogus" in joined


def test_touch_updates_timestamp():
    record = make_record()
    before = record.updated_at
    record.touch()
    assert record.updated_at >= before
