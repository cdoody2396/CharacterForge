"""Stage 3f — matting / keyable output. Pure logic (variants, gate, config
coercion, preflight) + the service orchestration with an injected fake
Matter, mirroring the 3c/3e fake-factory idiom. No numpy/PIL/onnxruntime —
importing app.imagegen.matte in this venv IS the lazy-import proof."""

import json
import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.imagegen import ImageService
from app.imagegen.cull import ContentVerdict
from app.imagegen.matte import (
    MASK_EPSILON,
    EscalationConfig,
    MatteConfig,
    MatteReading,
    MatteToolkit,
    MatteUnavailable,
    VARIANTS,
    VariantSpec,
    coerce_escalation_config,
    coerce_matte_config,
    evaluate_matte,
    matting_escalation_model_path,
    matting_model_path,
    preflight_matte,
)
from app.model import CatalogEntry, CatalogManifest, CharacterRecord


def make_record(**kwargs) -> CharacterRecord:
    base = dict(
        name="Matte Subject",
        age=24,
        selections={"race": "elf", "gender_presentation": "feminine"},
        tags={"outfit": ["casual"]},
    )
    base.update(kwargs)
    return CharacterRecord.create(**base)


def audit_events(audit):
    path = audit.path_for_today()
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]


def seeded_catalog(creator, n=3, **kwargs):
    """A saved record with an n-frame seed catalog on disk (frames + sidecars
    + manifest), as 3e would leave it."""
    record = make_record(**kwargs)
    creator.store.save(record)
    frames_dir = creator.store.catalog_frames_dir(record.id)
    frames_dir.mkdir(parents=True)
    entries = []
    for i in range(1, n + 1):
        name = f"frame-20260712-{i:02d}.png"
        (frames_dir / name).write_bytes(b"PNG" + bytes([i]))
        (frames_dir / f"frame-20260712-{i:02d}.json").write_text(
            "{}", encoding="utf-8")
        entries.append(CatalogEntry(
            frame_id=f"frame-20260712-{i:02d}", path=f"catalog/{name}",
            state={"expression": "neutral", "pose": f"p{i}", "outfit": "casual"},
            on_demand=False, bytes=4))
    creator.store.save_catalog(
        CatalogManifest(character_id=record.id, entries=entries, stale=False,
                        # fixed PAST stamp: updated_at has second granularity,
                        # so a same-second matte run would look like "no change"
                        updated_at="2025-12-31T00:00:00+00:00"))
    return record


class FakeMatteFactory:
    """Drives the Matter + classifier from a per-frame `outcomes` list
    (indexed in first-seen source-frame order, like FakeToolkitFactory)."""

    def __init__(self, outcomes=None, *, raise_kind=None, raise_exc=None,
                 block_all=False):
        self.outcomes = list(outcomes or [])
        self.raise_kind = raise_kind
        self.raise_exc = raise_exc
        self.block_all = block_all
        self._order: dict = {}
        self.built = 0            # primary toolkits built (config.model_path None)
        self.esc_built = 0        # escalation toolkits built (config.model_path set)
        self.matte_calls = 0      # primary matte() calls
        self.esc_matte_calls = 0  # escalation matte() calls
        self.classify_calls = 0
        self.configs: list = []
        self.side_effect = None  # callable(src, out) run inside matte()

    def _outcome(self, path):
        key = Path(path).name  # classify + matte hit the same source frame
        if key not in self._order:
            self._order[key] = len(self._order)
        idx = self._order[key]
        return self.outcomes[idx] if idx < len(self.outcomes) else {}

    def __call__(self, settings, config):
        if self.raise_kind:
            raise MatteUnavailable(self.raise_kind)
        if self.raise_exc is not None:
            raise self.raise_exc
        # The escalation config is the ONLY one that carries model_path (5.5g).
        is_esc = getattr(config, "model_path", None) is not None
        if is_esc:
            self.esc_built += 1
        else:
            self.built += 1
        self.configs.append(config)
        factory = self

        class _M:
            def matte(self, src, out):
                if is_esc:
                    factory.esc_matte_calls += 1
                else:
                    factory.matte_calls += 1
                if factory.side_effect is not None:
                    factory.side_effect(src, out)
                o = factory._outcome(src)
                if o.get("matte_raise"):
                    raise RuntimeError("matte boom")
                if o.get("matte_raise_after_write"):
                    # a real backend can fail AFTER writing tmp bytes — the
                    # service must delete the written tmp on the raise path
                    Path(out).write_bytes(b"PARTIAL")
                    raise RuntimeError("matte boom late")
                if is_esc:
                    # the escalation re-matte keys the bust better: its own
                    # coverage + distinguishable bytes so promotion is assertable
                    Path(out).write_bytes(b"RGBA-ESC")
                    cov = o.get("esc_coverage", o.get("coverage", 0.5))
                    return MatteReading(coverage=cov,
                                        mean_alpha=o.get("mean_alpha", 0.4))
                Path(out).write_bytes(b"RGBA")
                return MatteReading(coverage=o.get("coverage", 0.5),
                                    mean_alpha=o.get("mean_alpha", 0.4))

        class _C:
            def classify(self, p):
                factory.classify_calls += 1
                if factory.block_all:
                    return ContentVerdict(blocked=True, category="minors",
                                          matched="loli")
                o = factory._outcome(p)
                if o.get("classify_raise"):
                    raise RuntimeError("classify boom")
                if o.get("blocked"):
                    return ContentVerdict(
                        blocked=True, category=o.get("category", "minors"),
                        matched=o.get("matched", "loli"))
                return ContentVerdict(blocked=False)

        return MatteToolkit(matter=_M(), classifier=_C(), closer=None)


