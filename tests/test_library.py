"""Stage 4 — Library & Management (§14).

Service-level tests for LibraryService (list / get / delete / thumbnail /
reconcile), the §14 LRU cache cap (pure selection + ImageService
enforcement), and the library settings coercion. The bridge surface is
pinned in test_shell_api.py; the edit path in test_creator.py.
"""

import json

import pytest

from app.imagegen.manage import (
    LibraryConfig,
    coerce_library_config,
    select_evictions,
)
from app.model import (
    BootstrapCandidate,
    BootstrapManifest,
    CatalogEntry,
    CatalogManifest,
)
from app.ui.library import load_record_guarded, resolve_contained

MB = 1024 * 1024


def audit_events(audit):
    path = audit.path_for_today()
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines()]


# The render-identity minimum every character now needs (5.5c).
SEL = {"race": "human", "gender_presentation": "feminine",
       "skin_type": "bare_skin", "skin_tone": "fair",
       "hair_color": "black", "hair_style": "bob", "eye_color": "brown",
       "body_type": "average"}


def _create(creator, name="Lib Test", age=25, **kw):
    sel = dict(SEL)
    sel.update(kw.pop("selections", {}))
    res = creator.create_character(
        {"mode": "quick", "name": name, "age": age, "selections": sel, **kw})
    assert res["ok"] is True
    return res["id"]


def _forge_cache(store, cid, sizes, matted=True):
    """A cache manifest + real files: sizes is {frame_id: (bytes, last_used)}.
    Returns the manifest."""
    frames = store.cache_frames_dir(cid)
    matted_dir = store.cache_matted_dir(cid)
    frames.mkdir(parents=True, exist_ok=True)
    matted_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for fid, (nbytes, last_used) in sizes.items():
        (frames / f"{fid}.png").write_bytes(b"\0" * nbytes)
        (frames / f"{fid}.json").write_bytes(b"{}")
        mpath = None
        if matted:
            (matted_dir / f"{fid}.png").write_bytes(b"\0" * 16)
            mpath = f"cache/matted/{fid}.png"
        entries.append(CatalogEntry(
            frame_id=fid, path=f"cache/{fid}.png", state={"expression": fid},
            matted_path=mpath, on_demand=True, bytes=nbytes,
            last_used=last_used))
    manifest = CatalogManifest(character_id=cid, entries=entries)
    store.save_cache(manifest)
    return manifest


# -- settings coercion (the resolved deferred thresholds item) ---------------


def test_library_settings_defaults(settings):
    assert settings.get("library.cache_cap_bytes") == 268435456
    assert settings.get("library.recommend_cache_bytes") == 201326592
    config = coerce_library_config(settings)
    assert config.cache_cap_bytes == 268435456
    assert config.recommend_cache_bytes == 201326592


@pytest.mark.parametrize("bad", ["nope", None, float("nan"), float("inf"),
                                 [1], {"x": 1}])
def test_library_config_bad_values_degrade_to_default(settings, bad):
    settings.set("library.cache_cap_bytes", bad, save=False)
    settings.set("library.recommend_cache_bytes", bad, save=False)
    d = LibraryConfig()
    config = coerce_library_config(settings)
    assert config.cache_cap_bytes == d.cache_cap_bytes
    assert config.recommend_cache_bytes == d.recommend_cache_bytes


def test_library_config_clamps(settings):
    settings.set("library.cache_cap_bytes", 1, save=False)          # below floor
    settings.set("library.recommend_cache_bytes", 1e30, save=False)  # above cap
    config = coerce_library_config(settings)
    assert config.cache_cap_bytes == 8 * MB
    assert config.recommend_cache_bytes == 1024 * 1024 * MB


# -- pure LRU selection -------------------------------------------------------


def _entry(fid, last_used):
    return CatalogEntry(frame_id=fid, path=f"cache/{fid}.png",
                        on_demand=True, last_used=last_used)


def test_select_evictions_noop_under_cap():
    pairs = [(_entry("a", "2026-01-01T00:00:00+00:00"), 100)]
    assert select_evictions(pairs, total_bytes=100, cap_bytes=200) == []


