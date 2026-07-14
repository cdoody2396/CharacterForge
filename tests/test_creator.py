"""Stage-2 creator isolation tests: the option-format extensions
(section/quick/color + the §12 no-anatomy-sliders rule) and the creator
service (describe / reload / create_character) against its Definition of
Done — both paths produce valid records, anatomy stays categorical, drop-in
option files surface with no code change, all free text passes Layer 1."""

import json

import pytest

from app.model import CharacterStore, load_option_catalog
from app.model.options import OptionFormatError
from app.ui.creator import (
    FREE_TEXT_FIELDS,
    NAME_MAX_LEN,
    TEXT_MAX_LEN,
    CreatorService,
    build_creator,
)


def write_options(directory, name, payload) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


# The render-identity minimum every character now needs (5.5c). A quick
# payload carries all of them so construction succeeds; a caller's `selections`
# override MERGES on top (so it can add/replace a group without dropping the
# required set — pass an explicit "" to clear one and exercise the gate).
REQUIRED_SELECTIONS = {
    "race": "human", "gender_presentation": "feminine", "skin_tone": "fair",
    "hair_color": "black", "hair_style": "short", "eye_color": "brown",
    "body_type": "average",
}


def _with_required(payload: dict, overrides: dict) -> dict:
    payload["selections"] = dict(REQUIRED_SELECTIONS)
    if "selections" in overrides:
        payload["selections"].update(overrides.pop("selections"))
    payload.update(overrides)
    return payload


def quick_payload(**overrides) -> dict:
    return _with_required({"mode": "quick", "name": "Seren", "age": 27},
                          overrides)


def edit_payload(**overrides) -> dict:
    """An update payload carrying the render-identity minimum (5.5c): the
    update path re-runs the required gate exactly like creation, so an edit
    must supply the required set (a selections override merges on top)."""
    return _with_required({"name": "Seren", "age": 27}, overrides)


# -- option-format extensions (section / quick / color, §12 rule) -----------


def test_bundled_catalog_declares_sections_and_quick():
    catalog = load_option_catalog(strict=True)
    race = catalog.get("race")
    assert race.section == "Identity"
    assert race.quick is True
    assert catalog.get("archetype").quick is False
    quick_ids = {g.id for g in catalog.groups() if g.quick}
    assert {"race", "gender_presentation", "body_type", "skin_tone",
            "hair_color", "hair_style", "eye_color"} == quick_ids


def test_bundled_colors_parse():
    catalog = load_option_catalog(strict=True)
    porcelain = catalog.get("skin_tone").get_option("porcelain")
    assert porcelain.color and porcelain.color.startswith("#")
    # colorless options stay colorless
    assert catalog.get("hair_style").get_option("short").color is None


def test_merge_overrides_quick_and_section(tmp_path):
    write_options(tmp_path, "z_extend.json", {
        # required must clear alongside quick (race is a required group now);
        # a required-but-not-quick merge is itself a load-time format error.
        "groups": [{"id": "race", "quick": False, "required": False,
                    "section": "Elsewhere"}]
    })
    catalog = load_option_catalog([tmp_path], strict=True)
    assert catalog.get("race").quick is False
    assert catalog.get("race").section == "Elsewhere"


def test_anatomy_slider_is_unrepresentable_new_group(tmp_path):
    write_options(tmp_path, "bad.json", {
        "groups": [{"id": "bust_cm", "kind": "slider", "region": "Chest",
                    "min": 0, "max": 100}]
    })
    with pytest.raises(OptionFormatError, match="anatomy is categorical"):
        load_option_catalog([tmp_path], include_bundled=False, strict=True)


def test_anatomy_slider_is_unrepresentable_via_merge(tmp_path):
    # an extension fragment cannot move an existing slider onto a region —
    # and the failed merge must leave the bundled group COMPLETELY untouched
    # (a half-merged group was an execution-confirmed §12 bypass)
    write_options(tmp_path, "bad_merge.json", {
        "groups": [{"id": "height", "label": "HACKED", "region": "Legs",
                    "min": -9999, "max": 9999}]
    })
    catalog = load_option_catalog([tmp_path])  # resilient load
    assert any("anatomy is categorical" in err for _, err in catalog.errors)
    height = catalog.get("height")
    assert height.region is None
    assert height.label == "Height"
    assert height.min == 140 and height.max == 220  # clamp bounds intact


def test_numeric_reservation_is_a_closed_list(tmp_path):
    # §12: sliders are reserved to height/weight/muscle (+ the age bounds) —
    # a region-less anatomy slider is rejected too, not just regioned ones
    write_options(tmp_path, "bad.json", {
        "groups": [{"id": "bust_size", "kind": "slider", "min": 0, "max": 100}]
    })
    with pytest.raises(OptionFormatError, match="reserved to"):
        load_option_catalog([tmp_path], include_bundled=False, strict=True)


def test_bundled_numeric_set_is_exactly_the_reserved_axes():
    catalog = load_option_catalog(strict=True)
    numeric = {g.field for g in catalog.groups() if g.is_numeric}
    assert numeric == {"height", "weight", "muscle", "age"}