@pytest.fixture()
def matte_models(tmp_path, settings) -> dict:
    """Fake local model files so preflight_matte passes (the real ONNX is
    faked). Deliberately NO face models — matting must not need buffalo_l."""
    model = tmp_path / "models" / "isnet-anime.onnx"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"\0" * 8)
    cc = tmp_path / "models" / "classifier"
    cc.mkdir(parents=True, exist_ok=True)
    settings.set("models.image.matting_model_path", str(model))
    settings.set("models.image.content_classifier_dir", str(cc))
    return {"model": model, "cc": cc}


def matte_service(creator, settings, audit, factory) -> ImageService:
    return ImageService(
        creator.store, settings, audit,
        catalog_provider=lambda: creator.catalog,
        matte_factory=factory,
    )


def place_escalation(settings, tmp_path, *, present=True):
    """Configure the 5.5g escalation model. present=True places a fake
    birefnet .onnx; present=False sets a path to a NONEXISTENT file (the
    misconfigured-but-on case that degrades to disabled at build time)."""
    model = tmp_path / "models" / "birefnet.onnx"
    if present:
        model.parent.mkdir(parents=True, exist_ok=True)
        model.write_bytes(b"\0" * 8)
    settings.set("models.image.matting_escalation_model_path", str(model))
    return model


# -- pure logic ----------------------------------------------------------------


def test_matte_module_imports_clean():
    # The lazy-import guarantee must hold whether or not the heavy deps are
    # installed (on the target machine they ARE). Probe in a FRESH
    # subprocess: importing the whole imagegen package must pull none of
    # them. (The in-process sys.modules is polluted by other test modules'
    # imports at collection time, so it is not an honest witness.)
    import subprocess

    code = (
        "import sys; import app.imagegen; "
        "leaked = [m for m in ('numpy', 'PIL', 'onnxruntime', 'torch', 'cv2')"
        " if m in sys.modules]; "
        "sys.exit(1 if leaked else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"heavy module leaked: {proc.stdout}{proc.stderr}"
    cfg = MatteConfig()
    assert evaluate_matte(MatteReading(coverage=0.5, mean_alpha=0.4), cfg) is None


def test_variant_constants_pinned():
    # The verified rembg constants (sessions @ 2.0.76) — guards silent drift.
    assert VARIANTS == {
        "isnet_anime": VariantSpec(1024, (0.485, 0.456, 0.406),
                                   (1.0, 1.0, 1.0), False),
        "isnet_general": VariantSpec(1024, (0.5, 0.5, 0.5),
                                     (1.0, 1.0, 1.0), False),
        "birefnet": VariantSpec(1024, (0.485, 0.456, 0.406),
                                (0.229, 0.224, 0.225), True),
    }
    assert MASK_EPSILON > 0  # the deviation from rembg's unguarded stretch


def test_evaluate_matte_gate():
    cfg = MatteConfig(coverage_min=0.1, coverage_max=0.9)

    def cov(c):
        return evaluate_matte(MatteReading(coverage=c, mean_alpha=0.0), cfg)

    assert cov(0.5) is None
    assert cov(0.1) is None and cov(0.9) is None  # inclusive band edges
    assert cov(0.05) == "matte_empty"
    assert cov(0.95) == "matte_full"
    assert cov(float("nan")) == "matte_failed"
    assert cov(float("inf")) == "matte_failed"


def test_coerce_matte_config_defaults(tmp_path):
    cfg = coerce_matte_config(Settings(tmp_path / "s.json"))
    assert cfg == MatteConfig()
    assert cfg.variant == "isnet_anime"


def test_coerce_matte_config_degrades_bad_hand_edits(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"image_gen": {"matting": {
        "variant": "lol", "erode_px": 99, "feather_px": -3,
        "coverage_min": "NaN", "coverage_max": 1.5,
    }}}), encoding="utf-8")
    cfg = coerce_matte_config(Settings(path))
    assert cfg.variant == "isnet_anime"   # unknown -> default
    assert cfg.erode_px == 8              # clamped to hi
    assert cfg.feather_px == 0            # clamped to lo
    assert cfg.coverage_min == 0.02       # NaN -> default
    assert cfg.coverage_max == 1.0        # 1.5 -> clamped (band stays valid)

    # a min>max nonsense band resets BOTH to defaults:
    path.write_text(json.dumps({"image_gen": {"matting": {
        "coverage_min": 0.5, "coverage_max": 0.2,
    }}}), encoding="utf-8")
    cfg = coerce_matte_config(Settings(path))
    assert (cfg.coverage_min, cfg.coverage_max) == (0.02, 0.98)  # both reset

    # unhashable variant must not raise (isinstance guard before `in`)
    path.write_text(json.dumps({"image_gen": {"matting": {
        "variant": ["isnet_anime"],
    }}}), encoding="utf-8")
    assert coerce_matte_config(Settings(path)).variant == "isnet_anime"

    # Infinity survives json.loads as a float -> non-finite -> default
    path.write_text('{"image_gen": {"matting": {"erode_px": Infinity}}}',
                    encoding="utf-8")
    assert coerce_matte_config(Settings(path)).erode_px == 0


def test_coerce_matte_config_clamps_valid_edges(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"image_gen": {"matting": {
        "coverage_min": -0.5, "coverage_max": 1.5, "erode_px": 2.7,
    }}}), encoding="utf-8")
    cfg = coerce_matte_config(Settings(path))
    assert (cfg.coverage_min, cfg.coverage_max) == (0.0, 1.0)
    assert cfg.erode_px == 2  # float -> int