def test_select_evictions_oldest_first_until_under_cap():
    a = _entry("a", "2026-01-01T00:00:00+00:00")
    b = _entry("b", "2026-01-02T00:00:00+00:00")
    c = _entry("c", "2026-01-03T00:00:00+00:00")
    evict = select_evictions([(c, 100), (a, 100), (b, 100)],
                             total_bytes=300, cap_bytes=150)
    assert [e.frame_id for e in evict] == ["a", "b"]  # LRU order, stops at cap


def test_select_evictions_missing_last_used_reads_as_oldest():
    a = _entry("a", None)
    b = _entry("b", "2026-01-01T00:00:00+00:00")
    evict = select_evictions([(b, 100), (a, 100)],
                             total_bytes=200, cap_bytes=150)
    assert [e.frame_id for e in evict] == ["a"]


def test_select_evictions_never_evicts_the_mru():
    a = _entry("a", "2026-01-01T00:00:00+00:00")
    evict = select_evictions([(a, 900)], total_bytes=900, cap_bytes=100)
    assert evict == []  # a single over-cap entry survives (no thrash)


def test_select_evictions_deterministic_tiebreak():
    a = _entry("a", None)
    b = _entry("b", None)
    c = _entry("c", "2026-01-01T00:00:00+00:00")
    evict = select_evictions([(b, 10), (a, 10), (c, 10)],
                             total_bytes=30, cap_bytes=15)
    assert [e.frame_id for e in evict] == ["a", "b"]


def test_select_evictions_protect_id_pins_the_fresh_frame():
    # same-second last_used: a cache hit stamps another entry ("zzz-hit") in
    # the same second the fresh insert ("aaa-fresh") lands; the frame_id
    # tiebreak makes zzz-hit the MRU survivor and would otherwise let the
    # just-generated aaa-fresh be evicted. protect_id pins it, so the genuine
    # oldest ("mmm-old") is evicted instead.
    fresh = _entry("aaa-fresh", "2026-01-02T00:00:05+00:00")
    hit = _entry("zzz-hit", "2026-01-02T00:00:05+00:00")
    old = _entry("mmm-old", "2026-01-01T00:00:00+00:00")
    evict = select_evictions([(fresh, 10), (hit, 10), (old, 10)],
                             total_bytes=30, cap_bytes=5,
                             protect_id="aaa-fresh")
    assert [e.frame_id for e in evict] == ["mmm-old"]
    # without the pin, the fresh frame would be taken
    unpinned = select_evictions([(fresh, 10), (hit, 10), (old, 10)],
                                total_bytes=30, cap_bytes=5)
    assert "aaa-fresh" in [e.frame_id for e in unpinned]


# -- ImageService.enforce_cache_cap (§14 backstop) ----------------------------


def test_enforce_cache_cap_noop_without_cache(creator, images):
    cid = _create(creator)
    res = images.enforce_cache_cap(cid)
    assert res["ok"] is True and res["evicted"] == 0


def test_enforce_cache_cap_evicts_lru_and_purges(creator, images, settings,
                                                 audit):
    cid = _create(creator)
    store = creator.store
    # 4 MB per frame minus slack so the tiny sidecars/mattes don't tip the
    # post-eviction total back over the cap
    size = 4 * MB - 4096
    _forge_cache(store, cid, {
        "old": (size, "2026-01-01T00:00:00+00:00"),
        "mid": (size, "2026-01-02T00:00:00+00:00"),
        "new": (size, "2026-01-03T00:00:00+00:00"),
    })
    settings.set("library.cache_cap_bytes", 8 * MB, save=False)

    res = images.enforce_cache_cap(cid)
    assert res["ok"] is True and res["evicted"] == 1
    assert res["freed_bytes"] >= size
    frames = store.cache_frames_dir(cid)
    assert not (frames / "old.png").exists()
    assert not (frames / "old.json").exists()
    assert not (store.cache_matted_dir(cid) / "old.png").exists()
    assert (frames / "mid.png").exists() and (frames / "new.png").exists()
    manifest = store.load_cache(cid)
    assert [e.frame_id for e in manifest.entries] == ["mid", "new"]
    assert any(e["kind"] == "cache_evicted" and e["evicted"] == 1
               for e in audit_events(audit))
    # idempotent: already under cap
    again = images.enforce_cache_cap(cid)
    assert again["ok"] is True and again["evicted"] == 0


