"""Stage-5 builder service (app/ui/builders.py) — the bridge doorway:
describe/create/update/get/delete + the reconcile sweep + the guarded loader."""

import json

import pytest

from app.ui.builders import BuilderService, build_builders, load_builder_guarded


@pytest.fixture()
def svc(builders) -> BuilderService:
    return builders


# -- describe ----------------------------------------------------------------

def test_describe_kinds(svc):
    d = svc.describe()
    assert set(d["kinds"]) == {"persona", "scene", "event", "scenario"}


def test_describe_scene_has_render_groups(svc):
    d = svc.describe("scene")
    gids = {g["id"] for g in d["groups"]}
    assert {"location", "time_of_day", "lighting", "weather"} <= gids
    assert d["free_text_fields"][0]["key"] == "setting_notes"


def test_describe_scenario_advertises_consent_from_code(svc):
    d = svc.describe("scenario")
    ids = [c["id"] for c in d["consent_frames"]]
    assert ids == ["enthusiastic", "established_relationship",
                   "negotiated_scene", "romantic"]


def test_describe_unknown_kind(svc):
    assert svc.describe("wombat")["ok"] is False


# -- create ------------------------------------------------------------------

def test_create_scene(svc):
    r = svc.create({"kind": "scene", "name": "Alley",
                    "selections": {"location": "city_street"},
                    "free_text": {"setting_notes": "neon puddles"}})
    assert r["ok"] and r["kind"] == "scene"


def test_create_scenario_requires_consent_at_doorway(svc):
    r = svc.create({"kind": "scenario", "name": "S"})
    assert not r["ok"] and r["field"] == "consent"


def test_create_scenario_rejects_unapproved_consent(svc):
    r = svc.create({"kind": "scenario", "name": "S", "consent": "coerced"})
    assert not r["ok"] and r["kind"] == "consent"


def test_create_scenario_with_approved_consent(svc):
    r = svc.create({"kind": "scenario", "name": "S", "consent": "romantic",
                    "selections": {"relationship": "partners"}})
    assert r["ok"]


def test_create_rejects_unknown_option(svc):
    r = svc.create({"kind": "scene", "name": "x",
                    "selections": {"location": "mars_base"}})
    assert not r["ok"] and r["kind"] == "invalid"


def test_create_rejects_foreign_free_text_field(svc):
    r = svc.create({"kind": "scene", "name": "x",
                    "free_text": {"backstory": "nope"}})
    assert not r["ok"] and "unknown free-text" in r["error"]


def test_create_blocked_name_audited(svc):
    r = svc.create({"kind": "persona", "name": "loli"})
    assert not r["ok"] and r["kind"] == "blocked"


# -- update ------------------------------------------------------------------

def test_update_keeps_kind_fixed(svc):
    created = svc.create({"kind": "scene", "name": "A",
                          "selections": {"location": "beach"}})
    updated = svc.update(created["id"],
                         {"kind": "scenario", "name": "A2", "consent": "romantic"})
    # the payload kind is ignored; a scene stays a scene
    assert updated["ok"] and updated["kind"] == "scene"


def test_update_preserves_id_and_created_at(svc):
    created = svc.create({"kind": "persona", "name": "P"})
    before = svc.get(created["id"])
    updated = svc.update(created["id"], {"name": "P2"})
    after = svc.get(updated["id"])
    assert after["id"] == before["id"]
    assert after["created_at"] == before["created_at"]
    assert after["name"] == "P2"


# -- get / delete ------------------------------------------------------------

def test_get_round_trips_to_form_shape(svc):
    created = svc.create({"kind": "scenario", "name": "S", "consent": "enthusiastic",
                          "selections": {"mood": "tense"},
                          "tags": {"themes": ["romance", "mystery"]}})
    got = svc.get(created["id"])
    assert got["consent"] == "enthusiastic"
    assert got["selections"]["mood"] == "tense"
    assert got["tags"]["themes"] == ["romance", "mystery"]


def test_delete_works_on_unloadable(svc):
    created = svc.create({"kind": "persona", "name": "ok"})
    # corrupt the stored file so it can't load
    svc.store.record_path(created["id"]).write_text(
        json.dumps({"name": "loli", "kind": "persona", "id": created["id"]}),
        encoding="utf-8")
    assert not load_builder_guarded(svc.store, svc._audit, created["id"]) \
        .__class__.__name__ == "BuilderRecord"  # it's an error dict
    assert svc.delete(created["id"])["ok"]


def test_list_filters_and_counts(svc):
    svc.create({"kind": "scene", "name": "S1"})
    svc.create({"kind": "persona", "name": "P1"})
    out = svc.list()
    kinds = sorted(b["kind"] for b in out["builders"] if b["ok"])
    assert kinds == ["persona", "scene"]


# -- reconcile ---------------------------------------------------------------

def test_reconcile_sweeps_unvouched_background_orphan(svc):
    created = svc.create({"kind": "scene", "name": "S"})
    bg = svc.store.background_dir(created["id"])
    bg.mkdir(parents=True)
    (bg / "bg-orphan.png").write_bytes(b"x" * 20)   # no manifest vouches for it
    out = svc.reconcile()
    assert out["orphans"] == 1
    assert not (bg / "bg-orphan.png").exists()


def test_reconcile_keeps_vouched_frame(svc):
    from app.model import BackgroundEntry, BackgroundManifest
    created = svc.create({"kind": "scene", "name": "S"})
    bg = svc.store.background_dir(created["id"])
    bg.mkdir(parents=True)
    (bg / "keep.png").write_bytes(b"x")
    m = BackgroundManifest(builder_id=created["id"])
    m.entries.append(BackgroundEntry(frame_id="keep", path="background/keep.png"))
    svc.store.save_background(m)
    svc.reconcile()
    assert (bg / "keep.png").exists()      # vouched -> survives


def test_reconcile_corrupt_manifest_sweeps_nothing(svc):
    created = svc.create({"kind": "scene", "name": "S"})
    bg = svc.store.background_dir(created["id"])
    bg.mkdir(parents=True)
    (bg / "x.png").write_bytes(b"x")
    svc.store.background_path(created["id"]).write_text("{ not json", encoding="utf-8")
    svc.reconcile()
    assert (bg / "x.png").exists()         # unprovable orphanhood -> untouched


# -- factory -----------------------------------------------------------------

def test_build_builders_factory(tmp_path, audit):
    svc = build_builders(tmp_path / "data", audit)
    assert svc.describe("scene")["ok"]