def test_coerce_escalation_config_unset_and_degrades(tmp_path):
    primary = MatteConfig(erode_px=3, feather_px=2,
                          coverage_min=0.05, coverage_max=0.9)
    path = tmp_path / "s.json"

    # UNSET escalation model path -> None (the byte-for-byte no-op guarantee).
    path.write_text("{}", encoding="utf-8")
    assert coerce_escalation_config(Settings(path), primary) is None

    # A placed model path -> a config that inherits the primary's band/knobs.
    model = tmp_path / "birefnet.onnx"
    model.write_bytes(b"\0")
    path.write_text(json.dumps({
        "models": {"image": {"matting_escalation_model_path": str(model)}},
    }), encoding="utf-8")
    ec = coerce_escalation_config(Settings(path), primary)
    assert isinstance(ec, EscalationConfig)
    assert ec.coverage == 0.85  # default threshold
    assert ec.config.variant == "birefnet"
    assert ec.config.model_path == str(model.resolve())
    # inherits the primary's gate + halo knobs (judged by the SAME band)
    assert (ec.config.erode_px, ec.config.feather_px) == (3, 2)
    assert (ec.config.coverage_min, ec.config.coverage_max) == (0.05, 0.9)

    # junk hand-edits degrade to defaults, never raise.
    path.write_text(json.dumps({
        "models": {"image": {"matting_escalation_model_path": str(model)}},
        "image_gen": {"matting": {
            "escalation_variant": "lol", "escalation_coverage": "NaN"}},
    }), encoding="utf-8")
    ec = coerce_escalation_config(Settings(path), primary)
    assert ec.config.variant == "birefnet" and ec.coverage == 0.85

    # a >1 coverage clamps; Infinity -> default.
    path.write_text(json.dumps({
        "models": {"image": {"matting_escalation_model_path": str(model)}},
        "image_gen": {"matting": {"escalation_coverage": 1.5}},
    }), encoding="utf-8")
    assert coerce_escalation_config(Settings(path), primary).coverage == 1.0
    path.write_text(
        '{"models": {"image": {"matting_escalation_model_path": "%s"}},'
        ' "image_gen": {"matting": {"escalation_coverage": Infinity}}}'
        % str(model).replace("\\", "\\\\"), encoding="utf-8")
    assert coerce_escalation_config(Settings(path), primary).coverage == 0.85

    # SET but MISSING file still yields a config (unset != misconfigured);
    # it degrades to disabled at build time in the service.
    path.write_text(json.dumps({
        "models": {"image": {
            "matting_escalation_model_path": str(tmp_path / "gone.onnx")}},
    }), encoding="utf-8")
    assert coerce_escalation_config(Settings(path), primary) is not None


def test_preflight_matte_kinds(tmp_path, settings, matte_models):
    assert preflight_matte(settings) is None  # both placed; NO face models set
    settings.set("models.image.content_classifier_dir", None)
    assert preflight_matte(settings) == "classifier_unavailable"
    settings.set("models.image.content_classifier_dir", str(matte_models["cc"]))
    settings.set("models.image.matting_model_path",
                 str(tmp_path / "models" / "gone.onnx"))
    assert preflight_matte(settings) == "matting_model_missing"
    settings.set("models.image.matting_model_path", None)
    assert preflight_matte(settings) == "matting_model_missing"
    # a relative path resolves under the app root (like every model path)
    from app.imagegen.engine import APP_ROOT
    settings.set("models.image.matting_model_path", "models/isnet-anime.onnx")
    assert matting_model_path(settings) == APP_ROOT / "models" / "isnet-anime.onnx"


# -- service orchestration -------------------------------------------------------


def test_matte_catalog_fills_matted_paths(creator, settings, audit, matte_models):
    factory = FakeMatteFactory()
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=3)
    before = creator.store.load_catalog(record.id).updated_at

    res = service.matte_catalog(record.id)
    assert res["ok"] is True
    assert res["frames"] == 3 and res["matted"] == 3
    assert res["skipped"] == 0 and res["blocked"] == 0 and res["failed"] == 0
    assert all(r["status"] == "matted" for r in res["results"])
    assert all(r["matted_path"].startswith("catalog/matted/")
               for r in res["results"])

    manifest = creator.store.load_catalog(record.id)
    assert all(e.matted_path and e.matted_path.startswith("catalog/matted/")
               for e in manifest.entries)
    matted_dir = creator.store.matted_dir(record.id)
    assert len(list(matted_dir.glob("frame-*.png"))) == 3
    # provenance: basename only, never a path; complete since all matted
    assert manifest.matting["variant"] == "isnet_anime"
    assert manifest.matting["model"] == "isnet-anime.onnx"
    assert manifest.matting["matted"] == 3
    assert manifest.matting["complete"] is True
    assert manifest.updated_at != before
    blob = json.dumps(manifest.to_dict())
    assert str(creator.store.root) not in blob
    assert any(e["kind"] == "catalog_matted" and e["matted"] == 3
               for e in audit_events(audit))


def test_matte_catalog_skip_and_force(creator, settings, audit, matte_models):
    factory = FakeMatteFactory()
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=3)
    assert service.matte_catalog(record.id)["ok"] is True
    assert factory.matte_calls == 3
    saved = creator.store.catalog_path(record.id).read_text(encoding="utf-8")

    res = service.matte_catalog(record.id)  # second run: all skipped
    assert res["ok"] is True and res["skipped"] == 3 and res["matted"] == 0
    assert factory.matte_calls == 3                     # no re-matte
    assert factory.classify_calls == 6                  # but ALWAYS re-screened
    assert creator.store.catalog_path(record.id).read_text(
        encoding="utf-8") == saved                      # true no-op: not re-saved

    res = service.matte_catalog(record.id, force=True)  # force redoes all
    assert res["ok"] is True and res["matted"] == 3 and res["skipped"] == 0
    assert factory.matte_calls == 6


