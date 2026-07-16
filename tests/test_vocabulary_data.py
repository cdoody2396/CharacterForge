"""5.6c Subset A+B vocabulary data (CHARACTER_VOCABULARY_V2.md §3-§4).

Guards over the authored catalog itself: every shipped fragment passes the
Layer-1 gate at assembly (flag 8) — per-fragment, across fragment boundaries,
and end-to-end through PromptAssembler over maximal per-family records with
the content gate open; visible_when wiring is referentially intact (a typo'd
class silently hides a group forever — degrade-to-visible covers junk shapes,
not wrong names); the required-7 ids stay byte-identical; legacy records
carrying retired groups (height/weight/muscle sliders, pre-V2 ids) load
leniently and lint (user decision 2026-07-16: keep + lint only).
"""

import pytest

from app.imagegen.prompt import PromptAssembler
from app.model import CharacterRecord, load_option_catalog
from app.model.options import BUNDLED_GATED_OPTIONS_DIR
from app.safety import filter_text


@pytest.fixture(scope="module")
def gated_catalog():
    """The full bundled catalog with the content gate open (both dirs)."""
    return load_option_catalog(dirs=[BUNDLED_GATED_OPTIONS_DIR], strict=True)


@pytest.fixture(scope="module")
def ungated_catalog():
    return load_option_catalog(strict=True)


@pytest.fixture(scope="module")
def assembler():
    return PromptAssembler()


def _render_groups(catalog):
    return [g for g in catalog.groups()
            if g.render and g.is_selection]


def _fragments(catalog):
    """(group id, option id, prompt) for every non-empty render fragment."""
    out = []
    for group in _render_groups(catalog):
        for opt in group.options:
            if opt.prompt:
                out.append((group.id, opt.id, opt.prompt))
    return out


# -- catalog shape ------------------------------------------------------------


def test_catalog_loads_strict_gate_open_and_closed(gated_catalog,
                                                   ungated_catalog):
    assert gated_catalog.errors == []
    assert ungated_catalog.errors == []
    # V2 §3 A6: the enumerated race list (doc says ~86, enumerates 112)
    assert len(gated_catalog.get("race").options) == 112
    assert len(gated_catalog.get("hybrid_race").options) == 112


def test_race_is_the_only_p0(gated_catalog):
    assert [g.id for g in gated_catalog.groups() if g.tier == "P0"] == ["race"]


def test_every_tier_is_valid_and_render_b_groups_are_tiered(gated_catalog):
    for g in gated_catalog.groups():
        assert g.tier in (None, "P0", "P1", "P2", "P3")
        # every render-side selection group carries a tier — only the numeric
        # age group and the Subset-C (render:false) groups stay untiered
        if g.render and g.is_selection and g.id not in ("disposition",
                                                        "traits", "voice"):
            assert g.tier is not None, f"render group {g.id!r} untiered"


def test_required_7_byte_identity_and_outfit_pin(gated_catalog):
    # flag 2: the required/quick ids are frozen byte-identical
    assert tuple(gated_catalog.required_group_ids()) == (
        "race", "gender_presentation", "skin_tone", "hair_color",
        "hair_style", "eye_color", "body_type")
    # app/imagegen/catalog.py pins OUTFIT_GROUP="outfit" reading record.tags:
    # the group keeps its id and a multi-style kind (V2 B56 "existing id")
    outfit = gated_catalog.get("outfit")
    assert outfit is not None and outfit.kind == "multi"
    from app.imagegen.catalog import OUTFIT_GROUP
    assert OUTFIT_GROUP == "outfit"


def test_gated_entries_structurally_absent_when_closed(ungated_catalog,
                                                       gated_catalog):
    for gid in ("chest_shape", "genitalia", "genitalia_size", "grooming"):
        assert ungated_catalog.get(gid) is None
        assert gated_catalog.get(gid) is not None
    outfit = ungated_catalog.get("outfit")
    for oid in ("lingerie", "boudoir_set", "towel_only", "nude"):
        assert not outfit.has_option(oid)
        assert gated_catalog.get("outfit").has_option(oid)
    piercings = ungated_catalog.get("piercings")
    for oid in ("nipple", "genital"):
        assert not piercings.has_option(oid)
        assert gated_catalog.get("piercings").has_option(oid)


# -- visible_when referential integrity ---------------------------------------


