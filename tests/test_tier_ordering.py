"""5.6b assembler tier ordering (CHARACTER_VOCABULARY_V2 §1): character
assembly walks P0 -> P1 -> P2 -> P3 -> untiered buckets, stable (order, id)
within each, so P0+P1 render identity lands inside the first 77-token CLIP
window (pooled embeds come from window 0 — first-window identity is
load-bearing). An all-untiered catalog assembles in the old flat order,
byte-identically. `tier` never touches form layout (catalog.groups())."""

import json
from pathlib import Path

import pytest

from app.imagegen.engine import clip_token_counter
from app.imagegen.prompt import (
    CLIP_CONTENT_BUDGET,
    PromptAssembler,
    _assembly_groups,
    token_report,
)
from app.model import CharacterRecord, load_option_catalog

APP_ROOT = Path(__file__).resolve().parents[1]
_REAL_TOKENIZER = (
    APP_ROOT / "models" / "sdxl_config" / "tokenizer" / "vocab.json"
).is_file()


def write_options(path, groups):
    path.write_text(json.dumps({"groups": groups}), encoding="utf-8")


def _single(gid, tier, prompt, order, **extra):
    group = {"id": gid, "kind": "single", "order": order,
             "options": [{"id": "on", "prompt": prompt}]}
    if tier is not None:
        group["tier"] = tier
    group.update(extra)
    return group


@pytest.fixture(scope="module")
def assembler():
    return PromptAssembler()


# -- ordering unit tests (fake or no counter) ---------------------------------


def test_untiered_catalog_keeps_flat_assembly_order(tmp_path):
    # An entirely untiered catalog assembles in exactly catalog.groups()
    # order — the byte-identity degrade contract. (The bundled catalog is
    # tiered since 5.6c, so this rides a synthetic untiered drop-in.)
    write_options(tmp_path / "a.json", [
        _single("g_first", None, "one", 1),
        _single("g_mid", None, "two", 2),
        _single("g_last", None, "three", 3),
    ])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False,
                                  strict=True)
    assert all(g.tier is None for g in catalog.groups())
    assert _assembly_groups(catalog) == catalog.groups()


def test_tier_buckets_override_form_order(tmp_path, assembler):
    # A P0 group with a LATE form order still assembles before a P2 group
    # with an EARLY form order; untiered groups rank after every tier.
    write_options(tmp_path / "a.json", [
        _single("style_tail", "P2", "wet brush strokes", 1),
        _single("species", "P0", "lamia", 99),
        _single("plain_extra", None, "holding a lantern", 2),
    ])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False,
                                  strict=True)
    # form layout (order, id) is untouched by tier
    assert [g.id for g in catalog.groups()] == [
        "style_tail", "plain_extra", "species"]
    # assembly is tier-bucketed
    assert [g.id for g in _assembly_groups(catalog)] == [
        "species", "style_tail", "plain_extra"]

    record = CharacterRecord.create(
        name="Tiered", age=25,
        selections={"gender_presentation": "feminine", "species": "on",
                    "style_tail": "on", "plain_extra": "on"})
    ap = assembler.assemble(record, catalog)
    pos = ap.positive
    assert pos.index("lamia") < pos.index("wet brush strokes")
    assert pos.index("wet brush strokes") < pos.index("holding a lantern")


def test_stable_order_within_a_tier_bucket(tmp_path, assembler):
    write_options(tmp_path / "a.json", [
        _single("b_second", "P1", "silver hair", 20),
        _single("a_first", "P1", "golden eyes", 10),
    ])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False,
                                  strict=True)
    assert [g.id for g in _assembly_groups(catalog)] == ["a_first", "b_second"]