def test_file_application_is_atomic(tmp_path):
    # a malformed second group must not leave the first group applied —
    # "skipped" has to mean the whole file had no effect
    write_options(tmp_path, "two_groups.json", {
        "groups": [
            {"id": "aura", "kind": "single",
             "options": [{"id": "calm", "label": "Calm"}]},
            {"id": "broken", "kind": "nope"},
        ]
    })
    catalog = load_option_catalog([tmp_path], include_bundled=False)
    assert catalog.get("aura") is None
    assert [f for f, _ in catalog.errors] == ["two_groups.json"]


def test_merge_coerces_string_properties(tmp_path):
    write_options(tmp_path, "z_extend.json", {
        "groups": [{"id": "race", "section": 42, "label": 7}]
    })
    catalog = load_option_catalog([tmp_path], strict=True)
    assert catalog.get("race").section == "42"
    assert catalog.get("race").label == "7"


def test_option_override_keeps_position(tmp_path):
    write_options(tmp_path, "z_recolor.json", {
        "groups": [{"id": "race",
                    "options": [{"id": "human", "label": "Human (recolored)"}]}]
    })
    catalog = load_option_catalog([tmp_path], strict=True)
    ids = catalog.get("race").option_ids()
    assert ids[0] == "human"  # overridden in place, not moved to the end
    assert catalog.get("race").get_option("human").label == "Human (recolored)"


def test_prompt_ranges_validated_at_load(tmp_path):
    write_options(tmp_path, "bad_ranges.json", {
        "groups": [{"id": "height", "kind": "slider", "field": "height",
                    "prompt_ranges": [{"min": "tall"}]}]
    })
    with pytest.raises(OptionFormatError, match="prompt_ranges"):
        load_option_catalog([tmp_path], include_bundled=False, strict=True)

    write_options(tmp_path, "bad_ranges.json", {
        "groups": [{"id": "height", "kind": "slider", "field": "height",
                    "prompt_ranges": ["not an object"]}]
    })
    with pytest.raises(OptionFormatError, match="prompt_ranges"):
        load_option_catalog([tmp_path], include_bundled=False, strict=True)


def test_prompt_ranges_reject_non_finite_bounds(tmp_path, audit):
    # 5.5c surfaced prompt_ranges on the creator_catalog() bridge (the slider
    # band label); a hand-edited Infinity/NaN bound must be a load error, not
    # invalid strict JSON that bricks the whole creator.
    import json as _json

    for bad in ("Infinity", "NaN"):
        opt_dir = tmp_path / "data" / "options"
        opt_dir.mkdir(parents=True, exist_ok=True)
        (opt_dir / "r.json").write_text(
            '{"groups":[{"id":"height","kind":"slider","field":"height",'
            f'"prompt_ranges":[{{"min":{bad},"prompt":"x"}}]}}]}}',
            encoding="utf-8")
        # strict load rejects it outright
        with pytest.raises(OptionFormatError, match="finite"):
            load_option_catalog([opt_dir], strict=True)
        # resilient load (the app path) records the skip AND keeps describe()
        # strict-JSON-safe — one bad file cannot brick the whole creator
        creator = build_creator(tmp_path / "data", audit)
        desc = creator.describe()
        _json.dumps(desc, allow_nan=False)  # raises if a non-finite slipped in
        assert any(e["file"] == "r.json" for e in desc["errors"])


def test_non_finite_bounds_rejected_at_load(tmp_path):
    # "inf"/"nan" bounds would skew clamp() and write non-spec JSON records
    write_options(tmp_path, "inf.json", {
        "groups": [{"id": "height", "kind": "slider", "field": "height",
                    "min": "inf"}]
    })
    with pytest.raises(OptionFormatError, match="finite"):
        load_option_catalog([tmp_path], include_bundled=False, strict=True)


def test_loader_survives_hostile_directory_contents(tmp_path):
    # a directory named *.json and absurdly nested JSON must not brick the
    # resilient load (they previously escaped as OSError/RecursionError)
    (tmp_path / "evil.json").mkdir(parents=True)
    (tmp_path / "deep.json").write_text(
        "[" * 60000 + "]" * 60000, encoding="utf-8")
    write_options(tmp_path, "good.json", {
        "groups": [{"id": "aura", "kind": "single",
                    "options": [{"id": "calm", "label": "Calm"}]}]
    })
    catalog = load_option_catalog([tmp_path], include_bundled=False)
    assert catalog.get("aura") is not None
    assert [f for f, _ in catalog.errors] == ["deep.json"]


def test_resilient_load_skips_bad_anatomy_file_keeps_rest(tmp_path):
    write_options(tmp_path, "bad.json", {
        "groups": [{"id": "bust_cm", "kind": "slider", "region": "Chest"}]
    })
    write_options(tmp_path, "good.json", {
        "groups": [{"id": "aura", "kind": "single", "section": "Extras",
                    "options": [{"id": "calm", "label": "Calm"}]}]
    })
    catalog = load_option_catalog([tmp_path], include_bundled=False)
    assert catalog.get("aura") is not None
    assert catalog.get("bust_cm") is None
    assert [f for f, _ in catalog.errors] == ["bad.json"]


