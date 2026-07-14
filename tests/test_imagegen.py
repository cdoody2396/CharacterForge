"""Stage 3a + 3b — image pipeline: prompt assembly, engine scaffold, service,
and IP-Adapter baseline identity.

Everything here is the [HERE]-verifiable slice: prompt assembly + gating,
VRAM-slot sequencing, request validation, persistence, audit, IP-Adapter mode
switching, reference path-safety, and steered-generation orchestration. The
real diffusers backend is exercised only far enough to prove it degrades to a
structured error without torch; generation itself validates on hardware.
"""

import json
import os
from pathlib import Path

import pytest

from app.imagegen import (
    EngineBusy,
    EngineUnavailable,
    GenerationFailed,
    GenerationRequest,
    ImageEngine,
    ImageService,
    PromptAssembler,
    PromptBlocked,
    ReferenceUnreadable,
    SAMPLERS,
    build_image_service,
)
from app.imagegen.engine import APP_ROOT, MAX_SEED
from app.model import CharacterRecord, load_option_catalog
from app.safety import filter_text


# -- helpers -----------------------------------------------------------------


def make_record(**kwargs) -> CharacterRecord:
    base = dict(
        name="Test Subject",
        age=22,
        selections={
            "race": "elf",
            "gender_presentation": "feminine",
            "skin_tone": "fair",
            "hair_color": "silver",
            "body_type": "athletic",
            "chest_size": "medium",
        },
        tags={"outfit": ["fantasy_armor"], "distinctive_features": ["freckles"]},
        sliders={"height": 150},
        free_text={"appearance_notes": "A crescent scar over the left eyebrow."},
    )
    base.update(kwargs)
    return CharacterRecord.create(**base)


@pytest.fixture(scope="module")
def bundled_catalog():
    return load_option_catalog()


@pytest.fixture(scope="module")
def assembler():
    return PromptAssembler()


class FakeImage:
    def __init__(self, seed: int):
        self.seed = seed

    def save(self, path):
        Path(path).write_bytes(b"FAKEPNG-" + str(self.seed).encode())


class FakeBackend:
    def __init__(self, checkpoint: Path, config_dir=None, ip_config=None, lora=None):
        self.checkpoint = checkpoint
        self.config_dir = config_dir
        self.ip_config = ip_config
        self.lora = lora
        self.identity = ip_config is not None  # built for the steered (3b) path
        self.catalog = lora is not None         # built for the LoRA (3e) path
        self.requests: list[GenerationRequest] = []
        self.references: list = []
        self.closed = False

    def generate(self, request: GenerationRequest, reference=None):
        self.requests.append(request)
        self.references.append(reference)
        return FakeImage(request.seed)

    def close(self):
        self.closed = True


class FakeFactory:
    def __init__(self):
        self.backends: list[FakeBackend] = []

    def __call__(self, checkpoint: Path, config_dir=None, ip_config=None,
                 lora=None) -> FakeBackend:
        backend = FakeBackend(checkpoint, config_dir, ip_config, lora)
        self.backends.append(backend)
        return backend


@pytest.fixture()
def checkpoint(tmp_path) -> Path:
    path = tmp_path / "models" / "illustrious-test.safetensors"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\0" * 16)
    return path


@pytest.fixture()
def fake_engine(settings, checkpoint):
    settings.set("models.image.checkpoint_path", str(checkpoint))
    factory = FakeFactory()
    engine = ImageEngine(settings, backend_factory=factory)
    engine.factory = factory  # test-side handle
    return engine


@pytest.fixture()
def service(creator, settings, audit, fake_engine) -> ImageService:
    return ImageService(
        creator.store,
        settings,
        audit,
        catalog_provider=lambda: creator.catalog,
        engine=fake_engine,
    )


@pytest.fixture()
def ip_adapter_dir(tmp_path, settings) -> Path:
    """A local h94/IP-Adapter mirror with the standard + plus ViT-H weights
    and the ViT-H image encoder present, wired into settings."""
    root = tmp_path / "models" / "ip_adapter"
    (root / "sdxl_models").mkdir(parents=True)
    (root / "sdxl_models" / "ip-adapter_sdxl_vit-h.safetensors").write_bytes(b"\0" * 8)
    (root / "sdxl_models" / "ip-adapter-plus_sdxl_vit-h.safetensors").write_bytes(b"\0" * 8)
    (root / "models" / "image_encoder").mkdir(parents=True)
    (root / "models" / "image_encoder" / "config.json").write_text("{}", encoding="utf-8")
    settings.set("models.image.ip_adapter.dir", str(root))
    return root


def audit_events(audit):
    path = audit.path_for_today()
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]


# -- prompt assembly -----------------------------------------------------------


def test_assembles_in_documented_order(assembler, bundled_catalog):
    ap = assembler.assemble(make_record(), bundled_catalog)
    # 1 quality → 2 subject → 3 adult anchor + range → 4 options in catalog order
    assert ap.positive.startswith(
        "masterpiece, best quality, very aesthetic, absurdres, "
        "solo, 1girl, adult, young adult"
    )
    pos = ap.positive
    # catalog (order, id): race 10 < skin 20 < hair 21 < body 30 < height 31
    # < chest 40 < features 71 < outfit 90; notes last
    for earlier, later in [
        ("elf, pointed ears", "fair skin"),
        ("fair skin", "silver hair"),
        ("silver hair", "athletic build"),
        ("athletic build", "very short, petite stature"),  # height 150 range
        ("very short, petite stature", "medium breasts"),
        ("medium breasts", "freckles"),
        ("freckles", "ornate fantasy armor"),
        ("ornate fantasy armor", "crescent scar"),
    ]:
        assert pos.index(earlier) < pos.index(later), (earlier, later)
    assert pos.rstrip().endswith("A crescent scar over the left eyebrow.")


def test_non_render_groups_stay_out_of_the_image_prompt(assembler, bundled_catalog):
    record = make_record(
        selections={
            "gender_presentation": "feminine",
            "disposition": "warm",
            "voice": "sultry",
        },
        tags={"traits": ["witty", "loyal"]},
    )
    ap = assembler.assemble(record, bundled_catalog)
    for chat_side in ("warm and affectionate", "sultry voice", "witty", "loyal"):
        assert chat_side not in ap.positive


def test_subject_anchor_maps_presentation(assembler, bundled_catalog):
    masc = make_record(selections={"gender_presentation": "masculine"})
    assert "solo, 1boy" in assembler.assemble(masc, bundled_catalog).positive
    unset = make_record(selections={})
    pos = assembler.assemble(unset, bundled_catalog).positive
    assert "absurdres, solo, adult" in pos
    assert "1girl" not in pos and "1boy" not in pos  # nothing invented


def test_adult_anchor_survives_a_catalog_without_age_ranges(assembler):
    empty = load_option_catalog([], include_bundled=False)
    ap = assembler.assemble(make_record(selections={}, tags={}, sliders={}), empty)
    assert ", adult" in ap.positive  # structural, not data


def test_age_range_fragment_dedupes_against_anchor(assembler, bundled_catalog):
    ap = assembler.assemble(make_record(age=30), bundled_catalog)  # range: "adult"
    assert ap.positive.count(", adult") == 1


def test_unknown_record_groups_skip_silently(assembler, bundled_catalog):
    record = make_record(selections={"future_group": "future_option"})
    ap = assembler.assemble(record, bundled_catalog)
    assert "future_option" not in ap.positive


def test_negative_prompt_carries_safety_then_quality(assembler, bundled_catalog):
    ap = assembler.assemble(make_record(), bundled_catalog)
    neg = ap.negative
    for anchor in ("child", "loli", "shota", "school uniform", "teenager"):
        assert anchor in neg
    assert "worst quality" in neg
    assert neg.index("loli") < neg.index("worst quality")  # safety leads
    # and none of the steer-away vocabulary leaks into the positive
    for anchor in ("loli", "shota", "teenager", "school uniform"):
        assert anchor not in ap.positive


def test_booru_anchors_pass_the_prompt_context_gate():
    assert filter_text("solo, 1girl, adult, young adult", "prompt").allowed


def test_freetext_that_saved_clean_can_still_block_in_prompt_context(
    assembler, bundled_catalog
):
    # "kids" is minors-CONTEXTUAL: passes the freetext record gate with no
    # sexual proximity, but image-prompt context blocks it outright (R7-class
    # strictness). The record saves; the render refuses, naming the field.
    record = make_record(
        free_text={"appearance_notes": "Grew up herding goats with the kids."}
    )
    with pytest.raises(PromptBlocked) as exc_info:
        assembler.assemble(record, bundled_catalog)
    assert exc_info.value.source == "free_text.appearance_notes"
    assert exc_info.value.category == "minors"


def test_dropin_option_fragment_is_gated_with_provenance(assembler, tmp_path):
    # An option file's `prompt` fragment is data no record gate ever saw —
    # the assembler must catch it and name the group.
    (tmp_path / "90_evil.json").write_text(json.dumps({
        "groups": [{
            "id": "setting_theme", "label": "Theme", "kind": "single",
            "options": [{"id": "academy", "label": "Academy",
                         "prompt": "school uniform"}],
        }]
    }), encoding="utf-8")
    catalog = load_option_catalog([tmp_path], include_bundled=False)
    record = make_record(
        selections={"setting_theme": "academy"}, tags={}, sliders={}, free_text={}
    )
    with pytest.raises(PromptBlocked) as exc_info:
        assembler.assemble(record, catalog)
    assert exc_info.value.source == "selections.setting_theme"
    assert exc_info.value.category == "minors"


def test_clean_fragments_joining_into_a_blocked_term_are_caught(
    assembler, tmp_path
):
    # "knee high" and "school of magic" each pass the per-fragment gate, but
    # ", " is two separators — inside the joiner fold's tolerance — so the
    # assembled string contains "high, school" ≙ "high school". The final
    # assembled-string pass must catch what per-fragment passes cannot.
    (tmp_path / "90_join.json").write_text(json.dumps({
        "groups": [
            {"id": "legwear", "label": "Legwear", "kind": "single", "order": 1,
             "options": [{"id": "kneehigh", "label": "Knee-high",
                          "prompt": "knee high"}]},
            {"id": "academy", "label": "Academy", "kind": "single", "order": 2,
             "options": [{"id": "magic", "label": "Magic",
                          "prompt": "school of magic"}]},
        ]
    }), encoding="utf-8")
    catalog = load_option_catalog([tmp_path], include_bundled=False)
    assert filter_text("knee high", "prompt").allowed
    assert filter_text("school of magic", "prompt").allowed
    record = make_record(
        selections={"legwear": "kneehigh", "academy": "magic"},
        tags={}, sliders={}, free_text={},
    )
    with pytest.raises(PromptBlocked) as exc_info:
        assembler.assemble(record, catalog)
    assert exc_info.value.source == "assembled"
    assert exc_info.value.category == "minors"


def test_render_flag_loads_defaults_and_merges(tmp_path):
    (tmp_path / "10_a.json").write_text(json.dumps({
        "groups": [{"id": "g1", "label": "G1", "kind": "single", "options": []}]
    }), encoding="utf-8")
    (tmp_path / "20_b.json").write_text(json.dumps({
        "groups": [{"id": "g1", "render": False}]
    }), encoding="utf-8")
    catalog = load_option_catalog([tmp_path], include_bundled=False)
    assert catalog.get("g1").render is False
    bundled = load_option_catalog()
    assert bundled.get("race").render is True          # default
    assert bundled.get("disposition").render is False  # marked chat-side


# -- engine ---------------------------------------------------------------------


def test_engine_refuses_without_a_configured_checkpoint(settings):
    engine = ImageEngine(settings, backend_factory=FakeFactory())
    with pytest.raises(EngineUnavailable, match="no image checkpoint configured"):
        engine.load()
    assert settings.get("models.active") is None  # slot untouched on failure


def test_engine_refuses_a_missing_checkpoint_file(settings, tmp_path):
    settings.set("models.image.checkpoint_path", str(tmp_path / "missing.safetensors"))
    engine = ImageEngine(settings, backend_factory=FakeFactory())
    with pytest.raises(EngineUnavailable, match="not found"):
        engine.load()


def test_engine_refuses_while_chat_holds_the_slot(fake_engine, settings):
    settings.set("models.active", "chat")
    with pytest.raises(EngineBusy):
        fake_engine.load()


def test_load_takes_the_slot_and_unload_releases_it(fake_engine, settings, checkpoint):
    fake_engine.load()
    assert fake_engine.loaded
    assert settings.get("models.active") == "image"
    assert fake_engine.factory.backends[0].checkpoint == checkpoint
    fake_engine.load()  # idempotent
    assert len(fake_engine.factory.backends) == 1
    fake_engine.unload()
    assert not fake_engine.loaded
    assert settings.get("models.active") is None
    assert fake_engine.factory.backends[0].closed


def test_generate_resolves_and_reports_a_seed(fake_engine):
    result = fake_engine.generate(GenerationRequest(positive="adult", negative=""))
    assert result.request.seed is not None
    assert 0 <= result.request.seed <= MAX_SEED
    explicit = fake_engine.generate(
        GenerationRequest(positive="adult", negative="", seed=1234)
    )
    assert explicit.request.seed == 1234
    assert fake_engine.factory.backends[0].requests[1].seed == 1234


@pytest.mark.parametrize(
    "bad",
    [
        dict(width=830),                 # not a multiple of 8
        dict(width=8),                   # below MIN_DIM
        dict(height=4096),               # above MAX_DIM
        dict(steps=0),
        dict(steps=10_000),
        dict(cfg_scale=0.0),
        dict(sampler="ddim_turbo"),
        dict(seed=-1),
        dict(seed=2**33),
        dict(positive="   "),
    ],
)
def test_generation_request_validation(fake_engine, bad):
    request = GenerationRequest(**{"positive": "adult", "negative": "", **bad})
    with pytest.raises(ValueError):
        fake_engine.generate(request)


def test_relative_checkpoint_resolves_against_app_root(settings):
    settings.set("models.image.checkpoint_path", "models/image/ckpt.safetensors")
    engine = ImageEngine(settings, backend_factory=FakeFactory())
    assert engine.checkpoint_path() == APP_ROOT / "models/image/ckpt.safetensors"


def test_heavy_variant_selects_heavy_path_and_falls_back(settings, tmp_path):
    engine = ImageEngine(settings, backend_factory=FakeFactory())
    settings.set("models.image.checkpoint_path", str(tmp_path / "default.st"))
    settings.set("models.image.variant", "heavy")
    assert engine.checkpoint_path() == tmp_path / "default.st"  # heavy unset
    settings.set("models.image.heavy_checkpoint_path", str(tmp_path / "heavy.st"))
    assert engine.checkpoint_path() == tmp_path / "heavy.st"


def test_status_is_a_cheap_structural_probe(fake_engine, checkpoint):
    status = fake_engine.status()
    assert status["loaded"] is False
    assert status["checkpoint"] == str(checkpoint)
    assert status["checkpoint_exists"] is True
    assert status["samplers"] == list(SAMPLERS)
    assert isinstance(status["torch_installed"], bool)


def test_backend_construction_crash_wraps_to_engine_unavailable(
    settings, checkpoint
):
    # A corrupt checkpoint crashes inside the factory (whatever diffusers
    # raises) — the bridge contract demands a structured error.
    def exploding_factory(*args):
        raise RuntimeError("safetensors header mismatch")

    settings.set("models.image.checkpoint_path", str(checkpoint))
    engine = ImageEngine(settings, backend_factory=exploding_factory)
    with pytest.raises(EngineUnavailable, match="failed to load"):
        engine.load()
    assert settings.get("models.active") is None


def test_backend_generate_crash_wraps_to_generation_failed(fake_engine):
    fake_engine.load()
    fake_engine.factory.backends[0].generate = lambda request: (_ for _ in ()).throw(
        MemoryError("CUDA out of memory")
    )
    with pytest.raises(GenerationFailed, match="CUDA out of memory"):
        fake_engine.generate(GenerationRequest(positive="adult", negative=""))


def test_real_backend_degrades_to_a_structured_error(settings, checkpoint):
    # Default (diffusers) factory on the sandbox: no torch / no CUDA either
    # way must surface as EngineUnavailable, never an ImportError escape.
    settings.set("models.image.checkpoint_path", str(checkpoint))
    engine = ImageEngine(settings)
    with pytest.raises(EngineUnavailable):
        engine.load()
    assert settings.get("models.active") is None


# -- red-team regressions (execution-confirmed bypasses, all fixed) ------------


def _twogroup_catalog(tmp_path, prompt_a, prompt_b):
    (tmp_path / "80_a.json").write_text(json.dumps({
        "groups": [{"id": "g_a", "label": "A", "kind": "single", "order": 80,
                    "options": [{"id": "o", "label": "O", "prompt": prompt_a}]}]
    }), encoding="utf-8")
    (tmp_path / "81_b.json").write_text(json.dumps({
        "groups": [{"id": "g_b", "label": "B", "kind": "single", "order": 81,
                    "options": [{"id": "o", "label": "O", "prompt": prompt_b}]}]
    }), encoding="utf-8")
    return load_option_catalog([tmp_path], include_bundled=False)


@pytest.mark.parametrize(
    "prompt_a, prompt_b",
    [
        ("cute little.", "girl next door"),      # trailing '.' -> 3 separators
        ("cute little...", "girl next door"),     # more padding
        ("cute little)", "girl next door"),       # other terminal punct
        ("knee high.", "school of magic"),        # defeats the test's own example
    ],
)
def test_separator_overflow_join_is_now_blocked(assembler, tmp_path, prompt_a, prompt_b):
    # Finding 1 (HIGH): padding a fragment edge with punctuation used to push
    # a cross-fragment blocked term past the join gate. The edge-normalized
    # adjacency pass now catches it.
    catalog = _twogroup_catalog(tmp_path, prompt_a, prompt_b)
    record = make_record(
        selections={"g_a": "o", "g_b": "o"}, tags={}, sliders={}, free_text={}
    )
    with pytest.raises(PromptBlocked) as exc_info:
        assembler.assemble(record, catalog)
    assert exc_info.value.source == "assembled"
    assert exc_info.value.category == "minors"


def test_word_split_across_option_fragments_is_blocked(assembler, tmp_path):
    # Finding 1 variant: a single always-blocked word split across two option
    # fragments ("sho"+"ta"). The zero-separator option-pair pass catches it.
    catalog = _twogroup_catalog(tmp_path, "sho", "ta portrait")
    record = make_record(
        selections={"g_a": "o", "g_b": "o"}, tags={}, sliders={}, free_text={}
    )
    with pytest.raises(PromptBlocked) as exc_info:
        assembler.assemble(record, catalog)
    assert exc_info.value.source == "assembled"


def test_separator_overflow_into_free_text_is_blocked(assembler, tmp_path):
    # The second half arriving via appearance_notes (the trailing fragment)
    # is still caught by the edge-normalized join.
    (tmp_path / "80_a.json").write_text(json.dumps({
        "groups": [{"id": "g_a", "label": "A", "kind": "single", "order": 80,
                    "options": [{"id": "o", "label": "O", "prompt": "cute little."}]}]
    }), encoding="utf-8")
    catalog = load_option_catalog([tmp_path], include_bundled=False)
    record = make_record(
        selections={"g_a": "o"}, tags={}, sliders={},
        free_text={"appearance_notes": "girl next door"},
    )
    with pytest.raises(PromptBlocked):
        assembler.assemble(record, catalog)