def test_scene_assembly_ignores_tiers(tmp_path):
    # img:scene stays assemble_scene's — the scene path keeps the flat
    # (order, id) walk even over a tiered catalog.
    from app.model.builder import BuilderRecord

    write_options(tmp_path / "a.json", [
        _single("mood", "P0", "moonlit", 99),
        _single("place", None, "rooftop garden", 1),
    ])
    catalog = load_option_catalog(dirs=[tmp_path], include_bundled=False,
                                  strict=True)
    record = BuilderRecord.create(kind="scene", name="Roof",
                                  selections={"place": "on", "mood": "on"})
    ap = PromptAssembler().assemble_scene(record, catalog)
    # flat order: place (order 1) before mood (order 99), tier ignored
    assert ap.positive.index("rooftop garden") < ap.positive.index("moonlit")


def test_gate_closed_assembly_skips_gated_fragment(tmp_path, assembler):
    # Structural governance: the assembler consumes the SAME catalog the gate
    # shaped. A record holding a gated selection renders WITHOUT the fragment
    # when the gated dir wasn't loaded — a skip, never a raise or a filter.
    ungated = tmp_path / "options"
    gated = tmp_path / "options_gated"
    ungated.mkdir()
    gated.mkdir()
    write_options(ungated / "10_base.json",
                  [_single("outfit_kind", None, "sundress", 10)])
    write_options(gated / "90_intimate.json",
                  [_single("intimate_wear", "P2", "lingerie", 90)])

    record = CharacterRecord.create(
        name="Gated", age=30,
        selections={"gender_presentation": "feminine", "outfit_kind": "on",
                    "intimate_wear": "on"})

    closed = load_option_catalog(dirs=[ungated], include_bundled=False,
                                 strict=True)
    ap = assembler.assemble(record, closed)
    assert "sundress" in ap.positive
    assert "lingerie" not in ap.positive

    open_ = load_option_catalog(dirs=[ungated, gated], include_bundled=False,
                                strict=True)
    ap = assembler.assemble(record, open_)
    assert "lingerie" in ap.positive


# -- the first-window contract (real tokenizer) --------------------------------

# A V2-shaped worst case: the P0 species core + every P1 render-identity group
# a maximal non-human character carries (CHARACTER_VOCABULARY_V2 subsets A/B),
# with realistic Danbooru-register fragments, plus P2/P3/untiered ballast deep
# enough to overflow several windows. The contract under test: chunking keeps
# everything, and the tier sort keeps P0+P1 (with the structural anchors and
# the LoRA trigger) inside window 0.
_V2_P0_P1 = [
    _single("race", "P0", "lamia", 10),
    _single("apparent_age", "P1", "mature female", 12),
    _single("body_type", "P1", "voluptuous", 30),
    _single("skin_tone", "P1", "dark skin", 20),
    _single("hair_color", "P1", "silver hair", 21),
    _single("hair_length", "P1", "very long hair", 22),
    _single("hair_style", "P1", "braided ponytail", 23),
    _single("eye_color", "P1", "golden eyes", 24),
    _single("ears", "P1", "pointy ears", 40),
    _single("horns", "P1", "curved horns", 41),
    _single("wings", "P1", "large feathered wings", 42),
    _single("lower_body", "P1", "snake lower body", 43),
    _single("scale_color", "P1", "blue scales", 44),
    _single("undead_state", "P1", "pale skin", 45),
]

_BALLAST = [
    _single("makeup", "P2", "smoky eye makeup, bold red lips", 50),
    _single("marks", "P2",
            "freckles, beauty mark under eye, glowing rune tattoos on arms",
            51),
    _single("tattoo_motif", "P2", "intricate floral tattoo sleeve", 52),
    _single("outfit", "P2",
            "elegant evening gown, gold jewelry, ornate necklace, long gloves",
            53),
    _single("accessories", "P2",
            "round glasses, ribbon choker, hoop earrings, flower in hair, "
            "wide-brim hat, parasol", 54),
    _single("outfit_fit", "P3", "form-fitting clothes", 60),
    _single("outfit_condition", "P3", "pristine immaculate fabric", 61),
    _single("aesthetic", "P3",
            "gothic elegant aesthetic, gloomy romantic atmosphere", 62),
    _single("complexion", "P3", "flawless luminous complexion", 63),
    _single("face_shape", "P3", "heart-shaped face", 64),
    _single("nose", "P3", "small upturned nose", 65),
    _single("lips", "P3", "full lips", 66),
    _single("eyebrows", "P3", "arched eyebrows", 67),
    _single("signature_extra", None,
            "standing in a moonlit ruined cathedral, stained glass windows, "
            "floating candles, dramatic lighting, wind-swept hair", 70),
]