# -- describe() --------------------------------------------------------------


def test_describe_shape(creator):
    described = creator.describe()
    by_id = {g["id"]: g for g in described["groups"]}
    assert by_id["race"]["quick"] is True
    assert by_id["race"]["section"] == "Identity"
    assert by_id["traits"]["multi"] is True
    assert by_id["chest_size"]["region"] == "Chest"
    assert by_id["height"]["kind"] == "slider"
    assert by_id["height"]["min"] == 140 and by_id["height"]["max"] == 220
    # groups arrive sorted for a stable layout
    orders = [g["order"] for g in described["groups"]]
    assert orders == sorted(orders)
    # option payloads carry color only when set, never prompts
    porcelain = next(o for o in by_id["skin_tone"]["options"] if o["id"] == "porcelain")
    assert porcelain["color"].startswith("#")
    assert "prompt" not in porcelain
    assert described["min_age"] == 20
    assert [f["key"] for f in described["free_text_fields"]] == [
        f["key"] for f in FREE_TEXT_FIELDS]
    assert described["errors"] == []
    # 5.5c: required set + derived widgets + slider band data ride the payload
    assert set(described["required_groups"]) == {
        "race", "gender_presentation", "skin_tone", "hair_color",
        "hair_style", "eye_color", "body_type"}
    assert by_id["race"]["required"] is True
    assert by_id["race"]["widget"] == "picker"      # 13 colorless options
    assert by_id["skin_tone"]["widget"] == "swatch"  # carries colors
    assert by_id["height"]["prompt_ranges"]          # band labels for the slider


def test_no_select_widget_ever_derived(creator):
    # <select> is gone (5.5c): every group resolves to one of the five widgets
    valid = {"segmented", "chips", "swatch", "picker", "slider"}
    for g in creator.describe()["groups"]:
        assert g["widget"] in valid


def test_dropin_60_option_file_becomes_a_picker(tmp_path, audit):
    # the §15 promise: a large drop-in surfaces as a searchable picker with no
    # code change (the old length>8 -> <select> heuristic is gone)
    data_dir = tmp_path / "data"
    opts = [{"id": f"clan_{i}", "label": f"Clan {i}"} for i in range(60)]
    write_options(data_dir / "options", "70_clans.json", {
        "groups": [{"id": "clan", "label": "Clan", "kind": "single",
                    "section": "Identity", "options": opts}]})
    creator = build_creator(data_dir, audit)
    clan = next(g for g in creator.describe()["groups"] if g["id"] == "clan")
    assert clan["widget"] == "picker"
    assert len(clan["options"]) == 60


def test_option_image_resolves_to_data_uri_and_stays_contained(tmp_path, audit):
    from base64 import b64decode

    data_dir = tmp_path / "data"
    opt_dir = data_dir / "options"
    (opt_dir / "thumbs").mkdir(parents=True)
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    (opt_dir / "thumbs" / "a.png").write_bytes(png)
    write_options(opt_dir, "80_pic.json", {
        "groups": [{"id": "sigil", "label": "Sigil", "kind": "single",
                    "section": "Identity", "options": [
                        {"id": "have", "label": "Has", "image": "thumbs/a.png"},
                        {"id": "gone", "label": "Missing", "image": "thumbs/x.png"},
                        {"id": "evil", "label": "Escape",
                         "image": "../../../secret.png"}]}]})
    creator = build_creator(data_dir, audit)
    sigil = next(g for g in creator.describe()["groups"] if g["id"] == "sigil")
    by_id = {o["id"]: o for o in sigil["options"]}
    # present file -> inline data URI with the real bytes
    assert by_id["have"]["image"].startswith("data:image/png;base64,")
    assert b64decode(by_id["have"]["image"].split(",", 1)[1]) == png
    # missing file and a containment-escaping path -> no image, never a raise
    assert "image" not in by_id["gone"]
    assert "image" not in by_id["evil"]


def test_describe_surfaces_option_file_errors(tmp_path, audit):
    data_dir = tmp_path / "data"
    write_options(data_dir / "options", "broken.json", {"groups": [{"kind": "single"}]})
    creator = build_creator(data_dir, audit)
    described = creator.describe()
    assert described["errors"] and described["errors"][0]["file"] == "broken.json"


# -- DoD: drop-in option files surface with no code change ------------------


def test_drop_in_file_surfaces_and_is_usable(tmp_path, audit):
    data_dir = tmp_path / "data"
    write_options(data_dir / "options", "90_ornaments.json", {
        "groups": [{
            "id": "hair_ornament", "label": "Hair Ornament", "kind": "single",
            "section": "Appearance", "quick": True, "order": 24,
            "options": [{"id": "flower_pin", "label": "Flower Pin"}],
        }]
    })
    creator = build_creator(data_dir, audit)
    described = creator.describe()
    added = next(g for g in described["groups"] if g["id"] == "hair_ornament")
    assert added["quick"] is True and added["section"] == "Appearance"

    res = creator.create_character(
        quick_payload(selections={"hair_ornament": "flower_pin"}))
    assert res["ok"] is True
    assert creator.store.load(res["id"]).selections["hair_ornament"] == "flower_pin"