def test_ordinary_prose_does_not_reintroduce_separator_false_positives(
    assembler, bundled_catalog
):
    # The edge-normalized join must NOT concatenate interior prose words:
    # "she shot at dawn" must stay separated, never fold to "shota".
    record = make_record(
        free_text={"appearance_notes": "A duelist who shot a rival at dawn."}
    )
    ap = assembler.assemble(record, bundled_catalog)  # no PromptBlocked
    assert "shot a rival" in ap.positive


def test_classroom_scene_background_now_blocks(assembler, tmp_path):
    # R7 coverage gap: a minor-coded school SCENE via a drop-in background
    # option now blocks (classroom/chalkboard added to the data file).
    (tmp_path / "95_scene.json").write_text(json.dumps({
        "groups": [{"id": "scene", "label": "Scene", "kind": "single",
                    "options": [{"id": "school", "label": "School",
                                 "prompt": "classroom, chalkboard, school desk"}]}]
    }), encoding="utf-8")
    catalog = load_option_catalog([tmp_path], include_bundled=False)
    record = make_record(
        selections={"scene": "school"}, tags={}, sliders={}, free_text={}
    )
    with pytest.raises(PromptBlocked) as exc_info:
        assembler.assemble(record, catalog)
    assert exc_info.value.category == "minors"


def test_classroom_terms_block_in_prompt_context():
    for term in ("classroom", "chalkboard", "blackboard", "school desk"):
        assert not filter_text(term, "prompt").allowed, term


def test_gender_presentation_no_longer_double_emits(assembler, bundled_catalog):
    # Code-review L2: the subject anchor is the single source for gender; the
    # option fragment (render:false) must not duplicate the token.
    andro = make_record(selections={"gender_presentation": "androgynous"})
    pos = assembler.assemble(andro, bundled_catalog).positive
    assert "solo, 1other, androgynous" in pos
    assert pos.count("androgynous") == 1
    fem = make_record(selections={"gender_presentation": "feminine"})
    assert "feminine" not in assembler.assemble(fem, bundled_catalog).positive


# -- review-pass regressions (execution-confirmed findings, all fixed) ---------


def test_slot_persist_failure_does_not_kill_a_loaded_engine(
    fake_engine, settings, monkeypatch
):
    # H1: disk-full/AV-locked settings.json during load(). The pipeline is
    # loaded and in-memory slot state is correct — a failed persist must not
    # raise through the bridge or drop the backend.
    monkeypatch.setattr(
        type(settings), "save", lambda self: (_ for _ in ()).throw(OSError("disk full"))
    )
    fake_engine.load()
    assert fake_engine.loaded
    assert settings.get("models.active") == "image"  # in-memory truth


def test_unload_survives_close_crash_and_persist_failure(
    fake_engine, settings, monkeypatch
):
    fake_engine.load()
    fake_engine.factory.backends[0].close = lambda: (_ for _ in ()).throw(
        RuntimeError("device-side assert")
    )
    monkeypatch.setattr(
        type(settings), "save", lambda self: (_ for _ in ()).throw(OSError("locked"))
    )
    fake_engine.unload()  # must not raise
    assert not fake_engine.loaded
    assert settings.get("models.active") is None


def test_variant_flip_swaps_backend_and_sidecar_records_actual_checkpoint(
    service, creator, settings, fake_engine, tmp_path
):
    # M1: settings changed while loaded — the engine must swap, and the
    # sidecar must record the checkpoint that actually generated the frame.
    heavy = tmp_path / "models" / "heavy.safetensors"
    heavy.write_bytes(b"\0" * 32)
    record = saved_record(creator)

    first = service.generate_base(record.id, seed=1)
    assert json.loads(Path(first["sidecar"]).read_text(encoding="utf-8"))[
        "checkpoint"] == "illustrious-test.safetensors"

    settings.set("models.image.heavy_checkpoint_path", str(heavy))
    settings.set("models.image.variant", "heavy")
    second = service.generate_base(record.id, seed=1)
    assert len(fake_engine.factory.backends) == 2  # swapped, not reused
    assert fake_engine.factory.backends[0].closed
    assert fake_engine.factory.backends[1].checkpoint == heavy
    sidecar = json.loads(Path(second["sidecar"]).read_text(encoding="utf-8"))
    assert sidecar["checkpoint"] == "heavy.safetensors"
    assert sidecar["checkpoint_bytes"] == 32


def test_blank_heavy_path_falls_back_to_default(settings, tmp_path):
    # L1: "" is a natural hand-edit "clear" — treat as unconfigured.
    engine = ImageEngine(settings, backend_factory=FakeFactory())
    settings.set("models.image.checkpoint_path", str(tmp_path / "default.st"))
    settings.set("models.image.variant", "heavy")
    settings.set("models.image.heavy_checkpoint_path", "")
    assert engine.checkpoint_path() == tmp_path / "default.st"


def test_pipeline_config_dir_reaches_the_backend(fake_engine, settings, tmp_path):
    # H2: a configured local pipeline-config dir makes the load fully offline.
    cfg = tmp_path / "sdxl-config"
    cfg.mkdir()
    settings.set("models.image.pipeline_config_dir", str(cfg))
    fake_engine.load()
    assert fake_engine.factory.backends[0].config_dir == cfg


def test_stored_record_blocked_on_load_reports_blocked_and_audits(
    service, creator, audit
):
    # M2: a hand-edited record (or one predating a blocklist tightening)
    # is a policy block with an audit trail, not a phantom "not found".
    cdir = creator.store.characters_dir / "tampered01"
    cdir.mkdir(parents=True)
    (cdir / "character.json").write_text(json.dumps({
        "id": "tampered01", "name": "loli", "age": 25,
    }), encoding="utf-8")
    res = service.preview_prompt("tampered01")
    assert res["ok"] is False and res["kind"] == "blocked"
    assert res["source"] == "name" and res["category"] == "minors"
    blocks = [e for e in audit_events(audit) if e["kind"] == "filter_block"]
    assert blocks and blocks[-1]["context"] == "image.load.name"


def test_corrupt_record_file_reports_io_kind(service, creator):
    cdir = creator.store.characters_dir / "corrupt01"
    cdir.mkdir(parents=True)
    (cdir / "character.json").write_text("{not json", encoding="utf-8")
    res = service.preview_prompt("corrupt01")
    assert res["ok"] is False and res["kind"] == "io"


def test_underage_hand_edit_reports_age_kind(service, creator):
    cdir = creator.store.characters_dir / "underage01"
    cdir.mkdir(parents=True)
    (cdir / "character.json").write_text(json.dumps({
        "id": "underage01", "name": "Tamper", "age": 17,
    }), encoding="utf-8")
    res = service.preview_prompt("underage01")
    assert res["ok"] is False and res["kind"] == "age"


def test_concurrent_generates_share_one_backend(service, creator, fake_engine):
    import threading

    record = saved_record(creator)
    results = []

    def hit():
        results.append(service.generate_base(record.id, seed=5))

    threads = [threading.Thread(target=hit) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r["ok"] for r in results)
    assert len(fake_engine.factory.backends) == 1  # lock serialized the loads
    assert len({r["path"] for r in results}) == 6  # no overwrites


def test_startup_resets_a_stale_vram_slot(tmp_path, monkeypatch):
    # L3: a crash cannot leave a model in VRAM; a persisted slot is stale.
    import app.main as app_main

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "settings.json").write_text(
        json.dumps({"models": {"active": "image"}}), encoding="utf-8"
    )
    monkeypatch.setattr(app_main, "DATA_DIR", data_dir)
    settings, *_ = app_main.build_services()
    assert settings.get("models.active") is None


def test_build_services_wires_library_over_the_shared_store(tmp_path,
                                                            monkeypatch):
    # Wiring: build_services returns the 7-tuple (Stage-5 adds the builder
    # service) and the LibraryService shares the creator's store + live catalog
    # (a wiring regression in main.py would otherwise pass the hand-wired
    # conftest fixtures).
    import app.main as app_main
    from app.ui.builders import BuilderService
    from app.ui.library import LibraryService

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(app_main, "DATA_DIR", data_dir)
    result = app_main.build_services()
    assert len(result) == 7
    settings, audit, content_filter, creator, images, library, builders = result
    assert isinstance(library, LibraryService)
    assert isinstance(builders, BuilderService)
    # shared store: a character created via the creator is visible to library
    created = creator.create_character(
        {"mode": "quick", "name": "Wired", "age": 22,
         "selections": {"race": "human", "gender_presentation": "feminine",
                        "skin_tone": "fair", "hair_color": "black",
                        "hair_style": "short", "eye_color": "brown",
                        "body_type": "average"}})
    assert created["ok"] is True
    listed = library.list_characters()
    assert any(r["id"] == created["id"] for r in listed["characters"])
    # reconcile is callable and structured (the startup sweep path)
    assert library.reconcile()["ok"] is True


def test_dropped_in_option_file_changes_prompts_after_live_reload(
    tmp_path, creator, service
):
    # §15 end-to-end: the service reads the creator's live catalog.
    record = saved_record(
        creator, selections={"aura": "ember"}, tags={}, sliders={}, free_text={}
    )
    before = service.preview_prompt(record.id)
    assert before["ok"] and "smouldering ember aura" not in before["positive"]
    dropin_dir = tmp_path / "data" / "options"
    dropin_dir.mkdir(parents=True, exist_ok=True)
    (dropin_dir / "95_aura.json").write_text(json.dumps({
        "groups": [{"id": "aura", "label": "Aura", "kind": "single",
                    "options": [{"id": "ember", "label": "Ember",
                                 "prompt": "smouldering ember aura"}]}]
    }), encoding="utf-8")
    creator.reload()
    after = service.preview_prompt(record.id)
    assert "smouldering ember aura" in after["positive"]


# -- service ------------------------------------------------------------------------


def saved_record(creator, **kwargs) -> CharacterRecord:
    record = make_record(**kwargs)
    creator.store.save(record)
    return record


def test_preview_prompt_round_trip(service, creator):
    record = saved_record(creator)
    res = service.preview_prompt(record.id)
    assert res["ok"] is True
    assert res["positive"].startswith("masterpiece")
    assert "loli" in res["negative"]
    assert any(p["source"] == "selections.race" for p in res["pieces"])


def test_preview_prompt_unknown_and_unsafe_ids(service):
    assert service.preview_prompt("nope")["kind"] == "not_found"
    assert service.preview_prompt("../../etc")["kind"] == "not_found"
    assert service.preview_prompt("")["kind"] == "invalid"


def test_generate_base_writes_frame_sidecar_and_audit(
    service, creator, audit, settings, checkpoint
):
    record = saved_record(creator)
    res = service.generate_base(record.id, seed=42)
    assert res["ok"] is True
    assert res["seed"] == 42

    frame = Path(res["path"])
    assert frame.is_file() and frame.read_bytes().startswith(b"FAKEPNG")
    assert frame.parent == creator.store.char_dir(record.id) / "reference"

    sidecar = json.loads(Path(res["sidecar"]).read_text(encoding="utf-8"))
    assert sidecar["kind"] == "base"
    assert sidecar["character_id"] == record.id
    assert sidecar["checkpoint"] == checkpoint.name
    assert sidecar["request"]["seed"] == 42
    assert sidecar["request"]["positive"] == res["positive"]
    assert sidecar["request"]["width"] == settings.get("image_gen.width")
    assert any(p["source"] == "free_text.appearance_notes"
               for p in sidecar["pieces"])

    events = audit_events(audit)
    gen = [e for e in events if e["kind"] == "image_generated"]
    assert len(gen) == 1
    assert gen[0]["character_id"] == record.id
    assert gen[0]["positive"] == res["positive"]
    assert settings.get("models.active") == "image"  # engine still holds slot


def test_generate_base_same_second_same_seed_never_overwrites(service, creator):
    record = saved_record(creator)
    first = service.generate_base(record.id, seed=7)
    second = service.generate_base(record.id, seed=7)
    assert first["ok"] and second["ok"]
    assert first["path"] != second["path"]
    assert Path(first["path"]).is_file() and Path(second["path"]).is_file()


def test_generate_base_blocked_prompt_refuses_and_audits(service, creator, audit):
    record = saved_record(
        creator,
        free_text={"appearance_notes": "Always around the kids at the temple."},
    )
    res = service.generate_base(record.id)
    assert res == {
        "ok": False, "kind": "blocked",
        "source": "free_text.appearance_notes", "category": "minors",
        "error": "image prompt blocked by the content policy (minors)",
    }
    blocks = [e for e in audit_events(audit) if e["kind"] == "filter_block"]
    assert blocks and blocks[-1]["context"] == (
        "image.prompt.free_text.appearance_notes"
    )
    assert not (creator.store.char_dir(record.id) / "reference").exists()


def test_generate_base_seed_shapes(service, creator):
    record = saved_record(creator)
    assert service.generate_base(record.id, seed=12.0)["seed"] == 12
    for bad in (True, -1, 1.5, "abc", 2**40):
        res = service.generate_base(record.id, seed=bad)
        assert res["ok"] is False and res["kind"] == "invalid", bad


def test_generate_base_without_engine_reports_structured_error(
    creator, settings, audit
):
    # No checkpoint configured (the build-sandbox reality): a saved record
    # still gets a structured engine error, not an exception.
    service = ImageService(
        creator.store, settings, audit, catalog_provider=lambda: creator.catalog
    )
    record = saved_record(creator)
    res = service.generate_base(record.id)
    assert res["ok"] is False
    assert res["kind"] == "engine"
    assert "no image checkpoint configured" in res["error"]


def test_generation_failure_reports_as_engine_kind(
    service, creator, fake_engine
):
    record = saved_record(creator)
    fake_engine.load()
    fake_engine.factory.backends[0].generate = lambda request: (_ for _ in ()).throw(
        MemoryError("CUDA out of memory")
    )
    res = service.generate_base(record.id)
    assert res["ok"] is False and res["kind"] == "engine"
    assert "CUDA out of memory" in res["error"]


def test_persist_failure_reports_as_io_kind(service, creator, monkeypatch):
    record = saved_record(creator)

    def exploding_save(path):
        raise OSError("disk full")

    monkeypatch.setattr(FakeImage, "save", lambda self, path: exploding_save(path))
    res = service.generate_base(record.id)
    assert res["ok"] is False and res["kind"] == "io"
    assert "disk full" in res["error"]


def test_config_errors_report_as_config_kind(service, creator, settings):
    settings.set("image_gen.width", 830)  # hand-edit: not a multiple of 8
    record = saved_record(creator)
    res = service.generate_base(record.id)
    assert res["ok"] is False and res["kind"] == "config"
    assert "multiple of 8" in res["error"]


@pytest.mark.parametrize("bad_value", [float("inf"), float("-inf")])
def test_infinity_in_settings_never_crashes_the_bridge(
    service, creator, settings, bad_value
):
    # Finding 2 (MEDIUM): json.loads parses Infinity/1e999 to float('inf'),
    # and int(inf) raises OverflowError. _generation_settings runs on both
    # generate and status, OUTSIDE the request try — it must never raise.
    settings.set("image_gen.height", bad_value)
    status = service.engine_status()  # must not raise
    assert status["generation"]["height"] == 1216  # fell back to default
    record = saved_record(creator)
    res = service.generate_base(record.id)  # must not raise
    assert res["ok"] is True  # default height used, generation proceeds


def test_infinity_reaches_generation_settings_via_json(tmp_path):
    from app.config import Settings

    path = tmp_path / "settings.json"
    path.write_text('{"image_gen": {"width": 1e999, "steps": Infinity}}',
                    encoding="utf-8")
    s = Settings(path)
    assert s.get("image_gen.width") == float("inf")  # the hand-edit reality
    from app.imagegen.service import ImageService
    from app.audit import AuditLog
    from app.model import CharacterStore
    svc = ImageService(
        CharacterStore(tmp_path), s, AuditLog(tmp_path / "logs", enabled=False),
        catalog_provider=lambda: load_option_catalog(),
    )
    gen = svc._generation_settings()  # must not raise
    assert gen["width"] == 832 and gen["steps"] == 28


def test_release_engine_frees_the_slot(service, creator, settings):
    record = saved_record(creator)
    assert service.generate_base(record.id)["ok"]
    assert settings.get("models.active") == "image"
    res = service.release_engine()
    assert res["ok"] is True and res["loaded"] is False
    assert settings.get("models.active") is None


def test_engine_status_includes_generation_settings(service, settings):
    status = service.engine_status()
    assert status["generation"] == {
        "width": 832, "height": 1216, "steps": 28,
        "cfg_scale": 5.5, "sampler": "euler_a", "ip_adapter_scale": 0.55,
    }


def test_image_gen_settings_defaults(settings):
    assert settings.get("image_gen.width") == 832
    assert settings.get("image_gen.height") == 1216
    assert settings.get("image_gen.steps") == 28
    assert settings.get("image_gen.cfg_scale") == 5.5
    assert settings.get("image_gen.sampler") == "euler_a"
    assert settings.get("image_gen.ip_adapter_scale") == 0.55
    assert settings.get("models.image.ip_adapter.dir") is None
    assert settings.get("models.image.ip_adapter.variant") == "standard"


# ============================================================================
# Stage 3b — IP-Adapter baseline identity
# ============================================================================

from app.imagegen import IPAdapterConfig  # noqa: E402
from app.imagegen.engine import (  # noqa: E402
    IP_ADAPTER_ENCODER_FOLDER,
    IP_ADAPTER_VARIANTS,
)


# -- GenerationRequest.ip_adapter_scale --------------------------------------


def test_request_ip_adapter_scale_defaults_none_and_omits_from_dict():
    r = GenerationRequest(positive="adult", negative="")
    r.validate()
    assert r.ip_adapter_scale is None
    assert "ip_adapter_scale" not in r.to_dict()  # base sidecar unchanged


@pytest.mark.parametrize("scale", [0.0, 0.55, 1.0])
def test_request_valid_ip_adapter_scale(scale):
    r = GenerationRequest(positive="adult", negative="", ip_adapter_scale=scale)
    r.validate()
    assert r.to_dict()["ip_adapter_scale"] == scale


@pytest.mark.parametrize("scale", [True, -0.1, 1.1, float("nan"), float("inf"), "0.5"])
def test_request_rejects_bad_ip_adapter_scale(scale):
    r = GenerationRequest(positive="adult", negative="", ip_adapter_scale=scale)
    with pytest.raises(ValueError):
        r.validate()


# -- IP-Adapter config resolution --------------------------------------------


def test_ip_adapter_config_none_when_unconfigured(settings):
    engine = ImageEngine(settings, backend_factory=FakeFactory())
    assert engine.ip_adapter_config() is None