def test_enforce_cache_cap_protects_single_mru(creator, images, settings):
    cid = _create(creator)
    _forge_cache(creator.store, cid,
                 {"only": (9 * MB, "2026-01-01T00:00:00+00:00")})
    settings.set("library.cache_cap_bytes", 8 * MB, save=False)
    res = images.enforce_cache_cap(cid)
    assert res["ok"] is True and res["evicted"] == 0
    assert (creator.store.cache_frames_dir(cid) / "only.png").exists()


def test_enforce_cache_cap_structured_on_bad_input(creator, images):
    assert images.enforce_cache_cap(None)["kind"] == "invalid"
    assert images.enforce_cache_cap("nope")["kind"] == "not_found"
    cid = _create(creator)
    creator.store.cache_path(cid).write_text("{broken", encoding="utf-8")
    assert images.enforce_cache_cap(cid)["kind"] == "cache_corrupt"


def test_enforce_cache_cap_counts_recorded_bytes_not_orphans(creator, images,
                                                             settings):
    # A big unrecorded orphan in cache/ must NOT drive eviction of good
    # recorded frames — the cap measures recorded artifacts; orphans are the
    # reconcile sweep's job (review catch: measuring the whole tree over-
    # evicted to pay for bytes eviction can't free).
    cid = _create(creator)
    store = creator.store
    _forge_cache(store, cid, {
        "a": (1 * MB, "2026-01-01T00:00:00+00:00"),
        "b": (1 * MB, "2026-01-02T00:00:00+00:00"),
    })
    (store.cache_frames_dir(cid) / "orphan.png").write_bytes(b"\0" * (20 * MB))
    settings.set("library.cache_cap_bytes", 8 * MB, save=False)
    res = images.enforce_cache_cap(cid)
    assert res["ok"] is True and res["evicted"] == 0  # recorded 2 MB < 8 MB cap
    assert [e.frame_id for e in store.load_cache(cid).entries] == ["a", "b"]


# -- guarded record load / contained resolver ---------------------------------


def test_load_record_guarded_taxonomy(creator, audit):
    store = creator.store
    assert load_record_guarded(store, audit, "")["kind"] == "invalid"
    assert load_record_guarded(store, audit, None)["kind"] == "invalid"
    assert load_record_guarded(store, audit, "ghost")["kind"] == "not_found"
    assert load_record_guarded(store, audit, "../up")["kind"] == "not_found"

    cid = _create(creator)
    loaded = load_record_guarded(store, audit, cid)
    assert not isinstance(loaded, dict) and loaded.id == cid

    # corrupt file -> io
    store.record_path(cid).write_text("{broken", encoding="utf-8")
    assert load_record_guarded(store, audit, cid)["kind"] == "io"

    # blocked name -> blocked + audited
    cid2 = _create(creator, name="Blocked Later")
    data = json.loads(store.record_path(cid2).read_text(encoding="utf-8"))
    data["name"] = "loli"
    store.record_path(cid2).write_text(json.dumps(data), encoding="utf-8")
    res = load_record_guarded(store, audit, cid2)
    assert res["kind"] == "blocked"
    assert any(e["kind"] == "filter_block"
               and e["context"].startswith("library.load")
               for e in audit_events(audit))

    # under-age hand-edit -> age (Layer 3 holds at the load door)
    cid3 = _create(creator, name="Age Later")
    data = json.loads(store.record_path(cid3).read_text(encoding="utf-8"))
    data["age"] = 17
    store.record_path(cid3).write_text(json.dumps(data), encoding="utf-8")
    assert load_record_guarded(store, audit, cid3)["kind"] == "age"


def test_resolve_contained_rules(creator):
    store = creator.store
    cid = _create(creator)
    ref = store.char_dir(cid) / "reference" / "r.png"
    ref.parent.mkdir(parents=True)
    ref.write_bytes(b"PNG")
    assert resolve_contained(store, cid, "reference/r.png") == ref.resolve()
    assert resolve_contained(store, cid, "") is None
    assert resolve_contained(store, cid, None) is None
    assert resolve_contained(store, cid, "reference/missing.png") is None
    assert resolve_contained(store, cid, "../escape.png") is None
    assert resolve_contained(store, cid, "ref\x00erence/r.png") is None
    assert resolve_contained(store, cid, str(ref)) is None  # absolute
    assert resolve_contained(store, "../..", "reference/r.png") is None