def test_reload_picks_up_new_file_without_restart(creator):
    assert creator.catalog.get("hair_ornament") is None
    options_dir = creator.store.root / "options"
    write_options(options_dir, "90_ornaments.json", {
        "groups": [{"id": "hair_ornament", "label": "Hair Ornament",
                    "kind": "single",
                    "options": [{"id": "flower_pin", "label": "Flower Pin"}]}]
    })
    described = creator.reload()
    assert any(g["id"] == "hair_ornament" for g in described["groups"])


# -- DoD: both create paths produce valid records ----------------------------


def test_quick_create_minimal_round_trips(creator):
    res = creator.create_character(quick_payload())
    assert res["ok"] is True
    assert res["issues"] == []
    record = creator.store.load(res["id"])
    assert record.name == "Seren"
    assert int(record.age) == 27
    assert record.identity.has_lora is False  # quick = IP-Adapter tier (§6)
    # the minimal quick record is now exactly the render-identity minimum (5.5c)
    assert record.selections == REQUIRED_SELECTIONS and record.tags == {}
    assert record.validate_against(creator.catalog) == []


def test_quick_create_missing_required_rejected(creator):
    # drop one required group -> unconstructable (the render-identity minimum)
    res = creator.create_character(quick_payload(selections={"eye_color": ""}))
    assert res["ok"] is False
    assert res["kind"] == "required"
    assert res["field"] == "selections.eye_color"


def test_quick_create_with_selections(creator):
    res = creator.create_character(quick_payload(selections={
        "race": "elf", "skin_tone": "fair", "hair_color": "silver",
        "eye_color": "violet", "body_type": "athletic",
    }))
    assert res["ok"] is True
    record = creator.store.load(res["id"])
    assert record.selections["race"] == "elf"
    assert record.validate_against(creator.catalog) == []


def test_detailed_create_full_round_trips(creator):
    res = creator.create_character({
        "mode": "detailed",
        "name": "Kaela Vane",
        "age": 132,
        "selections": {
            "race": "tiefling", "gender_presentation": "feminine",
            "skin_tone": "olive", "hair_color": "red", "hair_style": "long",
            "eye_color": "gold", "body_type": "curvy", "chest_size": "large",
            "hip_size": "wide", "genital_config": "vulva",
            "disposition": "fiery", "voice": "sultry",
        },
        "tags": {
            "archetype": ["mage", "noble"],
            "traits": ["confident", "witty", "ambitious"],
            "outfit": ["gown", "lingerie"],
            "distinctive_features": ["horns", "tail"],
        },
        "sliders": {"height": 175, "weight": 70, "muscle": 35},
        "free_text": {
            "backstory": "Exiled court mage of an infernal duchy.",
            "personality_notes": "Sharp tongue, softer center.",
            "appearance_notes": "Curved horns swept back; ember-red skin.",
        },
    })
    assert res["ok"] is True
    assert res["issues"] == []
    record = creator.store.load(res["id"])
    assert record.tags["archetype"] == ["mage", "noble"]
    assert record.sliders == {"height": 175, "weight": 70, "muscle": 35}
    assert record.free_text["backstory"].startswith("Exiled")
    assert record.selections["chest_size"] == "large"
    assert record.validate_against(creator.catalog) == []


def test_mode_is_reported_and_audited(creator, audit):
    res = creator.create_character(quick_payload())
    assert res["mode"] == "quick"
    events = [json.loads(line) for line in
              audit.path_for_today().read_text(encoding="utf-8").splitlines()]
    created = [e for e in events if e["kind"] == "character_created"]
    assert created and created[-1]["mode"] == "quick"
    assert created[-1]["id"] == res["id"]


def test_record_lands_in_store_layout(creator):
    res = creator.create_character(quick_payload())
    path = creator.store.record_path(res["id"])
    assert path.is_file()
    assert path.parent.parent.name == "characters"


# -- payload shape validation -------------------------------------------------


def test_mode_required_and_validated(creator):
    for bad in ({}, quick_payload(mode="turbo"), quick_payload(mode=None)):
        res = creator.create_character(bad)
        assert res["ok"] is False and res["kind"] == "invalid"
        assert res["field"] == "mode"


def test_non_dict_payload(creator):
    for bad in (None, "x", 42, ["mode"]):
        res = creator.create_character(bad)
        assert res["ok"] is False and res["kind"] == "invalid"


def test_name_required(creator):
    for bad_name in ("", "   ", None):
        res = creator.create_character(quick_payload(name=bad_name))
        assert res["ok"] is False
        assert res["kind"] == "invalid" and res["field"] == "name"
    assert creator.store.list_ids() == []


def test_name_length_cap(creator):
    res = creator.create_character(quick_payload(name="x" * (NAME_MAX_LEN + 1)))
    assert res["ok"] is False and res["field"] == "name"