def test_visible_when_references_resolve(gated_catalog):
    race = gated_catalog.get("race")
    carried_classes = {c for opt in race.options for c in opt.classes}
    for group in gated_catalog.groups():
        cond = group.visible_when
        if cond is None:
            continue
        ref = gated_catalog.get(cond["group"])
        assert ref is not None, (
            f"{group.id}: visible_when references missing group "
            f"{cond['group']!r}")
        if "in" in cond:
            for oid in cond["in"]:
                assert ref.has_option(oid), (
                    f"{group.id}: visible_when 'in' id {oid!r} not in "
                    f"{ref.id!r}")
        if "class" in cond:
            # every condition class must be carried by >=1 race option —
            # a typo'd class hides the group forever, silently
            assert cond["group"] == "race"
            assert cond["class"] in carried_classes, (
                f"{group.id}: visible_when class {cond['class']!r} carried "
                f"by no race option")


def test_every_race_class_drives_some_condition(gated_catalog):
    # the inverse direction: classes on race options exist to fire conditions
    # (plus the beastfolk family class carried per the 5.6a test precedent)
    conditions = {g.visible_when["class"] for g in gated_catalog.groups()
                  if g.visible_when and "class" in g.visible_when}
    race = gated_catalog.get("race")
    carried = {c for opt in race.options for c in opt.classes}
    assert carried - conditions == {"beastfolk"}


def test_conditional_referents_are_always_visible(gated_catalog):
    # authoring note 2: a condition must reference the required set or an
    # unconditioned group — a conditionally-visible referent has orphan
    # semantics
    for group in gated_catalog.groups():
        cond = group.visible_when
        if cond is None:
            continue
        ref = gated_catalog.get(cond["group"])
        assert ref.visible_when is None, (
            f"{group.id} references conditionally-visible {ref.id}")


# -- flag 8: every shipped fragment passes Layer 1 at assembly ----------------


def test_every_fragment_passes_layer1_in_prompt_context(gated_catalog):
    bad = []
    for gid, oid, prompt in _fragments(gated_catalog):
        result = filter_text(prompt, "prompt")
        if not result.allowed:
            bad.append((gid, oid, prompt,
                        getattr(result, "category", None)))
    assert bad == []


def test_fragment_boundaries_survive_the_adjacency_gate(gated_catalog):
    # Any two fragments can become adjacent in some record (intermediate
    # groups unset), and the assembler's adjacency gate re-checks both an
    # edge-normalized single-space join and a zero-separator concatenation of
    # consecutive pieces (which is how "…hair, red" + "skin-tight…" once
    # formed a slur). Exhaustively cross the deduped fragment edges (last <=2
    # words x first <=2 words, spaced; last word x first word, zero-sep) —
    # ~2x10^5 formations — then prescan with cheap substring/regex passes
    # over the blocklists' own term data (same normalization) and confirm
    # only the candidates through the real filter. Sound for the variants
    # clean authored text can form: plain terms, doubled-letter folds, and
    # the data files' regex rules; leet/digit obfuscation variants cannot
    # arise from these fragments (asserted digit-free below).
    import re

    from app.safety import normalize as norm
    from app.safety.layer1 import _CATEGORY_FILES, _parse_list_file, DATA_DIR

    frags = sorted({p for _, _, p in _fragments(gated_catalog)})
    assert not any(ch.isdigit() for p in frags for ch in p)

    t2 = sorted({norm.normalize(" ".join(p.split()[-2:])) for p in frags})
    h2 = sorted({norm.normalize(" ".join(p.split()[:2])) for p in frags})
    t1 = sorted({norm.normalize(p.split()[-1]) for p in frags})
    h1 = sorted({norm.normalize(p.split()[0]) for p in frags})

    strip = re.compile(r"[^a-z0-9]+")
    terms, regexes = set(), []
    for always, contextual in _CATEGORY_FILES.values():
        for fname in (always, contextual):
            if not fname:
                continue
            t, r = _parse_list_file(DATA_DIR / fname)
            terms.update(t)
            terms.update(norm.collapse_doubles(x) for x in t)
            regexes.extend(r)

    # every spanning split of a <=3-word term is covered by (2-word tail x
    # 1-word head) + (1-word tail x 2-word head); widen this cross if a
    # longer literal term ever lands in the blocklists. The wide (2-word)
    # edges only matter for 3-word terms, so they prune to edges containing
    # one of those terms' chunks — the 1-word x 1-word cross stays exhaustive.
    assert max(len(strip.split(t)) for t in terms) <= 3
    chunks3 = {c for t in terms if len(strip.split(t)) == 3
               for c in strip.split(t)}
    t2r = [t for t in t2 if any(c in t for c in chunks3)]
    h2r = [h for h in h2 if any(c in h for c in chunks3)]

    pairs = sorted({f"{t} {h}" for t in t2r for h in h1}
                   | {f"{t} {h}" for t in t1 for h in h2r}
                   | {f"{t} {h}" for t in t1 for h in h1}
                   | {f"{t}{h}" for t in t1 for h in h1})
    corpus = "\x00".join(pairs)
    corpus_glued = "\x00".join(strip.sub("", p) for p in pairs)

    candidates = set()
    for term in terms:
        if term in corpus:
            candidates.update(p for p in pairs if term in p)
        glued = strip.sub("", term)
        if glued and glued in corpus_glued:
            candidates.update(p for p in pairs if glued in strip.sub("", p))
    for raw in regexes:
        pat = re.compile(raw)
        if pat.search(corpus):
            candidates.update(p for p in pairs if pat.search(p))

    offenders = sorted(p for p in candidates
                       if not filter_text(p, "prompt").allowed)
    assert offenders == [], (
        f"fragment boundary formations tripped Layer 1: {offenders[:5]}")


