"""Stage-5 scene image pipeline (ImageService.generate_background /
composite_frame / matted_frames) — the [HARDWARE] leg exercised GPU-less with
an injected fake engine + fake Layer-2 classifier, plus the all-[HERE]
compositing path."""

import pytest
from PIL import Image

from app.imagegen.cull import ClassifierToolkit, ContentVerdict
from app.imagegen.engine import ImageEngine
from app.imagegen.service import ImageService
from app.model import CatalogEntry, CatalogManifest


# -- fakes -------------------------------------------------------------------

class FakeImage:
    def __init__(self, seed):
        self.seed = seed

    def save(self, path):
        # a REAL png so the Layer-2 classifier / compositor can open it
        Image.new("RGB", (64, 96), (40, 80, 120)).save(path)


class FakeBackend:
    def __init__(self, *a, **k):
        self.closed = False

    def generate(self, request, reference=None):
        return FakeImage(request.seed)

    def close(self):
        self.closed = True


class FakeFactory:
    def __call__(self, checkpoint, config_dir=None, ip_config=None, lora=None):
        return FakeBackend()


class FakeClassifier:
    def __init__(self, blocked):
        self.blocked = blocked
        self.calls = []

    def classify(self, path):
        self.calls.append(str(path))
        return ContentVerdict(
            blocked=self.blocked,
            category="minors" if self.blocked else None,
            matched="loli" if self.blocked else None)


def classifier_factory(blocked=False):
    fc = FakeClassifier(blocked)

    def factory(settings):
        return ClassifierToolkit(classifier=fc, closer=None)

    factory.fc = fc
    return factory


@pytest.fixture()
def scene_env(creator, settings, audit, builders, tmp_path):
    """A service wired with a fake engine + fake classifier, over the builder
    store + scene catalog (like main), and a scene builder to render."""
    ckpt = tmp_path / "m.safetensors"
    ckpt.write_bytes(b"\0" * 16)
    settings.set("models.image.checkpoint_path", str(ckpt))
    ccdir = tmp_path / "cc"
    ccdir.mkdir()
    settings.set("models.image.content_classifier_dir", str(ccdir))

    def make(blocked=False):
        engine = ImageEngine(settings, backend_factory=FakeFactory())
        return ImageService(
            creator.store, settings, audit,
            catalog_provider=lambda: creator.catalog,
            engine=engine, builder_store=builders.store,
            scene_catalog_provider=builders.scene_catalog,
            classifier_factory=classifier_factory(blocked))

    scene = builders.create({"kind": "scene", "name": "Beach",
                             "selections": {"location": "beach",
                                            "time_of_day": "dusk"}})
    return make, scene["id"], builders


# -- generate_background -----------------------------------------------------

def test_generate_background_passes_layer2_and_persists(scene_env):
    make, scene_id, builders = scene_env
    svc = make(blocked=False)
    r = svc.generate_background(scene_id)
    assert r["ok"] and "no humans" in r["positive"]
    assert "1girl" in r["negative"]     # people-steer negatives present
    st = svc.background_status(scene_id)
    assert st["count"] == 1 and st["classifier_ready"] is True


def test_generate_background_blocked_frame_is_purged(scene_env):
    make, scene_id, builders = scene_env
    svc = make(blocked=True)
    r = svc.generate_background(scene_id)
    assert not r["ok"] and r["kind"] == "blocked" and r["category"] == "minors"
    # nothing recorded, no frame kept
    assert svc.background_status(scene_id)["count"] == 0


def test_generate_background_rejects_non_scene(scene_env):
    make, scene_id, builders = scene_env
    persona = builders.create({"kind": "persona", "name": "P"})
    svc = make()
    r = svc.generate_background(persona["id"])
    assert not r["ok"] and r["kind"] == "not_scene"


def test_generate_background_classifier_unavailable(creator, settings, audit,
                                                    builders, tmp_path):
    ckpt = tmp_path / "m.safetensors"
    ckpt.write_bytes(b"\0" * 16)
    settings.set("models.image.checkpoint_path", str(ckpt))
    # no content_classifier_dir set -> preflight fails, before any render
    engine = ImageEngine(settings, backend_factory=FakeFactory())
    svc = ImageService(creator.store, settings, audit,
                       catalog_provider=lambda: creator.catalog, engine=engine,
                       builder_store=builders.store,
                       scene_catalog_provider=builders.scene_catalog,
                       classifier_factory=classifier_factory())
    scene = builders.create({"kind": "scene", "name": "S"})
    r = svc.generate_background(scene["id"])
    assert not r["ok"] and r["kind"] == "classifier_unavailable"