def test_age_required(creator):
    for bad_age in (None, "", "  "):
        res = creator.create_character(quick_payload(age=bad_age))
        assert res["ok"] is False
        assert res["kind"] == "invalid" and res["field"] == "age"


def test_under_20_rejected_nothing_saved(creator):
    res = creator.create_character(quick_payload(age=19))
    assert res["ok"] is False and res["kind"] == "age" and res["field"] == "age"
    assert creator.store.list_ids() == []


def test_age_20_accepted_and_numeric_string_coerced(creator):
    assert creator.create_character(quick_payload(age=20))["ok"] is True
    assert creator.create_character(quick_payload(age="25"))["ok"] is True


def test_unknown_selection_group(creator):
    res = creator.create_character(quick_payload(selections={"nope": "x"}))
    assert res["ok"] is False and res["kind"] == "invalid"
    assert res["field"] == "selections.nope"


def test_unknown_selection_option(creator):
    res = creator.create_character(quick_payload(selections={"race": "gnome"}))
    assert res["ok"] is False and res["field"] == "selections.race"


def test_selection_on_multi_group_rejected(creator):
    res = creator.create_character(quick_payload(selections={"traits": "witty"}))
    assert res["ok"] is False and res["field"] == "selections.traits"


def test_tags_on_single_group_rejected(creator):
    res = creator.create_character(quick_payload(tags={"race": ["elf"]}))
    assert res["ok"] is False and res["field"] == "tags.race"


def test_tags_must_be_list(creator):
    res = creator.create_character(quick_payload(tags={"traits": "witty"}))
    assert res["ok"] is False and res["field"] == "tags.traits"


def test_tags_dedupe_preserving_order(creator):
    res = creator.create_character(quick_payload(
        tags={"traits": ["witty", "loyal", "witty", "", "loyal"]}))
    assert res["ok"] is True
    record = creator.store.load(res["id"])
    assert record.tags["traits"] == ["witty", "loyal"]


def test_unknown_tag_option(creator):
    res = creator.create_character(quick_payload(tags={"traits": ["sparkly"]}))
    assert res["ok"] is False and res["field"] == "tags.traits"


def test_empty_selection_and_tags_dropped(creator):
    # an empty NON-required selection is dropped; the required set stays
    res = creator.create_character(quick_payload(
        selections={"chest_size": ""}, tags={"traits": []}))
    assert res["ok"] is True
    record = creator.store.load(res["id"])
    assert record.selections == REQUIRED_SELECTIONS and record.tags == {}


def test_slider_clamps_to_group_bounds(creator):
    res = creator.create_character(quick_payload(
        sliders={"height": 9999, "weight": 1}))
    assert res["ok"] is True
    record = creator.store.load(res["id"])
    assert record.sliders["height"] == 220  # max
    assert record.sliders["weight"] == 40  # min


def test_slider_rejects_non_numeric_and_bool(creator):
    for bad in ("tall", None, True):
        res = creator.create_character(quick_payload(sliders={"height": bad}))
        assert res["ok"] is False and res["field"] == "sliders.height"


def test_slider_rejects_huge_int_and_non_finite(creator):
    # a JSON integer beyond float range must be a structured error, not an
    # uncaught OverflowError escaping to the bridge
    res = creator.create_character(quick_payload(sliders={"height": 10 ** 400}))
    assert res["ok"] is False and res["field"] == "sliders.height"
    for bad in (float("nan"), float("inf"), "inf"):
        res = creator.create_character(quick_payload(sliders={"height": bad}))
        assert res["ok"] is False and res["field"] == "sliders.height"


def test_slider_on_categorical_group_rejected(creator):
    res = creator.create_character(quick_payload(sliders={"race": 3}))
    assert res["ok"] is False and res["field"] == "sliders.race"


def test_age_group_not_reachable_as_slider_or_selection(creator):
    res = creator.create_character(quick_payload(sliders={"age": 25}))
    assert res["ok"] is False and res["field"] == "sliders.age"
    res = creator.create_character(quick_payload(selections={"age": "25"}))
    assert res["ok"] is False and res["field"] == "selections.age"


def test_free_text_unknown_key_rejected(creator):
    res = creator.create_character(quick_payload(
        mode="detailed", free_text={"diary": "hello"}))
    assert res["ok"] is False and res["field"] == "free_text.diary"


def test_free_text_empty_dropped_and_length_capped(creator):
    res = creator.create_character(quick_payload(
        mode="detailed", free_text={"backstory": "   "}))
    assert res["ok"] is True
    assert "backstory" not in creator.store.load(res["id"]).free_text

    res = creator.create_character(quick_payload(
        mode="detailed", free_text={"backstory": "x" * (TEXT_MAX_LEN + 1)}))
    assert res["ok"] is False and res["field"] == "free_text.backstory"


# -- DoD: all free text passes through Layer 1 -------------------------------