def test_ip_adapter_config_resolves_variant_and_encoder(settings, ip_adapter_dir):
    engine = ImageEngine(settings, backend_factory=FakeFactory())
    cfg = engine.ip_adapter_config()
    assert isinstance(cfg, IPAdapterConfig)
    assert cfg.dir == ip_adapter_dir
    assert cfg.variant == "standard"
    assert cfg.weight_name == "ip-adapter_sdxl_vit-h.safetensors"
    assert cfg.image_encoder_folder == IP_ADAPTER_ENCODER_FOLDER == "models/image_encoder"
    settings.set("models.image.ip_adapter.variant", "plus")
    assert engine.ip_adapter_config().weight_name == "ip-adapter-plus_sdxl_vit-h.safetensors"


def test_ip_adapter_config_unknown_variant_falls_back_to_standard(settings, ip_adapter_dir):
    settings.set("models.image.ip_adapter.variant", "faceid-plus-v99")
    engine = ImageEngine(settings, backend_factory=FakeFactory())
    cfg = engine.ip_adapter_config()
    assert cfg.variant == "standard"  # no unpaired weight/encoder from a hand-edit
    assert cfg.weight_name == IP_ADAPTER_VARIANTS["standard"]["weight_name"]


def test_ip_adapter_dir_relative_resolves_against_app_root(settings):
    settings.set("models.image.ip_adapter.dir", "models/ipa")
    engine = ImageEngine(settings, backend_factory=FakeFactory())
    assert engine.ip_adapter_config().dir == APP_ROOT / "models/ipa"


def test_status_surfaces_ip_adapter_availability(settings, checkpoint, ip_adapter_dir):
    settings.set("models.image.checkpoint_path", str(checkpoint))
    engine = ImageEngine(settings, backend_factory=FakeFactory())
    status = engine.status()
    assert status["ip_adapter_configured"] is True
    assert status["ip_adapter_dir_exists"] is True
    assert status["ip_adapter_weight_exists"] is True
    assert status["ip_adapter_encoder_exists"] is True
    assert status["ip_adapter_variants"] == ["standard", "plus"]
    assert status["ip_adapter"]["image_encoder_folder"] == "models/image_encoder"
    assert status["loaded_mode"] is None


# -- engine mode switching (fake factory) ------------------------------------


def test_load_identity_builds_ip_adapter_backend(fake_engine, settings, ip_adapter_dir):
    fake_engine.load(mode="identity")
    backend = fake_engine.factory.backends[0]
    assert backend.identity is True
    assert isinstance(backend.ip_config, IPAdapterConfig)
    assert fake_engine.loaded_ip_config is not None
    assert fake_engine.status()["loaded_mode"] == "identity"


def test_base_to_identity_swaps_backend(fake_engine, settings, ip_adapter_dir):
    fake_engine.load(mode="base")
    assert fake_engine.factory.backends[0].identity is False
    fake_engine.load(mode="identity")  # mode flip -> unload + rebuild
    assert len(fake_engine.factory.backends) == 2
    assert fake_engine.factory.backends[0].closed
    assert fake_engine.factory.backends[1].identity is True


def test_identity_is_idempotent_within_mode(fake_engine, settings, ip_adapter_dir):
    fake_engine.load(mode="identity")
    fake_engine.load(mode="identity")
    assert len(fake_engine.factory.backends) == 1


def test_identity_variant_flip_rebuilds(fake_engine, settings, ip_adapter_dir):
    fake_engine.load(mode="identity")
    settings.set("models.image.ip_adapter.variant", "plus")
    fake_engine.load(mode="identity")  # ip_config changed -> rebuild
    assert len(fake_engine.factory.backends) == 2
    assert fake_engine.factory.backends[1].ip_config.variant == "plus"


def test_identity_to_base_swaps_back(fake_engine, settings, ip_adapter_dir):
    fake_engine.load(mode="identity")
    fake_engine.load(mode="base")
    assert len(fake_engine.factory.backends) == 2
    assert fake_engine.factory.backends[1].identity is False
    assert fake_engine.loaded_ip_config is None


def test_base_mode_stays_idempotent_with_ip_configured(fake_engine, ip_adapter_dir):
    # The 3a idempotency contract must hold even when an IP-Adapter is
    # configured (base load-key ip_config is always None).
    fake_engine.load(mode="base")
    fake_engine.load(mode="base")
    assert len(fake_engine.factory.backends) == 1


# -- engine identity degradation without torch (real factory) ----------------


def test_identity_requires_configured_ip_adapter(settings, checkpoint):
    settings.set("models.image.checkpoint_path", str(checkpoint))
    engine = ImageEngine(settings, backend_factory=FakeFactory())
    # dir unset -> refuse before any backend build
    with pytest.raises(EngineUnavailable, match="no IP-Adapter configured"):
        engine.load(mode="identity")
    assert settings.get("models.active") is None


def test_identity_missing_weight_and_encoder(settings, checkpoint, tmp_path):
    settings.set("models.image.checkpoint_path", str(checkpoint))
    root = tmp_path / "ipa"
    root.mkdir()
    settings.set("models.image.ip_adapter.dir", str(root))
    engine = ImageEngine(settings, backend_factory=FakeFactory())
    with pytest.raises(EngineUnavailable, match="IP-Adapter weights not found"):
        engine.load(mode="identity")
    # add the weight, still missing the encoder dir
    (root / "sdxl_models").mkdir()
    (root / "sdxl_models" / "ip-adapter_sdxl_vit-h.safetensors").write_bytes(b"\0")
    with pytest.raises(EngineUnavailable, match="image encoder not found"):
        engine.load(mode="identity")


def test_generate_identity_passes_reference_and_scale(fake_engine, ip_adapter_dir, tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\0")
    req = GenerationRequest(positive="adult", negative="", seed=9, ip_adapter_scale=0.7)
    result = fake_engine.generate_identity(req, ref)
    backend = fake_engine.factory.backends[0]
    assert backend.identity is True
    assert backend.requests[0].ip_adapter_scale == 0.7
    assert backend.references[0] == ref
    assert result.request.seed == 9


def test_generate_identity_reference_unreadable_propagates(fake_engine, ip_adapter_dir, tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\0")
    fake_engine.load(mode="identity")
    fake_engine.factory.backends[0].generate = lambda request, reference: (
        _ for _ in ()
    ).throw(ReferenceUnreadable("corrupt"))
    req = GenerationRequest(positive="adult", negative="", ip_adapter_scale=0.5)
    with pytest.raises(ReferenceUnreadable):
        fake_engine.generate_identity(req, ref)


def test_generate_identity_generic_failure_wraps(fake_engine, ip_adapter_dir, tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\0")
    fake_engine.load(mode="identity")
    fake_engine.factory.backends[0].generate = lambda request, reference: (
        _ for _ in ()
    ).throw(MemoryError("oom"))
    req = GenerationRequest(positive="adult", negative="", ip_adapter_scale=0.5)
    with pytest.raises(GenerationFailed):
        fake_engine.generate_identity(req, ref)


# -- _resolve_reference path safety (pure pathlib; the security boundary) -----


def test_resolve_reference_accepts_contained_relative(service, creator):
    record = saved_record(creator)
    frame = creator.store.char_dir(record.id) / "reference" / "base-1.png"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b"FAKEPNG")
    res = service._resolve_reference(record.id, "reference/base-1.png",
                                     allow_absolute=False)
    assert not isinstance(res, dict)
    abs_path, rel = res
    assert abs_path == frame.resolve()
    assert rel == "reference/base-1.png"


@pytest.mark.parametrize("evil", [
    "../../etc/passwd",
    "reference/../../secret.png",
    "..\\..\\windows",
    "C:/Windows/System32/x.png",
    "C:x",
    "//server/share/x.png",
    "",
    "   ",
    "reference/ok\x00.png",   # NUL: resolve() raises ValueError, not OSError
    "\x00",
])
def test_resolve_reference_rejects_traversal_and_absolute(service, creator, evil):
    record = saved_record(creator)
    res = service._resolve_reference(record.id, evil, allow_absolute=False)
    assert isinstance(res, dict) and res["kind"] == "reference_invalid"


def test_nul_byte_reference_never_raises_through_the_bridge(service, creator):
    # Review P1: a NUL makes Path.resolve() raise ValueError (not OSError), which
    # must not escape any bridge-reachable path — including the ordinary preview.
    record = saved_record(creator)
    # set-time (allow_absolute=True) path
    assert service.set_reference(record.id, "reference/x\x00.png")["kind"] == \
        "reference_invalid"
    # hand-edit the stored path to contain a NUL, then exercise every use-time
    # caller: reference_status, preview_prompt (has_reference), generate_identity.
    rec = creator.store.load(record.id)
    rec.identity.reference_image_path = "reference/ok\x00.png"
    creator.store.save(rec)
    assert service.reference_status(record.id)["kind"] == "reference_invalid"
    preview = service.preview_prompt(record.id)  # must not raise
    assert preview["ok"] is True and preview["has_reference"] is False
    assert service.generate_identity(record.id)["kind"] == "reference_invalid"


def test_resolve_reference_missing_file_is_distinct_kind(service, creator):
    record = saved_record(creator)
    res = service._resolve_reference(record.id, "reference/gone.png",
                                     allow_absolute=False)
    assert isinstance(res, dict) and res["kind"] == "reference_missing"


def test_resolve_reference_absolute_allowed_only_at_set_time(service, creator):
    record = saved_record(creator)
    frame = creator.store.char_dir(record.id) / "reference" / "base-1.png"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b"FAKEPNG")
    # set-time: an in-dir absolute path is accepted and returned RELATIVE
    ok = service._resolve_reference(record.id, str(frame), allow_absolute=True)
    assert not isinstance(ok, dict) and ok[1] == "reference/base-1.png"
    # an absolute path OUTSIDE the char dir is rejected even at set-time
    outside = creator.store.characters_dir / "other" / "x.png"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"X")
    bad = service._resolve_reference(record.id, str(outside), allow_absolute=True)
    assert isinstance(bad, dict) and bad["kind"] == "reference_invalid"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlink support")
def test_resolve_reference_rejects_symlink_escape(service, creator, tmp_path):
    record = saved_record(creator)
    ref_dir = creator.store.char_dir(record.id) / "reference"
    ref_dir.mkdir(parents=True)
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"SECRET")
    link = ref_dir / "link.png"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted")
    res = service._resolve_reference(record.id, "reference/link.png",
                                     allow_absolute=False)
    # resolve() collapses the symlink; containment then rejects the escape
    assert isinstance(res, dict) and res["kind"] == "reference_invalid"


# -- set_reference / clear_reference / reference_status ----------------------


def _base_frame(service, creator, **kw):
    record = saved_record(creator, **kw)
    res = service.generate_base(record.id, seed=1)
    assert res["ok"]
    return record, res["path"]


def test_set_reference_stores_relative_and_audits(service, creator, audit):
    record, frame_path = _base_frame(service, creator)
    res = service.set_reference(record.id, frame_path)
    assert res["ok"] is True
    assert res["reference"].startswith("reference/base-")
    reloaded = creator.store.load(record.id)
    assert reloaded.identity.reference_image_path == res["reference"]
    assert "\\" not in reloaded.identity.reference_image_path  # posix form
    assert any(e["kind"] == "identity_reference_set" for e in audit_events(audit))


def test_set_reference_rejects_out_of_dir(service, creator):
    record = saved_record(creator)
    # a frame belonging to ANOTHER character
    other, other_frame = _base_frame(service, creator)
    res = service.set_reference(record.id, other_frame)
    assert res["ok"] is False and res["kind"] == "reference_invalid"
    assert creator.store.load(record.id).identity.reference_image_path is None


def test_clear_reference_unsets_and_audits(service, creator, audit):
    record, frame_path = _base_frame(service, creator)
    service.set_reference(record.id, frame_path)
    res = service.clear_reference(record.id)
    assert res["ok"] is True
    assert creator.store.load(record.id).identity.reference_image_path is None
    assert any(e["kind"] == "identity_reference_cleared" for e in audit_events(audit))


def test_reference_status_reports_precise_state(service, creator):
    record, frame_path = _base_frame(service, creator)
    assert service.reference_status(record.id) == {
        "ok": True, "id": record.id, "has_reference": False, "reference": None,
    }
    service.set_reference(record.id, frame_path)
    ok = service.reference_status(record.id)
    assert ok["has_reference"] is True and ok["reference"].startswith("reference/")
    # hand-edit the stored path to escape -> use-time resolver flags it
    rec = creator.store.load(record.id)
    rec.identity.reference_image_path = "../../secret.png"
    creator.store.save(rec)
    bad = service.reference_status(record.id)
    assert bad["kind"] == "reference_invalid"


# -- generate_identity --------------------------------------------------------


def _identity_ready(service, creator, settings, ip_adapter_dir, **kw):
    """A saved record with a promoted reference and the IP-Adapter configured."""
    record, frame_path = _base_frame(service, creator, **kw)
    assert service.set_reference(record.id, frame_path)["ok"]
    return record


def test_generate_identity_happy_path(service, creator, settings, audit, ip_adapter_dir,
                                      checkpoint, fake_engine):
    record = _identity_ready(service, creator, settings, ip_adapter_dir)
    res = service.generate_identity(record.id, seed=123, scale=0.6)
    assert res["ok"] is True
    assert res["seed"] == 123 and res["scale"] == 0.6
    assert res["reference"].startswith("reference/base-")

    frame = Path(res["path"])
    assert frame.parent == creator.store.char_dir(record.id) / "identity"
    assert frame.name.startswith("identity-")

    sidecar = json.loads(Path(res["sidecar"]).read_text(encoding="utf-8"))
    assert sidecar["kind"] == "identity" and sidecar["stage"] == "3b-identity"
    assert sidecar["reference"] == res["reference"]
    assert sidecar["request"]["ip_adapter_scale"] == 0.6
    ipa = sidecar["ip_adapter"]
    assert ipa["variant"] == "standard"
    assert ipa["weight_name"] == "ip-adapter_sdxl_vit-h.safetensors"
    assert ipa["image_encoder_folder"] == "models/image_encoder"
    assert ipa["scale"] == 0.6
    assert ipa["dir"] == ip_adapter_dir.name  # basename only, no absolute path
    # no absolute path anywhere in the sidecar or the record
    blob = json.dumps(sidecar)
    assert str(ip_adapter_dir) not in blob and str(creator.store.root) not in blob

    events = audit_events(audit)
    assert any(e["kind"] == "identity_generated" and e["reference"] == res["reference"]
               for e in events)
    # the fake identity backend actually received the resolved reference path
    backend = fake_engine.factory.backends[-1]
    assert backend.identity is True
    expected = (creator.store.char_dir(record.id) / res["reference"]).resolve()
    assert backend.references[-1].resolve() == expected


def test_generate_identity_no_reference(service, creator, settings, ip_adapter_dir):
    record = saved_record(creator)
    res = service.generate_identity(record.id)
    assert res["ok"] is False and res["kind"] == "no_reference"
    assert not (creator.store.char_dir(record.id) / "identity").exists()


def test_generate_identity_use_time_containment(service, creator, settings, ip_adapter_dir):
    record = _identity_ready(service, creator, settings, ip_adapter_dir)
    # hand-edit the stored reference to escape the char dir
    rec = creator.store.load(record.id)
    rec.identity.reference_image_path = "../../secret.png"
    creator.store.save(rec)
    res = service.generate_identity(record.id)
    assert res["ok"] is False and res["kind"] == "reference_invalid"
    assert not (creator.store.char_dir(record.id) / "identity").exists()


def test_generate_identity_deleted_reference(service, creator, settings, ip_adapter_dir):
    record = _identity_ready(service, creator, settings, ip_adapter_dir)
    # delete the promoted frame after set_reference passed
    rel = creator.store.load(record.id).identity.reference_image_path
    (creator.store.char_dir(record.id) / rel).unlink()
    res = service.generate_identity(record.id)
    assert res["ok"] is False and res["kind"] == "reference_missing"


@pytest.mark.parametrize("bad", [1.5, -0.1, "abc", True])
def test_generate_identity_bad_scale(service, creator, settings, ip_adapter_dir, bad):
    record = _identity_ready(service, creator, settings, ip_adapter_dir)
    res = service.generate_identity(record.id, scale=bad)
    assert res["ok"] is False and res["kind"] == "invalid"


def test_generate_identity_default_scale(service, creator, settings, ip_adapter_dir,
                                         fake_engine):
    record = _identity_ready(service, creator, settings, ip_adapter_dir)
    res = service.generate_identity(record.id)  # scale=None -> settings default
    assert res["ok"] is True and res["scale"] == 0.55


def test_generate_identity_scale_infinity_settings_degrades(service, creator, settings,
                                                            ip_adapter_dir):
    settings.set("image_gen.ip_adapter_scale", float("inf"))
    record = _identity_ready(service, creator, settings, ip_adapter_dir)
    res = service.generate_identity(record.id)  # must not raise
    assert res["ok"] is True and res["scale"] == 0.55


def test_generate_identity_blocked_prompt(service, creator, settings, ip_adapter_dir, audit):
    # Establish a clean reference first, THEN make the prompt-context gate
    # trip: "kids at recess" passes the freetext record gate (no sexual
    # proximity) but blocks outright in the strict image-prompt context.
    record = _identity_ready(service, creator, settings, ip_adapter_dir)
    rec = creator.store.load(record.id)
    rec.free_text["appearance_notes"] = "hanging out with the kids at recess"
    creator.store.save(rec)
    res = service.generate_identity(record.id)
    assert res["ok"] is False and res["kind"] == "blocked"
    assert not (creator.store.char_dir(record.id) / "identity").exists()


def test_generate_identity_engine_unavailable_when_ip_not_configured(
    service, creator, settings, fake_engine
):
    # reference set, but no IP-Adapter dir configured -> structured engine error
    record, frame_path = _base_frame(service, creator)
    service.set_reference(record.id, frame_path)
    res = service.generate_identity(record.id)
    assert res["ok"] is False and res["kind"] == "engine"
    assert "no IP-Adapter configured" in res["error"]


def test_generate_identity_reference_unreadable_kind(service, creator, settings,
                                                     ip_adapter_dir, fake_engine):
    record = _identity_ready(service, creator, settings, ip_adapter_dir)
    fake_engine.load(mode="identity")
    fake_engine.factory.backends[-1].generate = lambda request, reference: (
        _ for _ in ()
    ).throw(ReferenceUnreadable("corrupt png"))
    res = service.generate_identity(record.id)
    assert res["ok"] is False and res["kind"] == "reference_unreadable"


def test_generate_identity_same_second_same_seed_never_overwrites(
    service, creator, settings, ip_adapter_dir
):
    record = _identity_ready(service, creator, settings, ip_adapter_dir)
    a = service.generate_identity(record.id, seed=3)
    b = service.generate_identity(record.id, seed=3)
    assert a["ok"] and b["ok"] and a["path"] != b["path"]
    assert Path(a["path"]).is_file() and Path(b["path"]).is_file()


def test_generate_identity_inherited_kinds(service, creator, settings, ip_adapter_dir):
    assert service.generate_identity("nope")["kind"] == "not_found"
    assert service.generate_identity("")["kind"] == "invalid"
    record = _identity_ready(service, creator, settings, ip_adapter_dir)
    assert service.generate_identity(record.id, seed="abc")["kind"] == "invalid"