def test_matte_catalog_dangling_matted_path_rematted(creator, settings, audit,
                                                     matte_models):
    factory = FakeMatteFactory()
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=2)
    assert service.matte_catalog(record.id)["ok"] is True

    # dangling: the matte file vanished -> re-matted without force
    manifest = creator.store.load_catalog(record.id)
    gone = creator.store.char_dir(record.id) / manifest.entries[0].matted_path
    gone.unlink()
    res = service.matte_catalog(record.id)
    assert res["ok"] is True and res["matted"] == 1 and res["skipped"] == 1
    assert gone.is_file()  # restored at the canonical name

    # non-canonical: matted_path hand-edited to a real file OUTSIDE matted/
    # -> not trusted, re-matted, canonical value written back
    (creator.store.char_dir(record.id) / "reference").mkdir()
    decoy = creator.store.char_dir(record.id) / "reference" / "base.png"
    decoy.write_bytes(b"DECOY")
    manifest = creator.store.load_catalog(record.id)
    manifest.entries[0].matted_path = "reference/base.png"
    creator.store.save_catalog(manifest)
    res = service.matte_catalog(record.id)
    assert res["ok"] is True and res["matted"] == 1
    after = creator.store.load_catalog(record.id)
    assert after.entries[0].matted_path.startswith("catalog/matted/")
    assert decoy.read_bytes() == b"DECOY"  # the decoy is never touched


def test_matte_catalog_blocked_frame_purged(creator, settings, audit, matte_models):
    factory = FakeMatteFactory(outcomes=[{"blocked": True}])
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=3)
    frames_dir = creator.store.catalog_frames_dir(record.id)
    first = sorted(frames_dir.glob("frame-*.png"))[0]

    res = service.matte_catalog(record.id)
    assert res["ok"] is True
    assert res["blocked"] == 1 and res["matted"] == 2
    assert not first.exists()                                # pixels purged
    assert not first.with_suffix(".json").exists()           # sidecar purged
    manifest = creator.store.load_catalog(record.id)
    assert len(manifest.entries) == 2                        # entry removed
    events = audit_events(audit)
    assert any(e["kind"] == "filter_block" and e["layer"] == 2
               and e["context"] == "image.matte.frame" for e in events)


def test_matte_catalog_reclassifies_already_matted(creator, settings, audit,
                                                   matte_models):
    # Gate BEFORE skip: a previously-matted (skip-eligible) frame that NOW
    # trips the classifier is purged — matte + entry included — not skipped.
    factory = FakeMatteFactory()
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=2)
    assert service.matte_catalog(record.id)["ok"] is True
    manifest = creator.store.load_catalog(record.id)
    matte_file = (creator.store.char_dir(record.id)
                  / manifest.entries[0].matted_path)
    assert matte_file.is_file()

    factory.block_all = False
    factory.outcomes = [{"blocked": True}]
    res = service.matte_catalog(record.id)  # no force needed
    assert res["ok"] is True and res["blocked"] == 1 and res["skipped"] == 1
    assert not matte_file.exists()          # the prior matte is purged too
    assert len(creator.store.load_catalog(record.id).entries) == 1


def test_matte_catalog_classifier_exception_is_block(creator, settings, audit,
                                                     matte_models):
    factory = FakeMatteFactory(outcomes=[{"classify_raise": True}])
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=2)
    res = service.matte_catalog(record.id)
    assert res["ok"] is True and res["blocked"] == 1 and res["matted"] == 1
    assert any(e["kind"] == "filter_block"
               and e["category"] == "classifier_error"
               for e in audit_events(audit))


def test_matte_catalog_escaped_paths_untouched(creator, settings, audit,
                                               tmp_path, matte_models):
    factory = FakeMatteFactory()
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=1)
    char_dir = creator.store.char_dir(record.id)
    (char_dir / "reference").mkdir()
    (char_dir / "reference" / "base.png").write_bytes(b"REF")
    (creator.store.matted_dir(record.id)).mkdir(parents=True)
    (creator.store.matted_dir(record.id) / "frame-x.png").write_bytes(b"OLD")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"OUTSIDE")

    manifest = creator.store.load_catalog(record.id)
    evil = ["../../outside.png", str(outside), "reference/base.png",
            "catalog/matted/frame-x.png"]
    manifest.entries = [CatalogEntry(frame_id=f"f{i}", path=p)
                        for i, p in enumerate(evil)]
    creator.store.save_catalog(manifest)

    res = service.matte_catalog(record.id)
    # invalid_path rows are not matte_failed -> the run itself stays ok:True
    assert res["ok"] is True and res["matted"] == 0 and res["failed"] == 4
    # every row refused as invalid_path; no matte ran; nothing deleted/written
    rows = {r["frame_id"]: r["status"] for r in res["results"]}
    assert rows == {"f0": "invalid_path", "f1": "invalid_path",
                    "f2": "invalid_path", "f3": "invalid_path"}
    assert factory.matte_calls == 0
    assert outside.read_bytes() == b"OUTSIDE"
    assert (char_dir / "reference" / "base.png").read_bytes() == b"REF"
    assert (creator.store.matted_dir(record.id) / "frame-x.png").read_bytes() == b"OLD"


def test_matte_catalog_missing_source_reported(creator, settings, audit,
                                               matte_models):
    factory = FakeMatteFactory()
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=3)
    frames = sorted(creator.store.catalog_frames_dir(record.id).glob("frame-*.png"))
    frames[0].unlink()  # benign deletion, not a tamper signal

    res = service.matte_catalog(record.id)
    assert res["ok"] is True and res["matted"] == 2
    assert sum(1 for r in res["results"] if r["status"] == "missing") == 1
    # entry KEPT (do-no-harm; 3e regeneration is the fix)
    assert len(creator.store.load_catalog(record.id).entries) == 3


