"""Stage-5 builder persistence (app/model/builder_store.py) + the shared
containment resolver (store.resolve_within)."""

import json

import pytest

from app.model import (
    BackgroundEntry,
    BackgroundManifest,
    BuilderNotFound,
    BuilderRecord,
    BuilderStore,
    resolve_within,
)


@pytest.fixture()
def store(tmp_path):
    return BuilderStore(tmp_path / "data")


def test_save_load_round_trip(store):
    rec = BuilderRecord.create("Scene A", "scene",
                               selections={"location": "beach"})
    store.save(rec)
    assert store.exists(rec.id)
    back = store.load(rec.id)
    assert back.name == "Scene A" and back.kind == "scene"


def test_load_missing_raises(store):
    with pytest.raises(BuilderNotFound):
        store.load("deadbeef")


def test_list_and_load_all(store):
    a = BuilderRecord.create("A", "persona")
    b = BuilderRecord.create("B", "scenario", consent="romantic")
    store.save(a)
    store.save(b)
    assert store.list_ids() == sorted([a.id, b.id])
    assert {r.kind for r in store.load_all()} == {"persona", "scenario"}


def test_delete_removes_whole_tree(store):
    rec = BuilderRecord.create("A", "scene")
    store.save(rec)
    store.background_dir(rec.id).mkdir(parents=True)
    (store.background_dir(rec.id) / "bg.png").write_bytes(b"x")
    assert store.delete(rec.id) is True
    assert not store.exists(rec.id)
    assert not store.builder_dir(rec.id).exists()


def test_delete_works_on_unloadable_record(store, tmp_path):
    # A hand-edited under-policy record cannot LOAD, but must still delete
    # (deletion is the remedy). Write a blocked record straight to disk.
    rec = BuilderRecord.create("ok", "persona")
    store.save(rec)
    store.record_path(rec.id).write_text(
        json.dumps({"name": "loli", "kind": "persona", "id": rec.id}),
        encoding="utf-8")
    with pytest.raises(Exception):
        store.load(rec.id)
    assert store.delete(rec.id) is True


def test_id_escape_is_rejected(store):
    with pytest.raises(ValueError):
        store.builder_dir("../../etc")


def test_background_manifest_persistence(store):
    rec = BuilderRecord.create("Scene", "scene")
    store.save(rec)
    m = BackgroundManifest(builder_id=rec.id)
    m.entries.append(BackgroundEntry(frame_id="bg-1",
                                     path="background/bg-1.png", bytes=50))
    store.save_background(m)
    back = store.load_background(rec.id)
    assert back is not None and back.entries[0].frame_id == "bg-1"


def test_measure_background_bytes(store):
    rec = BuilderRecord.create("Scene", "scene")
    store.save(rec)
    assert store.measure_background_bytes(rec.id) == 0
    bg = store.background_dir(rec.id)
    bg.mkdir(parents=True)
    (bg / "a.png").write_bytes(b"x" * 123)
    assert store.measure_background_bytes(rec.id) == 123


def test_clear_background(store):
    rec = BuilderRecord.create("Scene", "scene")
    store.save(rec)
    store.background_dir(rec.id).mkdir(parents=True)
    (store.background_dir(rec.id) / "a.png").write_bytes(b"x")
    store.save_background(BackgroundManifest(builder_id=rec.id))
    assert store.clear_background(rec.id) is True
    assert not store.background_dir(rec.id).exists()
    assert store.load_background(rec.id) is None
    # the record itself survives a background clear
    assert store.exists(rec.id)


# -- resolve_within (shared containment) -------------------------------------

def test_resolve_within_accepts_contained_file(tmp_path):
    base = tmp_path / "b"
    base.mkdir()
    (base / "f.png").write_bytes(b"x")
    assert resolve_within(base, "f.png") == (base / "f.png").resolve()


@pytest.mark.parametrize("bad", ["../evil.png", "/abs/x.png", "sub/../../x", "a\x00b"])
def test_resolve_within_rejects_escapes(tmp_path, bad):
    base = tmp_path / "b"
    base.mkdir()
    assert resolve_within(base, bad) is None


def test_resolve_within_missing_file_is_none(tmp_path):
    base = tmp_path / "b"
    base.mkdir()
    assert resolve_within(base, "nope.png") is None