def test_blocked_name_rejected_and_audited(creator, audit):
    res = creator.create_character(quick_payload(name="loli"))
    assert res["ok"] is False and res["kind"] == "blocked"
    assert res["field"] == "name"
    assert res["category"] == "minors"
    assert creator.store.list_ids() == []
    events = [json.loads(line) for line in
              audit.path_for_today().read_text(encoding="utf-8").splitlines()]
    blocks = [e for e in events if e["kind"] == "filter_block"]
    assert blocks and blocks[-1]["context"] == "creator.name"


def test_blocked_free_text_rejected_with_field(creator):
    res = creator.create_character(quick_payload(
        mode="detailed", free_text={"backstory": "she is a loli"}))
    assert res["ok"] is False and res["kind"] == "blocked"
    assert res["field"] == "free_text.backstory"
    assert creator.store.list_ids() == []


def test_blocked_option_id_from_drop_in_file_still_gated(tmp_path, audit):
    # Defense in depth: even if a (user-authored) option file smuggles a
    # blocked term in as an option id, the record-level Layer-1 gate holds.
    data_dir = tmp_path / "data"
    write_options(data_dir / "options", "evil.json", {
        "groups": [{"id": "pet_name", "label": "Pet Name", "kind": "single",
                    "options": [{"id": "loli", "label": "Innocent Label"}]}]
    })
    creator = build_creator(data_dir, audit)
    res = creator.create_character(quick_payload(selections={"pet_name": "loli"}))
    assert res["ok"] is False and res["kind"] == "blocked"
    assert res["field"] == "selections.pet_name"
    assert creator.store.list_ids() == []


def test_slider_key_channel_is_gated(tmp_path, audit):
    # the slider KEY is text that persists; a drop-in numeric group whose id
    # is a blocked term must not flow to disk (execution-confirmed gap,
    # closed at the record gate)
    data_dir = tmp_path / "data"
    write_options(data_dir / "options", "evil.json", {
        "groups": [{"id": "loli", "kind": "slider", "field": "height",
                    "min": 0, "max": 10}]
    })
    creator = build_creator(data_dir, audit)
    res = creator.create_character(quick_payload(sliders={"loli": 5}))
    assert res["ok"] is False and res["kind"] == "blocked"
    assert creator.store.list_ids() == []


def test_record_level_slider_key_gate():
    from app.model import CharacterRecord, ContentBlocked
    with pytest.raises(ContentBlocked):
        CharacterRecord.create(name="X", age=25, sliders={"loli": 1})


def test_contextual_terms_blocked_on_selection_and_tag_values(tmp_path, audit):
    # selection/tag values are discrete prompt-bound tokens, gated in strict
    # prompt context — contextual terms ("child", "forced") block outright
    # even though they'd need sexual proximity to trip in prose
    data_dir = tmp_path / "data"
    write_options(data_dir / "options", "evil.json", {
        "groups": [
            {"id": "companion", "kind": "single",
             "options": [{"id": "child", "label": "Innocent"}]},
            {"id": "kinks", "kind": "tags",
             "options": [{"id": "forced", "label": "Innocent"}]},
        ]
    })
    creator = build_creator(data_dir, audit)
    res = creator.create_character(quick_payload(selections={"companion": "child"}))
    assert res["ok"] is False and res["kind"] == "blocked"
    res = creator.create_character(quick_payload(tags={"kinks": ["forced"]}))
    assert res["ok"] is False and res["kind"] == "blocked"
    assert creator.store.list_ids() == []


def test_clean_adult_content_passes(creator):
    res = creator.create_character(quick_payload(
        mode="detailed",
        name="Twenty-Two",
        age=22,
        free_text={"backstory": "A twenty-two year old adult courtesan."},
    ))
    assert res["ok"] is True


# -- service construction edge ----------------------------------------------


def test_creator_without_bundled_catalog(tmp_path, audit):
    store = CharacterStore(tmp_path)
    creator = CreatorService(store=store, audit=audit, include_bundled=False)
    described = creator.describe()
    assert described["groups"] == []
    assert described["required_groups"] == []  # no catalog -> nothing required
    # name+age alone still creates a valid record (no groups exist to require,
    # and none to select — a bare payload, not the bundled quick_payload)
    res = creator.create_character({"mode": "quick", "name": "Seren", "age": 27})
    assert res["ok"] is True


# -- record editing (Stage 4, §14) -------------------------------------------


def _events(audit):
    path = audit.path_for_today()
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines()]


def _created(creator, **overrides):
    res = creator.create_character(quick_payload(**overrides))
    assert res["ok"] is True
    return res["id"]


def _forge_manifests(store, cid, *, entries=True):
    """Real catalog + cache manifests (with frames on disk) so stale marking
    has something to mark."""
    from app.model import CatalogEntry, CatalogManifest

    frames = store.catalog_frames_dir(cid)
    frames.mkdir(parents=True, exist_ok=True)
    (frames / "f.png").write_bytes(b"PNG")
    store.save_catalog(CatalogManifest(
        character_id=cid,
        entries=[CatalogEntry(frame_id="f", path="catalog/f.png")]
        if entries else []))
    cframes = store.cache_frames_dir(cid)
    cframes.mkdir(parents=True, exist_ok=True)
    (cframes / "c.png").write_bytes(b"PNG")
    store.save_cache(CatalogManifest(
        character_id=cid,
        entries=[CatalogEntry(frame_id="c", path="cache/c.png",
                              on_demand=True)]
        if entries else []))


