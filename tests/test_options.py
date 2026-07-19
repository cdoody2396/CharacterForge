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
    # 5.6c: the height/weight/muscle sliders are gone (V2 flag 3) — the V2
    # replacements (height_band, muscle_def) and new groups load instead
    catalog = load_option_catalog()
    assert len(catalog) > 0
    ids = catalog.group_ids()
    for expected in ("age", "race", "height_band", "muscle_def", "hair_length",
                     "ears", "chest_size", "marks", "aesthetic"):
        assert expected in ids, f"missing bundled group {expected!r}"
    for gone in ("height", "weight", "muscle", "distinctive_features",
                 "hip_size", "rear_size", "genital_config", "style"):
        assert gone not in ids, f"retired group {gone!r} still bundled"


def test_bundled_age_group_declares_twenty_floor():
    catalog = load_option_catalog()
    age = catalog.get("age")
    assert age is not None
    assert age.is_numeric
    assert age.min == 20  # the creator floor mirrors the structural gate


def test_age_is_the_only_bundled_numeric_group():
    # 5.6c: the numeric machinery stays dormant in the format (still guarding
    # the reserved axes) but no bundled slider group remains except age
    catalog = load_option_catalog()
    numeric = [g.id for g in catalog.groups() if g.is_numeric]
    assert numeric == ["age"]
    assert not catalog.get("race").is_numeric
    assert catalog.get("race").is_selection


def test_anatomy_groups_carry_regions_for_progressive_disclosure():
    catalog = load_option_catalog()
    regions = catalog.by_region()
    assert "Chest" in regions
    assert "Hips & Rear" in regions
    # the Genitalia region is gated (5.6c, V2 flag 7): structurally absent
    # from the ungated catalog, present only with the gated dirs loaded
    assert "Genitalia" not in regions
    # ungrouped (no region) groups bucket under None
    assert None in regions
    chest_group_ids = {g.id for g in regions["Chest"]}
    assert "chest_size" in chest_group_ids

    from app.model.options import BUNDLED_GATED_OPTIONS_DIR
    gated = load_option_catalog(dirs=[BUNDLED_GATED_OPTIONS_DIR])
    gated_regions = gated.by_region()
    assert "Genitalia" in gated_regions
    assert {g.id for g in gated_regions["Chest"]} == {"chest_size",
                                                      "chest_shape"}


def test_groups_sorted_by_order():
    catalog = load_option_catalog()
    groups = catalog.groups()
    orders = [g.order for g in groups]
    assert orders == sorted(orders)
    assert groups[0].id == "age"  # order 1


# -- 5.5c §15 delta: required / widget / image --------------------------------


def test_bundled_required_set_is_the_render_identity_minimum():
    catalog = load_option_catalog(strict=True)
    # the protected 7 + the 5.7 skin_type surface (sign-off 2026-07-18)
    assert set(catalog.required_group_ids()) == {
        "race", "gender_presentation", "skin_type", "skin_tone",
        "hair_color", "hair_style", "eye_color", "body_type"}
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
    # colors -> swatch; single<=5 -> segmented; <=12 -> chips; otherwise ->
    # picker (race has 112 options; outfit ~85). The slider branch never
    # fires on bundled data since 5.6c — covered by the drop-in test below.
    assert derive_widget(catalog.get("skin_tone")) == "swatch"
    assert derive_widget(catalog.get("gender_presentation")) == "segmented"
    assert derive_widget(catalog.get("waist")) == "segmented"
    assert derive_widget(catalog.get("chest_size")) == "chips"
    assert derive_widget(catalog.get("height_band")) == "chips"
    assert derive_widget(catalog.get("outfit")) == "picker"
    assert derive_widget(catalog.get("race")) == "picker"


def test_widget_derivation_slider_branch_still_fires_on_dropins(tmp_path):
    write_options(tmp_path / "h.json", [
        {"id": "height", "kind": "slider", "field": "height",
         "min": 140, "max": 220}])
    catalog = load_option_catalog([tmp_path], include_bundled=False,
                                  strict=True)
    assert derive_widget(catalog.get("height")) == "slider"


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