def test_preview_prompt_reports_has_reference(service, creator):
    record, frame_path = _base_frame(service, creator)
    assert service.preview_prompt(record.id)["has_reference"] is False
    service.set_reference(record.id, frame_path)
    assert service.preview_prompt(record.id)["has_reference"] is True


# ============================================================================
# Stage 3c — identity bootstrap + auto-filter (service orchestration)
# ============================================================================

import math as _math  # noqa: E402

from app.imagegen import (  # noqa: E402
    ContentVerdict,
    CullToolkit,
    CullUnavailable,
    FaceReading,
    QualityReading,
)

_IREF = (1.0, 0.0, 0.0, 0.0)


def _emb(sim: float):
    sim = max(-1.0, min(1.0, sim))
    return (sim, _math.sqrt(max(0.0, 1.0 - sim * sim)), 0.0, 0.0)


class FakeToolkitFactory:
    """Drives the four cull abstractions from a per-candidate `outcomes` list
    (indexed in first-embed order = generation order). `block_all` forces the
    classifier to block (for the confirm-time re-classification test)."""

    def __init__(self, outcomes=None, *, ref_found=True, raise_kind=None,
                 raise_exc=None, block_all=False, block_swapped=False, swapper=None):
        self.outcomes = list(outcomes or [])
        self.ref_found = ref_found
        self.raise_kind = raise_kind
        self.raise_exc = raise_exc
        self.block_all = block_all
        self.block_swapped = block_swapped
        self.swapper = swapper
        self._order: dict = {}
        self.built = 0
        self.active_at_build: list = []
        self.calls: list = []

    def _outcome(self, path):
        key = str(path)
        if key not in self._order:
            self._order[key] = len(self._order)
        idx = self._order[key]
        return self.outcomes[idx] if idx < len(self.outcomes) else {}

    def reading_for(self, path):
        o = self._outcome(path)
        if o.get("error"):
            raise ValueError("decode boom")
        if not o.get("found", True):
            return FaceReading(found=False, face_count=o.get("count", 0))
        return FaceReading(found=True, face_count=o.get("count", 1),
                           det_score=o.get("det_score", 0.9),
                           area_fraction=o.get("area", 0.3),
                           sharpness=o.get("sharpness", 500.0),
                           embedding=_emb(o.get("similarity", 1.0)))

    def verdict_for(self, path):
        if self.block_all:
            return ContentVerdict(blocked=True, category="minors", matched="loli")
        if self.block_swapped and str(path).endswith("-swap.png"):
            return ContentVerdict(blocked=True, category="minors", matched="loli")
        o = self._outcome(path)
        if o.get("classify_raise"):
            raise RuntimeError("classify boom")
        if o.get("blocked"):
            return ContentVerdict(blocked=True, category=o.get("category", "minors"),
                                  matched=o.get("matched", "loli"))
        return ContentVerdict(blocked=False)

    def aesthetic_for(self, path):
        return self._outcome(path).get("aesthetic", 0.5)

    def __call__(self, settings, reference_abs, need_swap):
        self.calls.append((reference_abs, need_swap))
        self.active_at_build.append(settings.get("models.active"))
        if self.raise_kind:
            raise CullUnavailable(self.raise_kind)
        if self.raise_exc is not None:
            raise self.raise_exc
        self.built += 1
        factory = self

        class _E:
            def embed(self, p):
                return factory.reading_for(p)

        class _C:
            def classify(self, p):
                return factory.verdict_for(p)

        class _Q:
            def score(self, p):
                return QualityReading(aesthetic=factory.aesthetic_for(p))

        ref = FaceReading(found=self.ref_found,
                          embedding=_IREF if self.ref_found else None,
                          face_count=1 if self.ref_found else 0)
        return CullToolkit(embedder=_E(), quality=_Q(), classifier=_C(),
                           swapper=self.swapper, ref_reading=ref, closer=None)


@pytest.fixture()
def cull_models(tmp_path, settings) -> dict:
    """Fake local model files so preflight_cull passes (the real cull is faked)."""
    fr = tmp_path / "models" / "insightface"
    (fr / "models" / "buffalo_l").mkdir(parents=True)
    (fr / "models" / "buffalo_l" / "det_10g.onnx").write_bytes(b"\0")
    (fr / "models" / "buffalo_l" / "w600k_r50.onnx").write_bytes(b"\0")
    cc = tmp_path / "models" / "classifier"
    cc.mkdir(parents=True)
    sw = tmp_path / "models" / "inswapper_128.onnx"
    sw.write_bytes(b"\0")
    settings.set("models.image.face_recognition_dir", str(fr))
    settings.set("models.image.content_classifier_dir", str(cc))
    settings.set("models.image.face_swapper_path", str(sw))
    return {"fr": fr, "cc": cc, "sw": sw}


def bootstrap_service(creator, settings, audit, fake_engine, factory):
    return ImageService(
        creator.store, settings, audit,
        catalog_provider=lambda: creator.catalog,
        engine=fake_engine, toolkit_factory=factory,
    )


def _ready(service, creator, settings, ip_adapter_dir, **kw):
    """A saved record with a promoted reference (via the 3b flow)."""
    return _identity_ready(service, creator, settings, ip_adapter_dir, **kw)


# -- bootstrap_generate ------------------------------------------------------


def test_bootstrap_generate_happy_path(creator, settings, audit, fake_engine,
                                       ip_adapter_dir, cull_models):
    factory = FakeToolkitFactory(outcomes=[{}] * 4)  # all kept, sim 1.0
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _ready(service, creator, settings, ip_adapter_dir)

    res = service.bootstrap_generate(record.id, batch=4)
    assert res["ok"] is True
    assert res["generated"] == 4
    assert res["phase"] == "proposed"
    assert res["counts"].get("proposed") == 4
    assert len(res["proposed"]) == 4

    manifest = creator.store.load_bootstrap(record.id)
    assert manifest is not None and len(manifest.candidates) == 4
    cand_dir = creator.store.candidates_dir(record.id)
    assert len(list(cand_dir.glob("cand-*.png"))) == 4

    # VRAM: slot released, and the toolkit was built AFTER the image unload
    assert settings.get("models.active") is None
    assert factory.active_at_build == [None]
    assert factory.built == 1

    blob = json.dumps(manifest.to_dict())
    assert str(creator.store.root) not in blob
    assert all(not c.path.startswith("/") and ":" not in c.path
               for c in manifest.candidates)

    assert any(e["kind"] == "bootstrap_generated" for e in audit_events(audit))


def test_bootstrap_preflight_missing_models(creator, settings, audit, fake_engine,
                                            ip_adapter_dir):
    factory = FakeToolkitFactory()
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _ready(service, creator, settings, ip_adapter_dir)
    res = service.bootstrap_generate(record.id, batch=4)
    # post-embedder-swap: the first missing witness is the classifier dir
    assert res["ok"] is False and res["kind"] == "classifier_unavailable"
    assert factory.built == 0
    assert not creator.store.candidates_dir(record.id).exists()


def test_bootstrap_no_reference(creator, settings, audit, fake_engine, cull_models):
    factory = FakeToolkitFactory()
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = saved_record(creator)
    res = service.bootstrap_generate(record.id, batch=4)
    assert res["ok"] is False and res["kind"] == "no_reference"
    assert factory.built == 0


def test_bootstrap_content_block_is_deleted_and_audited(creator, settings, audit,
                                                        fake_engine, ip_adapter_dir,
                                                        cull_models):
    factory = FakeToolkitFactory(outcomes=[{"blocked": True}, {}, {}, {}])
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _ready(service, creator, settings, ip_adapter_dir)
    res = service.bootstrap_generate(record.id, batch=4)
    assert res["ok"] is True
    counts = res["counts"]
    assert counts.get("rejected_content") == 1
    assert counts.get("proposed") == 3
    manifest = creator.store.load_bootstrap(record.id)
    blocked = [c for c in manifest.candidates if c.status == "rejected_content"]
    assert len(blocked) == 1
    assert not (creator.store.char_dir(record.id) / blocked[0].path).exists()
    blocks = [e for e in audit_events(audit)
              if e["kind"] == "filter_block" and e.get("layer") == 2]
    assert blocks and blocks[-1]["context"] == "image.bootstrap.candidate"


def test_bootstrap_fail_closed_when_toolkit_unavailable(creator, settings, audit,
                                                        fake_engine, ip_adapter_dir,
                                                        cull_models):
    factory = FakeToolkitFactory(raise_kind="classifier_unavailable")
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _ready(service, creator, settings, ip_adapter_dir)
    res = service.bootstrap_generate(record.id, batch=3)
    assert res["ok"] is False and res["kind"] == "classifier_unavailable"
    assert creator.store.load_bootstrap(record.id) is None
    assert settings.get("models.active") is None


def test_bootstrap_reference_with_no_face(creator, settings, audit, fake_engine,
                                          ip_adapter_dir, cull_models):
    factory = FakeToolkitFactory(ref_found=False)
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _ready(service, creator, settings, ip_adapter_dir)
    res = service.bootstrap_generate(record.id, batch=2)
    assert res["ok"] is False and res["kind"] == "no_faces"


def test_bootstrap_vram_released_when_engine_fails_first_frame(
    creator, settings, audit, fake_engine, ip_adapter_dir, cull_models
):
    factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _ready(service, creator, settings, ip_adapter_dir)
    fake_engine.load(mode="identity")
    fake_engine.factory.backends[-1].generate = lambda request, reference: (
        _ for _ in ()
    ).throw(MemoryError("oom on frame 1"))
    res = service.bootstrap_generate(record.id, batch=4)
    assert res["ok"] is False and res["kind"] == "engine"
    assert settings.get("models.active") is None
    assert factory.built == 0


@pytest.mark.parametrize("bad", [0, -1, 1.5, "x", True, 999])
def test_bootstrap_bad_batch(creator, settings, audit, fake_engine, ip_adapter_dir,
                             cull_models, bad):
    factory = FakeToolkitFactory()
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _ready(service, creator, settings, ip_adapter_dir)
    res = service.bootstrap_generate(record.id, batch=bad)
    assert res["ok"] is False and res["kind"] == "invalid"


# -- bootstrap_status / recull ----------------------------------------------


def test_bootstrap_status_lifecycle(creator, settings, audit, fake_engine,
                                    ip_adapter_dir, cull_models):
    factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _ready(service, creator, settings, ip_adapter_dir)
    before = service.bootstrap_status(record.id)
    assert before["phase"] is None and before["has_vetted"] is False
    service.bootstrap_generate(record.id, batch=4)
    after = service.bootstrap_status(record.id)
    assert after["phase"] == "proposed"
    assert after["counts"].get("proposed") == 4
    assert len(after["proposed"]) == 4 and after["proposed"][0]["rank"] == 1


def test_bootstrap_recull_uses_no_engine(creator, settings, audit, fake_engine,
                                         ip_adapter_dir, cull_models):
    factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _ready(service, creator, settings, ip_adapter_dir)
    service.bootstrap_generate(record.id, batch=4)
    backends_before = len(fake_engine.factory.backends)
    res = service.bootstrap_recull(record.id, overrides={"similarity_floor": 1.01})
    assert res["ok"] is True
    assert res["counts"].get("proposed", 0) == 0
    assert len(fake_engine.factory.backends) == backends_before


def test_bootstrap_recull_without_bootstrap(creator, settings, audit, fake_engine,
                                            ip_adapter_dir, cull_models):
    factory = FakeToolkitFactory()
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _ready(service, creator, settings, ip_adapter_dir)
    res = service.bootstrap_recull(record.id)
    assert res["ok"] is False and res["kind"] == "no_bootstrap"


# -- confirm_vetted ----------------------------------------------------------


def _bootstrap_ok(creator, settings, audit, fake_engine, ip_adapter_dir, factory,
                  batch=4):
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _ready(service, creator, settings, ip_adapter_dir)
    res = service.bootstrap_generate(record.id, batch=batch)
    assert res["ok"], res
    return service, record, res


def test_confirm_vetted_happy_path(creator, settings, audit, fake_engine,
                                   ip_adapter_dir, cull_models):
    factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service, record, res = _bootstrap_ok(creator, settings, audit, fake_engine,
                                         ip_adapter_dir, factory)
    ids = [p["candidate_id"] for p in res["proposed"][:2]]
    out = service.confirm_vetted(record.id, ids)
    assert out["ok"] is True and out["count"] == 2
    vetted = creator.store.load_vetted(record.id)
    assert vetted is not None and vetted.count == 2
    vdir = creator.store.vetted_dir(record.id)
    assert len(list(vdir.glob("vetted-*.png"))) == 2
    assert creator.store.load_bootstrap(record.id).phase == "confirmed"
    assert any(e["kind"] == "vetted_confirmed" for e in audit_events(audit))
    assert str(creator.store.root) not in json.dumps(vetted.to_dict())


def test_confirm_vetted_rejects_forged_and_rejected_ids(creator, settings, audit,
                                                        fake_engine, ip_adapter_dir,
                                                        cull_models):
    factory = FakeToolkitFactory(outcomes=[{"blocked": True}, {}, {}, {}])
    service, record, res = _bootstrap_ok(creator, settings, audit, fake_engine,
                                         ip_adapter_dir, factory)
    manifest = creator.store.load_bootstrap(record.id)
    blocked_id = [c.candidate_id for c in manifest.candidates
                  if c.status == "rejected_content"][0]
    assert service.confirm_vetted(record.id, ["cand-does-not-exist"])["kind"] == \
        "invalid_selection"
    assert service.confirm_vetted(record.id, [blocked_id])["kind"] == \
        "invalid_selection"
    assert service.confirm_vetted(record.id, [])["kind"] == "invalid"
    assert service.confirm_vetted(record.id, "notalist")["kind"] == "invalid"


def test_confirm_vetted_reclassifies_fail_closed(creator, settings, audit,
                                                 fake_engine, ip_adapter_dir,
                                                 cull_models):
    factory = FakeToolkitFactory(outcomes=[{}] * 3)
    service, record, res = _bootstrap_ok(creator, settings, audit, fake_engine,
                                         ip_adapter_dir, factory, batch=3)
    ids = [p["candidate_id"] for p in res["proposed"][:2]]
    factory.block_all = True
    out = service.confirm_vetted(record.id, ids)
    assert out["ok"] is False and out["kind"] == "blocked"
    assert creator.store.load_vetted(record.id) is None
    blocks = [e for e in audit_events(audit)
              if e["kind"] == "filter_block" and e.get("context") == "image.confirm_vetted"]
    assert blocks


def test_confirm_vetted_use_time_containment(creator, settings, audit, fake_engine,
                                             ip_adapter_dir, cull_models):
    factory = FakeToolkitFactory(outcomes=[{}] * 3)
    service, record, res = _bootstrap_ok(creator, settings, audit, fake_engine,
                                         ip_adapter_dir, factory, batch=3)
    cid = res["proposed"][0]["candidate_id"]
    manifest = creator.store.load_bootstrap(record.id)
    manifest.get(cid).path = "../../secret.png"
    creator.store.save_bootstrap(manifest)
    out = service.confirm_vetted(record.id, [cid])
    assert out["ok"] is False and out["kind"] == "invalid_selection"
    assert creator.store.load_vetted(record.id) is None


# -- clear_bootstrap + zero record mutation ----------------------------------


def test_clear_bootstrap_removes_artifacts(creator, settings, audit, fake_engine,
                                           ip_adapter_dir, cull_models):
    factory = FakeToolkitFactory(outcomes=[{}] * 3)
    service, record, res = _bootstrap_ok(creator, settings, audit, fake_engine,
                                         ip_adapter_dir, factory, batch=3)
    service.confirm_vetted(record.id, [res["proposed"][0]["candidate_id"]])
    assert service.clear_bootstrap(record.id, scope="all")["removed"] is True
    assert creator.store.load_bootstrap(record.id) is None
    assert creator.store.load_vetted(record.id) is None


def test_bootstrap_does_not_mutate_the_record(creator, settings, audit, fake_engine,
                                              ip_adapter_dir, cull_models):
    factory = FakeToolkitFactory(outcomes=[{}] * 3)
    service, record, res = _bootstrap_ok(creator, settings, audit, fake_engine,
                                         ip_adapter_dir, factory, batch=3)
    before = creator.store.load(record.id).identity.to_dict()
    service.confirm_vetted(record.id, [res["proposed"][0]["candidate_id"]])
    after = creator.store.load(record.id).identity.to_dict()
    assert before == after
    assert after["has_lora"] is False and after["lora_path"] is None


# -- Stage 3c review-pass regressions (all execution-confirmed defects) -------


class _FakeSwapper:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def swap(self, target_path, source_ref_path, out_path):
        self.calls.append((target_path, source_ref_path, out_path))
        if not self.ok:
            return False
        Path(out_path).write_bytes(b"SWAPPED")
        return True


def test_face_swap_sets_swapped_path_after_recheck(creator, settings, audit,
                                                   fake_engine, ip_adapter_dir,
                                                   cull_models):
    settings.set("image_gen.bootstrap.face_swap_enabled", True)
    swapper = _FakeSwapper(ok=True)
    factory = FakeToolkitFactory(outcomes=[{}] * 3, swapper=swapper)
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _ready(service, creator, settings, ip_adapter_dir)
    res = service.bootstrap_generate(record.id, batch=3)
    assert res["ok"] is True
    manifest = creator.store.load_bootstrap(record.id)
    swapped = [c for c in manifest.candidates if c.swapped_path]
    assert swapped and all(c.swapped_path.startswith("bootstrap/swapped/")
                           for c in swapped)
    for c in swapped:
        assert (creator.store.char_dir(record.id) / c.swapped_path).is_file()
    assert any(e["kind"] == "bootstrap_faceswapped" for e in audit_events(audit))


def test_face_swap_blocked_pixels_fall_back_to_original(creator, settings, audit,
                                                        fake_engine, ip_adapter_dir,
                                                        cull_models):
    settings.set("image_gen.bootstrap.face_swap_enabled", True)
    factory = FakeToolkitFactory(outcomes=[{}] * 3, swapper=_FakeSwapper(ok=True),
                                 block_swapped=True)
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _ready(service, creator, settings, ip_adapter_dir)
    res = service.bootstrap_generate(record.id, batch=3)
    assert res["ok"] is True
    manifest = creator.store.load_bootstrap(record.id)
    assert all(c.swapped_path is None for c in manifest.candidates)
    swap_dir = creator.store.swapped_dir(record.id)
    assert not swap_dir.exists() or not list(swap_dir.glob("*.png"))


def test_face_swap_failed_swap_falls_back(creator, settings, audit, fake_engine,
                                          ip_adapter_dir, cull_models):
    settings.set("image_gen.bootstrap.face_swap_enabled", True)
    factory = FakeToolkitFactory(outcomes=[{}] * 2, swapper=_FakeSwapper(ok=False))
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _ready(service, creator, settings, ip_adapter_dir)
    res = service.bootstrap_generate(record.id, batch=2)
    assert res["ok"] is True
    manifest = creator.store.load_bootstrap(record.id)
    assert all(c.swapped_path is None for c in manifest.candidates)