def test_matte_catalog_degenerate_tmp_cleaned(creator, settings, audit,
                                              matte_models):
    factory = FakeMatteFactory(outcomes=[{"coverage": 0.0}, {"coverage": 1.0}])
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=3)
    # pre-seed a crashed-run leftover — swept at run start
    matted_dir = creator.store.matted_dir(record.id)
    matted_dir.mkdir(parents=True)
    stale = matted_dir / "frame-stale.png.tmp"
    stale.write_bytes(b"STALE")

    res = service.matte_catalog(record.id)
    assert res["ok"] is True and res["matted"] == 1 and res["failed"] == 2
    statuses = sorted(r["status"] for r in res["results"])
    assert statuses == ["matte_empty", "matte_full", "matted"]
    assert not stale.exists()
    assert not list(matted_dir.glob("*.png.tmp"))          # no temp left
    assert len(list(matted_dir.glob("frame-*.png"))) == 1  # only the good one
    manifest = creator.store.load_catalog(record.id)
    assert sum(1 for e in manifest.entries if e.matted_path) == 1


def test_matte_catalog_force_failure_keeps_prior_matte(creator, settings, audit,
                                                       matte_models):
    factory = FakeMatteFactory()
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=2)
    assert service.matte_catalog(record.id)["ok"] is True
    manifest = creator.store.load_catalog(record.id)
    prior = (creator.store.char_dir(record.id) / manifest.entries[0].matted_path)
    prior_bytes = prior.read_bytes()

    factory.outcomes = [{"matte_raise": True}]  # frame 1 fails the redo
    res = service.matte_catalog(record.id, force=True)
    assert res["ok"] is True and res["matted"] == 1 and res["failed"] == 1
    assert prior.read_bytes() == prior_bytes  # prior good matte survives
    after = creator.store.load_catalog(record.id)
    assert after.entries[0].matted_path  # and stays recorded

    # the DEGENERATE arm of a force redo keeps the prior matte too
    factory.outcomes = [{"coverage": 0.0}]
    res = service.matte_catalog(record.id, force=True)
    assert res["ok"] is True and res["matted"] == 1
    assert any(r["status"] == "matte_empty" for r in res["results"])
    assert prior.read_bytes() == prior_bytes
    assert creator.store.load_catalog(record.id).entries[0].matted_path


# -- 5.5g close-up-bust escalation (3f residual) ------------------------------


def _matted_bytes(creator, record, entry_idx=0):
    manifest = creator.store.load_catalog(record.id)
    entry = manifest.entries[entry_idx]
    return (creator.store.char_dir(record.id) / entry.matted_path).read_bytes()


def test_matte_escalation_promotes_lower_coverage_birefnet(
        creator, settings, audit, matte_models, tmp_path):
    # A bust: primary keys almost nothing out (0.95); BiRefNet keys it well
    # (0.30). The escalated, lower-coverage cutout is promoted.
    place_escalation(settings, tmp_path)
    factory = FakeMatteFactory(outcomes=[{"coverage": 0.95, "esc_coverage": 0.30}])
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=1)

    res = service.matte_catalog(record.id)
    assert res["ok"] is True and res["matted"] == 1
    assert factory.esc_built == 1 and factory.esc_matte_calls == 1
    assert _matted_bytes(creator, record) == b"RGBA-ESC"      # escalated pixels
    assert res["results"][0]["coverage"] == 0.30              # escalated reading
    m = creator.store.load_catalog(record.id).matting
    assert m["escalated"] == 1 and m["escalation_variant"] == "birefnet"
    assert m["escalation_model"] == "birefnet.onnx"           # basename only
    assert not list(creator.store.matted_dir(record.id).glob("*.tmp"))


def test_matte_escalation_rescues_matte_full_frame(
        creator, settings, audit, matte_models, tmp_path):
    # Primary 0.995 would be matte_full (keyed nothing out) today; the BiRefNet
    # re-matte at 0.22 rescues it into a usable, promoted cutout.
    place_escalation(settings, tmp_path)
    factory = FakeMatteFactory(outcomes=[{"coverage": 0.995, "esc_coverage": 0.22}])
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=1)

    res = service.matte_catalog(record.id)
    assert res["ok"] is True and res["matted"] == 1
    assert _matted_bytes(creator, record) == b"RGBA-ESC"


def test_matte_escalation_keeps_primary_when_birefnet_not_better(
        creator, settings, audit, matte_models, tmp_path):
    place_escalation(settings, tmp_path)
    # (a) escalated coverage is NOT strictly lower -> keep primary.
    factory = FakeMatteFactory(outcomes=[{"coverage": 0.90, "esc_coverage": 0.92}])
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=1)
    res = service.matte_catalog(record.id)
    assert res["ok"] is True and res["matted"] == 1
    assert _matted_bytes(creator, record) == b"RGBA"          # primary kept
    assert res["results"][0]["coverage"] == 0.90
    m = creator.store.load_catalog(record.id).matting
    assert m["escalated"] == 0
    assert not list(creator.store.matted_dir(record.id).glob("*.tmp"))

    # (b) escalated result FAILS the gate (empty) -> keep primary.
    factory2 = FakeMatteFactory(outcomes=[{"coverage": 0.90, "esc_coverage": 0.0}])
    service2 = matte_service(creator, settings, audit, factory2)
    record2 = seeded_catalog(creator, n=1)
    res2 = service2.matte_catalog(record2.id)
    assert res2["ok"] is True and res2["matted"] == 1
    assert _matted_bytes(creator, record2) == b"RGBA"
    assert creator.store.load_catalog(record2.id).matting["escalated"] == 0