def test_validate_selection(tmp_path):
    write_options(tmp_path / "h.json", [
        {"id": "height", "kind": "slider", "field": "height",
         "min": 140, "max": 220}])
    catalog = load_option_catalog([tmp_path])
    assert catalog.validate_selection("race", "human")
    assert not catalog.validate_selection("race", "nonexistent")
    assert catalog.validate_selection("traits", ["confident", "shy"])
    assert not catalog.validate_selection("traits", ["confident", "bogus"])
    # numeric validation rides a drop-in slider (bundled sliders gone, 5.6c)
    assert catalog.validate_selection("height", 175)
    assert not catalog.validate_selection("height", "tall")
    assert not catalog.validate_selection("unknown_group", "x")


def test_slider_clamp_and_prompt(tmp_path):
    # the slider machinery is dormant on bundled data (5.6c) but a drop-in
    # numeric group still clamps and maps prompt_ranges
    write_options(tmp_path / "h.json", [
        {"id": "height", "kind": "slider", "field": "height",
         "min": 140, "max": 220, "prompt_ranges": [
             {"min": 168, "max": 182, "prompt": "average height"},
             {"min": 183, "prompt": "tall"}]}])
    catalog = load_option_catalog([tmp_path], include_bundled=False)
    height = catalog.get("height")
    assert height.clamp(300) == height.max
    assert height.clamp(100) == height.min
    assert "average" in height.prompt_for(175)
    assert "tall" in height.prompt_for(200)


# -- 5.6a fifth format extension: class / tier / visible_when / gated dirs ----


def test_option_class_parses_string_and_list(tmp_path):
    write_options(tmp_path / "a.json", [{
        "id": "race", "kind": "single", "options": [
            {"id": "catfolk", "class": ["beastfolk", "beastfolk-mammal"]},
            {"id": "human", "class": "near-human"},   # bare string -> 1-tuple
            {"id": "slime"},                          # absent -> ()
        ]}])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    race = catalog.get("race")
    assert race.get_option("catfolk").classes == ("beastfolk", "beastfolk-mammal")
    assert race.get_option("human").classes == ("near-human",)
    assert race.get_option("slime").classes == ()
    # round-trips under the JSON key "class" (classes is the Python-side name)
    assert race.get_option("catfolk").to_dict()["class"] == [
        "beastfolk", "beastfolk-mammal"]
    assert "class" not in race.get_option("slime").to_dict()


def test_option_class_junk_is_a_format_error(tmp_path):
    write_options(tmp_path / "a.json", [{
        "id": "race", "kind": "single",
        "options": [{"id": "x", "class": {"not": "a list"}}]}])
    with pytest.raises(OptionFormatError):
        load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)


def test_tier_parses_and_unknown_is_a_format_error(tmp_path):
    write_options(tmp_path / "a.json", [
        {"id": "race", "kind": "single", "tier": "P0", "options": [{"id": "x"}]},
        {"id": "marks", "kind": "multi", "options": [{"id": "y"}]},
    ])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    assert catalog.get("race").tier == "P0"
    assert catalog.get("marks").tier is None  # absent -> untiered

    write_options(tmp_path / "b.json", [
        {"id": "bad", "kind": "single", "tier": "P9", "options": []}])
    with pytest.raises(OptionFormatError):
        load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    # resilient load: the bad file is skipped whole, recorded on errors
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False)
    assert "bad" not in catalog
    assert any(f == "b.json" for f, _ in catalog.errors)


def test_tier_atomicity_bad_second_group_drops_whole_file(tmp_path):
    write_options(tmp_path / "a.json", [
        {"id": "ok_group", "kind": "single", "tier": "P2", "options": []},
        {"id": "bad_group", "kind": "single", "tier": "nope", "options": []},
    ])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False)
    assert "ok_group" not in catalog  # _apply_file staged copy discarded
    assert "bad_group" not in catalog
    assert len(catalog.errors) == 1