# -- list ---------------------------------------------------------------------


def test_list_empty(library):
    res = library.list_characters()
    assert res["ok"] is True and res["characters"] == [] and res["count"] == 0
    assert res["recommend_cache_bytes"] > 0 and res["cache_cap_bytes"] > 0


def test_list_summary_fields(creator, library):
    cid = _create(creator, name="Summary", age=31,
                  selections={"race": "elf"})
    res = library.list_characters()
    assert res["count"] == 1
    row = res["characters"][0]
    assert row["ok"] is True and row["id"] == cid
    assert row["name"] == "Summary" and row["age"] == 31
    assert row["created_at"] and row["updated_at"]
    assert row["has_lora"] is False and row["has_reference"] is False
    assert row["catalog"] == {"frames": 0, "stale": False}
    assert row["cache"] == {"frames": 0, "stale": False}
    assert row["footprint"]["total_bytes"] == 0
    assert row["recommend_delete"] is False


def test_list_footprint_reads_cached_value(creator, library, images):
    # 5.5e: the library reads the CACHED footprint off the record, NOT a
    # per-row disk walk (~10k stat()s at 200 characters). Files dropped outside
    # an artifact op read 0 until a recompute; refresh_footprint (what every
    # artifact op + the reconcile sweep call) is what populates it.
    cid = _create(creator)
    cdir = creator.store.char_dir(cid)
    (cdir / "lora").mkdir()
    (cdir / "lora" / "identity.safetensors").write_bytes(b"\0" * 300)
    (cdir / "catalog").mkdir()
    (cdir / "catalog" / "f.png").write_bytes(b"\0" * 200)
    (cdir / "cache").mkdir()
    (cdir / "cache" / "c.png").write_bytes(b"\0" * 100)
    assert library.list_characters()["characters"][0]["footprint"][
        "total_bytes"] == 0
    images.refresh_footprint(cid)
    fp = library.list_characters()["characters"][0]["footprint"]
    assert fp["lora_bytes"] == 300
    assert fp["catalog_bytes"] == 200
    assert fp["cache_bytes"] == 100
    assert fp["total_bytes"] == 600


def test_list_recommendation_past_cache_threshold(creator, library, images,
                                                  settings):
    cid = _create(creator)
    cdir = creator.store.char_dir(cid)
    (cdir / "cache").mkdir()
    (cdir / "cache" / "big.png").write_bytes(b"\0" * (8 * MB + 1024))
    settings.set("library.recommend_cache_bytes", 8 * MB, save=False)
    images.refresh_footprint(cid)  # 5.5e: cache the footprint the flag reads
    row = library.list_characters()["characters"][0]
    assert row["recommend_delete"] is True
    # under the threshold it stays quiet
    (cdir / "cache" / "big.png").write_bytes(b"\0" * 1024)
    images.refresh_footprint(cid)
    row = library.list_characters()["characters"][0]
    assert row["recommend_delete"] is False


def test_list_returns_resolved_tag_labels(creator, library):
    # 5.5e: library_list carries the character's multi-select tags (wardrobe /
    # marks / traits — 5.6c vocabulary) resolved to human labels so the UI can
    # filter on them.
    cid = _create(creator)
    catalog = creator.catalog
    record = creator.store.load(cid)
    record.tags = {"outfit": ["gown", "kimono"], "traits": ["witty"],
                   "marks": ["freckles"]}
    creator.store.save(record)
    row = next(r for r in library.list_characters()["characters"]
               if r["id"] == cid)
    tags = row["tags"]
    for gid, oid in [("outfit", "gown"), ("outfit", "kimono"),
                     ("traits", "witty"), ("marks", "freckles")]:
        assert catalog.get(gid).get_option(oid).label in tags
    # deduped and stable
    assert len(tags) == len(set(tags))
    # an option id no longer in the catalog falls back to the raw id (the
    # record stays the source of truth, §15)
    record.tags = {"outfit": ["removed_outfit"]}
    creator.store.save(record)
    row = next(r for r in library.list_characters()["characters"]
               if r["id"] == cid)
    assert row["tags"] == ["removed_outfit"]