def test_tampered_candidate_id_manifest_is_corrupt(creator, settings, audit,
                                                   fake_engine, ip_adapter_dir,
                                                   cull_models):
    factory = FakeToolkitFactory(outcomes=[{}] * 3)
    service, record, res = _bootstrap_ok(creator, settings, audit, fake_engine,
                                         ip_adapter_dir, factory, batch=3)
    path = creator.store.bootstrap_path(record.id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["candidates"][0]["candidate_id"] = "../../../PWNED"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert service.bootstrap_status(record.id)["kind"] == "bootstrap_corrupt"
    assert service.confirm_vetted(record.id, ["x"])["kind"] == "bootstrap_corrupt"


def test_corrupt_manifest_never_raises_through_the_bridge(creator, settings, audit,
                                                          fake_engine, ip_adapter_dir,
                                                          cull_models):
    factory = FakeToolkitFactory(outcomes=[{}] * 2)
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = saved_record(creator)
    path = creator.store.bootstrap_path(record.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    for corrupt in ("{not json", json.dumps([1, 2, 3]),
                    json.dumps({"character_id": "../escape", "candidates": []}),
                    "{}",  # valid JSON, missing required keys -> KeyError
                    json.dumps({"character_id": "x", "candidates": [{"seed": 1}]})):
        path.write_text(corrupt, encoding="utf-8")
        assert service.bootstrap_status(record.id)["kind"] == "bootstrap_corrupt"
        assert service.bootstrap_recull(record.id)["kind"] == "bootstrap_corrupt"
        assert service.confirm_vetted(record.id, ["x"])["kind"] == "bootstrap_corrupt"


def test_toolkit_factory_arbitrary_exception_is_structured(creator, settings, audit,
                                                           fake_engine, ip_adapter_dir,
                                                           cull_models):
    factory = FakeToolkitFactory(
        raise_exc=ModuleNotFoundError("No module named insightface"))
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _ready(service, creator, settings, ip_adapter_dir)
    res = service.bootstrap_generate(record.id, batch=2)
    assert res["ok"] is False and res["kind"] == "cull_unavailable"
    assert settings.get("models.active") is None


def test_confirm_vetted_atomic_on_copy_failure(creator, settings, audit, fake_engine,
                                               ip_adapter_dir, cull_models, monkeypatch):
    factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service, record, res = _bootstrap_ok(creator, settings, audit, fake_engine,
                                         ip_adapter_dir, factory, batch=4)
    first_ids = [p["candidate_id"] for p in res["proposed"][:2]]
    assert service.confirm_vetted(record.id, first_ids)["ok"] is True
    prior = creator.store.load_vetted(record.id)
    assert prior.count == 2
    prior_files = sorted(p.name for p in creator.store.vetted_dir(record.id).glob("*.png"))

    import app.imagegen.service as svc_mod
    calls = {"n": 0}
    real_copy = svc_mod.shutil.copyfile

    def boom(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "disk full")
        return real_copy(src, dst)

    monkeypatch.setattr(svc_mod.shutil, "copyfile", boom)
    # re-confirm the OTHER still-proposed candidates; the copy fails mid-way
    other_ids = [p["candidate_id"] for p in res["proposed"][2:4]]
    out = service.confirm_vetted(record.id, other_ids)
    assert out["ok"] is False and out["kind"] == "io"
    after = creator.store.load_vetted(record.id)
    assert after is not None and after.count == 2
    assert sorted(p.name for p in creator.store.vetted_dir(record.id).glob("*.png")) == prior_files
    assert not (creator.store.char_dir(record.id) / "vetted.new").exists()


# ============================================================================
# Stage 3d — identity LoRA promotion (service orchestration)
# ============================================================================

from app.imagegen import TrainFailed, TrainUnavailable  # noqa: E402
from app.model import VettedEntry, VettedManifest  # noqa: E402


class FakeTrainer:
    def __init__(self, factory):
        self.f = factory

    def train(self, request):
        self.f.requests.append(request)
        if self.f.fail:
            raise TrainFailed("training blew up")
        if self.f.raise_exc is not None:
            raise self.f.raise_exc
        # capture the caption the service wrote, then produce the LoRA
        concept = next(request.dataset_dir.glob("*_identity"))
        caps = sorted(concept.glob("img-*.txt"))
        self.f.captions = [c.read_text(encoding="utf-8") for c in caps]
        out = request.output_dir / f"{request.output_name}.safetensors"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"LORA-" + request.trigger.encode())
        return out


class FakeTrainerFactory:
    def __init__(self, *, unavailable=False, fail=False, raise_exc=None):
        self.unavailable = unavailable
        self.fail = fail
        self.raise_exc = raise_exc
        self.built = 0
        self.active_at_build = []
        self.requests = []
        self.captions = []

    def __call__(self, settings):
        self.active_at_build.append(settings.get("models.active"))
        if self.unavailable:
            raise TrainUnavailable("trainer_unavailable")
        self.built += 1
        return FakeTrainer(self)


@pytest.fixture()
def trainer_dir(tmp_path, settings):
    d = tmp_path / "sd-scripts"
    d.mkdir()
    (d / "sdxl_train_network.py").write_text("# kohya", encoding="utf-8")
    settings.set("models.image.lora_trainer_dir", str(d))
    return d


def train_svc(creator, settings, audit, fake_engine, trainer_factory):
    return ImageService(
        creator.store, settings, audit,
        catalog_provider=lambda: creator.catalog,
        engine=fake_engine, trainer_factory=trainer_factory)


def _with_vetted(creator, n=3, **paths):
    record = saved_record(creator)
    vdir = creator.store.vetted_dir(record.id)
    vdir.mkdir(parents=True)
    entries = []
    for i in range(1, n + 1):
        name = f"vetted-{i:02d}.png"
        (vdir / name).write_bytes(b"VETTED" + str(i).encode())
        entries.append(VettedEntry(path=f"vetted/{name}",
                                   source_candidate_id=f"c{i}", seed=i))
    creator.store.save_vetted(VettedManifest(character_id=record.id, entries=entries))
    return record


def test_train_lora_happy_path(creator, settings, audit, fake_engine, trainer_dir):
    factory = FakeTrainerFactory()
    service = train_svc(creator, settings, audit, fake_engine, factory)
    record = _with_vetted(creator, n=3)

    res = service.train_lora(record.id)
    assert res["ok"] is True
    assert res["lora_path"] == "lora/identity.safetensors"
    assert res["trigger"] == ImageService._lora_trigger(record)
    assert len(res["trigger"]) == 6 and res["dataset_size"] == 3
    assert res["steps"] == 1600 and res["network_dim"] == 16
    assert res["lora_bytes"] > 0

    # the LoRA file exists; dataset + output scratch dirs were cleaned
    lora = creator.store.lora_dir(record.id) / "identity.safetensors"
    assert lora.is_file()
    assert not creator.store.lora_dataset_dir(record.id).exists()
    assert not (creator.store.lora_dir(record.id) / "output").exists()

    # record promoted (the first record mutation since 3b)
    reloaded = creator.store.load(record.id)
    assert reloaded.identity.has_lora is True
    assert reloaded.identity.lora_path == "lora/identity.safetensors"
    assert reloaded.identity.footprint.lora_bytes > 0

    # provenance manifest
    manifest = creator.store.load_lora_manifest(record.id)
    assert manifest is not None and manifest.trigger == res["trigger"]
    assert manifest.dataset_size == 3 and manifest.base_checkpoint

    # VRAM: slot marked busy during training, released after; engine unloaded
    assert factory.active_at_build == ["image"]
    assert settings.get("models.active") is None
    assert fake_engine.loaded is False

    # the trainer got a real TrainRequest under the char dir
    req = factory.requests[0]
    assert req.trigger == res["trigger"]
    assert req.dataset_dir == creator.store.lora_dataset_dir(record.id)
    assert req.base_checkpoint.name == "illustrious-test.safetensors"

    # the caption carries the trigger + the gated identity description
    assert factory.captions and factory.captions[0].startswith(res["trigger"])
    assert "adult" in factory.captions[0]

    assert any(e["kind"] == "lora_trained" for e in audit_events(audit))


def test_train_lora_no_vetted(creator, settings, audit, fake_engine, trainer_dir):
    factory = FakeTrainerFactory()
    service = train_svc(creator, settings, audit, fake_engine, factory)
    record = saved_record(creator)  # no vetted set
    res = service.train_lora(record.id)
    assert res["ok"] is False and res["kind"] == "no_vetted"
    assert factory.built == 0


def test_train_lora_trainer_unavailable(creator, settings, audit, fake_engine):
    # no trainer_dir configured -> preflight refuses before any work
    factory = FakeTrainerFactory()
    service = train_svc(creator, settings, audit, fake_engine, factory)
    record = _with_vetted(creator, n=2)
    res = service.train_lora(record.id)
    assert res["ok"] is False and res["kind"] == "trainer_unavailable"
    assert factory.built == 0


def test_train_lora_no_checkpoint(creator, settings, audit, fake_engine, trainer_dir):
    settings.set("models.image.checkpoint_path", str(trainer_dir / "missing.safetensors"))
    factory = FakeTrainerFactory()
    service = train_svc(creator, settings, audit, fake_engine, factory)
    record = _with_vetted(creator, n=2)
    res = service.train_lora(record.id)
    assert res["ok"] is False and res["kind"] == "engine"
    assert factory.built == 0


def test_train_lora_blocked_caption(creator, settings, audit, fake_engine, trainer_dir):
    factory = FakeTrainerFactory()
    service = train_svc(creator, settings, audit, fake_engine, factory)
    record = _with_vetted(creator, n=2)
    # make the assembled prompt trip the strict image-prompt gate
    rec = creator.store.load(record.id)
    rec.free_text["appearance_notes"] = "hanging out with the kids at recess"
    creator.store.save(rec)
    res = service.train_lora(record.id)
    assert res["ok"] is False and res["kind"] == "blocked"
    assert factory.built == 0


def test_train_lora_failure_leaves_record_unpromoted(creator, settings, audit,
                                                     fake_engine, trainer_dir):
    factory = FakeTrainerFactory(fail=True)
    service = train_svc(creator, settings, audit, fake_engine, factory)
    record = _with_vetted(creator, n=2)
    res = service.train_lora(record.id)
    assert res["ok"] is False and res["kind"] == "train_failed"
    reloaded = creator.store.load(record.id)
    assert reloaded.identity.has_lora is False
    assert settings.get("models.active") is None  # slot released on failure
    assert not creator.store.lora_dataset_dir(record.id).exists()


def test_train_lora_arbitrary_exception_is_train_failed(creator, settings, audit,
                                                        fake_engine, trainer_dir):
    factory = FakeTrainerFactory(raise_exc=RuntimeError("kaboom"))
    service = train_svc(creator, settings, audit, fake_engine, factory)
    record = _with_vetted(creator, n=2)
    res = service.train_lora(record.id)
    assert res["ok"] is False and res["kind"] == "train_failed"
    assert settings.get("models.active") is None


def test_retrain_failure_preserves_prior_lora(creator, settings, audit, fake_engine,
                                              trainer_dir):
    good = FakeTrainerFactory()
    service = train_svc(creator, settings, audit, fake_engine, good)
    record = _with_vetted(creator, n=2)
    assert service.train_lora(record.id)["ok"] is True
    lora = creator.store.lora_dir(record.id) / "identity.safetensors"
    original = lora.read_bytes()

    # re-train with a failing trainer -> the prior LoRA + promotion survive
    service._trainer_factory = FakeTrainerFactory(fail=True)
    res = service.train_lora(record.id)
    assert res["ok"] is False and res["kind"] == "train_failed"
    assert lora.is_file() and lora.read_bytes() == original
    assert creator.store.load(record.id).identity.has_lora is True


def test_train_lora_skips_escaped_vetted_images(creator, settings, audit, fake_engine,
                                                trainer_dir):
    factory = FakeTrainerFactory()
    service = train_svc(creator, settings, audit, fake_engine, factory)
    record = saved_record(creator)
    vdir = creator.store.vetted_dir(record.id)
    vdir.mkdir(parents=True)
    # a hand-edited manifest whose only image path escapes the char dir
    creator.store.save_vetted(VettedManifest(character_id=record.id, entries=[
        VettedEntry(path="../../secret.png", source_candidate_id="c1", seed=1)]))
    res = service.train_lora(record.id)
    assert res["ok"] is False and res["kind"] == "no_vetted"
    assert factory.built == 0


def test_lora_status_and_clear(creator, settings, audit, fake_engine, trainer_dir):
    factory = FakeTrainerFactory()
    service = train_svc(creator, settings, audit, fake_engine, factory)
    record = _with_vetted(creator, n=2)
    assert service.lora_status(record.id)["has_lora"] is False

    service.train_lora(record.id)
    status = service.lora_status(record.id)
    assert status["has_lora"] is True
    assert status["lora_path"] == "lora/identity.safetensors"
    assert status["trigger"] == ImageService._lora_trigger(record)
    assert status["provenance"]["dataset_size"] == 2

    cleared = service.clear_lora(record.id)
    assert cleared["ok"] is True and cleared["removed"] is True
    assert not creator.store.lora_dir(record.id).exists()
    reloaded = creator.store.load(record.id)
    assert reloaded.identity.has_lora is False and reloaded.identity.lora_path is None
    assert reloaded.identity.footprint.lora_bytes == 0
    assert any(e["kind"] == "lora_cleared" for e in audit_events(audit))


def test_lora_status_flag_without_file_reads_false(creator, settings, audit,
                                                   fake_engine, trainer_dir):
    # a hand-edited record claiming has_lora but with no file -> status False
    service = train_svc(creator, settings, audit, fake_engine, FakeTrainerFactory())
    record = saved_record(creator)
    rec = creator.store.load(record.id)
    rec.identity.has_lora = True
    rec.identity.lora_path = "lora/identity.safetensors"
    creator.store.save(rec)
    assert service.lora_status(record.id)["has_lora"] is False


def test_train_lora_inherited_kinds(creator, settings, audit, fake_engine, trainer_dir):
    service = train_svc(creator, settings, audit, fake_engine, FakeTrainerFactory())
    assert service.train_lora("nope")["kind"] == "not_found"
    assert service.train_lora("")["kind"] == "invalid"


# -- Stage 3d review-pass regressions -----------------------------------------


def test_corrupt_lora_manifest_never_raises(creator, settings, audit, fake_engine,
                                            trainer_dir):
    # Review M1: a valid-JSON lora.json missing a required key raised KeyError
    # (a LookupError) straight through image_lora_status. Now lora_corrupt.
    service = train_svc(creator, settings, audit, fake_engine, FakeTrainerFactory())
    record = saved_record(creator)
    p = creator.store.lora_manifest_path(record.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    for corrupt in ("{not json", "{}", json.dumps([1, 2, 3]),
                    json.dumps({"trigger": "x"}),                 # no character_id/lora_file
                    json.dumps({"character_id": "x"})):           # no lora_file
        p.write_text(corrupt, encoding="utf-8")
        assert service.lora_status(record.id)["kind"] == "lora_corrupt"


def test_corrupt_vetted_manifest_missing_key_in_train(creator, settings, audit,
                                                      fake_engine, trainer_dir):
    service = train_svc(creator, settings, audit, fake_engine, FakeTrainerFactory())
    record = saved_record(creator)
    vp = creator.store.vetted_path(record.id)
    vp.parent.mkdir(parents=True, exist_ok=True)
    for corrupt in ("{}", json.dumps({"entries": []}),           # no character_id
                    json.dumps({"character_id": "x",
                                "entries": [{"seed": 1}]})):      # entry missing path
        vp.write_text(corrupt, encoding="utf-8")
        assert service.train_lora(record.id)["kind"] == "bootstrap_corrupt"


def test_train_lora_manifest_save_failure_is_structured(creator, settings, audit,
                                                        fake_engine, trainer_dir,
                                                        monkeypatch):
    # Review M2: a save_lora_manifest OSError must be structured io, not a raise.
    factory = FakeTrainerFactory()
    service = train_svc(creator, settings, audit, fake_engine, factory)
    record = _with_vetted(creator, n=2)

    def boom(manifest):
        raise OSError(28, "disk full")

    monkeypatch.setattr(creator.store, "save_lora_manifest", boom)
    res = service.train_lora(record.id)
    assert res["ok"] is False and res["kind"] == "io"
    assert settings.get("models.active") is None            # slot released
    # promotion cleanly failed: record not flipped (manifest is a prerequisite)
    assert creator.store.load(record.id).identity.has_lora is False


def test_lora_trigger_is_short_hex_for_a_weird_id(creator):
    # Review L1: the trigger hashes the id, so it is provably [0-9a-f] even for
    # a hand-edited (path-safe but NOT content-gated) id containing "loli".
    # 5.5b: shortened to 6 hex chars (~4 CLIP tokens) from the prior 16-char
    # "cfid"+12hex (11 tokens) — every 3d property preserved.
    import app.imagegen.service as svc_mod
    rec = make_record()
    object.__setattr__(rec, "id", "loli 5678 xx")  # bypass the id invariant
    trig = svc_mod.ImageService._lora_trigger(rec)
    assert len(trig) == 6
    assert all(c in "0123456789abcdef" for c in trig)  # provably [0-9a-f]
    for coded in ("loli", "shota", "cp", " "):
        assert coded not in trig


def test_generation_reads_trigger_from_manifest_not_derivation(creator, settings,
                                                               audit, fake_engine,
                                                               trainer_dir):
    # 5.5b defect fix: a LoRA trained before the trigger derivation changed
    # keeps its ORIGINAL trigger (persisted in its manifest). Generation must
    # read that, not re-derive — re-deriving silently de-triggers the LoRA.
    from app.model import LoraManifest
    service = train_svc(creator, settings, audit, fake_engine, FakeTrainerFactory())
    record = _with_vetted(creator, n=2)
    assert service.train_lora(record.id)["ok"] is True
    # Simulate a LoRA trained under the OLD 16-char derivation: overwrite the
    # persisted trigger with the legacy value.
    legacy = "cfidafa4efa8344b"
    manifest = creator.store.load_lora_manifest(record.id)
    creator.store.save_lora_manifest(LoraManifest(
        character_id=record.id, trigger=legacy, lora_file=manifest.lora_file))
    # The generation path resolves the LEGACY trigger, not the new 6-hex one.
    resolved = service._generation_trigger(creator.store.load(record.id))
    assert resolved == legacy
    assert resolved != service._lora_trigger(record)  # not re-derived


def test_generation_trigger_falls_back_when_manifest_absent(creator, settings,
                                                            audit, fake_engine):
    # No manifest on disk (never trained, or a pre-trigger manifest) -> the
    # generation trigger degrades to the current derivation rather than failing.
    service = build_image_service(
        creator.store, settings, audit, lambda: creator.catalog)
    record = make_record()
    creator.store.save(record)
    assert service._generation_trigger(record) == service._lora_trigger(record)


def test_train_lora_footprint_includes_manifest(creator, settings, audit,
                                                 fake_engine, trainer_dir):
    factory = FakeTrainerFactory()
    service = train_svc(creator, settings, audit, fake_engine, factory)
    record = _with_vetted(creator, n=2)
    res = service.train_lora(record.id)
    assert res["ok"]
    lora_file = creator.store.lora_dir(record.id) / "identity.safetensors"
    # footprint counts the whole lora/ dir (.safetensors + lora.json), so it
    # exceeds just the safetensors size.
    fp = creator.store.load(record.id).identity.footprint.lora_bytes
    assert fp > lora_file.stat().st_size


def test_train_lora_skips_in_dir_non_vetted_paths(creator, settings, audit,
                                                  fake_engine, trainer_dir):
    # A tampered vetted manifest pointing at an in-dir file OUTSIDE vetted/
    # (e.g. character.json) is skipped, not fed in as a training frame.
    factory = FakeTrainerFactory()
    service = train_svc(creator, settings, audit, fake_engine, factory)
    record = saved_record(creator)
    creator.store.vetted_dir(record.id).mkdir(parents=True)
    creator.store.save_vetted(VettedManifest(character_id=record.id, entries=[
        VettedEntry(path="character.json", source_candidate_id="c1", seed=1)]))
    res = service.train_lora(record.id)
    assert res["ok"] is False and res["kind"] == "no_vetted"
    assert factory.built == 0


# ============================================================================
# Stage 3e — seed catalog generation (engine LoRA mode + service orchestration)
# ============================================================================

import app.imagegen.catalog as catalog_mod  # noqa: E402


@pytest.fixture()
def lora_file(tmp_path) -> Path:
    p = tmp_path / "models" / "identity.safetensors"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * 8)
    return p


# -- engine catalog mode -----------------------------------------------------


def test_engine_catalog_mode_builds_lora_backend(fake_engine, lora_file):
    fake_engine.load(mode="catalog", lora=lora_file)
    backend = fake_engine.factory.backends[0]
    assert backend.catalog is True and backend.lora == lora_file
    assert fake_engine.loaded_lora == lora_file
    assert fake_engine.status()["loaded_mode"] == "catalog"
    assert fake_engine.status()["loaded_lora"] == str(lora_file)


def test_engine_catalog_requires_a_lora(fake_engine):
    with pytest.raises(EngineUnavailable, match="no LoRA supplied"):
        fake_engine.load(mode="catalog", lora=None)


def test_engine_catalog_missing_lora_file(fake_engine, tmp_path):
    with pytest.raises(EngineUnavailable, match="LoRA weights not found"):
        fake_engine.load(mode="catalog", lora=tmp_path / "gone.safetensors")


def test_generate_catalog_passes_lora_and_scale(fake_engine, lora_file):
    req = GenerationRequest(positive="cfid, adult", negative="", seed=5, lora_scale=0.9)
    result = fake_engine.generate_catalog(req, lora_file)
    backend = fake_engine.factory.backends[0]
    assert backend.catalog is True
    assert backend.requests[0].lora_scale == 0.9
    assert result.request.seed == 5


def test_engine_catalog_swaps_from_other_modes(fake_engine, ip_adapter_dir, lora_file):
    fake_engine.load(mode="base")
    assert fake_engine.factory.backends[0].catalog is False
    fake_engine.load(mode="catalog", lora=lora_file)      # base -> catalog swap
    assert len(fake_engine.factory.backends) == 2
    assert fake_engine.factory.backends[1].catalog is True
    fake_engine.load(mode="catalog", lora=lora_file)      # idempotent
    assert len(fake_engine.factory.backends) == 2
    fake_engine.load(mode="identity")                     # catalog -> identity swap
    assert len(fake_engine.factory.backends) == 3
    assert fake_engine.factory.backends[2].identity is True
    assert fake_engine.loaded_lora is None


def test_base_stays_idempotent_with_lora_around(fake_engine, lora_file):
    fake_engine.load(mode="base")
    fake_engine.load(mode="base")
    assert len(fake_engine.factory.backends) == 1  # None==None load-key holds


# -- service generate_catalog ------------------------------------------------


def _catalog_ready(creator, **kw):
    """A fully-promoted record: reference set + has_lora + a LoRA file on disk."""
    record = saved_record(creator, **kw)
    cdir = creator.store.char_dir(record.id)
    (cdir / "reference").mkdir(parents=True)
    (cdir / "reference" / "ref.png").write_bytes(b"REF")
    (cdir / "lora").mkdir(parents=True)
    (cdir / "lora" / "identity.safetensors").write_bytes(b"LORA")
    rec = creator.store.load(record.id)
    rec.identity.reference_image_path = "reference/ref.png"
    rec.identity.has_lora = True
    rec.identity.lora_path = "lora/identity.safetensors"
    creator.store.save(rec)
    return record


def _small_matrix(settings, expressions=1, poses=2):
    settings.set("image_gen.catalog.max_expressions", expressions)
    settings.set("image_gen.catalog.max_poses", poses)


def test_generate_catalog_happy_path(creator, settings, audit, fake_engine,
                                     cull_models):
    _small_matrix(settings)  # 1 expr x 2 poses x 1 outfit (fantasy_armor) = 2
    factory = FakeToolkitFactory(outcomes=[{}] * 4)  # all kept
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _catalog_ready(creator)

    res = service.generate_catalog(record.id)
    assert res["ok"] is True
    assert res["frames"] == 2 and res["requested"] == 2 and res["incomplete"] == 0
    assert {e["state"]["pose"] for e in res["entries"]}  # pose states recorded

    # frames + manifest on disk under catalog/
    manifest = creator.store.load_catalog(record.id)
    assert manifest is not None and len(manifest.entries) == 2
    assert manifest.stale is False
    frames_dir = creator.store.catalog_frames_dir(record.id)
    assert len(list(frames_dir.glob("frame-*.png"))) == 2
    # staging swapped away
    assert not (creator.store.char_dir(record.id) / "catalog.new").exists()
    # all char-relative paths
    assert all(e.path.startswith("catalog/") for e in manifest.entries)
    assert str(creator.store.root) not in json.dumps(manifest.to_dict())

    # the catalog backend was LoRA-steered with the cell prompts (trigger + state)
    backend = fake_engine.factory.backends[-1]
    assert backend.catalog is True
    trigger = ImageService._lora_trigger(record)  # no manifest -> derived fallback
    assert any(trigger in r.positive for r in backend.requests)

    # VRAM released; the cull toolkit built AFTER the image unload
    assert settings.get("models.active") is None
    assert factory.active_at_build == [None]
    assert any(e["kind"] == "catalog_generated" for e in audit_events(audit))


def test_generate_catalog_rejects_and_retries(creator, settings, audit, fake_engine,
                                              cull_models):
    _small_matrix(settings, expressions=1, poses=2)  # 2 cells
    # first-seen frame is off-model (low similarity) -> rejected -> retried
    factory = FakeToolkitFactory(outcomes=[{"similarity": 0.1}])
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _catalog_ready(creator)
    res = service.generate_catalog(record.id)
    assert res["ok"] is True
    assert res["frames"] == 2 and res["incomplete"] == 0
    assert factory.built == 2  # two cull passes (one initial + one retry)


def test_generate_catalog_no_lora(creator, settings, audit, fake_engine, cull_models):
    factory = FakeToolkitFactory()
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = saved_record(creator)  # no LoRA
    res = service.generate_catalog(record.id)
    assert res["ok"] is False and res["kind"] == "no_lora"


def test_generate_catalog_lora_missing(creator, settings, audit, fake_engine,
                                       cull_models):
    factory = FakeToolkitFactory()
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _catalog_ready(creator)
    (creator.store.char_dir(record.id) / "lora" / "identity.safetensors").unlink()
    res = service.generate_catalog(record.id)
    assert res["ok"] is False and res["kind"] == "lora_missing"


def test_generate_catalog_no_reference(creator, settings, audit, fake_engine,
                                       cull_models):
    factory = FakeToolkitFactory()
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _catalog_ready(creator)
    rec = creator.store.load(record.id)
    rec.identity.reference_image_path = None
    creator.store.save(rec)
    res = service.generate_catalog(record.id)
    assert res["ok"] is False and res["kind"] == "no_reference"


def test_generate_catalog_cull_models_missing(creator, settings, audit, fake_engine):
    factory = FakeToolkitFactory()
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _catalog_ready(creator)
    res = service.generate_catalog(record.id)  # no cull_models fixture
    # post-embedder-swap: the first missing witness is the classifier dir
    assert res["ok"] is False and res["kind"] == "classifier_unavailable"
    assert factory.built == 0


def test_generate_catalog_empty_when_all_rejected(creator, settings, audit,
                                                  fake_engine, cull_models):
    _small_matrix(settings, expressions=1, poses=2)
    settings.set("image_gen.catalog.max_attempts", 1)
    factory = FakeToolkitFactory(block_all=True)  # everything content-blocked
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _catalog_ready(creator)
    res = service.generate_catalog(record.id)
    assert res["ok"] is False and res["kind"] == "catalog_empty"
    assert creator.store.load_catalog(record.id) is None


def test_generate_catalog_preserves_prior_on_engine_failure(creator, settings, audit,
                                                            fake_engine, cull_models):
    _small_matrix(settings, expressions=1, poses=2)
    factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _catalog_ready(creator)
    # first build a good catalog
    assert service.generate_catalog(record.id)["ok"] is True
    prior = creator.store.load_catalog(record.id)
    assert prior.entries
    prior_frames = sorted(p.name for p in
                          creator.store.catalog_frames_dir(record.id).glob("*.png"))

    # now a re-generate whose engine fails -> prior catalog intact
    fake_engine.load(mode="catalog", lora=creator.store.char_dir(record.id) / "lora" / "identity.safetensors")
    fake_engine.factory.backends[-1].generate = lambda request, reference=None: (
        _ for _ in ()).throw(MemoryError("oom"))
    res = service.generate_catalog(record.id)
    assert res["ok"] is False and res["kind"] == "engine"
    after = creator.store.load_catalog(record.id)
    assert after is not None and len(after.entries) == len(prior.entries)
    assert sorted(p.name for p in creator.store.catalog_frames_dir(record.id).glob("*.png")) == prior_frames
    assert not (creator.store.char_dir(record.id) / "catalog.new").exists()
    assert settings.get("models.active") is None


def test_generate_catalog_blocked_record(creator, settings, audit, fake_engine,
                                         cull_models):
    factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _catalog_ready(creator)
    rec = creator.store.load(record.id)
    rec.free_text["appearance_notes"] = "hanging out with the kids at recess"
    creator.store.save(rec)
    res = service.generate_catalog(record.id)
    assert res["ok"] is False and res["kind"] == "blocked"


# -- catalog_status / clear_catalog ------------------------------------------


def test_catalog_status_and_clear(creator, settings, audit, fake_engine, cull_models):
    _small_matrix(settings, expressions=1, poses=2)
    factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _catalog_ready(creator)
    assert service.catalog_status(record.id) == {
        "ok": True, "id": record.id, "has_catalog": False, "frames": 0,
        "stale": False, "states": []}
    service.generate_catalog(record.id)
    st = service.catalog_status(record.id)
    assert st["has_catalog"] is True and st["frames"] == 2 and len(st["states"]) == 2

    cleared = service.clear_catalog(record.id)
    assert cleared["ok"] is True and cleared["removed"] is True
    assert creator.store.load_catalog(record.id) is None
    assert not creator.store.catalog_frames_dir(record.id).exists()
    assert any(e["kind"] == "catalog_cleared" for e in audit_events(audit))


def test_catalog_status_corrupt_manifest(creator, settings, audit, fake_engine):
    service = bootstrap_service(creator, settings, audit, fake_engine,
                                FakeToolkitFactory())
    record = saved_record(creator)
    p = creator.store.catalog_path(record.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    for corrupt in ("{not json", "{}", json.dumps({"character_id": "../x"})):
        p.write_text(corrupt, encoding="utf-8")
        assert service.catalog_status(record.id)["kind"] == "catalog_corrupt"


def test_generate_catalog_inherited_kinds(creator, settings, audit, fake_engine,
                                          cull_models):
    service = bootstrap_service(creator, settings, audit, fake_engine,
                                FakeToolkitFactory())
    assert service.generate_catalog("nope")["kind"] == "not_found"
    assert service.generate_catalog("")["kind"] == "invalid"


# -- Stage 3e review-pass regressions -----------------------------------------


def test_generate_catalog_no_states(creator, settings, audit, fake_engine,
                                    cull_models, monkeypatch):
    # A malformed catalog_states.json -> load_catalog_states() == ([],[]) ->
    # build_cells [] -> structured no_states (the DoD-flagged untested branch).
    monkeypatch.setattr(catalog_mod, "load_catalog_states", lambda: ([], []))
    factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _catalog_ready(creator)
    res = service.generate_catalog(record.id)
    assert res["ok"] is False and res["kind"] == "no_states"
    assert factory.built == 0


def test_generate_catalog_partial_success(creator, settings, audit, fake_engine,
                                          cull_models):
    # One cell fails its only attempt -> ok:True with incomplete>0 and fewer
    # frames than requested (the untested partial branch).
    _small_matrix(settings, expressions=1, poses=2)
    settings.set("image_gen.catalog.max_attempts", 1)
    factory = FakeToolkitFactory(outcomes=[{"similarity": 0.1}])  # first frame off-model
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _catalog_ready(creator)
    res = service.generate_catalog(record.id)
    assert res["ok"] is True
    assert res["frames"] == 1 and res["requested"] == 2 and res["incomplete"] == 1
    assert len(creator.store.load_catalog(record.id).entries) == 1


def test_catalog_relaxed_face_area_min_keeps_small_faces(creator, settings, audit,
                                                         fake_engine, cull_models):
    # A pose-varied frame with a small face (area 0.02) would fail the 3c
    # bootstrap floor (0.04) but passes the relaxed catalog floor (0.01).
    _small_matrix(settings, expressions=1, poses=2)
    factory = FakeToolkitFactory(outcomes=[{"area": 0.02}] * 4)
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _catalog_ready(creator)
    res = service.generate_catalog(record.id)
    assert res["ok"] is True and res["frames"] == 2  # not culled on face area


def test_catalog_finalize_failure_preserves_prior(creator, settings, audit,
                                                  fake_engine, cull_models,
                                                  monkeypatch):
    # Review MEDIUM: a mid-swap os.replace failure must roll back so the prior
    # catalog + manifest stay consistent (not phantom).
    _small_matrix(settings, expressions=1, poses=2)
    factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _catalog_ready(creator)
    assert service.generate_catalog(record.id)["ok"] is True
    prior = creator.store.load_catalog(record.id)
    frames_dir = creator.store.catalog_frames_dir(record.id)
    prior_names = sorted(p.name for p in frames_dir.glob("*.png"))
    assert prior.entries

    import app.imagegen.service as svc_mod
    real_replace = svc_mod.os.replace

    def boom(src, dst):
        if "catalog.new" in str(src):  # the staging -> catalog swap
            raise OSError(13, "locked")
        return real_replace(src, dst)

    monkeypatch.setattr(svc_mod.os, "replace", boom)
    res = service.generate_catalog(record.id)
    assert res["ok"] is False and res["kind"] == "io"
    after = creator.store.load_catalog(record.id)
    assert after is not None and len(after.entries) == len(prior.entries)
    assert sorted(p.name for p in frames_dir.glob("*.png")) == prior_names
    char_dir = creator.store.char_dir(record.id)
    assert not (char_dir / "catalog.new").exists()
    assert not (char_dir / "catalog.old").exists()  # backup cleaned up on rollback


def test_catalog_finalize_double_fault_drops_dangling_manifest(creator, settings,
                                                               audit, fake_engine,
                                                               cull_models, monkeypatch):
    # Red-team F3: if the rollback's OWN restore also fails (double disk fault),
    # the end state must stay CONSISTENT — drop the dangling manifest so
    # catalog_status reports no catalog, never phantom frames.
    _small_matrix(settings, expressions=1, poses=2)
    factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _catalog_ready(creator)
    assert service.generate_catalog(record.id)["ok"] is True  # a prior catalog

    import app.imagegen.service as svc_mod
    real_replace = svc_mod.os.replace

    def boom(src, dst):
        if Path(dst).name == "catalog":  # every rename INTO the live catalog dir
            raise OSError(13, "locked")
        return real_replace(src, dst)

    monkeypatch.setattr(svc_mod.os, "replace", boom)
    res = service.generate_catalog(record.id)
    assert res["ok"] is False and res["kind"] == "io"
    # consistent: no phantom manifest (dropped), and status agrees
    assert creator.store.load_catalog(record.id) is None
    assert service.catalog_status(record.id)["has_catalog"] is False
    assert settings.get("models.active") is None
    # prior frames remain recoverable in catalog.old
    assert (creator.store.char_dir(record.id) / "catalog.old").exists()


def test_identity_needs_cpu_offload_thresholds():
    # Hardware-validation catch (2026-07-12, RTX 4070 Super 12 GB): the
    # fully-resident identity stack peaks ~12.2-12.3 GB and WDDM-spills on a
    # 12 GB card (~2x slower). Below the floor the backend must pick
    # model-cpu-offload; at/above it, the resident path. Garbage total ->
    # the default resident path (degrade, never crash).
    from app.imagegen.engine import (
        IDENTITY_RESIDENT_VRAM_MIN_GB,
        identity_needs_cpu_offload,
    )

    gib = 1 << 30
    assert identity_needs_cpu_offload(12 * gib) is True
    assert identity_needs_cpu_offload(int(IDENTITY_RESIDENT_VRAM_MIN_GB * gib) - 1) is True
    assert identity_needs_cpu_offload(int(IDENTITY_RESIDENT_VRAM_MIN_GB * gib)) is False
    assert identity_needs_cpu_offload(16 * gib) is False
    assert identity_needs_cpu_offload(None) is False
    assert identity_needs_cpu_offload("garbage") is False


def test_pin_hf_offline_gated_on_pipeline_config_dir(settings, monkeypatch):
    # Hardware-validation catch (2026-07-12): huggingface_hub freezes
    # HF_HUB_OFFLINE at import, and the normal flow's first heavy import is
    # the BASE backend's (which predates the 3b identity offline gate) — so
    # a bootstrap cull was making live etag requests for cached models.
    # pin_hf_offline runs at startup: offline iff the local config dir is
    # set (the documented one-time config warm needs the hub when unset).
    import os

    from app.imagegen.engine import pin_hf_offline

    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    pin_hf_offline(settings)  # unset -> hub stays reachable for the warm
    assert "HF_HUB_OFFLINE" not in os.environ
    assert "TRANSFORMERS_OFFLINE" not in os.environ

    settings.set("models.image.pipeline_config_dir", "   ")
    pin_hf_offline(settings)  # blank counts as unset
    assert "HF_HUB_OFFLINE" not in os.environ

    settings.set("models.image.pipeline_config_dir", "models/sdxl_config")
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")  # hard-set wins over stale env
    pin_hf_offline(settings)
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"


# ============================================================================
# Stage 3g — on-demand generation + cache (state -> frame; the "grow" of §7)
# ============================================================================

from app.imagegen import MatteReading, MatteToolkit, MatteUnavailable  # noqa: E402
from app.model import CatalogEntry, CatalogManifest  # noqa: E402


class CacheMatteFactory:
    """Minimal matte-toolkit fake for the 3g flows: the matter writes RGBA
    bytes, the classifier is outcome-driven (keyed by source basename in
    first-seen order, like FakeMatteFactory), and both call counts are
    recorded so tests can assert fresh-frame-no-classify vs heal-classify."""

    def __init__(self, outcomes=None, *, raise_kind=None, raise_exc=None):
        self.outcomes = list(outcomes or [])
        self.raise_kind = raise_kind
        self.raise_exc = raise_exc
        self._order: dict = {}
        self.built = 0
        self.matte_calls = 0
        self.classify_calls = 0

    def _outcome(self, path):
        key = Path(path).name
        if key not in self._order:
            self._order[key] = len(self._order)
        idx = self._order[key]
        return self.outcomes[idx] if idx < len(self.outcomes) else {}

    def __call__(self, settings, config):
        if self.raise_kind:
            raise MatteUnavailable(self.raise_kind)
        if self.raise_exc is not None:
            raise self.raise_exc
        self.built += 1
        factory = self

        class _M:
            def matte(self, src, out):
                factory.matte_calls += 1
                o = factory._outcome(src)
                if o.get("matte_raise"):
                    raise RuntimeError("matte boom")
                Path(out).write_bytes(b"RGBA")
                return MatteReading(coverage=o.get("coverage", 0.5),
                                    mean_alpha=0.4)

        class _C:
            def classify(self, p):
                factory.classify_calls += 1
                o = factory._outcome(p)
                if o.get("blocked"):
                    return ContentVerdict(blocked=True, category="minors",
                                          matched="loli")
                return ContentVerdict(blocked=False)

        return MatteToolkit(matter=_M(), classifier=_C(), closer=None)


@pytest.fixture()
def matting_model(tmp_path, settings) -> Path:
    """A fake user-placed matting model so preflight_matte passes (the
    classifier dir arrives via cull_models)."""
    model = tmp_path / "models" / "isnet-anime.onnx"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"\0" * 8)
    settings.set("models.image.matting_model_path", str(model))
    return model


def cache_service(creator, settings, audit, fake_engine, cull_factory,
                  matte_factory=None):
    return ImageService(
        creator.store, settings, audit,
        catalog_provider=lambda: creator.catalog,
        engine=fake_engine, toolkit_factory=cull_factory,
        matte_factory=matte_factory or CacheMatteFactory(),
    )


STATE = {"expression": "smile", "pose": "sitting", "outfit": "fantasy_armor"}


def _engine_requests(fake_engine) -> int:
    return sum(len(b.requests) for b in fake_engine.factory.backends)


# -- generate (novel state) ---------------------------------------------------


def test_on_demand_generates_culls_mattes_and_caches(
        creator, settings, audit, fake_engine, cull_models, matting_model):
    cull_factory = FakeToolkitFactory(outcomes=[{}] * 4)
    matte_factory = CacheMatteFactory()
    service = cache_service(creator, settings, audit, fake_engine,
                            cull_factory, matte_factory)
    record = _catalog_ready(creator)

    res = service.generate_on_demand(record.id, dict(STATE))
    assert res["ok"] is True
    assert res["cached"] is False and res["source"] == "generated"
    assert res["state"] == STATE and res["attempts"] == 1
    assert res["path"].startswith("cache/")
    assert res["matted_path"] == "cache/matted/" + res["frame_id"] + ".png"
    assert res["matte_status"] == "matted"

    cdir = creator.store.char_dir(record.id)
    frame = cdir / res["path"]
    assert frame.is_file() and Path(res["abs_path"]) == frame.resolve()
    sidecar = json.loads(frame.with_suffix(".json").read_text(encoding="utf-8"))
    assert sidecar["stage"] == "3g-cache" and sidecar["kind"] == "cache"
    assert (cdir / res["matted_path"]).is_file()
    assert not (cdir / "cache.new").exists()          # staging swept

    manifest = creator.store.load_cache(record.id)
    assert manifest is not None and len(manifest.entries) == 1
    entry = manifest.entries[0]
    assert entry.on_demand is True and entry.last_used
    assert entry.state == STATE and entry.bytes > 0
    # the LoRA-steered cell prompt = trigger + the state fragments
    trigger = ImageService._lora_trigger(record)  # no manifest -> derived fallback
    reqs = [r for b in fake_engine.factory.backends for r in b.requests]
    assert any(trigger in r.positive and "gentle smile" in r.positive
               and "sitting" in r.positive for r in reqs)
    # VRAM: slot free; the cull toolkit built AFTER the image unload; the
    # fresh frame was matted WITHOUT a redundant classify (same-run pixels)
    assert settings.get("models.active") is None
    assert cull_factory.active_at_build == [None]
    assert matte_factory.matte_calls == 1 and matte_factory.classify_calls == 0
    assert any(e["kind"] == "cache_generated" for e in audit_events(audit))


def test_on_demand_insert_enforces_lru_cap(creator, settings, audit,
                                           fake_engine, cull_models,
                                           matting_model):
    """The §14 backstop rides every cache insert: a grown-over-cap cache is
    brought back under the cap right after the new frame lands, oldest
    last_used first, and the fresh frame is the protected MRU."""
    cull_factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service = cache_service(creator, settings, audit, fake_engine,
                            cull_factory)
    record = _catalog_ready(creator)
    # a pre-existing cache already past the cap (the floor is 8 MB)
    frames = creator.store.cache_frames_dir(record.id)
    frames.mkdir(parents=True)
    mb = 1024 * 1024
    for fid, stamp in (("old", "2026-01-01T00:00:00+00:00"),
                       ("mid", "2026-01-02T00:00:00+00:00")):
        (frames / f"{fid}.png").write_bytes(b"\0" * (5 * mb))
    creator.store.save_cache(CatalogManifest(character_id=record.id, entries=[
        CatalogEntry(frame_id="old", path="cache/old.png",
                     state={"expression": "old"}, on_demand=True,
                     bytes=5 * mb, last_used="2026-01-01T00:00:00+00:00"),
        CatalogEntry(frame_id="mid", path="cache/mid.png",
                     state={"expression": "mid"}, on_demand=True,
                     bytes=5 * mb, last_used="2026-01-02T00:00:00+00:00"),
    ]))
    settings.set("library.cache_cap_bytes", 8 * mb, save=False)

    res = service.generate_on_demand(record.id, dict(STATE))
    assert res["ok"] is True and res["source"] == "generated"
    assert res["evicted"] == 1
    assert not (frames / "old.png").exists()      # LRU went
    assert (frames / "mid.png").exists()
    manifest = creator.store.load_cache(record.id)
    ids = [e.frame_id for e in manifest.entries]
    assert "old" not in ids and "mid" in ids and res["frame_id"] in ids
    assert any(e["kind"] == "cache_evicted" for e in audit_events(audit))


def test_on_demand_insert_pins_fresh_frame_against_lru_tie(
        monkeypatch, creator, settings, audit, fake_engine, cull_models,
        matting_model):
    """The post-insert cap hook must pass the just-inserted frame's id as
    protect_frame_id so a same-second last_used tie can never evict it."""
    cull_factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service = cache_service(creator, settings, audit, fake_engine,
                            cull_factory)
    record = _catalog_ready(creator)
    seen = {}
    real = service.enforce_cache_cap
    monkeypatch.setattr(service, "enforce_cache_cap",
                        lambda cid, protect_frame_id=None: seen.setdefault(
                            "pid", protect_frame_id) or real(
                            cid, protect_frame_id))
    res = service.generate_on_demand(record.id, dict(STATE))
    assert res["ok"] is True
    assert seen["pid"] == res["frame_id"]  # the fresh frame, pinned


def test_on_demand_instant_hit_from_cache(creator, settings, audit, fake_engine,
                                          cull_models, matting_model):
    cull_factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service = cache_service(creator, settings, audit, fake_engine, cull_factory)
    record = _catalog_ready(creator)
    first = service.generate_on_demand(record.id, dict(STATE))
    assert first["ok"] is True
    gen_count = _engine_requests(fake_engine)
    builds = cull_factory.built

    # age the LRU stamp so the bump is observable at second granularity
    manifest = creator.store.load_cache(record.id)
    manifest.entries[0].last_used = "2020-01-01T00:00:00+00:00"
    creator.store.save_cache(manifest)

    res = service.generate_on_demand(record.id, dict(STATE))
    assert res["ok"] is True and res["cached"] is True
    assert res["source"] == "cache" and res["frame_id"] == first["frame_id"]
    assert res["matted_path"] == first["matted_path"]
    assert _engine_requests(fake_engine) == gen_count   # no generation
    assert cull_factory.built == builds                 # no cull models
    stored = creator.store.load_cache(record.id).entries[0]
    assert stored.last_used > "2020-01-01"              # LRU signal bumped


def test_on_demand_hit_from_catalog_without_matting(creator, settings, audit,
                                                    fake_engine, cull_models):
    service = cache_service(creator, settings, audit, fake_engine,
                            FakeToolkitFactory())
    record = _catalog_ready(creator)
    frames_dir = creator.store.catalog_frames_dir(record.id)
    frames_dir.mkdir(parents=True)
    (frames_dir / "frame-1.png").write_bytes(b"PNG1")
    creator.store.save_catalog(CatalogManifest(
        character_id=record.id,
        entries=[CatalogEntry(frame_id="frame-1", path="catalog/frame-1.png",
                              state=dict(STATE), on_demand=False, bytes=4)],
        updated_at="2025-12-31T00:00:00+00:00"))

    res = service.generate_on_demand(record.id, dict(STATE))
    assert res["ok"] is True and res["cached"] is True
    assert res["source"] == "catalog"
    # matting unconfigured: served unmatted with the precise reason
    assert res["matted_path"] is None
    assert res["matte_status"] == "matting_model_missing"
    assert _engine_requests(fake_engine) == 0
    # a catalog hit is not LRU-tracked and wrote nothing
    stored = creator.store.load_catalog(record.id)
    assert stored.updated_at == "2025-12-31T00:00:00+00:00"
    assert stored.entries[0].last_used is None


def test_on_demand_hit_heals_missing_matte(creator, settings, audit,
                                           fake_engine, cull_models,
                                           matting_model):
    matte_factory = CacheMatteFactory()
    service = cache_service(creator, settings, audit, fake_engine,
                            FakeToolkitFactory(), matte_factory)
    record = _catalog_ready(creator)
    frames_dir = creator.store.catalog_frames_dir(record.id)
    frames_dir.mkdir(parents=True)
    (frames_dir / "frame-1.png").write_bytes(b"PNG1")
    creator.store.save_catalog(CatalogManifest(
        character_id=record.id,
        entries=[CatalogEntry(frame_id="frame-1", path="catalog/frame-1.png",
                              state=dict(STATE), on_demand=False, bytes=4)],
        updated_at="2025-12-31T00:00:00+00:00"))

    res = service.generate_on_demand(record.id, dict(STATE))
    assert res["ok"] is True and res["cached"] is True
    assert res["matte_status"] == "matted"
    assert res["matted_path"] == "catalog/matted/frame-1.png"
    # heal IS a processing boundary: classified fail-closed before matting
    assert matte_factory.classify_calls == 1 and matte_factory.matte_calls == 1
    assert (creator.store.matted_dir(record.id) / "frame-1.png").is_file()
    stored = creator.store.load_catalog(record.id)
    assert stored.entries[0].matted_path == "catalog/matted/frame-1.png"
    assert any(e["kind"] == "cache_matted" and e["status"] == "matted"
               for e in audit_events(audit))


def test_on_demand_heal_blocked_purges_and_regenerates(
        creator, settings, audit, fake_engine, cull_models, matting_model):
    cull_factory = FakeToolkitFactory(outcomes=[{}] * 4)
    matte_factory = CacheMatteFactory()
    service = cache_service(creator, settings, audit, fake_engine,
                            cull_factory, matte_factory)
    record = _catalog_ready(creator)
    first = service.generate_on_demand(record.id, dict(STATE))
    assert first["ok"] is True

    # hand-strip the matte + block the OLD frame's pixels on re-screen
    cdir = creator.store.char_dir(record.id)
    (cdir / first["matted_path"]).unlink()
    manifest = creator.store.load_cache(record.id)
    manifest.entries[0].matted_path = None
    creator.store.save_cache(manifest)
    matte_factory.outcomes = [{"blocked": True}]  # first-seen = the old frame

    res = service.generate_on_demand(record.id, dict(STATE))
    assert res["ok"] is True
    assert res["cached"] is False and res["source"] == "generated"
    assert res["frame_id"] != first["frame_id"]
    assert not (cdir / first["path"]).exists()          # purged pixels
    manifest = creator.store.load_cache(record.id)
    assert len(manifest.entries) == 1                    # replaced, not appended
    assert manifest.entries[0].frame_id == res["frame_id"]
    blocks = [e for e in audit_events(audit)
              if e["kind"] == "filter_block"
              and e.get("context") == "image.cache.heal"]
    assert len(blocks) == 1 and blocks[0]["layer"] == 2


def test_on_demand_force_replaces_same_state(creator, settings, audit,
                                             fake_engine, cull_models,
                                             matting_model):
    cull_factory = FakeToolkitFactory(outcomes=[{}] * 8)
    service = cache_service(creator, settings, audit, fake_engine, cull_factory)
    record = _catalog_ready(creator)
    first = service.generate_on_demand(record.id, dict(STATE))
    res = service.generate_on_demand(record.id, dict(STATE), force=True)
    assert res["ok"] is True and res["cached"] is False
    assert res["replaced"] == 1 and res["frame_id"] != first["frame_id"]
    cdir = creator.store.char_dir(record.id)
    assert not (cdir / first["path"]).exists()
    assert not (cdir / first["path"]).with_suffix(".json").exists()
    assert not (cdir / first["matted_path"]).exists()
    manifest = creator.store.load_cache(record.id)
    assert [e.frame_id for e in manifest.entries] == [res["frame_id"]]


def test_on_demand_frame_rejected_after_attempts(creator, settings, audit,
                                                 fake_engine, cull_models,
                                                 matting_model):
    # every render is off-model -> rejected -> retried -> structured refusal
    cull_factory = FakeToolkitFactory(outcomes=[{"similarity": 0.1}] * 8)
    service = cache_service(creator, settings, audit, fake_engine, cull_factory)
    record = _catalog_ready(creator)
    res = service.generate_on_demand(record.id, dict(STATE))
    assert res["ok"] is False and res["kind"] == "frame_rejected"
    cdir = creator.store.char_dir(record.id)
    assert not (cdir / "cache.new").exists()
    cache_dir = cdir / "cache"
    assert not cache_dir.exists() or not list(cache_dir.glob("*.png"))
    assert creator.store.load_cache(record.id) is None
    assert cull_factory.built == 2  # max_attempts default


def test_on_demand_content_block_audits_and_retries(creator, settings, audit,
                                                    fake_engine, cull_models,
                                                    matting_model):
    cull_factory = FakeToolkitFactory(outcomes=[{"blocked": True}, {}])
    service = cache_service(creator, settings, audit, fake_engine, cull_factory)
    record = _catalog_ready(creator)
    res = service.generate_on_demand(record.id, dict(STATE))
    assert res["ok"] is True and res["attempts"] == 2
    blocks = [e for e in audit_events(audit)
              if e["kind"] == "filter_block"
              and e.get("context") == "image.cache.frame"]
    assert len(blocks) == 1 and blocks[0]["layer"] == 2


def test_on_demand_state_validation_kinds(creator, settings, audit,
                                          fake_engine, cull_models):
    service = cache_service(creator, settings, audit, fake_engine,
                            FakeToolkitFactory())
    record = _catalog_ready(creator)
    assert service.generate_on_demand(record.id, "nope")["kind"] == "invalid"
    assert service.generate_on_demand(record.id, {})["kind"] == "invalid"
    bad = dict(STATE)
    bad["expression"] = "unknown-expr"
    assert service.generate_on_demand(record.id, bad)["kind"] == "unknown_state"
    assert service.generate_on_demand("nope", dict(STATE))["kind"] == "not_found"
    assert service.generate_on_demand("", dict(STATE))["kind"] == "invalid"


def test_on_demand_precondition_kinds(creator, settings, audit, fake_engine,
                                      cull_models):
    service = cache_service(creator, settings, audit, fake_engine,
                            FakeToolkitFactory())
    # no LoRA at all
    record = saved_record(creator)
    assert service.generate_on_demand(record.id, dict(STATE))["kind"] == "no_lora"
    # LoRA flagged but the file is gone
    ready = _catalog_ready(creator)
    (creator.store.char_dir(ready.id) / "lora" / "identity.safetensors").unlink()
    assert service.generate_on_demand(
        ready.id, dict(STATE))["kind"] == "lora_missing"
    # reference gone
    ready2 = _catalog_ready(creator)
    (creator.store.char_dir(ready2.id) / "reference" / "ref.png").unlink()
    assert service.generate_on_demand(
        ready2.id, dict(STATE))["kind"] == "reference_missing"


def test_on_demand_cull_models_missing(creator, settings, audit, fake_engine):
    # no cull_models fixture -> preflight fails BEFORE any GPU work
    service = cache_service(creator, settings, audit, fake_engine,
                            FakeToolkitFactory())
    record = _catalog_ready(creator)
    res = service.generate_on_demand(record.id, dict(STATE))
    assert res["ok"] is False
    assert res["kind"] in ("face_models_missing", "classifier_unavailable")
    assert _engine_requests(fake_engine) == 0


def test_on_demand_matte_failure_still_caches_then_heals(
        creator, settings, audit, fake_engine, cull_models, matting_model):
    cull_factory = FakeToolkitFactory(outcomes=[{}] * 4)
    matte_factory = CacheMatteFactory(outcomes=[{"matte_raise": True}])
    service = cache_service(creator, settings, audit, fake_engine,
                            cull_factory, matte_factory)
    record = _catalog_ready(creator)
    res = service.generate_on_demand(record.id, dict(STATE))
    assert res["ok"] is True                     # matte is best-effort
    assert res["matted_path"] is None
    assert res["matte_status"] == "matte_failed"
    entry = creator.store.load_cache(record.id).entries[0]
    assert entry.matted_path is None

    # the next hit heals the gap (classify + matte now succeed)
    matte_factory.outcomes = []
    res2 = service.generate_on_demand(record.id, dict(STATE))
    assert res2["ok"] is True and res2["cached"] is True
    assert res2["matte_status"] == "matted"
    stored = creator.store.load_cache(record.id).entries[0]
    assert stored.matted_path == res2["matted_path"]


def test_on_demand_dangling_cache_entry_reads_as_novel(
        creator, settings, audit, fake_engine, cull_models, matting_model):
    cull_factory = FakeToolkitFactory(outcomes=[{}] * 8)
    service = cache_service(creator, settings, audit, fake_engine, cull_factory)
    record = _catalog_ready(creator)
    first = service.generate_on_demand(record.id, dict(STATE))
    (creator.store.char_dir(record.id) / first["path"]).unlink()  # dangle it
    res = service.generate_on_demand(record.id, dict(STATE))
    assert res["ok"] is True and res["cached"] is False
    assert res["replaced"] == 1                  # the dead entry was dropped
    assert len(creator.store.load_cache(record.id).entries) == 1


def test_on_demand_corrupt_cache_manifest(creator, settings, audit,
                                          fake_engine, cull_models):
    service = cache_service(creator, settings, audit, fake_engine,
                            FakeToolkitFactory())
    record = _catalog_ready(creator)
    creator.store.cache_path(record.id).write_text("{not json",
                                                   encoding="utf-8")
    res = service.generate_on_demand(record.id, dict(STATE))
    assert res["ok"] is False and res["kind"] == "cache_corrupt"
    assert service.cache_status(record.id)["kind"] == "cache_corrupt"
    # a manifest claiming ANOTHER character is corrupt too (save_cache routes
    # by the manifest's own id)
    other = CatalogManifest(character_id="someoneelse")
    creator.store.cache_path(record.id).write_text(
        json.dumps(other.to_dict()), encoding="utf-8")
    assert service.cache_status(record.id)["kind"] == "cache_corrupt"


def test_cache_survives_a_catalog_regeneration(creator, settings, audit,
                                               fake_engine, cull_models,
                                               matting_model):
    _small_matrix(settings)
    cull_factory = FakeToolkitFactory(outcomes=[{}] * 8)
    service = cache_service(creator, settings, audit, fake_engine, cull_factory)
    record = _catalog_ready(creator)
    cached = service.generate_on_demand(record.id, dict(STATE))
    assert cached["ok"] is True
    assert service.generate_catalog(record.id)["ok"] is True  # 3e swap
    # the grown cache is a sibling: it survives the swap and still serves
    res = service.generate_on_demand(record.id, dict(STATE))
    assert res["ok"] is True and res["cached"] is True
    assert res["source"] == "cache" and res["frame_id"] == cached["frame_id"]


# -- cache_status / clear_cache ------------------------------------------------


def test_cache_status_and_clear(creator, settings, audit, fake_engine,
                                cull_models, matting_model):
    cull_factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service = cache_service(creator, settings, audit, fake_engine, cull_factory)
    record = _catalog_ready(creator)
    empty = service.cache_status(record.id)
    assert empty == {"ok": True, "id": record.id, "has_cache": False,
                     "frames": 0, "matted": 0, "unmatted": 0, "bytes": 0,
                     "stale": False, "states": [], "matte_ready": True,
                     "matte_missing": None}
    service.generate_on_demand(record.id, dict(STATE))
    st = service.cache_status(record.id)
    assert st["has_cache"] is True and st["frames"] == 1
    assert st["matted"] == 1 and st["unmatted"] == 0 and st["bytes"] > 0
    row = st["states"][0]
    assert row["state"] == STATE and row["matted"] is True and row["last_used"]
    cleared = service.clear_cache(record.id)
    assert cleared["ok"] is True and cleared["removed"] is True
    assert service.cache_status(record.id)["has_cache"] is False
    assert not creator.store.cache_frames_dir(record.id).exists()
    assert any(e["kind"] == "cache_cleared" for e in audit_events(audit))


def test_cache_status_matted_path_must_resolve_into_cache_matted(
        creator, settings, audit, fake_engine, cull_models, matting_model):
    cull_factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service = cache_service(creator, settings, audit, fake_engine, cull_factory)
    record = _catalog_ready(creator)
    service.generate_on_demand(record.id, dict(STATE))
    manifest = creator.store.load_cache(record.id)
    manifest.entries[0].matted_path = "../../../evil.png"   # hand-edit
    creator.store.save_cache(manifest)
    st = service.cache_status(record.id)
    assert st["matted"] == 0 and st["unmatted"] == 1        # counts UNMATTED


# -- 3g internals ---------------------------------------------------------------


def test_save_manifest_quietly_token_mismatch_never_clobbers(
        creator, settings, audit, fake_engine, cull_models, matting_model):
    cull_factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service = cache_service(creator, settings, audit, fake_engine, cull_factory)
    record = _catalog_ready(creator)
    service.generate_on_demand(record.id, dict(STATE))
    manifest = creator.store.load_cache(record.id)
    before = creator.store.cache_path(record.id).read_text(encoding="utf-8")
    # a stale token (someone else saved since) must write NOTHING
    ok = service._save_manifest_quietly(record.id, "cache", manifest,
                                        "1999-01-01T00:00:00+00:00")
    assert ok is False
    assert creator.store.cache_path(record.id).read_text(
        encoding="utf-8") == before
    # the matching token writes
    ok = service._save_manifest_quietly(record.id, "cache", manifest,
                                        manifest.updated_at)
    assert ok is True


def test_move_unique_never_clobbers(tmp_path, images):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    (dest_dir / "frame-1.png").write_bytes(b"KEEP")
    (src_dir / "frame-1.png").write_bytes(b"NEW")
    moved = images._move_unique(src_dir / "frame-1.png", dest_dir)
    assert moved.name == "frame-1-2.png"
    assert (dest_dir / "frame-1.png").read_bytes() == b"KEEP"
    assert moved.read_bytes() == b"NEW"


def test_on_demand_blocked_cell_prompt(creator, settings, audit, fake_engine,
                                       cull_models, monkeypatch):
    # a states drop-in whose fragment trips the Layer-1 prompt gate: the cell
    # is blocked + audited, nothing generates
    service = cache_service(creator, settings, audit, fake_engine,
                            FakeToolkitFactory())
    record = _catalog_ready(creator)
    fake_states = ([catalog_mod.CatalogState("evil", "young girl")],
                   [catalog_mod.CatalogState("sitting", "sitting")])
    monkeypatch.setattr(catalog_mod, "load_catalog_states",
                        lambda: fake_states)
    res = service.generate_on_demand(
        record.id, {"expression": "evil", "pose": "sitting",
                    "outfit": "fantasy_armor"})
    assert res["ok"] is False and res["kind"] == "blocked"
    assert _engine_requests(fake_engine) == 0
    assert any(e["kind"] == "filter_block" and e["layer"] == 1
               and str(e.get("context", "")).startswith("image.cache.")
               for e in audit_events(audit))


def test_on_demand_mutates_only_footprint_not_the_record(
        creator, settings, audit, fake_engine, cull_models, matting_model):
    # 3g promised zero record mutation; 5.5e deliberately relaxes that to ONE
    # field — the on-demand path grows the cache, so it caches the new cache
    # footprint into the record (the library reads it instead of walking the
    # tree). Nothing ELSE mutates, and the hit path (second call) writes only
    # cache bookkeeping, never the record.
    cull_factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service = cache_service(creator, settings, audit, fake_engine, cull_factory)
    record = _catalog_ready(creator)
    before = json.loads(
        creator.store.record_path(record.id).read_text(encoding="utf-8"))
    service.generate_on_demand(record.id, dict(STATE))
    service.generate_on_demand(record.id, dict(STATE))  # and the hit path
    after = json.loads(
        creator.store.record_path(record.id).read_text(encoding="utf-8"))
    assert after["identity"]["footprint"]["cache_bytes"] > 0
    # everything but the footprint is byte-identical (no touch(), no drift)
    before["identity"]["footprint"] = after["identity"]["footprint"] = None
    assert before == after


# -- 5.5d/5.5e: avatar candidates, frame thumbnails, footprint caching ---------


def test_generate_base_candidates_renders_n_and_sets_nothing(service, creator):
    record = saved_record(creator)
    res = service.generate_base_candidates(record.id, 3)
    assert res["ok"] is True
    assert res["count"] == 3 and res["requested"] == 3
    assert len(res["candidates"]) == 3
    # every candidate is a real reference-dir frame with its own seed
    seeds = {c["seed"] for c in res["candidates"]}
    for c in res["candidates"]:
        frame = Path(c["path"])
        assert frame.is_file()
        assert frame.parent == creator.store.char_dir(record.id) / "reference"
    assert len(seeds) >= 1  # random seeds resolved per frame
    # OFFERED, not mandatory: the wizard step sets NO reference — the user picks
    reloaded = creator.store.load(record.id)
    assert reloaded.identity.reference_image_path is None
    # §3 slot freed after the batch
    assert service.engine_status()["loaded"] is False


@pytest.mark.parametrize("count,expected", [
    (None, 4), (0, 1), (-3, 1), (99, 8), ("nope", 4), (float("inf"), 4), (2, 2),
])
def test_generate_base_candidates_count_is_clamped(service, creator, count,
                                                   expected):
    record = saved_record(creator)
    res = service.generate_base_candidates(record.id, count)
    assert res["ok"] is True and res["count"] == expected


def test_generate_base_candidates_blocked_record_refuses(service, creator):
    record = saved_record(
        creator,
        free_text={"appearance_notes": "Always around the kids at the temple."})
    res = service.generate_base_candidates(record.id, 2)
    assert res["ok"] is False and res["kind"] == "blocked"
    assert not (creator.store.char_dir(record.id) / "reference").exists()


def test_generate_base_candidates_unknown_character(service):
    assert service.generate_base_candidates("ghost", 2)["kind"] == "not_found"
    assert service.generate_base_candidates("", 2)["kind"] == "invalid"


def _write_real_png(path, size=(64, 96), color=(30, 120, 200)):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")


def test_frame_thumbnail_data_uri_for_owned_frame(service, creator):
    record = saved_record(creator)
    frame = creator.store.char_dir(record.id) / "reference" / "base-1.png"
    _write_real_png(frame, size=(800, 1200))
    # absolute path (what a generate_* result hands the UI)
    res = service.frame_thumbnail(record.id, str(frame))
    assert res["ok"] is True
    assert res["thumbnail"].startswith("data:image/jpeg;base64,")
    import base64
    import io as io_mod

    from PIL import Image
    raw = base64.b64decode(res["thumbnail"].split(",", 1)[1])
    with Image.open(io_mod.BytesIO(raw)) as im:
        assert max(im.size) <= 384  # default bound
    # char-relative path resolves the same frame
    assert service.frame_thumbnail(
        record.id, "reference/base-1.png")["thumbnail"] is not None


def test_frame_thumbnail_max_px_clamped_and_never_raises(service, creator):
    record = saved_record(creator)
    frame = creator.store.char_dir(record.id) / "reference" / "b.png"
    _write_real_png(frame, size=(500, 500))
    import base64
    import io as io_mod

    from PIL import Image
    # explicit small bound honored
    res = service.frame_thumbnail(record.id, str(frame), 128)
    raw = base64.b64decode(res["thumbnail"].split(",", 1)[1])
    with Image.open(io_mod.BytesIO(raw)) as im:
        assert max(im.size) <= 128
    # a hand-edited non-finite/garbage size degrades to the default, never raises
    for bad in (float("inf"), float("nan"), "big", None, 0, -5):
        assert service.frame_thumbnail(record.id, str(frame), bad)["ok"] is True


def test_frame_thumbnail_missing_escaped_or_corrupt_is_none(service, creator,
                                                            tmp_path):
    record = saved_record(creator)
    cdir = creator.store.char_dir(record.id)
    # missing
    assert service.frame_thumbnail(record.id, "reference/nope.png")[
        "thumbnail"] is None
    # escaped (absolute + traversal both refuse to a None thumbnail)
    outside = tmp_path / "outside.png"
    _write_real_png(outside)
    assert service.frame_thumbnail(record.id, str(outside))["thumbnail"] is None
    assert service.frame_thumbnail(record.id, "../../outside.png")[
        "thumbnail"] is None
    # corrupt (a non-image byte blob under the char dir)
    bad = cdir / "reference" / "bad.png"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not an image")
    assert service.frame_thumbnail(record.id, str(bad))["thumbnail"] is None
    # and NONE of these ever raised or leaked an error dict
    assert service.frame_thumbnail(record.id, "reference/nope.png")["ok"] is True


def test_frame_thumbnail_unknown_character(service):
    assert service.frame_thumbnail("ghost", "reference/x.png")[
        "kind"] == "not_found"


def test_refresh_footprint_reloads_fresh_and_never_clobbers_a_concurrent_edit(
        creator, service):
    # A long job holds a record loaded at start; refresh_footprint must re-read
    # the record so it overwrites ONLY the footprint, never a concurrent edit.
    record = saved_record(creator)
    cid = record.id
    # simulate a concurrent creator edit landing AFTER the job's stale copy
    edited = creator.store.load(cid)
    edited.name = "Renamed Mid-Job"
    creator.store.save(edited)
    # grow an artifact dir, then refresh (as an artifact op would)
    cdir = creator.store.char_dir(cid)
    (cdir / "cache").mkdir(parents=True, exist_ok=True)
    (cdir / "cache" / "c.png").write_bytes(b"\0" * 512)
    service.refresh_footprint(cid)
    after = creator.store.load(cid)
    assert after.name == "Renamed Mid-Job"          # edit preserved
    assert after.identity.footprint.cache_bytes == 512  # footprint updated


def test_refresh_footprint_on_a_blocked_record_is_a_quiet_noop(service, creator):
    record = saved_record(creator)
    # hand-edit the stored record into a policy block
    path = creator.store.record_path(record.id)
    blob = json.loads(path.read_text(encoding="utf-8"))
    blob["free_text"] = {"appearance_notes": "loli content"}
    path.write_text(json.dumps(blob), encoding="utf-8")
    service.refresh_footprint(record.id)  # must not raise


def test_catalog_state_space_offers_ids_only(service, creator):
    record = saved_record(creator)
    res = service.catalog_state_space(record.id)
    assert res["ok"] is True
    for key in ("expressions", "poses", "outfits"):
        assert isinstance(res[key], list) and res[key]  # bundled states exist
        for item in res[key]:
            assert set(item) == {"id", "label"}
            assert isinstance(item["id"], str) and isinstance(item["label"], str)
    assert service.catalog_state_space("ghost")["kind"] == "not_found"


def test_generate_catalog_caches_footprint(creator, settings, audit, fake_engine,
                                           cull_models):
    _small_matrix(settings)
    factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service = bootstrap_service(creator, settings, audit, fake_engine, factory)
    record = _catalog_ready(creator)
    assert service.generate_catalog(record.id)["ok"]
    reloaded = creator.store.load(record.id)
    assert reloaded.identity.footprint.catalog_bytes > 0
    # and clearing it zeroes the cached catalog bytes
    service.clear_catalog(record.id)
    assert creator.store.load(record.id).identity.footprint.catalog_bytes == 0


# -- Stage 3g review-pass regressions ------------------------------------------


def test_non_dict_manifest_entries_never_raise_through_the_bridge(
        creator, settings, audit, fake_engine, cull_models):
    # Review HIGH: CatalogEntry.from_dict's last_used read ran .get before
    # the ["frame_id"] subscript, so a non-dict entry (a natural hand-edit:
    # "entries": [null]) raised AttributeError — in NO loader guard tuple —
    # straight through every manifest bridge (3e catalog_status, 3f
    # matte_status, and all of 3g). Now: structured *_corrupt, every channel.
    service = cache_service(creator, settings, audit, fake_engine,
                            FakeToolkitFactory())
    record = _catalog_ready(creator)
    for entries in ([None], ["x"], [[1]], [5, "x"], "xx", {"oops": 1}):
        blob = json.dumps({"schema_version": 1, "character_id": record.id,
                           "stale": False, "matting": None,
                           "updated_at": "2026-01-01T00:00:00+00:00",
                           "entries": entries})
        creator.store.cache_path(record.id).write_text(blob, encoding="utf-8")
        assert service.cache_status(record.id)["kind"] == "cache_corrupt", entries
        assert service.generate_on_demand(
            record.id, dict(STATE))["kind"] == "cache_corrupt", entries
        creator.store.cache_path(record.id).unlink()
        creator.store.catalog_path(record.id).write_text(blob, encoding="utf-8")
        assert service.catalog_status(record.id)["kind"] == "catalog_corrupt", entries
        assert service.matte_status(record.id)["kind"] == "catalog_corrupt", entries
        assert service.generate_on_demand(
            record.id, dict(STATE))["kind"] == "catalog_corrupt", entries
        creator.store.catalog_path(record.id).unlink()


def test_deeply_nested_manifest_is_corrupt_not_a_recursion_crash(
        creator, settings, audit, fake_engine, cull_models):
    # Red-team LOW: json.loads raises RecursionError on pathological nesting,
    # which escaped every loader guard. Now in ARTIFACT_LOAD_ERRORS.
    service = cache_service(creator, settings, audit, fake_engine,
                            FakeToolkitFactory())
    record = _catalog_ready(creator)
    creator.store.cache_path(record.id).write_text("[" * 6000 + "]" * 6000,
                                                   encoding="utf-8")
    assert service.cache_status(record.id)["kind"] == "cache_corrupt"
    creator.store.cache_path(record.id).unlink()


def test_hand_edited_nonfinite_state_stays_json_safe(
        creator, settings, audit, fake_engine, cull_models, matting_model):
    # Red-team MEDIUM: an Infinity/NaN value in a hand-edited entry `state`
    # rode verbatim into the cache_status / serve-hit payloads — json.dumps
    # emits bare Infinity (invalid JSON) and the JS promise hangs. from_dict
    # now str-normalizes state, so every payload survives allow_nan=False.
    cull_factory = FakeToolkitFactory(outcomes=[{}] * 4)
    service = cache_service(creator, settings, audit, fake_engine, cull_factory)
    record = _catalog_ready(creator)
    assert service.generate_on_demand(record.id, dict(STATE))["ok"] is True
    blob = json.loads(creator.store.cache_path(record.id).read_text(
        encoding="utf-8"))
    blob["entries"][0]["state"]["poison"] = float("inf")
    text = json.dumps(blob, allow_nan=True)  # what a hand-edit can produce
    assert "Infinity" in text
    creator.store.cache_path(record.id).write_text(text, encoding="utf-8")

    st = service.cache_status(record.id)
    assert st["ok"] is True
    json.dumps(st, allow_nan=False)  # raises on any non-finite -> must not
    hit = service.generate_on_demand(record.id, dict(STATE))
    assert hit["ok"] is True and hit["cached"] is True
    json.dumps(hit, allow_nan=False)
    assert hit["state"]["poison"] == "inf"  # str-normalized at load


def test_record_with_non_dict_identity_reports_io(creator, settings, audit,
                                                  fake_engine, cull_models):
    # The same AttributeError class on the RECORD channel: a hand-edited
    # `"identity": "x"` made IdentityAnchor.from_dict call .get on a string.
    service = cache_service(creator, settings, audit, fake_engine,
                            FakeToolkitFactory())
    record = saved_record(creator)
    path = creator.store.record_path(record.id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["identity"] = "x"
    path.write_text(json.dumps(data), encoding="utf-8")
    res = service.generate_on_demand(record.id, dict(STATE))
    assert res["ok"] is False and res["kind"] == "io"


def test_catalog_entry_from_dict_rejects_non_dict_with_guarded_type():
    for bad in (None, "x", 5, [1]):
        with pytest.raises(ValueError):
            CatalogEntry.from_dict(bad)
