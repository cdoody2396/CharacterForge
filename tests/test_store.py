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


# -- on-demand cache manifest (Stage 3g) ---------------------------------------


def test_cache_manifest_round_trip_and_layout(store):
    record = make_record()
    store.save(record)
    manifest = CatalogManifest(
        character_id=record.id,
        entries=[CatalogEntry(
            frame_id="f1", path="cache/f1.png",
            state={"expression": "smile", "pose": "sitting", "outfit": "asis"},
            on_demand=True, bytes=1024, last_used="2026-07-12T00:00:00+00:00")],
    )
    path = store.save_cache(manifest)
    assert path == store.cache_path(record.id)
    assert path.name == "cache.json"                # sibling of catalog.json
    assert store.load_catalog(record.id) is None    # never crosses channels
    loaded = store.load_cache(record.id)
    assert loaded is not None and loaded.entries[0].on_demand is True
    assert loaded.entries[0].last_used == "2026-07-12T00:00:00+00:00"
    assert loaded.to_dict() == manifest.to_dict()


def test_cache_entry_last_used_backcompat(store):
    # last_used is additive: a pre-3g entry (no key) loads as None.
    entry = CatalogEntry.from_dict({"frame_id": "f", "path": "cache/f.png"})
    assert entry.last_used is None
    assert CatalogEntry.from_dict(entry.to_dict()).last_used is None


def test_missing_cache_returns_none(store):
    record = make_record()
    store.save(record)
    assert store.load_cache(record.id) is None


def test_cache_dirs_are_siblings_of_catalog(store):
    record = make_record()
    cdir = store.char_dir(record.id)
    assert store.cache_frames_dir(record.id) == cdir / "cache"
    assert store.cache_matted_dir(record.id) == cdir / "cache" / "matted"
    # deliberately NOT inside catalog/ — a 3e swap must not destroy the cache
    assert store.catalog_frames_dir(record.id) not in (
        store.cache_frames_dir(record.id).parents)


def test_clear_cache_removes_frames_mattes_and_manifest(store):
    record = make_record()
    store.save(record)
    frames = store.cache_frames_dir(record.id)
    matted = store.cache_matted_dir(record.id)
    matted.mkdir(parents=True)
    (frames / "f1.png").write_bytes(b"x")
    (matted / "f1.png").write_bytes(b"y")
    store.save_cache(CatalogManifest(character_id=record.id))
    assert store.clear_cache(record.id) is True
    assert not frames.exists() and not store.cache_path(record.id).exists()
    assert store.clear_cache(record.id) is False  # idempotent
    # the seed catalog channel is untouched by a cache clear
    store.save_catalog(CatalogManifest(character_id=record.id))
    store.clear_cache(record.id)
    assert store.load_catalog(record.id) is not None


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
    # 3g mattes live under cache/matted/ and count as cache_bytes (rglob)
    (cdir / "cache" / "matted").mkdir(parents=True)
    (cdir / "cache" / "matted" / "f2.png").write_bytes(b"m" * 50)
    fp = store.measure_footprint(record.id)
    assert fp.lora_bytes == 500
    assert fp.catalog_bytes == 300
    assert fp.cache_bytes == 150
    assert fp.total_bytes == 950
