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
        # every render-side selection group carries a tier — the numeric age
        # group and the Subset-C/D (render:false) chat-side groups short-circuit
        # here on `g.render` and stay untiered
        if g.render and g.is_selection:
            assert g.tier is not None, f"render group {g.id!r} untiered"


def test_required_7_byte_identity_and_outfit_pin(gated_catalog):
    # flag 2: the 7 protected required/quick ids stay byte-identical; 5.7
    # adds skin_type (the unified surface, user sign-off 2026-07-18) — an
    # ADDITION, the protected 7 untouched. Set-compare: the hair reorder
    # (length -> style -> color) changed sort position, not membership.
    assert set(gated_catalog.required_group_ids()) == {
        "race", "gender_presentation", "skin_type", "skin_tone",
        "hair_color", "hair_style", "eye_color", "body_type"}
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
    # 5.7: the surface color groups re-keyed from race class to skin_type, so
    # several classes no longer fire a visible_when — they remain load-bearing
    # for the library species filter and picker grouping (class metadata is
    # the taxonomy, not only a condition key). The classes still driving
    # conditions are exactly:
    conditions = {g.visible_when["class"] for g in gated_catalog.groups()
                  if g.visible_when and "class" in g.visible_when}
    assert conditions == {"construct", "ethereal", "monstrous",
                          "elemental-cosmic", "undead"}
    # every condition-referenced class is carried by some race option
    race = gated_catalog.get("race")
    carried = {c for opt in race.options for c in opt.classes}
    assert conditions <= carried


def test_conditional_referents_are_at_most_one_hop_deep(gated_catalog):
    # authoring note 2, amended at 5.7: a condition references an
    # unconditioned group, OR a group whose own condition references an
    # unconditioned group (chain depth <= 1 hop — the one-pass client and
    # the one-hop-deeper server drop both converge on such chains; deeper
    # chains would not). The only shipped chain is
    # hair_color_pattern -> hair_color_2 -> hair_length.
    chained = []
    for group in gated_catalog.groups():
        cond = group.visible_when
        if cond is None:
            continue
        ref = gated_catalog.get(cond["group"])
        if ref.visible_when is None:
            continue
        chained.append(group.id)
        ref2 = gated_catalog.get(ref.visible_when["group"])
        assert ref2.visible_when is None, (
            f"{group.id} -> {ref.id} -> {ref2.id} chains deeper than one hop")
    assert chained == ["hair_color_pattern"], chained


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
# each with its skin_type surface + visible species blocks set — the records
# a user could actually construct through the conditional form (5.7: surface
# groups key off skin_type; the *_coverage groups are retired)
_FAMILY_RECORDS = {
    "human": {"apparent_age": "40s"},
    "elf": {"apparent_age": "ageless_adult", "ears": "pointed_long"},
    "oni": {"horns": "single_oni_horn", "skin_tone": "crimson"},
    "catfolk": {"ears": "feline", "tail": "feline",
                "skin_type": "fur_over_skin",
                "fur_color": "tawny", "fur_pattern": "striped"},
    "lamia": {"lower_body": "serpent_coil", "skin_type": "scales_over_skin",
              "scale_color": "emerald", "scale_sheen": "iridescent"},
    "harpy": {"lower_body": "bird_legs", "skin_type": "feathers_over_skin",
              "feather_color": "white", "wings": "large_feathered"},
    # 5.7 UI pass: on full_scales the tone lives in skin_tone (relabeled
    # Scale Tone) — scale_color is over-skin only now, so the form never
    # sends it here
    "dragon_anthro": {"skin_type": "full_scales", "scale_sheen": "glossy",
                      "horns": "curved_back", "tail": "spiked_dragon",
                      "wings": "dragon"},
    "succubus_incubus": {"horns": "demon_crown", "tail": "spade_demon",
                         "wings": "bat"},
    "ghost": {"undead_state": "spectral", "skin_type": "ethereal_form",
              "ethereal_opacity": "translucent", "glow_color": "ice_blue"},
    "android": {"skin_type": "metal_chassis", "chassis_finish": "chrome",
                "chassis_seams": "visible_joints",
                "faceplate": "synth_skin_seams", "ears": "mechanical"},
    "flamekin": {"skin_type": "ethereal_form",
                 "ethereal_opacity": "faint_shimmer", "glow_color":
                 "ember_orange", "elemental_marks": None},  # multi below
}