def test_visible_when_valid_shapes_normalize(tmp_path):
    write_options(tmp_path / "a.json", [
        {"id": "fur_color", "kind": "single",
         "visible_when": {"group": "race", "class": "beastfolk-mammal"},
         "options": []},
        {"id": "hair_color_pattern", "kind": "single",
         "visible_when": {"group": "hair_color_2", "any": True},
         "options": []},
        {"id": "genitalia_size", "kind": "single",
         "visible_when": {"group": "genitalia", "in": ["penis", "both"]},
         "options": []},
    ])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    assert catalog.get("fur_color").visible_when == {
        "group": "race", "class": "beastfolk-mammal"}
    assert catalog.get("hair_color_pattern").visible_when == {
        "group": "hair_color_2", "any": True}
    assert catalog.get("genitalia_size").visible_when == {
        "group": "genitalia", "in": ["penis", "both"]}


@pytest.mark.parametrize("junk", [
    "race is beastfolk",              # not an object
    17,                               # not an object
    True,                             # not an object
    {},                               # no group
    {"group": "race"},                # no predicate
    {"group": "race", "any": True, "class": "x"},  # two predicates
    {"group": "", "any": True},       # empty group
    {"group": None, "any": True},     # non-string group
    {"group": "race", "any": "yes"},  # any must be boolean true
    {"group": "race", "in": []},      # empty in-list
    {"group": "race", "in": "human"},  # in must be a list
    {"group": "race", "in": ["human", 3]},  # non-string member
    {"group": "race", "class": ""},   # empty class
    {"group": "race", "class": ["a"]},  # class must be a string
    {"group": "race", "when": "x"},   # unknown predicate key only
])
def test_visible_when_junk_degrades_to_always_visible(tmp_path, junk):
    # The doc's fallback semantics: unparsable -> ALWAYS VISIBLE, never a
    # format error (even strict) and never a hidden group.
    write_options(tmp_path / "a.json", [
        {"id": "g", "kind": "single", "visible_when": junk, "options": []}])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    assert catalog.get("g").visible_when is None


def test_visible_when_on_required_group_loads_required_when_visible(tmp_path):
    # 5.7 deliberately inverts the 5.6a rule (user sign-off, 2026-07-18):
    # required + visible_when now loads cleanly — the construction gate
    # evaluates requiredness against live selections instead
    # (required_group_ids_for), so a hidden required group is not required
    # while hidden rather than an unsatisfiable form.
    write_options(tmp_path / "a.json", [
        {"id": "race", "kind": "single", "options": [{"id": "human"}]},
        {"id": "g", "kind": "single", "quick": True, "required": True,
         "visible_when": {"group": "race", "any": True}, "options": []}])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    assert catalog.get("g").required is True
    assert catalog.get("g").visible_when == {"group": "race", "any": True}
    # static set still lists it; the selection-aware set drops it while hidden
    assert "g" in catalog.required_group_ids()
    assert "g" not in catalog.required_group_ids_for({}, {})
    assert "g" in catalog.required_group_ids_for({"race": "human"}, {})


def test_merge_fragment_sets_and_clears_tier_and_visible_when(tmp_path):
    write_options(tmp_path / "10_base.json", [
        {"id": "g", "kind": "single", "options": [{"id": "a"}]}])
    write_options(tmp_path / "20_ext.json", [
        {"id": "g", "tier": "P1",
         "visible_when": {"group": "race", "any": True}}])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    assert catalog.get("g").tier == "P1"
    assert catalog.get("g").visible_when == {"group": "race", "any": True}

    write_options(tmp_path / "30_clear.json", [
        {"id": "g", "tier": None, "visible_when": None}])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    assert catalog.get("g").tier is None
    assert catalog.get("g").visible_when is None


def test_merge_fragment_visible_when_on_required_group_merges(tmp_path):
    # 5.7: the merge path accepts a condition on a required group too — an
    # extension fragment can make a bundled required group conditional (the
    # skin_tone drop-in pattern).
    write_options(tmp_path / "10_base.json", [
        {"id": "g", "kind": "single", "quick": True, "required": True,
         "options": [{"id": "a"}]}])
    write_options(tmp_path / "20_ext.json", [
        {"id": "g", "visible_when": {"group": "race", "any": True}}])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    assert catalog.get("g").required is True
    assert catalog.get("g").visible_when == {"group": "race", "any": True}


# -- 5.7 sixth format extension: not_in / required-when-visible / hint -------