def test_matte_escalation_below_threshold_skips(
        creator, settings, audit, matte_models, tmp_path):
    # A clean wide frame (0.30 < 0.85) never triggers the second model.
    place_escalation(settings, tmp_path)
    factory = FakeMatteFactory(outcomes=[{"coverage": 0.30, "esc_coverage": 0.10}])
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=1)
    res = service.matte_catalog(record.id)
    assert res["ok"] is True and res["matted"] == 1
    assert factory.esc_built == 0 and factory.esc_matte_calls == 0
    assert _matted_bytes(creator, record) == b"RGBA"


def test_matte_escalation_not_configured_no_second_build(
        creator, settings, audit, matte_models):
    # No escalation model path set: byte-for-byte the pre-5.5g behavior even on
    # a bust-coverage frame — no second toolkit, no new manifest keys.
    factory = FakeMatteFactory(outcomes=[{"coverage": 0.95, "esc_coverage": 0.30}])
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=1)
    res = service.matte_catalog(record.id)
    assert res["ok"] is True and res["matted"] == 1
    assert factory.built == 1 and factory.esc_built == 0
    assert _matted_bytes(creator, record) == b"RGBA"
    m = creator.store.load_catalog(record.id).matting
    assert "escalated" not in m and "escalation_variant" not in m


def test_matte_escalation_missing_model_degrades(
        creator, settings, audit, matte_models, tmp_path):
    # Path SET but file ABSENT: escalation disabled at build time, run still ok,
    # no crash, no leftover tmp, primary cutout shipped.
    place_escalation(settings, tmp_path, present=False)
    factory = FakeMatteFactory(outcomes=[{"coverage": 0.95, "esc_coverage": 0.30}])
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=1)
    res = service.matte_catalog(record.id)
    assert res["ok"] is True and res["matted"] == 1
    assert factory.esc_built == 0                 # .is_file() guard disabled it
    assert _matted_bytes(creator, record) == b"RGBA"   # 0.95 < 0.98 -> matted
    assert not list(creator.store.matted_dir(record.id).glob("*.tmp"))


def test_matte_escalation_lazy_build_once(
        creator, settings, audit, matte_models, tmp_path):
    # Two busts: the escalation toolkit is built ONCE and reused across frames.
    place_escalation(settings, tmp_path)
    factory = FakeMatteFactory(outcomes=[
        {"coverage": 0.95, "esc_coverage": 0.30},
        {"coverage": 0.94, "esc_coverage": 0.28},
    ])
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=2)
    res = service.matte_catalog(record.id)
    assert res["ok"] is True and res["matted"] == 2
    assert factory.esc_built == 1 and factory.esc_matte_calls == 2
    assert creator.store.load_catalog(record.id).matting["escalated"] == 2


def test_matte_catalog_backend_exception_and_all_failed(creator, settings, audit,
                                                        matte_models):
    # one failure among successes: per-frame row, run still ok. The failing
    # backend wrote tmp bytes BEFORE raising — the tmp must not survive.
    factory = FakeMatteFactory(outcomes=[{"matte_raise_after_write": True}])
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=3)
    res = service.matte_catalog(record.id)
    assert res["ok"] is True and res["matted"] == 2 and res["failed"] == 1
    assert any(r["status"] == "matte_failed" and "matte boom" in r["error"]
               for r in res["results"])
    assert not list(creator.store.matted_dir(record.id).glob("*.png.tmp"))

    # ALL failing (the wrong-model-file case) escalates to a systemic error
    # that still carries the run tallies (like every other result shape)
    factory2 = FakeMatteFactory(outcomes=[{"matte_raise": True}] * 2)
    service2 = matte_service(creator, settings, audit, factory2)
    record2 = seeded_catalog(creator, n=2)
    before = creator.store.catalog_path(record2.id).read_text(encoding="utf-8")
    res = service2.matte_catalog(record2.id)
    assert res["ok"] is False and res["kind"] == "matte_failed"
    assert "matte boom" in res["error"]
    assert res["frames"] == 2 and res["failed"] == 2
    assert res["matted"] == 0 and res["skipped"] == 0 and res["blocked"] == 0
    assert creator.store.catalog_path(record2.id).read_text(
        encoding="utf-8") == before  # manifest untouched

    # a blocked+failed mix (destructive: the purge saved the manifest) also
    # escalates — and the blocked tally must survive into the error dict
    factory3 = FakeMatteFactory(outcomes=[{"blocked": True},
                                          {"matte_raise": True}])
    service3 = matte_service(creator, settings, audit, factory3)
    record3 = seeded_catalog(creator, n=2)
    res = service3.matte_catalog(record3.id)
    assert res["ok"] is False and res["kind"] == "matte_failed"
    assert res["blocked"] == 1 and res["failed"] == 1
    assert len(creator.store.load_catalog(record3.id).entries) == 1  # purge saved


def test_matte_catalog_factory_errors_structured(creator, settings, audit,
                                                 matte_models):
    record = seeded_catalog(creator, n=1)
    # a structured MatteUnavailable from the factory keeps its kind
    service = matte_service(creator, settings, audit,
                            FakeMatteFactory(raise_kind="classifier_unavailable"))
    res = service.matte_catalog(record.id)
    assert res["ok"] is False and res["kind"] == "classifier_unavailable"
    # an arbitrary exception (missing import, corrupt model) is wrapped
    service = matte_service(creator, settings, audit,
                            FakeMatteFactory(raise_exc=ImportError("no ort")))
    res = service.matte_catalog(record.id)
    assert res["ok"] is False and res["kind"] == "matte_unavailable"
    assert "no ort" in res["error"]