def test_list_identity_flags(creator, library):
    cid = _create(creator)
    store = creator.store
    ref = store.char_dir(cid) / "reference" / "r.png"
    ref.parent.mkdir(parents=True)
    ref.write_bytes(b"PNG")
    record = store.load(cid)
    record.identity.reference_image_path = "reference/r.png"
    record.identity.has_lora = True
    record.identity.lora_path = "lora/identity.safetensors"
    store.save(record)
    row = library.list_characters()["characters"][0]
    assert row["has_reference"] is True
    assert row["has_lora"] is True
    # an escaped hand-edited reference reads as no reference, not an error
    record.identity.reference_image_path = "../../outside.png"
    store.save(record)
    row = library.list_characters()["characters"][0]
    assert row["has_reference"] is False


def test_list_degrades_broken_records_to_error_rows(creator, library):
    good = _create(creator, name="Good")
    bad = _create(creator, name="Bad Soon")
    creator.store.record_path(bad).write_text("{broken", encoding="utf-8")
    res = library.list_characters()
    assert res["count"] == 2
    rows = {r["id"]: r for r in res["characters"]}
    assert rows[good]["ok"] is True
    assert rows[bad]["ok"] is False and rows[bad]["kind"] == "io"
    assert rows[bad]["name"] is None
    assert rows[bad]["footprint"] is not None  # still measurable, deletable


def test_list_blocked_record_row(creator, library, audit):
    cid = _create(creator, name="Fine Now")
    data = json.loads(
        creator.store.record_path(cid).read_text(encoding="utf-8"))
    data["name"] = "loli"
    creator.store.record_path(cid).write_text(json.dumps(data),
                                              encoding="utf-8")
    row = library.list_characters()["characters"][0]
    assert row["ok"] is False and row["kind"] == "blocked"


def test_list_manifest_corruption_is_per_channel(creator, library):
    cid = _create(creator)
    creator.store.catalog_path(cid).write_text("{broken", encoding="utf-8")
    row = library.list_characters()["characters"][0]
    assert row["ok"] is True  # the record itself is fine
    assert row["catalog"]["error"] == "catalog_corrupt"
    assert row["cache"] == {"frames": 0, "stale": False}


def test_list_reports_stale_and_frames(creator, library):
    cid = _create(creator)
    store = creator.store
    frames = store.catalog_frames_dir(cid)
    frames.mkdir(parents=True)
    (frames / "f1.png").write_bytes(b"PNG")
    store.save_catalog(CatalogManifest(
        character_id=cid, stale=True,
        entries=[CatalogEntry(frame_id="f1", path="catalog/f1.png")]))
    row = library.list_characters()["characters"][0]
    assert row["catalog"] == {"frames": 1, "stale": True}


# -- get_character ------------------------------------------------------------


def test_get_character_round_trips_form_fields(creator, library):
    selections = {**SEL, "race": "elf"}
    res = creator.create_character({
        "mode": "detailed", "name": "Edit Me", "age": 27,
        "selections": selections,
        "tags": {"traits": ["curious"]},
        "free_text": {"signature_note": "A quiet scholar of the old library."},
    })
    assert res["ok"] is True
    got = library.get_character(res["id"])
    assert got["ok"] is True
    assert got["name"] == "Edit Me" and got["age"] == 27
    assert got["selections"] == selections
    assert got["tags"] == {"traits": ["curious"]}
    assert got["sliders"] == {}
    assert got["free_text"] == {
        "signature_note": "A quiet scholar of the old library."}
    assert got["identity"] == {"has_lora": False, "has_reference": False}
    assert got["issues"] == []


def test_get_character_structured_errors(library):
    assert library.get_character("")["kind"] == "invalid"
    assert library.get_character("ghost")["kind"] == "not_found"


def test_get_character_reports_unknown_option_issues(creator, library):
    from app.model import CharacterRecord

    record = CharacterRecord.create(name="Odd", age=25,
                                    selections={"vanished_group": "x"})
    creator.store.save(record)
    got = library.get_character(record.id)
    assert got["ok"] is True
    assert any("vanished_group" in issue for issue in got["issues"])


# -- delete ---------------------------------------------------------------------