# surfaces without visible skin (5.7 UI pass): skin_tone is now ALWAYS
# visible and required (it relabels per surface in the creator), but
# complexion keys to skin-bearing surfaces — the maximal record drops it for
# these, exactly as the form would
_SKINLESS_SURFACES = {"full_fur", "full_plumage", "full_scales", "stone",
                      "metal_chassis", "ethereal_form"}

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
        "skin_type": "bare_skin",
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
    if selections["skin_type"] in _SKINLESS_SURFACES:
        selections.pop("complexion", None)  # hidden -> the form never sends it
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
        free_text={"signature_note": "a crescent scar over one brow"})


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
        "skin_type": "bare_skin", "skin_tone": "fair",
        "hair_color": "black", "hair_style": "bob",
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
        "group": "skin_type", "in": ["fur_over_skin"]}  # 5.7 UI pass
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


# -- Subset C/D (5.6d): render:false chat-side vocabulary ---------------------

# doc-derived group -> option count (CHARACTER_VOCABULARY_V2.md §5 C1-C31,
# §6 D1-D34). C25 catchphrase + companion_name are free-text slots (flag 10),
# not groups; the D-vi builder handoff is home:builder, not authored here.
# quirks enumerates 36 (header ~36); occupation enumerates 151 (header ~125);
# fav_food enumerates 35 (header 36) - the enumerated lists are authoritative.
_CD_GROUPS = {
    # C-i/ii/iii (50_mind.json)
    "warmth": 5, "energy": 5, "assertiveness": 5, "candor": 5, "impulse": 5,
    "default_mood": 9, "traits": 71, "flaws": 28, "quirks": 36, "vices": 15,
    "values": 22, "moral_compass": 5, "fears": 22, "near_goal": 30,
    "life_dream": 20, "lines_never_cross": 16, "intellect_style": 8,
    "skills": 72, "signature_skill": 72,
    # C-iv/v (55_speech.json)
    "voice_timbre": 20, "speech_pace": 5, "speech_register": 6,
    "speech_patterns": 26, "verbal_tic": 14, "accent_flavor": 14, "laugh": 8,
    "expressiveness": 5, "temper_fuse": 5, "affection_style": 10,
    "comfort_ritual": 14,
    # D (70_life.json)
    "setting": 31, "roots": 9, "locale": 24, "social_standing": 7,
    "reputation": 6, "legal_status": 7, "occupation": 151, "workplace": 38,
    "job_feeling": 5, "origin_story": 20, "family_now": 8, "siblings": 5,
    "defining_events": 52, "secrets": 18, "turning_point": 16,
    "living_situation": 17, "finances": 6, "companion": 15, "hobbies": 61,
    "fav_food": 35, "disliked_food": 14, "music_taste": 22, "pet_peeves": 20,
    "with_strangers": 9, "warming_pace": 5, "with_friends": 10,
    "toward_authority": 6, "in_conflict": 7, "trust": 5, "when_interested": 8,
    "attachment_behavior": 5, "jealousy": 5, "address_habits": 8,
    "avoided_topics": 14,
}

_REQ7 = {  # the protected 7 + the 5.7 skin_type surface
    "race": "human", "gender_presentation": "feminine",
    "skin_type": "bare_skin", "skin_tone": "fair",
    "hair_color": "black", "hair_style": "bob", "eye_color": "brown",
    "body_type": "average",
}