def test_matte_catalog_preflight_before_factory(creator, settings, audit):
    # NO matte_models fixture: preflight refuses and the factory never runs.
    factory = FakeMatteFactory()
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=1)
    res = service.matte_catalog(record.id)
    assert res["ok"] is False and res["kind"] == "matting_model_missing"
    assert factory.built == 0


def test_matte_catalog_no_catalog_corrupt_and_mismatched_id(creator, settings,
                                                            audit, matte_models):
    factory = FakeMatteFactory()
    service = matte_service(creator, settings, audit, factory)
    record = make_record()
    creator.store.save(record)
    res = service.matte_catalog(record.id)
    assert res["ok"] is False and res["kind"] == "no_catalog"

    # an empty-entry manifest is also "no catalog"
    creator.store.save_catalog(CatalogManifest(character_id=record.id))
    assert service.matte_catalog(record.id)["kind"] == "no_catalog"

    p = creator.store.catalog_path(record.id)
    for corrupt in ("{not json", "{}", json.dumps({"character_id": "../x"})):
        p.write_text(corrupt, encoding="utf-8")
        assert service.matte_catalog(record.id)["kind"] == "catalog_corrupt"
        assert service.matte_status(record.id)["kind"] == "catalog_corrupt"

    # a manifest claiming ANOTHER character is corrupt: save_catalog routes by
    # manifest.character_id, so saving it would clobber that other character
    other = seeded_catalog(creator, n=1)
    p.write_text(json.dumps(
        creator.store.load_catalog(other.id).to_dict()), encoding="utf-8")
    res = service.matte_catalog(record.id)
    assert res["ok"] is False and res["kind"] == "catalog_corrupt"
    assert factory.built == 0


def test_matte_catalog_concurrent_regen_aborts(creator, settings, audit,
                                               matte_models):
    factory = FakeMatteFactory()
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=2)

    def regen(src, out):
        # a concurrent 3e re-generate swaps in a FRESH manifest mid-matte
        if factory.side_effect is not None:  # once
            factory.side_effect = None
            fresh = CatalogManifest(
                character_id=record.id,
                entries=[CatalogEntry(frame_id="new", path="catalog/new.png")],
                stale=False, updated_at="2026-01-01T00:00:00+00:00")
            creator.store.save_catalog(fresh)

    factory.side_effect = regen
    res = service.matte_catalog(record.id)
    assert res["ok"] is False and res["kind"] == "catalog_changed"
    assert res["matted"] == 2  # the abort dict still carries the tallies
    # the fresh manifest was NOT clobbered by the stale in-memory copy
    on_disk = creator.store.load_catalog(record.id)
    assert on_disk.updated_at == "2026-01-01T00:00:00+00:00"
    assert [e.frame_id for e in on_disk.entries] == ["new"]
    # and the aborted run still left a run-level Layer-4 trail
    assert any(e["kind"] == "catalog_matted"
               and e.get("aborted") == "catalog_changed"
               for e in audit_events(audit))


def test_matte_status_counts_and_ready(creator, settings, audit, matte_models):
    factory = FakeMatteFactory()
    service = matte_service(creator, settings, audit, factory)
    record = make_record()
    creator.store.save(record)

    st = service.matte_status(record.id)
    assert st == {"ok": True, "id": record.id, "has_catalog": False,
                  "frames": 0, "matted": 0, "unmatted": 0, "stale": False,
                  "matting": None, "ready": True, "missing": None}

    record2 = seeded_catalog(creator, n=3)
    assert service.matte_catalog(record2.id)["ok"] is True
    st = service.matte_status(record2.id)
    assert st["frames"] == 3 and st["matted"] == 3 and st["unmatted"] == 0
    assert st["matting"]["model"] == "isnet-anime.onnx"

    # a dangling matte counts unmatted; so does a hand-edited escape
    manifest = creator.store.load_catalog(record2.id)
    (creator.store.char_dir(record2.id) / manifest.entries[0].matted_path).unlink()
    manifest.entries[1].matted_path = "../../x.png"
    creator.store.save_catalog(manifest)
    st = service.matte_status(record2.id)
    assert st["matted"] == 1 and st["unmatted"] == 2

    # readiness reflects preflight
    settings.set("models.image.matting_model_path", None)
    st = service.matte_status(record2.id)
    assert st["ready"] is False and st["missing"] == "matting_model_missing"


def test_matte_catalog_inherited_kinds(creator, settings, audit, matte_models):
    service = matte_service(creator, settings, audit, FakeMatteFactory())
    assert service.matte_catalog("nope")["kind"] == "not_found"
    assert service.matte_catalog("")["kind"] == "invalid"
    assert service.matte_status("nope")["kind"] == "not_found"


# -- Stage 3f review-pass regressions ------------------------------------------