@pytest.mark.skipif(not _REAL_TOKENIZER,
                    reason="the model's CLIP tokenizer is not on disk here")
def test_p0_p1_fit_the_first_window_on_a_maximal_record(tmp_path, settings,
                                                        assembler):
    settings.set("models.image.pipeline_config_dir", "models/sdxl_config")
    count = clip_token_counter(settings)
    assert count is not None

    # own subdir: the settings fixture writes settings.json into tmp_path,
    # and the loader globs every *.json in a directory it is given
    opts = tmp_path / "opts"
    opts.mkdir()
    write_options(opts / "v2.json", _V2_P0_P1 + _BALLAST)
    catalog = load_option_catalog(dirs=[opts], include_bundled=False,
                                  strict=True)

    record = CharacterRecord.create(
        name="Maximal", age=140,
        selections={"gender_presentation": "feminine",
                    **{g["id"]: "on" for g in _V2_P0_P1 + _BALLAST}})

    # the 5.5b 6-hex LoRA trigger rides in the lead slot, inside window 0
    ap = assembler.assemble(record, catalog, lead=(("lora", "a1b2c3"),))
    report = token_report(ap, count)

    # the ballast must overflow the single window, or the test proves nothing
    assert report["total"] > CLIP_CONTENT_BUDGET
    assert report["boundary_index"] < len(ap.pieces)

    tier_by_group = {g.id: g.tier for g in catalog.groups()}

    def piece_tier(source):
        # "selections.<gid>" / "tags.<gid>.<opt>" -> that group's tier;
        # structural anchors + the trigger are window-0 head material.
        if source in ("quality", "subject", "age", "lora"):
            return "P0"
        for prefix in ("selections.", "tags."):
            if source.startswith(prefix):
                gid = source[len(prefix):].split(".", 1)[0]
                return tier_by_group.get(gid)
        return None

    boundary = report["boundary_index"]
    for i, piece in enumerate(ap.pieces):
        tier = piece_tier(piece.source)
        if tier in ("P0", "P1"):
            assert i < boundary, (
                f"P0/P1 fragment {piece.source!r} ({piece.text!r}) landed "
                f"outside the first 77-token window (index {i}, boundary "
                f"{boundary}) — the V2 tier contract is broken")

    # and the tier sort actually did the work: some P2/P3/untiered fragment
    # sits past the boundary while every P0/P1 sits inside
    assert any(piece_tier(p.source) not in ("P0", "P1")
               for p in ap.pieces[boundary:])


# -- the first-window contract over the REAL authored catalog (5.6c) ----------

# Worst UI-constructible records per heavy species class-set: the assembler
# ignores visible_when (visibility is a form concern), but the contract is
# over records the conditional form can actually produce — one race's full
# class set firing its P1 identity carriers, with token-heavy option choices
# everywhere and every unconditional P1 group set.
_HEAVY_BASE = {
    "gender_presentation": "feminine",
    "skin_tone": "gold_metallic",          # "metallic gold skin"
    "hair_color": "strawberry_blonde",
    "hair_style": "crown_braid",
    "eye_color": "pupil_less_white",       # "blank white eyes"
    "body_type": "voluptuous",
    "apparent_age": "40s",                 # "middle-aged adult"
    "hair_length": "floor_length",         # "absurdly long hair"
    "ears": "pointed_long",
    "horns": "forward_curve",              # "forward-curving horns"
    "tail": "kitsune_nine",                # "nine fox tails"
    "wings": "large_feathered",
}