def test_update_round_trips_and_preserves_immutables(creator, audit):
    cid = _created(creator, selections={"race": "elf"})
    # forge identity state + age the stored timestamps so the bump shows
    record = creator.store.load(cid)
    record.identity.has_lora = True
    record.identity.lora_path = "lora/identity.safetensors"
    record.identity.reference_image_path = "reference/r.png"
    record.updated_at = "2020-01-01T00:00:00+00:00"
    created_at = record.created_at
    creator.store.save(record)

    res = creator.update_character(cid, edit_payload(
        name="Seren Renamed", age=30, selections={"race": "human"}))
    assert res["ok"] is True and res["id"] == cid
    assert res["name"] == "Seren Renamed"
    stored = creator.store.load(cid)
    assert stored.name == "Seren Renamed" and int(stored.age) == 30
    assert stored.selections == {**REQUIRED_SELECTIONS, "race": "human"}
    assert stored.id == cid
    assert stored.created_at == created_at
    assert stored.updated_at > "2020-01-01"          # touched
    # the identity anchor survives the edit untouched (§14)
    assert stored.identity.has_lora is True
    assert stored.identity.lora_path == "lora/identity.safetensors"
    assert stored.identity.reference_image_path == "reference/r.png"
    assert any(e["kind"] == "character_updated" and e["id"] == cid
               for e in _events(audit))


def test_update_payload_replaces_field_sets(creator):
    # a NON-required selection omitted from the payload is removed; the
    # required set persists (it cannot be omitted — the gate re-runs, 5.5c)
    cid = _created(creator, selections={"chest_size": "large"})
    res = creator.update_character(cid, edit_payload())
    assert res["ok"] is True
    assert creator.store.load(cid).selections == REQUIRED_SELECTIONS


def test_update_render_change_marks_catalog_and_cache_stale(creator):
    cid = _created(creator, selections={"race": "elf"})
    _forge_manifests(creator.store, cid)
    res = creator.update_character(
        cid, edit_payload(selections={"race": "human"}))
    assert res["ok"] is True
    assert res["render_changed"] is True
    assert res["stale_marked"] == {"catalog": True, "cache": True}
    assert creator.store.load_catalog(cid).stale is True
    assert creator.store.load_cache(cid).stale is True


def test_update_non_visual_edit_does_not_mark_stale(creator):
    cid = _created(creator)
    _forge_manifests(creator.store, cid)
    # a rename + personality notes edit renders identically (name and
    # render:false groups never enter the prompt)
    res = creator.update_character(cid, edit_payload(
        name="A Whole New Name",
        free_text={"personality_notes": "Now quite chatty."}))
    assert res["ok"] is True
    assert res["render_changed"] is False
    assert res["stale_marked"] == {"catalog": False, "cache": False}
    assert creator.store.load_catalog(cid).stale is False
    assert creator.store.load_cache(cid).stale is False


def test_update_age_change_is_render_relevant(creator):
    cid = _created(creator, age=25)
    _forge_manifests(creator.store, cid)
    res = creator.update_character(cid, edit_payload(age=60))
    assert res["ok"] is True and res["render_changed"] is True
    assert creator.store.load_catalog(cid).stale is True


def test_update_appearance_notes_are_render_relevant(creator):
    cid = _created(creator)
    _forge_manifests(creator.store, cid)
    res = creator.update_character(cid, edit_payload(
        free_text={"appearance_notes": "a crescent scar over one brow"}))
    assert res["ok"] is True and res["render_changed"] is True


def test_update_without_manifests_reports_unmarked(creator):
    cid = _created(creator)
    res = creator.update_character(cid, edit_payload(selections={"race": "elf"}))
    assert res["ok"] is True and res["render_changed"] is True
    assert res["stale_marked"] == {"catalog": False, "cache": False}


def test_update_empty_manifest_not_marked(creator):
    cid = _created(creator)
    _forge_manifests(creator.store, cid, entries=False)
    res = creator.update_character(cid, edit_payload(selections={"race": "elf"}))
    assert res["ok"] is True
    assert res["stale_marked"] == {"catalog": False, "cache": False}


def test_update_corrupt_manifest_is_nonfatal(creator):
    cid = _created(creator)
    _forge_manifests(creator.store, cid)
    creator.store.catalog_path(cid).write_text("{broken", encoding="utf-8")
    res = creator.update_character(cid, edit_payload(selections={"race": "elf"}))
    assert res["ok"] is True                       # the edit itself saved
    assert res["stale_marked"]["catalog"] is False  # unmarkable, reported
    assert res["stale_marked"]["cache"] is True     # the other channel marked


