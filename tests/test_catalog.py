"""Stage 3e — sandbox-verifiable core: the catalog matrix logic, config
coercion, and the assembler's catalog hooks (exclude_groups / lead / extra).
No torch — the LoRA generation + auto-filter are tested via fakes elsewhere."""

import json

import pytest

from app.config import Settings
from app.imagegen import PromptAssembler
from app.imagegen.catalog import (
    ASIS_OUTFIT,
    CatalogCell,
    CatalogConfig,
    CatalogState,
    STATE_KEYS,
    build_cells,
    coerce_catalog_config,
    load_catalog_states,
    record_outfits,
    resolve_cell,
)
from app.model import CharacterRecord, load_option_catalog


def make_record(**kw):
    base = dict(name="Cat Test", age=24,
                selections={"race": "elf", "gender_presentation": "feminine"},
                tags={"outfit": ["casual", "formal"]})
    base.update(kw)
    return CharacterRecord.create(**base)


# -- states + config ---------------------------------------------------------


def test_load_catalog_states():
    expr, poses = load_catalog_states()
    assert len(expr) >= 5 and len(poses) >= 4
    assert all(isinstance(s, CatalogState) and s.id and s.prompt for s in expr)


def test_load_catalog_states_malformed_yields_empty(monkeypatch, tmp_path):
    # Review: valid-but-non-object JSON ([]/null/42) raised AttributeError
    # (contradicting the "malformed -> empty" contract). Now empty lists.
    import app.imagegen.catalog as cat
    bad = tmp_path / "states.json"
    for blob in ("{not json", "[]", "null", "42", '"x"'):
        bad.write_text(blob, encoding="utf-8")
        monkeypatch.setattr(cat, "STATES_FILE", bad)
        assert cat.load_catalog_states() == ([], [])


def test_catalog_config_face_area_min_default():
    from app.imagegen.catalog import CatalogConfig
    assert CatalogConfig().face_area_min == 0.01  # relaxed for pose-varied frames


def test_coerce_catalog_config_defaults(tmp_path):
    cfg = coerce_catalog_config(Settings(tmp_path / "s.json"))
    assert cfg.max_frames == 48 and cfg.max_attempts == 2 and cfg.lora_scale == 1.0


def test_coerce_catalog_config_clamps_bad_hand_edits(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"image_gen": {"catalog": {
        "max_frames": 10_000, "max_expressions": 0, "lora_scale": "NaN",
        "max_attempts": 99, "max_poses": "lots",
    }}}), encoding="utf-8")
    cfg = coerce_catalog_config(Settings(path))
    assert cfg.max_frames == 512          # clamped to hi
    assert cfg.max_expressions == 1       # 0 -> clamped to lo
    assert cfg.lora_scale == 1.0          # NaN -> default
    assert cfg.max_attempts == 10         # clamped to hi
    assert cfg.max_poses == 4             # "lots" -> default


# -- outfits + matrix --------------------------------------------------------


def test_record_outfits_from_wardrobe():
    catalog = load_option_catalog()
    outfits = record_outfits(make_record(), catalog)
    ids = [o[0] for o in outfits]
    assert "casual" in ids and "formal" in ids
    assert all(prompt for _, prompt in outfits)


def test_record_outfits_asis_when_no_wardrobe():
    catalog = load_option_catalog()
    outfits = record_outfits(make_record(tags={}), catalog)
    assert outfits == [(ASIS_OUTFIT, "")]


def test_build_cells_matrix_and_caps():
    catalog = load_option_catalog()
    expr, poses = load_catalog_states()
    cfg = CatalogConfig(max_expressions=3, max_poses=2, max_outfits=2, max_frames=48)
    cells = build_cells(make_record(), catalog, expr, poses, cfg)
    # 2 outfits (casual, formal) x 3 expr x 2 poses = 12
    assert len(cells) == 12
    states = {(c.outfit_id, c.expression_id, c.pose_id) for c in cells}
    assert len(states) == 12  # unique cells
    assert {c.outfit_id for c in cells} == {"casual", "formal"}


def test_build_cells_respects_max_frames():
    catalog = load_option_catalog()
    expr, poses = load_catalog_states()
    cfg = CatalogConfig(max_expressions=5, max_poses=5, max_outfits=2, max_frames=7)
    cells = build_cells(make_record(), catalog, expr, poses, cfg)
    assert len(cells) == 7


def test_build_cells_empty_without_states():
    catalog = load_option_catalog()
    assert build_cells(make_record(), catalog, [], [], CatalogConfig()) == []


def test_cell_extra_fragments():
    cell = build_cells(make_record(), load_option_catalog(),
                       [CatalogState("smile", "gentle smile")],
                       [CatalogState("portrait", "upper body portrait")],
                       CatalogConfig())[0]
    extra = cell.extra()
    texts = [t for _, t in extra]
    assert "gentle smile" in texts and "upper body portrait" in texts
    assert cell.state() == {"expression": "smile", "pose": "portrait",
                            "outfit": "casual"}