# one representative per V2 §3 A6 family (+ harpy: feathered & monstrous),
# each with its class-visible species blocks set — the records a user could
# actually construct through the conditional form
_FAMILY_RECORDS = {
    "human": {"apparent_age": "40s"},
    "elf": {"apparent_age": "ageless_adult", "ears": "pointed_long"},
    "oni": {"horns": "single_oni_horn", "skin_tone": "crimson"},
    "catfolk": {"ears": "feline", "tail": "feline", "fur_coverage": "partial",
                "fur_color": "tawny", "fur_pattern": "striped"},
    "lamia": {"lower_body": "serpent_coil", "scale_coverage": "partial",
              "scale_color": "emerald", "scale_sheen": "iridescent"},
    "harpy": {"lower_body": "bird_legs", "feather_coverage": "partial",
              "feather_color": "white", "wings": "large_feathered"},
    "dragon_anthro": {"scale_coverage": "full", "scale_color": "obsidian",
                      "scale_sheen": "glossy", "horns": "curved_back",
                      "tail": "spiked_dragon", "wings": "dragon"},
    "succubus_incubus": {"horns": "demon_crown", "tail": "spade_demon",
                         "wings": "bat"},
    "ghost": {"undead_state": "spectral", "ethereal_opacity": "translucent",
              "glow_color": "ice_blue"},
    "android": {"chassis_finish": "chrome", "chassis_seams": "visible_joints",
                "faceplate": "synth_skin_seams", "ears": "mechanical"},
    "flamekin": {"ethereal_opacity": "faint_shimmer", "glow_color":
                 "ember_orange", "elemental_marks": None},  # multi below
}

_GATED_SPREAD = [  # exercised across the family records, all four configs
    {"outfit": "lingerie", "genitalia": "vulva", "chest_shape": "teardrop",
     "grooming": "bare"},
    {"outfit": "boudoir_set", "genitalia": "penis", "genitalia_size": "large",
     "grooming": "trimmed"},
    {"outfit": "towel_only", "genitalia": "both", "genitalia_size": "average",
     "grooming": "styled"},
    {"outfit": "nude", "genitalia": "none", "chest_shape": "heavy",
     "grooming": "natural"},
]


def _maximal_record(catalog, race, extra, gated):
    selections = {
        "race": race, "gender_presentation": "feminine",
        "skin_tone": "gold_metallic", "hair_color": "strawberry_blonde",
        "hair_style": "crown_braid", "eye_color": "pupil_less_white",
        "body_type": "voluptuous", "apparent_age": "30s",
        "hybrid_race": "fae", "archetype": "courtesan",
        "complexion": "battle_worn", "hair_color_2": "mint",
        "hair_color_pattern": "gradient", "hair_length": "floor_length",
        "bangs": "hime_side_locks", "facial_hair": "stubble",
        "eye_color_2": "crimson", "eye_shape": "upturned",
        "face_shape": "heart", "lips": "very_full", "nose": "aquiline",
        "eyebrows": "arched", "makeup": "war_paint",
        "height_band": "towering", "muscle_def": "massive",
        "tattoo_motif": "irezumi", "chest_size": "huge", "waist": "narrow",
        "hips": "very_wide", "rear": "heavy", "body_hair": "heavy",
        "outfit_fit": "skin_tight", "outfit_condition": "tattered",
        "neckline": "revealing",
    }
    selections.update({k: v for k, v in extra.items() if v is not None})
    selections.update({k: v for k, v in gated.items() if k != "outfit"})
    tags = {
        "eye_features": [o.id for o in catalog.get("eye_features").options],
        "other_features": [o.id for o in
                           catalog.get("other_features").options],
        "marks": [o.id for o in catalog.get("marks").options],
        "tattoo_placement": [o.id for o in
                             catalog.get("tattoo_placement").options],
        "piercings": [o.id for o in catalog.get("piercings").options],
        "outfit": [gated["outfit"], "plate_armor"],
        "outfit_palette": ["burgundy", "gold"],
        "accessories": [o.id for o in catalog.get("accessories").options],
        "aesthetic": [o.id for o in catalog.get("aesthetic").options],
    }
    if "elemental_marks" in extra:
        tags["elemental_marks"] = [
            o.id for o in catalog.get("elemental_marks").options]
    return CharacterRecord.create(
        name=f"Maximal {race}", age=140, selections=selections, tags=tags,
        free_text={"appearance_notes": "a crescent scar over one brow"})


