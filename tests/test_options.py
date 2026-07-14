"""Option-definition format + loader (DECISIONS.md §15). New definition files
must surface options without code changes; groups merge by id."""

import json

import pytest

from app.model.options import (
    OptionFormatError,
    derive_widget,
    load_option_catalog,
)


def write_options(path, groups):
    path.write_text(json.dumps({"groups": groups}), encoding="utf-8")


# -- bundled catalog ----------------------------------------------------------


def test_bundled_catalog_loads_and_is_enumerable():
    catalog = load_option_catalog()
    assert len(catalog) > 0
    ids = catalog.group_ids()
    for expected in ("age", "race", "height", "weight", "muscle", "chest_size"):
        assert expected in ids, f"missing bundled group {expected!r}"


def test_bundled_age_group_declares_twenty_floor():
    catalog = load_option_catalog()
    age = catalog.get("age")
    assert age is not None
    assert age.is_numeric
    assert age.min == 20  # the creator floor mirrors the structural gate


def test_reserved_sliders_are_numeric_others_are_not():
    catalog = load_option_catalog()
    for gid in ("height", "weight", "muscle"):
        assert catalog.get(gid).is_numeric
    assert not catalog.get("race").is_numeric
    assert catalog.get("race").is_selection


def test_anatomy_groups_carry_regions_for_progressive_disclosure():
    catalog = load_option_catalog()
    regions = catalog.by_region()
    assert "Chest" in regions
    assert "Genitalia" in regions
    # ungrouped (no region) groups bucket under None
    assert None in regions
    chest_group_ids = {g.id for g in regions["Chest"]}
    assert "chest_size" in chest_group_ids


def test_groups_sorted_by_order():
    catalog = load_option_catalog()
    groups = catalog.groups()
    orders = [g.order for g in groups]
    assert orders == sorted(orders)
    assert groups[0].id == "age"  # order 1


# -- 5.5c §15 delta: required / widget / image --------------------------------


def test_bundled_required_set_is_the_render_identity_minimum():
    catalog = load_option_catalog(strict=True)
    assert set(catalog.required_group_ids()) == {
        "race", "gender_presentation", "skin_tone", "hair_color",
        "hair_style", "eye_color", "body_type"}
    # every required group is quick (the load-time invariant)
    for gid in catalog.required_group_ids():
        assert catalog.get(gid).quick is True


def test_required_but_not_quick_is_a_load_error(tmp_path):
    write_options(tmp_path / "bad.json", [
        {"id": "faction", "kind": "single", "required": True,
         "options": [{"id": "a", "label": "A"}]}])  # required but not quick
    with pytest.raises(OptionFormatError, match="required but not quick"):
        load_option_catalog([tmp_path], include_bundled=False, strict=True)


def test_required_merge_flip_without_quick_is_a_load_error(tmp_path):
    # flipping a bundled required group's quick off (without clearing required)
    # is a load-time error — the two must stay consistent
    write_options(tmp_path / "z.json", [{"id": "race", "quick": False}])
    with pytest.raises(OptionFormatError, match="required but not quick"):
        load_option_catalog([tmp_path], strict=True)


def test_unknown_widget_is_a_load_error(tmp_path):
    write_options(tmp_path / "bad.json", [
        {"id": "g", "kind": "single", "widget": "carousel",
         "options": [{"id": "a", "label": "A"}]}])
    with pytest.raises(OptionFormatError, match="invalid widget"):
        load_option_catalog([tmp_path], include_bundled=False, strict=True)


def test_widget_derivation_matches_the_spec_table():
    catalog = load_option_catalog(strict=True)
    # kind slider -> slider; colors -> swatch; single<=5 -> segmented;
    # <=12 -> chips; otherwise -> picker (race has 13 options)
    assert derive_widget(catalog.get("height")) == "slider"
    assert derive_widget(catalog.get("skin_tone")) == "swatch"
    assert derive_widget(catalog.get("gender_presentation")) == "segmented"
    assert derive_widget(catalog.get("chest_size")) == "segmented"
    assert derive_widget(catalog.get("outfit")) == "chips"
    assert derive_widget(catalog.get("race")) == "picker"