def test_delete_character(creator, library, audit):
    cid = _create(creator, name="Doomed")
    assert creator.store.exists(cid)
    res = library.delete_character(cid)
    assert res == {"ok": True, "id": cid, "removed": True}
    assert not creator.store.char_dir(cid).exists()
    assert any(e["kind"] == "character_deleted" for e in audit_events(audit))
    # second delete: nothing there, still ok
    assert library.delete_character(cid)["removed"] is False


def test_delete_requires_valid_id_only(creator, library):
    assert library.delete_character("")["kind"] == "invalid"
    assert library.delete_character(None)["kind"] == "invalid"
    assert library.delete_character("../../etc")["kind"] == "not_found"
    # a record that no longer LOADS is still deletable (the §14 remedy)
    cid = _create(creator, name="Broken Soon")
    creator.store.record_path(cid).write_text("{broken", encoding="utf-8")
    res = library.delete_character(cid)
    assert res["ok"] is True and res["removed"] is True


# -- thumbnail -------------------------------------------------------------------


def _write_png(path, size=(64, 96), color=(120, 40, 200)):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")


def _set_reference(store, cid, rel="reference/r.png"):
    record = store.load(cid)
    record.identity.reference_image_path = rel
    store.save(record)


def test_thumbnail_none_without_reference(creator, library):
    cid = _create(creator)
    res = library.thumbnail(cid)
    assert res == {"ok": True, "id": cid, "thumbnail": None}


def test_thumbnail_data_uri_from_reference(creator, library):
    cid = _create(creator)
    _write_png(creator.store.char_dir(cid) / "reference" / "r.png",
               size=(600, 900))
    _set_reference(creator.store, cid)
    res = library.thumbnail(cid)
    assert res["ok"] is True
    assert res["thumbnail"].startswith("data:image/jpeg;base64,")
    # bounded: decode and check the long side
    import base64
    import io as io_mod

    from PIL import Image

    raw = base64.b64decode(res["thumbnail"].split(",", 1)[1])
    with Image.open(io_mod.BytesIO(raw)) as im:
        assert max(im.size) <= 256


def test_thumbnail_corrupt_image_is_none(creator, library):
    cid = _create(creator)
    ref = creator.store.char_dir(cid) / "reference" / "r.png"
    ref.parent.mkdir(parents=True)
    ref.write_bytes(b"not a png at all")
    _set_reference(creator.store, cid)
    assert library.thumbnail(cid)["thumbnail"] is None


def test_thumbnail_escaped_reference_is_none(creator, library, tmp_path):
    cid = _create(creator)
    outside = tmp_path / "outside.png"
    _write_png(outside)
    _set_reference(creator.store, cid, "../../outside.png")
    assert library.thumbnail(cid)["thumbnail"] is None
    _set_reference(creator.store, cid, str(outside))
    assert library.thumbnail(cid)["thumbnail"] is None


def test_thumbnail_structured_errors(library):
    assert library.thumbnail("")["kind"] == "invalid"
    assert library.thumbnail("ghost")["kind"] == "not_found"


# -- reconcile -------------------------------------------------------------------


def test_reconcile_empty_store(library, audit):
    res = library.reconcile()
    assert res["ok"] is True
    assert res["staging_dirs"] == 0 and res["cache_orphans"] == 0
    assert any(e["kind"] == "library_reconciled"
               for e in audit_events(audit))


def test_reconcile_sweeps_staging_dirs(creator, library, audit):
    cid = _create(creator)
    cdir = creator.store.char_dir(cid)
    for name in ("catalog.old", "catalog.new", "cache.new", "vetted.new"):
        (cdir / name).mkdir()
        (cdir / name / "left.png").write_bytes(b"\0" * 64)
    res = library.reconcile()
    assert res["staging_dirs"] == 4
    assert res["bytes_freed"] >= 4 * 64
    for name in ("catalog.old", "catalog.new", "cache.new", "vetted.new"):
        assert not (cdir / name).exists()
    assert any(e["kind"] == "library_swept" and e["staging_dirs"] == 4
               for e in audit_events(audit))


