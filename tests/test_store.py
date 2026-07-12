"""Persistence layer: records + catalog manifests + footprint (Stage 1 DoD:
'a character record round-trips to disk and back')."""

import json

import pytest

from app.model import CharacterRecord, CharacterStore
from app.model.character import CatalogEntry, CatalogManifest
from app.model.age import AgeError


@pytest.fixture()
def store(tmp_path) -> CharacterStore:
    return CharacterStore(tmp_path / "appdata")


def make_record(name="Lyra Nightbloom", age=24):
    return CharacterRecord.create(
        name=name,
        age=age,
        selections={"race": "human", "hair_color": "silver"},
        tags={"traits": ["curious", "loyal"]},
        sliders={"height": 165},
        free_text={"backstory": "A wandering alchemist chasing a cure."},
    )


def test_save_and_load_round_trip(store):
    record = make_record()
    path = store.save(record)
    assert path.is_file()
    loaded = store.load(record.id)
    assert loaded.to_dict() == record.to_dict()


def test_disk_json_is_readable_and_atomic(store, tmp_path):
    record = make_record()
    store.save(record)
    raw = json.loads(store.record_path(record.id).read_text(encoding="utf-8"))
    assert raw["name"] == record.name
    assert raw["age"] == int(record.age)
    # no leftover temp files in the character directory
    leftovers = list(store.char_dir(record.id).glob("*.tmp"))
    assert leftovers == []


def test_list_ids_and_load_all(store):
    a = make_record(name="Alpha")
    b = make_record(name="Bravo")
    store.save(a)
    store.save(b)
    assert sorted(store.list_ids()) == sorted([a.id, b.id])
    names = {r.name for r in store.load_all()}
    assert names == {"Alpha", "Bravo"}


def test_exists_and_delete(store):
    record = make_record()
    store.save(record)
    assert store.exists(record.id)
    assert store.delete(record.id) is True
    assert not store.exists(record.id)
    assert store.delete(record.id) is False


def test_empty_store_lists_nothing(store):
    assert store.list_ids() == []
    assert store.load_all() == []


def test_load_missing_raises_typed_not_found(store):
    from app.model import CharacterNotFound

    with pytest.raises(CharacterNotFound):
        store.load("does-not-exist")


def test_crafted_id_cannot_escape_store(store, tmp_path):
    # A record cannot even hold an unsafe id (InvalidId at construction), so
    # build a valid record then confirm the store rejects a tampered id string
    # passed directly to its path/delete APIs.
    from app.model import InvalidId

    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "important.txt").write_text("keep me", encoding="utf-8")
    for bad in ("../../victim", "..", "a/b", "a\\b"):
        with pytest.raises((InvalidId, ValueError)):
            store.char_dir(bad)
        with pytest.raises((InvalidId, ValueError)):
            store.delete(bad)
        with pytest.raises((InvalidId, ValueError)):
            store.record_path(bad)
    # the external directory is untouched
    assert (victim / "important.txt").is_file()


def test_loading_tampered_underage_file_raises(store):
    record = make_record()
    store.save(record)
    path = store.record_path(record.id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["age"] = 14
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AgeError):
        store.load(record.id)


# -- catalog manifest ---------------------------------------------------------


def test_catalog_manifest_round_trip(store):
    record = make_record()
    store.save(record)
    manifest = CatalogManifest(
        character_id=record.id,
        entries=[
            CatalogEntry(frame_id="f1", path="catalog/f1.png",
                         state={"expression": "neutral", "pose": "standing"}, bytes=2048),
            CatalogEntry(frame_id="f2", path="cache/f2.png",
                         on_demand=True, bytes=1024),
        ],
    )
    store.save_catalog(manifest)
    loaded = store.load_catalog(record.id)
    assert loaded is not None
    assert loaded.character_id == record.id
    assert len(loaded.entries) == 2
    assert loaded.total_bytes() == 3072
    assert loaded.entries[1].on_demand is True


def test_missing_catalog_returns_none(store):
    record = make_record()
    store.save(record)
    assert store.load_catalog(record.id) is None


def test_catalog_manifest_matting_roundtrip_backcompat(store):
    # Stage 3f: `matting` is purely additive — a pre-3f manifest (no key)
    # loads as None, and the provenance dict round-trips unchanged.
    record = make_record()
    store.save(record)
    manifest = CatalogManifest(character_id=record.id)
    data = manifest.to_dict()
    assert "matting" in data and data["matting"] is None  # always emitted
    del data["matting"]  # a pre-3f manifest on disk
    store.catalog_path(record.id).write_text(json.dumps(data), encoding="utf-8")
    assert store.load_catalog(record.id).matting is None
    manifest.matting = {"variant": "isnet_anime", "matted": 3}
    store.save_catalog(manifest)
    assert store.load_catalog(record.id).matting == {
        "variant": "isnet_anime", "matted": 3}


def test_matted_dir_under_catalog(store):
    record = make_record()
    assert store.matted_dir(record.id) == (
        store.catalog_frames_dir(record.id) / "matted")


# -- footprint ----------------------------------------------------------------


def test_measure_footprint(store):
    record = make_record()
    store.save(record)
    cdir = store.char_dir(record.id)
    (cdir / "lora").mkdir(parents=True)
    (cdir / "lora" / "model.safetensors").write_bytes(b"x" * 500)
    (cdir / "catalog").mkdir(parents=True)
    (cdir / "catalog" / "f1.png").write_bytes(b"y" * 300)
    (cdir / "cache").mkdir(parents=True)
    (cdir / "cache" / "f2.png").write_bytes(b"z" * 100)
    fp = store.measure_footprint(record.id)
    assert fp.lora_bytes == 500
    assert fp.catalog_bytes == 300
    assert fp.cache_bytes == 100
    assert fp.total_bytes == 900