# -- resolve_cell (Stage 3g state vocabulary) ---------------------------------


def _resolve(state, record=None):
    catalog = load_option_catalog()
    expr, poses = load_catalog_states()
    return resolve_cell(record or make_record(), catalog, expr, poses, state)


def test_resolve_cell_happy_path():
    cell = _resolve({"expression": "smile", "pose": "sitting",
                     "outfit": "casual"})
    assert isinstance(cell, CatalogCell)
    assert cell.state() == {"expression": "smile", "pose": "sitting",
                            "outfit": "casual"}
    # prompts come from the DATA, never the caller (5.5g canonical booru tags)
    assert cell.expression_prompt == "smile"
    assert cell.pose_prompt == "sitting"
    assert cell.outfit_prompt  # from the option catalog


def test_resolve_cell_asis_always_valid():
    # even with a wardrobe defined, the base look is a legitimate state
    cell = _resolve({"expression": "neutral", "pose": "portrait",
                     "outfit": ASIS_OUTFIT})
    assert isinstance(cell, CatalogCell)
    assert cell.outfit_id == ASIS_OUTFIT and cell.outfit_prompt == ""
    # and it is the only outfit for a wardrobe-less record
    cell2 = _resolve({"expression": "neutral", "pose": "portrait",
                      "outfit": ASIS_OUTFIT}, record=make_record(tags={}))
    assert isinstance(cell2, CatalogCell)


@pytest.mark.parametrize("bad,expected_kind", [
    (None, "invalid"),
    ("smile", "invalid"),
    ([], "invalid"),
    ({}, "invalid"),                                          # missing keys
    ({"expression": "smile", "pose": "sitting"}, "invalid"),  # missing outfit
    ({"expression": "smile", "pose": "sitting", "outfit": "casual",
      "extra": "x"}, "invalid"),                              # unknown key
    ({"expression": 5, "pose": "sitting", "outfit": "casual"}, "invalid"),
    ({"expression": " ", "pose": "sitting", "outfit": "casual"}, "invalid"),
    ({"expression": "nope", "pose": "sitting", "outfit": "casual"},
     "unknown_state"),
    ({"expression": "smile", "pose": "nope", "outfit": "casual"},
     "unknown_state"),
    ({"expression": "smile", "pose": "sitting", "outfit": "ballgown"},
     "unknown_state"),                                        # not in wardrobe
])
def test_resolve_cell_rejects_bad_shapes_and_unknown_ids(bad, expected_kind):
    result = _resolve(bad)
    assert isinstance(result, tuple)
    kind, message = result
    assert kind == expected_kind and message


def test_resolve_cell_outfit_must_be_this_characters():
    # a REAL outfit option that this record did not select is still unknown
    result = _resolve({"expression": "smile", "pose": "sitting",
                       "outfit": "fantasy_armor"},
                      record=make_record(tags={"outfit": ["casual"]}))
    assert result[0] == "unknown_state"


def test_state_keys_are_the_documented_triple():
    assert STATE_KEYS == ("expression", "pose", "outfit")


# -- assembler catalog hooks -------------------------------------------------


def test_assembler_exclude_lead_extra():
    asm = PromptAssembler()
    catalog = load_option_catalog()
    record = make_record()
    ap = asm.assemble(
        record, catalog,
        exclude_groups=frozenset({"outfit"}),
        lead=(("trigger", "cfidabc123"),),
        extra=(("state.expression.smile", "gentle smile"),
               ("state.pose.portrait", "upper body portrait")))
    pos = ap.positive
    assert "cfidabc123" in pos                       # trigger injected
    assert "gentle smile" in pos and "upper body portrait" in pos
    assert "casual clothing" not in pos and "formal attire" not in pos  # outfit excluded
    # trigger leads the identity groups (after the anchors)
    assert pos.index("cfidabc123") < pos.index("elf, pointed ears")


def test_every_shipped_state_fragment_passes_layer1_at_assembly():
    # 5.5g: the canonical-tag rewrite of catalog_states.json is neutral/adult by
    # design, and the file claims every fragment still passes the Layer-1 prompt
    # gate at assembly. Assert it directly, so a future drop-in edit that adds a
    # blocked fragment fails here rather than silently at generation time.
    asm = PromptAssembler()
    catalog = load_option_catalog()
    record = make_record()
    expressions, poses = load_catalog_states()
    for kind, states in (("expression", expressions), ("pose", poses)):
        for st in states:
            if not st.prompt:
                continue
            ap = asm.assemble(record, catalog,
                              extra=((f"state.{kind}.{st.id}", st.prompt),))
            # each comma-token of the fragment survives the gate verbatim
            for token in (t.strip() for t in st.prompt.split(",")):
                assert token and token in ap.positive, (kind, st.id, token)
    # and a normal assemble (no exclude) DOES include the wardrobe
    assert "casual clothing" in asm.assemble(record, catalog).positive