def test_reconcile_sweeps_bootstrap_orphans(creator, library):
    cid = _create(creator)
    store = creator.store
    cand = store.candidates_dir(cid)
    cand.mkdir(parents=True)
    # recorded candidate + sidecar stay
    (cand / "keep.png").write_bytes(b"PNG")
    (cand / "keep.json").write_bytes(b"{}")
    # unrecorded pair (mid-batch kill) goes
    (cand / "orphan.png").write_bytes(b"PNG")
    (cand / "orphan.json").write_bytes(b"{}")
    # non-artifact files are never touched
    (cand / "notes.txt").write_text("mine", encoding="utf-8")
    store.save_bootstrap(BootstrapManifest(
        character_id=cid,
        candidates=[BootstrapCandidate(
            candidate_id="keep", path="bootstrap/candidates/keep.png",
            seed=1)]))
    res = library.reconcile()
    assert res["bootstrap_orphans"] == 2
    assert (cand / "keep.png").exists() and (cand / "keep.json").exists()
    assert not (cand / "orphan.png").exists()
    assert not (cand / "orphan.json").exists()
    assert (cand / "notes.txt").exists()


def test_reconcile_bootstrap_absent_manifest_sweeps_all(creator, library):
    cid = _create(creator)
    cand = creator.store.candidates_dir(cid)
    cand.mkdir(parents=True)
    (cand / "a.png").write_bytes(b"PNG")
    (cand / "a.json").write_bytes(b"{}")
    res = library.reconcile()
    assert res["bootstrap_orphans"] == 2
    assert not (cand / "a.png").exists()


def test_reconcile_corrupt_manifest_sweeps_nothing(creator, library):
    cid = _create(creator)
    store = creator.store
    cand = store.candidates_dir(cid)
    cand.mkdir(parents=True)
    (cand / "a.png").write_bytes(b"PNG")
    store.bootstrap_path(cid).write_text("{broken", encoding="utf-8")
    res = library.reconcile()
    assert res["bootstrap_orphans"] == 0
    assert (cand / "a.png").exists()  # orphanhood not provable -> kept
    assert any("bootstrap_corrupt" in c.get("notes", [])
               for c in res["characters"])


def test_reconcile_sweeps_cache_orphans(creator, library):
    cid = _create(creator)
    store = creator.store
    _forge_cache(store, cid, {"keep": (64, "2026-01-01T00:00:00+00:00")})
    frames = store.cache_frames_dir(cid)
    matted = store.cache_matted_dir(cid)
    # the named 3g kill-window orphan: frame+sidecar+matte, unrecorded
    (frames / "orphan.png").write_bytes(b"PNG")
    (frames / "orphan.json").write_bytes(b"{}")
    (matted / "orphan.png").write_bytes(b"PNG")
    # a stray promoted-tmp leftover
    (matted / "half.png.tmp").write_bytes(b"PNG")
    res = library.reconcile()
    assert res["cache_orphans"] == 4
    assert (frames / "keep.png").exists() and (frames / "keep.json").exists()
    assert (matted / "keep.png").exists()
    assert not (frames / "orphan.png").exists()
    assert not (matted / "orphan.png").exists()
    assert not (matted / "half.png.tmp").exists()


def test_reconcile_cache_absent_manifest_sweeps_all(creator, library):
    cid = _create(creator)
    frames = creator.store.cache_frames_dir(cid)
    frames.mkdir(parents=True)
    (frames / "a.png").write_bytes(b"PNG")
    res = library.reconcile()
    assert res["cache_orphans"] == 1
    assert not (frames / "a.png").exists()


def test_reconcile_drops_dangling_manifest_entries(creator, library):
    cid = _create(creator)
    store = creator.store
    frames = store.catalog_frames_dir(cid)
    frames.mkdir(parents=True)
    (frames / "real.png").write_bytes(b"PNG")
    store.save_catalog(CatalogManifest(character_id=cid, entries=[
        CatalogEntry(frame_id="real", path="catalog/real.png"),
        CatalogEntry(frame_id="gone", path="catalog/gone.png"),
        CatalogEntry(frame_id="escape", path="../../evil.png"),
    ]))
    res = library.reconcile()
    assert res["catalog_entries_dropped"] == 2
    manifest = store.load_catalog(cid)
    assert [e.frame_id for e in manifest.entries] == ["real"]