def test_explicit_widget_overrides_derivation(tmp_path):
    write_options(tmp_path / "w.json", [
        {"id": "race", "widget": "segmented"}])  # force a non-derived widget
    catalog = load_option_catalog([tmp_path], strict=True)
    assert catalog.get("race").widget == "segmented"
    assert derive_widget(catalog.get("race")) == "segmented"


def test_option_image_parses_and_round_trips(tmp_path):
    write_options(tmp_path / "img.json", [
        {"id": "g", "kind": "single",
         "options": [{"id": "a", "label": "A", "image": "thumbs/a.png"}]}])
    catalog = load_option_catalog([tmp_path], include_bundled=False, strict=True)
    opt = catalog.get("g").get_option("a")
    assert opt.image == "thumbs/a.png"
    assert opt.to_dict()["image"] == "thumbs/a.png"


# -- drop-in extension --------------------------------------------------------


def test_dropin_file_adds_options_without_code_change(tmp_path):
    write_options(
        tmp_path / "99_extra_races.json",
        [{
            "id": "race",
            "kind": "single",
            "options": [{"id": "vampire", "label": "Vampire", "prompt": "vampire"}],
        }],
    )
    catalog = load_option_catalog(dirs=[tmp_path])
    race = catalog.get("race")
    assert race.has_option("vampire")      # newly dropped in
    assert race.has_option("human")        # original bundled option preserved


def test_dropin_new_group_appears(tmp_path):
    write_options(
        tmp_path / "90_new_group.json",
        [{
            "id": "horns_style",
            "label": "Horn Style",
            "kind": "single",
            "region": "Features",
            "options": [{"id": "curved", "label": "Curved", "prompt": "curved horns"}],
        }],
    )
    catalog = load_option_catalog(dirs=[tmp_path])
    assert "horns_style" in catalog
    assert catalog.get("horns_style").region == "Features"


def test_merge_across_two_files_pure(tmp_path):
    write_options(
        tmp_path / "a.json",
        [{"id": "mood", "kind": "tags", "options": [{"id": "happy", "label": "Happy"}]}],
    )
    write_options(
        tmp_path / "b.json",
        [{"id": "mood", "kind": "tags", "options": [{"id": "sad", "label": "Sad"}]}],
    )
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False)
    mood = catalog.get("mood")
    assert set(mood.option_ids()) == {"happy", "sad"}
    assert len(mood.sources) == 2


def test_later_file_overrides_option_definition(tmp_path):
    write_options(
        tmp_path / "a.json",
        [{"id": "c", "kind": "single", "options": [{"id": "x", "label": "First"}]}],
    )
    write_options(
        tmp_path / "b.json",
        [{"id": "c", "kind": "single", "options": [{"id": "x", "label": "Second"}]}],
    )
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False)
    assert catalog.get("c").get_option("x").label == "Second"
    assert len(catalog.get("c").option_ids()) == 1  # not duplicated


# -- validation ---------------------------------------------------------------


def test_invalid_kind_rejected_in_strict(tmp_path):
    write_options(tmp_path / "bad.json", [{"id": "g", "kind": "wobble", "options": []}])
    with pytest.raises(OptionFormatError):
        load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)


def test_kind_change_on_merge_rejected_in_strict(tmp_path):
    write_options(tmp_path / "a.json", [{"id": "g", "kind": "single", "options": []}])
    write_options(tmp_path / "b.json", [{"id": "g", "kind": "slider", "min": 0, "max": 1}])
    with pytest.raises(OptionFormatError):
        load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)


def test_missing_id_rejected_in_strict(tmp_path):
    write_options(tmp_path / "bad.json", [{"kind": "single", "options": []}])
    with pytest.raises(OptionFormatError):
        load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)


def test_bad_json_rejected_in_strict(tmp_path):
    (tmp_path / "bad.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(OptionFormatError):
        load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)


# -- resilient loading (§15: one bad file must not brick the creator) --------


def test_resilient_skips_bad_file_keeps_good(tmp_path):
    write_options(tmp_path / "a_good.json", [{"id": "good", "kind": "single", "options": []}])
    (tmp_path / "b_bad.json").write_text("{ not json", encoding="utf-8")
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False)
    assert "good" in catalog                      # good file still loaded
    assert any("b_bad.json" == name for name, _ in catalog.errors)
    assert len(catalog.errors) == 1