def test_visible_when_not_in_normalizes(tmp_path):
    write_options(tmp_path / "a.json", [
        {"id": "hair_style", "kind": "single",
         "visible_when": {"group": "hair_length", "not_in": ["bald"]},
         "options": []}])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    assert catalog.get("hair_style").visible_when == {
        "group": "hair_length", "not_in": ["bald"]}


@pytest.mark.parametrize("junk", [
    {"group": "race", "not_in": []},            # empty list
    {"group": "race", "not_in": "bald"},        # not a list
    {"group": "race", "not_in": ["bald", 3]},   # non-string member
    {"group": "race", "not_in": ["bald"], "any": True},  # two predicates
])
def test_visible_when_not_in_junk_degrades(tmp_path, junk):
    write_options(tmp_path / "a.json", [
        {"id": "g", "kind": "single", "visible_when": junk, "options": []}])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    assert catalog.get("g").visible_when is None


def _eval_catalog(tmp_path):
    """A small catalog exercising every predicate against every referent
    shape: single-select, multi-select, numeric, multi-class options."""
    write_options(tmp_path / "a.json", [
        {"id": "race", "kind": "single", "options": [
            {"id": "human", "class": "near-human"},
            {"id": "ghost", "class": ["undead", "ethereal"]},
        ]},
        {"id": "marks", "kind": "multi", "options": [
            {"id": "scar"}, {"id": "tattoo"}]},
        {"id": "age", "kind": "number", "field": "age",
         "min": 20, "max": 100},
        {"id": "by_any", "kind": "single", "options": [],
         "visible_when": {"group": "marks", "any": True}},
        {"id": "by_in", "kind": "single", "options": [],
         "visible_when": {"group": "race", "in": ["ghost"]}},
        {"id": "by_not_in", "kind": "single", "options": [],
         "visible_when": {"group": "race", "not_in": ["ghost"]}},
        {"id": "by_class", "kind": "single", "options": [],
         "visible_when": {"group": "race", "class": "ethereal"}},
        {"id": "by_numeric", "kind": "single", "options": [],
         "visible_when": {"group": "age", "any": True}},
        {"id": "by_unknown", "kind": "single", "options": [],
         "visible_when": {"group": "no_such_group", "any": True}},
    ])
    return load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)


def test_visible_now_truth_table(tmp_path):
    catalog = _eval_catalog(tmp_path)
    v = catalog.visible_now
    # any: needs a chosen value in the referenced (multi) group
    assert v("by_any", {}, {}) is False
    assert v("by_any", {}, {"marks": ["scar"]}) is True
    # in: positive predicates need a match; empty selection -> hidden
    assert v("by_in", {}, {}) is False
    assert v("by_in", {"race": "human"}, {}) is False
    assert v("by_in", {"race": "ghost"}, {}) is True
    # not_in: EMPTY SELECTION IS VISIBLE (the load-bearing polarity: quick
    # mode may not show the referenced group at all)
    assert v("by_not_in", {}, {}) is True
    assert v("by_not_in", {"race": "human"}, {}) is True
    assert v("by_not_in", {"race": "ghost"}, {}) is False
    # class: multi-class options match any of their classes
    assert v("by_class", {}, {}) is False
    assert v("by_class", {"race": "human"}, {}) is False
    assert v("by_class", {"race": "ghost"}, {}) is True
    # degrades — numeric or unknown referent, unknown group id: all visible
    assert v("by_numeric", {}, {}) is True
    assert v("by_unknown", {}, {}) is True
    assert v("no_such_group", {}, {}) is True
    # unconditional group
    assert v("race", {}, {}) is True


def test_required_group_ids_for_is_selection_aware(tmp_path):
    write_options(tmp_path / "a.json", [
        {"id": "skin_type", "kind": "single", "quick": True, "required": True,
         "options": [{"id": "bare_skin"}, {"id": "metal_chassis"}]},
        {"id": "skin_tone", "kind": "single", "quick": True, "required": True,
         "visible_when": {"group": "skin_type", "in": ["bare_skin"]},
         "options": [{"id": "fair"}]},
    ])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    assert set(catalog.required_group_ids()) == {"skin_type", "skin_tone"}
    # no surface picked yet: positive predicate -> tone hidden -> not required
    assert set(catalog.required_group_ids_for({}, {})) == {"skin_type"}
    assert set(catalog.required_group_ids_for(
        {"skin_type": "bare_skin"}, {})) == {"skin_type", "skin_tone"}
    assert set(catalog.required_group_ids_for(
        {"skin_type": "metal_chassis"}, {})) == {"skin_type"}