def test_reconcile_clears_dangling_matted_pointer(creator, library):
    cid = _create(creator)
    store = creator.store
    frames = store.cache_frames_dir(cid)
    frames.mkdir(parents=True)
    (frames / "f.png").write_bytes(b"PNG")
    store.save_cache(CatalogManifest(character_id=cid, entries=[
        CatalogEntry(frame_id="f", path="cache/f.png",
                     matted_path="cache/matted/gone.png", on_demand=True),
    ]))
    library.reconcile()
    manifest = store.load_cache(cid)
    assert manifest.entries[0].matted_path is None
    assert manifest.entries[0].path == "cache/f.png"  # entry itself kept


def test_reconcile_runs_lru_cap(creator, library, settings):
    cid = _create(creator)
    _forge_cache(creator.store, cid, {
        "old": (5 * MB, "2026-01-01T00:00:00+00:00"),
        "new": (5 * MB, "2026-01-02T00:00:00+00:00"),
    })
    settings.set("library.cache_cap_bytes", 8 * MB, save=False)
    res = library.reconcile()
    assert res["cache_evicted"] == 1
    assert not (creator.store.cache_frames_dir(cid) / "old.png").exists()
    assert (creator.store.cache_frames_dir(cid) / "new.png").exists()


def test_reconcile_is_idempotent(creator, library):
    cid = _create(creator)
    cdir = creator.store.char_dir(cid)
    (cdir / "catalog.old").mkdir()
    (cdir / "catalog.old" / "x.png").write_bytes(b"PNG")
    first = library.reconcile()
    assert first["staging_dirs"] == 1
    second = library.reconcile()
    assert second["staging_dirs"] == 0
    assert second["bootstrap_orphans"] == 0
    assert second["cache_orphans"] == 0
    assert second["bytes_freed"] == 0


def test_reconcile_skips_unsafe_ids(library):
    # direct unit probe of the guard: an id that fails the store rules is
    # reported as skipped, never touched
    out = library._reconcile_character("../evil")
    assert out == {"id": "../evil", "skipped": "invalid_id"}


def test_reconcile_survives_a_per_character_fault(creator, library, audit,
                                                  monkeypatch):
    # a deep-fs fault on one character must never abort the whole sweep nor
    # escape the bridge (never-raise contract): it becomes a skipped entry.
    good = _create(creator, name="Good")
    bad = _create(creator, name="Boom")

    orig = library._reconcile_character

    def boom(cid):
        if cid == bad:
            raise OSError("simulated deep-fs fault")
        return orig(cid)

    monkeypatch.setattr(library, "_reconcile_character", boom)
    res = library.reconcile()
    assert res["ok"] is True
    assert any(s["id"] == bad and s["skipped"] == "error"
               for s in res["skipped"])
    assert any(e["kind"] == "library_sweep_failed" and e["character_id"] == bad
               for e in audit_events(audit))


def test_reconcile_leaves_catalog_manifest_alone_when_clean(creator, library):
    cid = _create(creator)
    store = creator.store
    frames = store.catalog_frames_dir(cid)
    frames.mkdir(parents=True)
    (frames / "f.png").write_bytes(b"PNG")
    store.save_catalog(CatalogManifest(
        character_id=cid,
        entries=[CatalogEntry(frame_id="f", path="catalog/f.png")],
        updated_at="2025-12-31T00:00:00+00:00"))
    library.reconcile()
    manifest = store.load_catalog(cid)
    # untouched: no gratuitous rewrite (the optimistic token survives)
    assert manifest.updated_at == "2025-12-31T00:00:00+00:00"


def test_nonfinite_slider_hand_edit_reads_as_corrupt(creator, library):
    # json.loads accepts Infinity; a record carrying one must never reach a
    # bridge payload (invalid strict JSON hangs the JS promise) — it reads
    # as a corrupt record instead, still deletable.
    cid = _create(creator)
    path = creator.store.record_path(cid)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sliders"] = {"height": float("inf")}
    path.write_text(json.dumps(data), encoding="utf-8")
    got = library.get_character(cid)
    assert got["ok"] is False and got["kind"] == "io"
    row = library.list_characters()["characters"][0]
    assert row["ok"] is False and row["kind"] == "io"
    assert library.delete_character(cid)["removed"] is True