def test_update_already_stale_counts_without_rewrite(creator):
    from app.model import CatalogEntry, CatalogManifest

    cid = _created(creator)
    frames = creator.store.catalog_frames_dir(cid)
    frames.mkdir(parents=True)
    (frames / "f.png").write_bytes(b"PNG")
    creator.store.save_catalog(CatalogManifest(
        character_id=cid, stale=True,
        entries=[CatalogEntry(frame_id="f", path="catalog/f.png")],
        updated_at="2025-12-31T00:00:00+00:00"))
    res = creator.update_character(cid, edit_payload(selections={"race": "elf"}))
    assert res["ok"] is True and res["stale_marked"]["catalog"] is True
    # no gratuitous rewrite: the optimistic token is untouched
    assert (creator.store.load_catalog(cid).updated_at
            == "2025-12-31T00:00:00+00:00")


def test_update_gates_rerun_blocked_content(creator, audit):
    cid = _created(creator)
    res = creator.update_character(cid, {
        "name": "Seren", "age": 27,
        "free_text": {"backstory": "loli"},
    })
    assert res["ok"] is False and res["kind"] == "blocked"
    assert creator.store.load(cid).free_text == {}  # nothing persisted
    assert any(e["kind"] == "filter_block"
               and e["context"].startswith("creator.update")
               for e in _events(audit))


def test_update_age_gate_holds(creator):
    cid = _created(creator, age=25)
    res = creator.update_character(cid, {"name": "Seren", "age": 17})
    assert res["ok"] is False and res["kind"] == "age"
    assert int(creator.store.load(cid).age) == 25


def test_update_shape_validation(creator):
    cid = _created(creator)
    assert creator.update_character(cid, "nope")["kind"] == "invalid"
    res = creator.update_character(
        cid, {"name": "Seren", "age": 27, "selections": {"ghost_group": "x"}})
    assert res["ok"] is False and res["kind"] == "invalid"
    assert res["field"] == "selections.ghost_group"
    res = creator.update_character(cid, {"name": "", "age": 27})
    assert res["kind"] == "invalid" and res["field"] == "name"


def test_update_structured_load_errors(creator):
    assert creator.update_character("", {})["kind"] == "invalid"
    assert creator.update_character("ghost", {})["kind"] == "not_found"
    cid = _created(creator)
    creator.store.record_path(cid).write_text("{broken", encoding="utf-8")
    assert creator.update_character(
        cid, {"name": "X", "age": 27})["kind"] == "io"


def test_update_mode_key_is_ignored(creator):
    cid = _created(creator)
    res = creator.update_character(cid, edit_payload(mode="banana"))
    assert res["ok"] is True


def test_update_never_invokes_generation(creator):
    # §14 "offers, not forces": the edit path must never touch the image
    # engine. Structural — CreatorService holds no ImageService reference at
    # all, and a render-relevant edit completes with no engine wired.
    assert not hasattr(creator, "_images")
    assert not hasattr(creator, "_engine")
    cid = _created(creator, selections={"race": "elf"})
    _forge_manifests(creator.store, cid)
    res = creator.update_character(cid, edit_payload(selections={"race": "human"}))
    assert res["ok"] is True and res["render_changed"] is True


def test_update_preserves_unknown_group_values(tmp_path, audit):
    # §15 source-of-truth: a value referencing a drop-in group that is no
    # longer loaded is carried forward across an unrelated edit, not stripped.
    from app.model import CharacterRecord

    # a creator whose catalog does NOT include a "faction" group
    creator = build_creator(tmp_path / "data", audit)
    record = CharacterRecord.create(
        name="Keeper", age=30,
        selections={"race": "elf", "faction": "shadowclad"},
        tags={"traits": ["curious"], "orders": ["nightwatch"]},
        sliders={"height": 170, "reach": 88.0})
    creator.store.save(record)

    # edit only the name; the unknown groups (faction/orders/reach) survive.
    # The edit must supply the required set (the gate re-runs, 5.5c).
    res = creator.update_character(record.id, edit_payload(
        name="Keeper Renamed", age=30, selections={"race": "elf"},
        tags={"traits": ["curious"]}, sliders={"height": 170}))
    assert res["ok"] is True
    stored = creator.store.load(record.id)
    assert stored.name == "Keeper Renamed"
    assert stored.selections == {**REQUIRED_SELECTIONS, "race": "elf",
                                 "faction": "shadowclad"}
    assert stored.tags == {"traits": ["curious"], "orders": ["nightwatch"]}
    assert stored.sliders == {"height": 170, "reach": 88.0}


def test_update_unknown_group_not_re_added_if_payload_resets_it(tmp_path,
                                                                audit):
    # if a since-removed group is somehow present in the payload's channel,
    # the payload wins for that group (no double-add). Here the payload
    # simply omits everything; a re-added known group must not resurrect a
    # stale unknown one under the same id.
    from app.model import CharacterRecord

    creator = build_creator(tmp_path / "data", audit)
    record = CharacterRecord.create(name="Two", age=25,
                                    selections={"faction": "a"})
    creator.store.save(record)
    res = creator.update_character(record.id, edit_payload(name="Two", age=25))
    assert res["ok"] is True
    # faction is unknown to the catalog -> carried forward alongside the
    # required set the edit supplied
    assert creator.store.load(record.id).selections == {
        **REQUIRED_SELECTIONS, "faction": "a"}