def test_generate_background_engine_unavailable_is_structured(creator, settings,
                                                              audit, builders):
    # No checkpoint + the real engine -> a structured 'engine' error, never a
    # traceback (the sandbox contract).
    svc = ImageService(creator.store, settings, audit,
                       catalog_provider=lambda: creator.catalog,
                       builder_store=builders.store,
                       scene_catalog_provider=builders.scene_catalog)
    scene = builders.create({"kind": "scene", "name": "S"})
    r = svc.generate_background(scene["id"])
    assert not r["ok"] and r["kind"] in ("engine", "classifier_unavailable")


# -- compositing (all [HERE]) ------------------------------------------------

def _character_with_matte(creator):
    """Create a character and give it one matted catalog frame."""
    created = creator.create_character({
        "mode": "quick", "name": "Ada", "age": 30,
        "selections": {"race": "human", "gender_presentation": "feminine",
                       "skin_type": "bare_skin", "skin_tone": "fair",
                       "hair_color": "black", "hair_style": "bob",
                       "eye_color": "brown", "body_type": "average"}})
    cid = created["id"]
    store = creator.store
    mdir = store.matted_dir(cid)
    mdir.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGBA", (80, 160), (0, 0, 0, 0))
    for y in range(160):
        for x in range(80):
            if 20 < x < 60 and 10 < y < 150:
                im.putpixel((x, y), (200, 50, 50, 255))
    im.save(mdir / "f1.png")
    manifest = CatalogManifest(character_id=cid)
    manifest.entries.append(CatalogEntry(
        frame_id="f1", path="catalog/f1-src.png",
        matted_path="catalog/matted/f1.png"))
    # a source frame must exist for matted_frames' entry (path) — write one
    (store.catalog_frames_dir(cid)).mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 160), (1, 2, 3)).save(store.catalog_frames_dir(cid) / "f1-src.png")
    store.save_catalog(manifest)
    return cid, "catalog/matted/f1.png"


def test_matted_frames_lists_existing_mattes(scene_env, creator):
    make, scene_id, builders = scene_env
    cid, matte_rel = _character_with_matte(creator)
    svc = make()
    out = svc.matted_frames(cid)
    assert out["ok"] and out["count"] == 1
    assert out["frames"][0]["matted_path"] == matte_rel


def test_composite_background_on(scene_env, creator):
    make, scene_id, builders = scene_env
    cid, matte_rel = _character_with_matte(creator)
    svc = make()
    svc.generate_background(scene_id)     # produce a background to composite over
    r = svc.composite_frame(cid, matte_rel, scene_id)
    assert r["ok"] and r["background"] is True
    assert r["preview"].startswith("data:image/png;base64,")


def test_composite_background_off_is_transparent_passthrough(scene_env, creator):
    make, scene_id, builders = scene_env
    cid, matte_rel = _character_with_matte(creator)
    svc = make()
    r = svc.composite_frame(cid, matte_rel, None)
    assert r["ok"] and r["background"] is False
    assert r["preview"].startswith("data:image/png;base64,")


def test_composite_rejects_unmatted_frame(scene_env, creator):
    make, scene_id, builders = scene_env
    cid, _ = _character_with_matte(creator)
    # point at the RGB source frame (no alpha) -> not_matted
    r = make().composite_frame(cid, "catalog/f1-src.png", None)
    assert not r["ok"] and r["kind"] == "not_matted"


def test_composite_rejects_non_scene_background(scene_env, creator):
    make, scene_id, builders = scene_env
    cid, matte_rel = _character_with_matte(creator)
    persona = builders.create({"kind": "persona", "name": "P"})
    r = make().composite_frame(cid, matte_rel, persona["id"])
    assert not r["ok"] and r["kind"] == "not_scene"


def test_composite_no_background_yet(scene_env, creator):
    make, scene_id, builders = scene_env
    cid, matte_rel = _character_with_matte(creator)
    # scene exists but has no generated background
    r = make().composite_frame(cid, matte_rel, scene_id)
    assert not r["ok"] and r["kind"] == "no_background"


def test_composite_oversized_frame_is_structured_not_a_raise(scene_env, creator,
                                                             monkeypatch):
    # §2 bridge contract: a hand-placed oversized/bomb frame makes PIL raise
    # DecompressionBombError (a BARE Exception subclass, not OSError/ValueError).
    # composite_frame MUST return a structured {ok:False,kind:'io'} — never let
    # it escape the bridge (which would hang the UI under pythonw).
    import PIL.Image as PImage
    make, scene_id, builders = scene_env
    cid, matte_rel = _character_with_matte(creator)   # 80x160 = 12800 px matte
    monkeypatch.setattr(PImage, "MAX_IMAGE_PIXELS", 2)  # bomb error fires > 4 px
    r = make().composite_frame(cid, matte_rel, None)
    assert isinstance(r, dict) and r["ok"] is False and r["kind"] == "io"