def test_hint_round_trips_on_groups_and_options(tmp_path):
    write_options(tmp_path / "a.json", [
        {"id": "hair_length", "kind": "single",
         "hint": "How much hair there is; style shapes it.",
         "options": [
             {"id": "bald", "hint": "Hides the style and color choices."},
             {"id": "long"},
         ]}])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    group = catalog.get("hair_length")
    assert group.hint == "How much hair there is; style shapes it."
    assert group.get_option("bald").hint == "Hides the style and color choices."
    assert group.get_option("long").hint is None
    assert group.get_option("bald").to_dict()["hint"].startswith("Hides")
    assert "hint" not in group.get_option("long").to_dict()


def test_hint_merge_overrides_and_clears(tmp_path):
    write_options(tmp_path / "10_base.json", [
        {"id": "g", "kind": "single", "hint": "original", "options": []}])
    write_options(tmp_path / "20_ext.json", [{"id": "g", "hint": "newer"}])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    assert catalog.get("g").hint == "newer"
    write_options(tmp_path / "30_clear.json", [{"id": "g", "hint": None}])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False, strict=True)
    assert catalog.get("g").hint is None


def test_bundled_catalog_is_the_v2_tiered_conditional_shape():
    # 5.6c inverted the 5.6a backward-compat check: the bundled files now
    # carry the V2 vocabulary — tiers on every render group, visible_when on
    # the species blocks, class metadata on the race options — and still load
    # with zero errors.
    catalog = load_option_catalog()
    assert catalog.errors == []
    tiers = {g.id: g.tier for g in catalog.groups()}
    assert tiers["race"] == "P0"
    assert tiers["hair_length"] == "P1"
    assert tiers["aesthetic"] == "P3"
    assert tiers["age"] is None                 # numeric stays untiered
    assert tiers["traits"] is None              # Subset C is 5.6d
    # 5.7 UI pass: narrowed to the over-skin surface — on full_fur the
    # always-visible skin_tone relabels to Fur Tone and carries the color
    assert catalog.get("fur_color").visible_when == {
        "group": "skin_type", "in": ["fur_over_skin"]}
    assert catalog.get("race").visible_when is None   # the root referent
    assert catalog.get("race").get_option("catfolk").classes == (
        "beastfolk", "beastfolk-mammal")
    assert catalog.get("race").get_option("human").classes == ()


def test_gated_directory_is_structurally_absent_when_not_passed(tmp_path):
    # §11 Layer-3: the gate is WHICH DIRECTORIES LOAD, never a filter. An
    # ungated load lacks the gated group and the gated option appended to an
    # ungated group.
    ungated = tmp_path / "options"
    gated = tmp_path / "options_gated"
    ungated.mkdir()
    gated.mkdir()
    write_options(ungated / "10_wardrobe.json", [
        {"id": "wardrobe", "kind": "single",
         "options": [{"id": "casual", "prompt": "casual clothes"}]}])
    write_options(gated / "90_intimate.json", [
        {"id": "wardrobe", "options": [{"id": "nude", "prompt": "nude"}]},
        {"id": "chest_shape2", "kind": "single", "tier": "P2",
         "options": [{"id": "round"}]},
    ])
    closed = load_option_catalog(dirs=[ungated], include_bundled=False, strict=True)
    assert "chest_shape2" not in closed
    assert not closed.get("wardrobe").has_option("nude")
    assert not closed.validate_selection("wardrobe", "nude")

    open_ = load_option_catalog(dirs=[ungated, gated],
                                include_bundled=False, strict=True)
    assert "chest_shape2" in open_
    assert open_.get("wardrobe").has_option("nude")
    assert open_.get("chest_shape2").tier == "P2"