def test_maximal_family_records_assemble_gate_open(gated_catalog, assembler):
    # end-to-end through the real per-fragment + adjacency + joined gates,
    # with every gated option configuration exercised at least once
    for i, (race, extra) in enumerate(_FAMILY_RECORDS.items()):
        gated = _GATED_SPREAD[i % len(_GATED_SPREAD)]
        record = _maximal_record(gated_catalog, race, extra, gated)
        assert record.validate_against(gated_catalog) == [], race
        ap = assembler.assemble(record, gated_catalog,
                                lead=(("lora", "a1b2c3"),))
        species_tag = gated_catalog.get("race").get_option(
            race).prompt.split(",")[0]
        assert species_tag in ap.positive, race


def test_every_outfit_assembles_individually(gated_catalog, assembler):
    # the wardrobe is the largest per-cell-varied fragment surface (the
    # imagegen catalog swaps outfits per cell) — run each one through a real
    # assembly so the adjacency gate sees it next to live neighbors
    base = {
        "race": "human", "gender_presentation": "feminine",
        "skin_tone": "fair", "hair_color": "black", "hair_style": "bob",
        "eye_color": "brown", "body_type": "average",
    }
    for opt in gated_catalog.get("outfit").options:
        record = CharacterRecord.create(
            name="Outfit", age=25, selections=dict(base),
            tags={"outfit": [opt.id]})
        ap = assembler.assemble(record, gated_catalog)
        if opt.prompt:
            assert opt.prompt in ap.positive, opt.id


# -- legacy record leniency (user decision: keep + lint only) ------------------


def test_legacy_record_loads_lints_and_assembles(ungated_catalog, assembler,
                                                 tmp_path):
    from app.model import CharacterStore

    legacy = CharacterRecord.create(
        name="Legacy", age=27,
        selections={"race": "elf", "gender_presentation": "feminine",
                    "skin_tone": "fair", "hair_color": "silver",
                    "hair_style": "long",           # retired style id
                    "eye_color": "violet", "body_type": "athletic",
                    "genital_config": "vulva"},     # retired group id
        tags={"outfit": ["lingerie"],               # gated (closed here)
              "distinctive_features": ["freckles"],  # retired group id
              "style": ["elegant"]},                # retired group id
        sliders={"height": 172, "weight": 60, "muscle": 35})  # retired

    # survives a full store round-trip
    store = CharacterStore(tmp_path / "data")
    store.save(legacy)
    loaded = store.load(legacy.id)
    assert loaded.sliders == {"height": 172, "weight": 60, "muscle": 35}

    # lints, never raises — and names the orphaned groups
    issues = " ".join(loaded.validate_against(ungated_catalog))
    for orphan in ("height", "weight", "muscle", "genital_config",
                   "distinctive_features", "style"):
        assert orphan in issues

    # assembles silently: retired values skip, live values render
    ap = assembler.assemble(loaded, ungated_catalog)
    assert "elf, pointed ears" in ap.positive
    assert "lingerie" not in ap.positive        # gate closed: skipped
    assert "172" not in ap.positive


# -- conditional groups end-to-end through describe() -------------------------


def test_describe_ships_v2_conditions_and_classes(tmp_path, audit):
    import json

    from app.model import CharacterStore
    from app.model.options import BUNDLED_GATED_OPTIONS_DIR as GATED_DIR
    from app.ui.creator import CreatorService

    holder = {"open": True}
    creator = CreatorService(
        store=CharacterStore(tmp_path / "data"), audit=audit,
        option_dirs=(tmp_path / "data" / "options",),
        gated_option_dirs=(GATED_DIR,),
        gate=lambda: holder["open"],
    )
    described = creator.describe()
    by_id = {g["id"]: g for g in described["groups"]}
    assert by_id["fur_color"]["visible_when"] == {
        "group": "race", "class": "beastfolk-mammal"}
    assert by_id["genitalia_size"]["visible_when"] == {
        "group": "genitalia", "in": ["penis", "both"]}
    race_opts = {o["id"]: o for o in by_id["race"]["options"]}
    # class ships as a JSON list (creator.js visibleNow requires an Array)
    assert race_opts["catfolk"]["class"] == ["beastfolk", "beastfolk-mammal"]
    assert "class" not in race_opts["human"]
    json.dumps(described, allow_nan=False)

    holder["open"] = False
    creator.reload()
    described = creator.describe()
    ids = {g["id"] for g in described["groups"]}
    assert "genitalia" not in ids and "chest_shape" not in ids