def test_bundled_survives_a_bad_dropin(tmp_path):
    (tmp_path / "broken.json").write_text("{ not json", encoding="utf-8")
    catalog = load_option_catalog(dirs=[tmp_path])  # include_bundled=True
    assert "race" in catalog                       # bundled groups still load
    assert catalog.errors


def test_bom_prefixed_file_loads(tmp_path):
    # UTF-8 BOM, as Windows editors commonly emit.
    (tmp_path / "bom.json").write_text(
        '﻿{"groups": [{"id": "bommed", "kind": "single", "options": []}]}',
        encoding="utf-8",
    )
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False)
    assert "bommed" in catalog
    assert catalog.errors == []


def test_extend_group_without_repeating_kind(tmp_path):
    write_options(tmp_path / "a.json", [{"id": "race2", "kind": "single",
                                         "options": [{"id": "human", "label": "Human"}]}])
    write_options(tmp_path / "b.json", [{"id": "race2",
                                         "options": [{"id": "orc", "label": "Orc"}]}])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    assert set(catalog.get("race2").option_ids()) == {"human", "orc"}


def test_numeric_string_coerced(tmp_path):
    write_options(tmp_path / "s.json",
                  [{"id": "h", "kind": "slider", "field": "height",
                    "min": "0", "max": "100", "default": "50"}])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    h = catalog.get("h")
    assert h.clamp(50) == 50          # no TypeError at use time
    assert h.min == 0.0 and h.max == 100.0


def test_non_numeric_slider_bound_rejected_strict(tmp_path):
    write_options(tmp_path / "s.json", [{"id": "h", "kind": "slider", "min": "tall"}])
    with pytest.raises(OptionFormatError):
        load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)


def test_default_clamped_into_range(tmp_path):
    write_options(tmp_path / "s.json",
                  [{"id": "h", "kind": "slider", "field": "height",
                    "min": 20, "max": 120, "default": 5}])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    assert catalog.get("h").default == 20  # pulled up to min


def test_alias_string_wrapped_not_exploded(tmp_path):
    write_options(tmp_path / "a.json", [{
        "id": "g", "kind": "single",
        "options": [{"id": "human", "label": "Human", "aliases": "person", "tags": "mammal"}],
    }])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    opt = catalog.get("g").get_option("human")
    assert opt.aliases == ("person",)   # not ('p','e','r','s','o','n')
    assert opt.tags == ("mammal",)


def test_alias_wrong_type_rejected_strict(tmp_path):
    write_options(tmp_path / "a.json", [{
        "id": "g", "kind": "single",
        "options": [{"id": "human", "aliases": 5}],
    }])
    with pytest.raises(OptionFormatError):
        load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)


def test_options_not_a_list_rejected_strict(tmp_path):
    write_options(tmp_path / "a.json", [{"id": "g", "kind": "single", "options": {"id": "x"}}])
    with pytest.raises(OptionFormatError):
        load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)


def test_bad_order_type_rejected_strict(tmp_path):
    write_options(tmp_path / "a.json", [{"id": "g", "kind": "single", "order": "abc", "options": []}])
    with pytest.raises(OptionFormatError):
        load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)


def test_missing_directory_skipped(tmp_path):
    catalog = load_option_catalog(dirs=[tmp_path / "nope"], include_bundled=False)
    assert len(catalog) == 0


def test_validate_selection():
    catalog = load_option_catalog()
    assert catalog.validate_selection("race", "human")
    assert not catalog.validate_selection("race", "nonexistent")
    assert catalog.validate_selection("traits", ["confident", "shy"])
    assert not catalog.validate_selection("traits", ["confident", "bogus"])
    assert catalog.validate_selection("height", 175)
    assert not catalog.validate_selection("height", "tall")
    assert not catalog.validate_selection("unknown_group", "x")


def test_slider_clamp_and_prompt():
    catalog = load_option_catalog()
    height = catalog.get("height")
    assert height.clamp(300) == height.max
    assert height.clamp(100) == height.min
    assert "average" in height.prompt_for(175)
    assert "tall" in height.prompt_for(200)