def test_hand_edited_infinity_never_tracebacks(creator, settings, audit,
                                               matte_models):
    # Review HIGH 3F-1: json.loads accepts Infinity/1e999 as floats and a
    # from_dict int() on one raises OverflowError — NOT a ValueError — which
    # escaped every loader guard tuple. Both 3f bridges, the 3e status, and
    # the record loader must map it to a structured kind instead.
    factory = FakeMatteFactory()
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=1)

    # catalog.json: entry bytes = Infinity -> catalog_corrupt (3f + 3e)
    manifest = creator.store.load_catalog(record.id).to_dict()
    manifest["entries"][0]["bytes"] = float("inf")  # dumps as `Infinity`
    creator.store.catalog_path(record.id).write_text(
        json.dumps(manifest), encoding="utf-8")
    assert service.matte_status(record.id)["kind"] == "catalog_corrupt"
    assert service.matte_catalog(record.id)["kind"] == "catalog_corrupt"
    assert service.catalog_status(record.id)["kind"] == "catalog_corrupt"
    assert factory.built == 0

    # character.json: footprint bytes = Infinity -> structured io, everywhere
    rec_path = creator.store.record_path(record.id)
    data = json.loads(rec_path.read_text(encoding="utf-8"))
    data["identity"]["footprint"]["lora_bytes"] = float("inf")
    rec_path.write_text(json.dumps(data), encoding="utf-8")
    assert service.matte_status(record.id)["kind"] == "io"


def test_matte_catalog_purges_renamed_matte(creator, settings, audit,
                                            matte_models):
    # Review MEDIUM 3F-2: the skip check trusts ANY matted_path resolving into
    # matted/ — so the blocked-frame purge must delete that same recorded
    # matte, not just the canonical <stem>.png name.
    factory = FakeMatteFactory()
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=1)
    assert service.matte_catalog(record.id)["ok"] is True
    matted_dir = creator.store.matted_dir(record.id)
    canonical = next(matted_dir.glob("frame-*.png"))
    renamed = matted_dir / "kept-by-hand.png"
    canonical.rename(renamed)
    manifest = creator.store.load_catalog(record.id)
    manifest.entries[0].matted_path = "catalog/matted/kept-by-hand.png"
    creator.store.save_catalog(manifest)
    # the skip check honors the renamed matte...
    assert service.matte_catalog(record.id)["skipped"] == 1

    factory.outcomes = [{"blocked": True}]  # ...now the source trips Layer 2
    res = service.matte_catalog(record.id)
    assert res["ok"] is True and res["blocked"] == 1
    assert not renamed.exists()  # the recorded matte is purged with the frame
    assert creator.store.load_catalog(record.id).entries == []


def test_matte_catalog_non_png_source_rejected(creator, settings, audit,
                                               matte_models):
    # Review LOW MATTE-1: matte outputs are keyed by source STEM, so
    # same-stem/other-extension sources (hand-placed shot.png + shot.jpeg)
    # would collide onto one matte file. Non-.png sources are refused.
    factory = FakeMatteFactory()
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=1)
    frames_dir = creator.store.catalog_frames_dir(record.id)
    (frames_dir / "shot.png").write_bytes(b"PNG-A")
    (frames_dir / "shot.jpeg").write_bytes(b"JPEG-B")
    manifest = creator.store.load_catalog(record.id)
    manifest.entries = [
        CatalogEntry(frame_id="a", path="catalog/shot.png"),
        CatalogEntry(frame_id="b", path="catalog/shot.jpeg"),
    ]
    creator.store.save_catalog(manifest)

    res = service.matte_catalog(record.id)
    assert res["ok"] is True and res["matted"] == 1
    rows = {r["frame_id"]: r["status"] for r in res["results"]}
    assert rows == {"a": "matted", "b": "invalid_path"}
    assert (frames_dir / "shot.jpeg").read_bytes() == b"JPEG-B"  # untouched
    # only ONE matte exists and it belongs to the .png entry
    after = creator.store.load_catalog(record.id)
    assert after.entries[0].matted_path == "catalog/matted/shot.png"
    assert after.entries[1].matted_path is None


def test_matte_catalog_tmp_stem_source_survives(creator, settings, audit,
                                                matte_models):
    # Review LOW 3F-3: with the old `*.tmp.png` temp suffix, a hand-placed
    # source named custom.tmp.png promoted to a final the NEXT run's sweep
    # deleted. The `*.png.tmp` temp namespace cannot collide with any final.
    factory = FakeMatteFactory()
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=1)
    frames_dir = creator.store.catalog_frames_dir(record.id)
    (frames_dir / "custom.tmp.png").write_bytes(b"PNG")
    manifest = creator.store.load_catalog(record.id)
    manifest.entries = [CatalogEntry(frame_id="c", path="catalog/custom.tmp.png")]
    creator.store.save_catalog(manifest)

    assert service.matte_catalog(record.id)["matted"] == 1
    final = creator.store.matted_dir(record.id) / "custom.tmp.png"
    assert final.is_file()
    res = service.matte_catalog(record.id)  # rerun: swept? no — skipped
    assert res["skipped"] == 1
    assert final.is_file()


def test_matte_nan_coverage_payload_is_finite(creator, settings, audit,
                                              matte_models):
    # Review LOW dod-3F-2: a non-finite coverage reading is matte_failed via
    # the gate, but the raw NaN must not ship in the bridge payload —
    # json.dumps would emit an invalid `NaN` token and hang the JS promise.
    factory = FakeMatteFactory(outcomes=[{"coverage": float("nan")}])
    service = matte_service(creator, settings, audit, factory)
    record = seeded_catalog(creator, n=1)
    res = service.matte_catalog(record.id)
    row = res["results"][0]
    assert row["status"] == "matte_failed" and row["coverage"] is None
    json.dumps(res, allow_nan=False)  # strict-JSON-serializable


def test_onnx_matter_close_releases_session():
    # Review LOW 3F-5: the factory closer nulled only its local binding while
    # the matter kept the session alive — close() must drop the live ref.
    from app.imagegen.matte import _OnnxMatter

    class _Input:
        name = "img"

    class _Session:
        def get_inputs(self):
            return [_Input()]

    matter = _OnnxMatter(_Session(), VARIANTS["isnet_anime"], MatteConfig())
    assert matter._session is not None
    matter.close()
    assert matter._session is None