def _cd_maximal_record(catalog):
    """The required-7 plus EVERY Subset C/D option (every single's first option
    + every option of each multi, incl. the 72 skills / 72 signature_skill).
    Constructing it runs the record's Layer-1 gate over every C/D option id in
    the strict 'prompt' context (character.py) - the real gating surface for
    render:false vocabulary, since the fragments never reach image assembly."""
    selections = dict(_REQ7)
    tags = {}
    for gid in _CD_GROUPS:
        g = catalog.get(gid)
        if g.multi:
            tags[gid] = [o.id for o in g.options]
        else:
            selections[gid] = g.options[0].id
    return CharacterRecord.create(
        name="CD Maximal", age=140, selections=selections, tags=tags)


def test_cd_group_counts_match_the_doc(gated_catalog):
    for gid, n in _CD_GROUPS.items():
        g = gated_catalog.get(gid)
        assert g is not None, f"missing C/D group {gid!r}"
        assert not g.render, f"C/D group {gid!r} must be render:false"
        assert len(g.options) == n, (gid, len(g.options), n)
    # C19 signature_skill mirrors C18 skills, conditioned on it having a value;
    # skills itself must stay unconditioned (authoring note 2 - a conditional
    # referent has orphan semantics)
    ss, sk = gated_catalog.get("signature_skill"), gated_catalog.get("skills")
    assert ss.visible_when == {"group": "skills", "any": True}
    assert sk.visible_when is None
    assert {o.id for o in ss.options} == {o.id for o in sk.options}
    # pre-V2 disposition/voice retire (keep+lint); traits id is reused by C7
    assert gated_catalog.get("disposition") is None
    assert gated_catalog.get("voice") is None
    assert gated_catalog.get("traits") is not None


def test_maximal_cd_record_passes_prompt_context_gate(gated_catalog):
    # constructs without ContentBlocked => every C/D option id clears Layer-1
    # in the strict prompt context (the option-id channel on record save)
    record = _cd_maximal_record(gated_catalog)
    assert record.validate_against(gated_catalog) == []


def test_cd_fragments_pass_layer1_in_prompt_context(gated_catalog):
    # hygiene: render:false fragments never reach image assembly (so the flag-8
    # fragment scan skips them), but they feed 6d persona injection - keep them
    # Layer-1 clean anyway
    bad = []
    for gid in _CD_GROUPS:
        for opt in gated_catalog.get(gid).options:
            if opt.prompt and not filter_text(opt.prompt, "prompt").allowed:
                bad.append((gid, opt.id, opt.prompt))
    assert bad == []


def test_render_false_cd_groups_contribute_zero_fragments(gated_catalog,
                                                         assembler):
    # leak check: no render:false C/D group reaches the assembled image prompt
    record = _cd_maximal_record(gated_catalog)
    ap = assembler.assemble(record, gated_catalog)
    leaked = set()
    for p in ap.pieces:
        parts = p.source.split(".")
        if len(parts) >= 2 and parts[0] in ("selections", "tags", "sliders"):
            if parts[1] in _CD_GROUPS:
                leaked.add(parts[1])
    assert leaked == set(), f"C/D fragments leaked into the image: {leaked}"
    # spot-check: distinctive SELECTED render:false descriptors stay absent
    for frag in ("a soft voice", "skilled at cooking", "fears abandonment",
                 "works as an office worker"):
        assert frag not in ap.positive


def test_legacy_appearance_notes_free_text_migrates_and_renders(
        gated_catalog, assembler, tmp_path):
    from app.model import CharacterStore

    # a pre-5.6d record stored its visual note under appearance_notes
    rec = CharacterRecord.create(
        name="Legacy Note", age=30, selections=dict(_REQ7),
        free_text={"appearance_notes": "a crescent scar over one brow"})
    store = CharacterStore(tmp_path / "data")
    store.save(rec)

    loaded = store.load(rec.id)
    # from_dict rehomes the legacy key to its B63 successor
    assert "appearance_notes" not in loaded.free_text
    assert loaded.free_text.get("signature_note") == \
        "a crescent scar over one brow"

    # ...and it still feeds the image prompt (signature_note is the one visual
    # free-text slot after the swap)
    ap = assembler.assemble(loaded, gated_catalog)
    assert "a crescent scar over one brow" in ap.positive
    assert any(p.source == "free_text.signature_note" for p in ap.pieces)