_HEAVY_SPECIES = {
    "lamia": {"lower_body": "serpent_coil", "scale_color": "obsidian"},
    "harpy": {"lower_body": "bird_legs", "feather_color": "golden"},
    "ghost": {"undead_state": "partially_skeletal",
              "ethereal_opacity": "mostly_transparent"},
    "android": {"chassis_finish": "brass_clockwork",
                "faceplate": "synth_skin_seams"},
}

_HEAVY_P2_P3 = {
    "hybrid_race": "starborn_celestial", "archetype": "courtesan",
    "complexion": "battle_worn", "hair_color_2": "strawberry_blonde",
    "hair_color_pattern": "split", "bangs": "hime_side_locks",
    "facial_hair": "mutton_chops", "eye_color_2": "pupil_less_white",
    "eye_shape": "downturned", "face_shape": "heart", "lips": "very_full",
    "nose": "aquiline", "eyebrows": "arched", "makeup": "festival_paint",
    "height_band": "towering", "muscle_def": "powerfully_built",
    "tattoo_motif": "minimalist_line", "chest_size": "very_large",
    "waist": "narrow", "hips": "very_wide", "rear": "heavy",
    "body_hair": "moderate", "outfit_fit": "form_fitting",
    "outfit_condition": "tattered", "neckline": "low_cut",
    "chest_shape": "teardrop", "genitalia": "both",
    "genitalia_size": "very_large", "grooming": "styled",
}


@pytest.mark.skipif(not _REAL_TOKENIZER,
                    reason="the model's CLIP tokenizer is not on disk here")
@pytest.mark.parametrize("race", sorted(_HEAVY_SPECIES))
def test_real_catalog_p0_p1_fit_the_first_window(race, settings, assembler):
    from app.model.options import BUNDLED_GATED_OPTIONS_DIR

    settings.set("models.image.pipeline_config_dir", "models/sdxl_config")
    count = clip_token_counter(settings)
    assert count is not None

    catalog = load_option_catalog(dirs=[BUNDLED_GATED_OPTIONS_DIR],
                                  strict=True)
    assert catalog.errors == []

    selections = {"race": race, **_HEAVY_BASE, **_HEAVY_SPECIES[race],
                  **_HEAVY_P2_P3}
    tags = {gid: [o.id for o in catalog.get(gid).options]
            for gid in ("eye_features", "other_features", "marks",
                        "tattoo_placement", "piercings", "accessories",
                        "aesthetic")}
    tags["outfit"] = ["royal_regalia", "plate_armor", "lingerie"]
    tags["outfit_palette"] = ["burgundy", "gold"]
    record = CharacterRecord.create(
        name="Maximal", age=140, selections=selections, tags=tags,
        free_text={"signature_note":
                   "a crescent scar over one brow, opal pendant"})
    assert record.validate_against(catalog) == []

    ap = assembler.assemble(record, catalog, lead=(("lora", "a1b2c3"),))
    report = token_report(ap, count)

    # the detail load must overflow the single window or this proves nothing
    assert report["total"] > CLIP_CONTENT_BUDGET
    assert report["boundary_index"] < len(ap.pieces)

    tier_by_group = {g.id: g.tier for g in catalog.groups()}

    def piece_tier(source):
        if source in ("quality", "subject", "age", "lora"):
            return "P0"
        for prefix in ("selections.", "tags."):
            if source.startswith(prefix):
                gid = source[len(prefix):].split(".", 1)[0]
                return tier_by_group.get(gid)
        return None

    boundary = report["boundary_index"]
    for i, piece in enumerate(ap.pieces):
        if piece_tier(piece.source) in ("P0", "P1"):
            assert i < boundary, (
                f"{race}: P0/P1 fragment {piece.source!r} ({piece.text!r}) "
                f"landed outside the first window (index {i}, boundary "
                f"{boundary})")
    assert any(piece_tier(p.source) not in ("P0", "P1")
               for p in ap.pieces[boundary:])
