"""The JS↔Python bridge, tested headless (no window). The window itself is
exercised by launching the app; these tests pin the bridge's contract."""

import json
import time

import pytest

from app.ui.shell import WEB_DIR, Api

# The render-identity minimum every character now needs (5.5c). Bridge setup
# creates route through make_char() so a quick character constructs.
SEL = {"race": "human", "gender_presentation": "feminine", "skin_tone": "fair",
       "hair_color": "black", "hair_style": "short", "eye_color": "brown",
       "body_type": "average"}


def make_char(api, name, age=24, **over):
    """Create a quick character through the bridge, carrying the required set
    (a `selections` override merges on top). Returns the create result."""
    sel = dict(SEL)
    sel.update(over.pop("selections", {}))
    payload = {"mode": "quick", "name": name, "age": age, "selections": sel}
    payload.update(over)
    return api.create_character(payload)


@pytest.fixture()
def api(settings, audit, content_filter, creator, images, library, builders) -> Api:
    return Api(settings, audit, content_filter, creator, images, library,
               builders)


def test_ping(api):
    assert api.ping() == "pong"


def test_app_info_shape(api):
    info = api.app_info()
    assert info["version"]
    assert "Stage 5" in info["stage"]
    assert info["settings_path"].endswith("settings.json")


def test_get_settings_round_trip(api):
    s = api.get_settings()
    assert s["models"]["image"]["variant"] == "default"


def test_set_setting_valid_persists(api, settings):
    res = api.set_setting("models.image.variant", "heavy")
    assert res == {"ok": True, "error": None}
    assert settings.get("models.image.variant") == "heavy"
    # persisted, not just in memory
    on_disk = json.loads(settings.path.read_text(encoding="utf-8"))
    assert on_disk["models"]["image"]["variant"] == "heavy"


def test_set_setting_rejects_unknown_key(api, settings):
    res = api.set_setting("models.image.checkpoint_path", "C:/evil")
    assert res["ok"] is False
    assert settings.get("models.image.checkpoint_path") is None


def test_set_setting_rejects_bad_value(api, settings):
    res = api.set_setting("models.image.variant", "ultra")
    assert res["ok"] is False
    assert settings.get("models.image.variant") == "default"


def test_logging_toggle_syncs_audit(api, audit):
    api.set_setting("safety.logging_enabled", False)
    assert audit.enabled is False
    api.set_setting("safety.logging_enabled", True)
    assert audit.enabled is True


def test_content_gate_settable_from_ui_and_type_guarded(api, settings):
    # 5.6a: the Settings checkbox writes content.gate_open through the bridge
    # (it must be whitelisted), booleans only — True==1 in Python, so a
    # numeric 1 is rejected by the type guard, not coerced.
    res = api.set_setting("content.gate_open", False)
    assert res == {"ok": True, "error": None}
    assert settings.get("content.gate_open") is False
    on_disk = json.loads(settings.path.read_text(encoding="utf-8"))
    assert on_disk["content"]["gate_open"] is False

    res = api.set_setting("content.gate_open", True)
    assert res == {"ok": True, "error": None}
    assert settings.get("content.gate_open") is True

    res = api.set_setting("content.gate_open", 1)
    assert res["ok"] is False
    assert settings.get("content.gate_open") is True  # unchanged


def test_check_text_allowed(api):
    res = api.check_text("A quiet evening among adults.", "freetext")
    assert res["allowed"] is True


def test_check_text_blocked_and_audited(api, audit):
    res = api.check_text("loli", "prompt")
    assert res["allowed"] is False
    assert res["category"] == "minors"
    lines = audit.path_for_today().read_text(encoding="utf-8").splitlines()
    events = [json.loads(l) for l in lines]
    assert any(e["kind"] == "filter_block" and e["category"] == "minors" for e in events)


def test_check_text_bad_context_is_safe(api):
    res = api.check_text("hello", "not-a-context")
    assert res["allowed"] is False
    assert res["category"] == "error"


def test_web_assets_exist():
    assert (WEB_DIR / "index.html").exists()
    assert (WEB_DIR / "app.css").exists()
    assert (WEB_DIR / "app.js").exists()
    assert (WEB_DIR / "creator.js").exists()


# -- creator bridge (Stage 2) --------------------------------------------


def test_creator_catalog_via_bridge(api):
    cat = api.creator_catalog()
    assert any(g["id"] == "race" for g in cat["groups"])
    assert cat["min_age"] == 20
    assert [f["key"] for f in cat["free_text_fields"]]
    # 5.5c: the required-selection set + derived widgets ride the same payload
    assert "gender_presentation" in cat["required_groups"]
    race = next(g for g in cat["groups"] if g["id"] == "race")
    assert race["widget"] == "picker" and race["required"] is True


def test_create_character_via_bridge(api, creator):
    res = make_char(api, "Bridge Test", 25, selections={"race": "elf"})
    assert res["ok"] is True
    assert creator.store.exists(res["id"])


def test_create_character_missing_required_via_bridge(api):
    # a quick create without the render-identity minimum is rejected (5.5c)
    res = api.create_character({"mode": "quick", "name": "Bare", "age": 25})
    assert res["ok"] is False and res["kind"] == "required"


def test_create_character_bridge_rejects_non_dict(api):
    res = api.create_character("not a dict")
    assert res["ok"] is False
    assert res["kind"] == "invalid"


def test_creator_reload_options_via_bridge(api):
    cat = api.creator_reload_options()
    assert any(g["id"] == "race" for g in cat["groups"])


# -- image pipeline bridge (Stage 3a) --------------------------------------


def test_image_engine_status_via_bridge(api):
    status = api.image_engine_status()
    assert status["loaded"] is False
    assert status["checkpoint"] is None  # sandbox: nothing configured
    assert status["generation"]["sampler"] == "euler_a"


def test_image_prompt_preview_via_bridge(api):
    created = make_char(api, "Render Probe", selections={"race": "elf"})
    assert created["ok"] is True
    res = api.image_prompt_preview(created["id"])
    assert res["ok"] is True
    assert "solo, 1girl" in res["positive"]
    assert "elf, pointed ears" in res["positive"]
    assert "loli" in res["negative"]
    # 5.5b: token accounting rides the same bridge — structured-unavailable on
    # the sandbox (no pipeline_config_dir set), never a raise.
    assert res["tokens"]["available"] is False


def test_creator_prompt_preview_partial_form(api, creator):
    # 5.5: the live panel previews the IN-PROGRESS form — a partial payload
    # (no name, only one selection, nothing near the required set) assembles;
    # NOTHING is persisted.
    before = len(creator.store.list_ids())
    res = api.creator_prompt_preview({
        "mode": "detailed", "age": 25,
        "selections": {"race": "elf"}, "tags": {}, "sliders": {},
        "free_text": {},
    })
    assert res["ok"] is True and res.get("preview") is True
    assert "elf, pointed ears" in res["positive"]
    assert "loli" in res["negative"]          # Layer-2 negatives unchanged
    assert res["id"] is None                  # transient — no record id
    assert len(creator.store.list_ids()) == before  # nothing saved


def test_creator_prompt_preview_gates_still_run(api, creator):
    # the required-selection gate is OFF for preview, but age (Layer 3) and
    # the Layer-1 content gates are NOT.
    res = api.creator_prompt_preview({"age": 17, "selections": {"race": "elf"}})
    assert res["ok"] is False and res["kind"] == "age"
    res2 = api.creator_prompt_preview({
        "age": 25, "selections": {"race": "elf"},
        "free_text": {"appearance_notes": "a young loli girl"},
    })
    assert res2["ok"] is False and res2["kind"] == "blocked"
    before = len(creator.store.list_ids())
    res3 = api.creator_prompt_preview("not a dict")
    assert res3["ok"] is False and res3["kind"] == "invalid"
    assert len(creator.store.list_ids()) == before


def test_image_generate_base_via_bridge_reports_engine_unavailable(api):
    created = make_char(api, "Render Probe Two")
    res = api.image_generate_base(created["id"])
    assert res["ok"] is False
    assert res["kind"] == "engine"  # sandbox: no checkpoint/GPU — structured


def test_image_engine_release_via_bridge(api):
    res = api.image_engine_release()
    assert res["ok"] is True and res["loaded"] is False


# -- identity reference + steered generation bridge (Stage 3b) -------------


def test_image_reference_status_via_bridge(api):
    created = make_char(api, "Ref Bridge")
    res = api.image_reference_status(created["id"])
    assert res["ok"] is True and res["has_reference"] is False


def test_image_set_and_clear_reference_via_bridge(api, creator):
    created = make_char(api, "Ref Set Bridge")
    # no real engine wired on the api fixture's service -> generate_base gives a
    # structured engine error on the sandbox; forge a frame directly instead.
    cid = created["id"]
    frame = creator.store.char_dir(cid) / "reference" / "base-1.png"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b"FAKEPNG")
    res = api.image_set_reference(cid, str(frame))
    assert res["ok"] is True and res["reference"] == "reference/base-1.png"
    assert api.image_reference_status(cid)["has_reference"] is True
    cleared = api.image_clear_reference(cid)
    assert cleared["ok"] is True
    assert api.image_reference_status(cid)["has_reference"] is False


def test_image_set_reference_rejects_traversal_via_bridge(api, creator):
    created = make_char(api, "Ref Evil Bridge")
    res = api.image_set_reference(created["id"], "../../secret.png")
    assert res["ok"] is False and res["kind"] == "reference_invalid"


def test_image_generate_identity_via_bridge_no_reference(api):
    created = make_char(api, "Ident Bridge")
    res = api.image_generate_identity(created["id"])
    assert res["ok"] is False and res["kind"] == "no_reference"


# -- identity bootstrap bridge (Stage 3c) ----------------------------------


def test_image_bootstrap_status_via_bridge(api):
    created = make_char(api, "Boot Status")
    res = api.image_bootstrap_status(created["id"])
    assert res["ok"] is True
    assert res["phase"] is None and res["has_vetted"] is False


def test_image_bootstrap_generate_via_bridge_no_reference(api):
    created = make_char(api, "Boot Gen")
    res = api.image_bootstrap_generate(created["id"], 4)
    assert res["ok"] is False and res["kind"] == "no_reference"


def test_image_bootstrap_recull_and_confirm_without_bootstrap(api):
    created = make_char(api, "Boot None")
    cid = created["id"]
    assert api.image_bootstrap_recull(cid)["kind"] == "no_bootstrap"
    assert api.image_confirm_vetted(cid, ["x"])["kind"] == "no_bootstrap"


def test_image_clear_bootstrap_via_bridge(api):
    created = make_char(api, "Boot Clear")
    res = api.image_clear_bootstrap(created["id"], "all")
    assert res["ok"] is True and res["scope"] == "all"


# -- identity LoRA bridge (Stage 3d) ---------------------------------------


def test_image_lora_status_via_bridge(api):
    created = make_char(api, "Lora Status")
    res = api.image_lora_status(created["id"])
    assert res["ok"] is True and res["has_lora"] is False


def test_image_train_lora_via_bridge_no_vetted(api):
    created = make_char(api, "Lora Train")
    res = api.image_train_lora(created["id"])
    assert res["ok"] is False and res["kind"] == "no_vetted"


def test_image_clear_lora_via_bridge(api):
    created = make_char(api, "Lora Clear")
    res = api.image_clear_lora(created["id"])
    assert res["ok"] is True and res["removed"] is False


# -- seed catalog bridge (Stage 3e) ----------------------------------------


def test_image_catalog_status_via_bridge(api):
    created = make_char(api, "Cat Status")
    res = api.image_catalog_status(created["id"])
    assert res["ok"] is True and res["has_catalog"] is False and res["frames"] == 0


def test_image_generate_catalog_via_bridge_no_lora(api):
    created = make_char(api, "Cat Gen")
    res = api.image_generate_catalog(created["id"])
    assert res["ok"] is False and res["kind"] == "no_lora"


def test_image_clear_catalog_via_bridge(api):
    created = make_char(api, "Cat Clear")
    res = api.image_clear_catalog(created["id"])
    assert res["ok"] is True and res["removed"] is False


# -- matting bridge (Stage 3f) ----------------------------------------------


def test_image_matte_status_via_bridge(api):
    created = make_char(api, "Matte Status")
    res = api.image_matte_status(created["id"])
    assert res["ok"] is True and res["has_catalog"] is False
    assert res["ready"] is False and res["missing"] == "matting_model_missing"


def test_image_matte_catalog_via_bridge_no_catalog(api):
    created = make_char(api, "Matte Gen")
    res = api.image_matte_catalog(created["id"])
    assert res["ok"] is False and res["kind"] == "no_catalog"


def test_image_matte_bridges_default_args(api):
    # the bridge defaults (character_id=None) must map to structured invalid
    assert api.image_matte_catalog()["kind"] == "invalid"
    assert api.image_matte_status()["kind"] == "invalid"


# -- on-demand cache bridge (Stage 3g) ----------------------------------------


def test_image_cache_status_via_bridge(api):
    created = make_char(api, "Cache Status")
    res = api.image_cache_status(created["id"])
    assert res["ok"] is True and res["has_cache"] is False and res["frames"] == 0
    assert res["matte_ready"] is False


def test_image_generate_on_demand_via_bridge(api):
    created = make_char(api, "Cache Gen")
    # malformed state is structured invalid; a valid novel state on an
    # unpromoted character is structured no_lora — no traceback either way
    res = api.image_generate_on_demand(created["id"], "not-a-dict")
    assert res["ok"] is False and res["kind"] == "invalid"
    res = api.image_generate_on_demand(
        created["id"],
        {"expression": "smile", "pose": "sitting", "outfit": "asis"})
    assert res["ok"] is False and res["kind"] == "no_lora"


def test_image_clear_cache_via_bridge(api):
    created = make_char(api, "Cache Clear")
    res = api.image_clear_cache(created["id"])
    assert res["ok"] is True and res["removed"] is False


def test_image_cache_bridges_default_args(api):
    assert api.image_generate_on_demand()["kind"] == "invalid"
    assert api.image_cache_status()["kind"] == "invalid"
    assert api.image_clear_cache()["kind"] == "invalid"


# -- library & management bridge (Stage 4) ----------------------------------


def test_library_list_via_bridge(api):
    res = api.library_list()
    assert res["ok"] is True and res["characters"] == []
    created = make_char(api, "Lib Bridge")
    res = api.library_list()
    assert res["count"] == 1
    row = res["characters"][0]
    assert row["id"] == created["id"] and row["name"] == "Lib Bridge"
    assert row["footprint"]["total_bytes"] == 0


def test_library_get_and_update_via_bridge(api, creator):
    created = make_char(api, "Lib Edit", selections={"race": "elf"})
    got = api.library_get(created["id"])
    assert got["ok"] is True and got["selections"] == {**SEL, "race": "elf"}
    # the edit re-runs the required gate, so it must carry the required set
    res = api.library_update(
        created["id"], {"name": "Lib Edited", "age": 26, "selections": dict(SEL)})
    assert res["ok"] is True and res["name"] == "Lib Edited"
    assert creator.store.load(created["id"]).name == "Lib Edited"


def test_library_delete_via_bridge(api, creator):
    created = make_char(api, "Lib Doomed")
    res = api.library_delete(created["id"])
    assert res["ok"] is True and res["removed"] is True
    assert not creator.store.exists(created["id"])


def test_library_thumbnail_via_bridge(api):
    created = make_char(api, "Lib Thumb")
    res = api.library_thumbnail(created["id"])
    assert res["ok"] is True and res["thumbnail"] is None


def test_library_reconcile_via_bridge(api):
    res = api.library_reconcile()
    assert res["ok"] is True and res["staging_dirs"] == 0


def test_library_bridges_default_args(api):
    assert api.library_get()["kind"] == "invalid"
    assert api.library_update()["kind"] == "invalid"
    assert api.library_delete()["kind"] == "invalid"
    assert api.library_thumbnail()["kind"] == "invalid"


def test_web_assets_include_library():
    assert (WEB_DIR / "library.js").exists()


def test_web_assets_include_jobs_and_profile():
    # 5.5d front-end: the job client + the character profile view.
    assert (WEB_DIR / "jobs.js").exists()
    assert (WEB_DIR / "profile.js").exists()
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'src="jobs.js"' in html and 'src="profile.js"' in html
    assert 'id="view-profile"' in html


# -- Stage-5 builder + scene + compositing bridges --------------------------

def test_builder_describe_bridge(api):
    d = api.builder_describe("scenario")
    assert d["ok"] and "consent_frames" in d


def test_builder_create_and_list_bridge(api):
    r = api.builder_create({"kind": "scene", "name": "Bridge scene",
                            "selections": {"location": "beach"}})
    assert r["ok"]
    listed = api.builder_list()
    assert any(b.get("id") == r["id"] for b in listed["builders"])


def test_builder_scenario_consent_gate_via_bridge(api):
    assert api.builder_create({"kind": "scenario", "name": "S"})["ok"] is False


def test_builder_get_update_delete_bridge(api):
    created = api.builder_create({"kind": "persona", "name": "P"})
    assert api.builder_get(created["id"])["ok"]
    assert api.builder_update(created["id"], {"name": "P2"})["ok"]
    assert api.builder_delete(created["id"])["ok"]


def test_builder_bridges_default_args(api):
    assert api.builder_get()["kind"] == "invalid"
    assert api.builder_update()["kind"] == "invalid"
    assert api.builder_delete()["kind"] == "invalid"


def test_builder_reconcile_bridge(api):
    res = api.builder_reconcile()
    assert res["ok"] is True and res["orphans"] == 0


def test_scene_background_bridges_structured_on_sandbox(api):
    scene = api.builder_create({"kind": "scene", "name": "S"})
    # background generation needs the model — a structured error, never a
    # traceback (engine or classifier unavailable in the sandbox).
    gen = api.scene_generate_background(scene["id"])
    assert gen["ok"] is False and "kind" in gen
    assert api.scene_background_status(scene["id"])["ok"] is True
    assert api.scene_clear_background(scene["id"])["ok"] is True


def test_image_composite_bridge_missing_character(api):
    res = api.image_composite()
    assert res["ok"] is False and res["kind"] in ("invalid", "not_found")


def test_image_matted_frames_bridge(api):
    assert api.image_matted_frames()["kind"] == "invalid"


def test_web_assets_include_builders():
    assert (WEB_DIR / "builders.js").exists()


# -- long-running jobs (Stage 5.5a) ------------------------------------------


def test_job_submit_unknown_kind_is_structured(api):
    res = api.job_submit("not-a-kind", "some-id")
    assert res["ok"] is False and res["kind"] == "job"
    assert res["reason"] == "invalid"


def test_job_status_unknown_id_is_structured(api):
    res = api.job_status("0" * 32)
    assert res["ok"] is False and res["kind"] == "job"


def test_job_cancel_unknown_id_is_structured(api):
    res = api.job_cancel("nope")
    assert res["ok"] is False and res["kind"] == "job"


def test_job_list_shape(api):
    res = api.job_list()
    assert res["ok"] is True and res["kind"] == "job"
    assert isinstance(res["jobs"], list)


def test_catalog_job_runs_through_the_bridge_and_reports(api):
    # End-to-end bridge path: submit -> poll -> terminal. On the sandbox a
    # catalog needs a trained LoRA, so it fails fast with a structured result
    # (no GPU needed) — proving the job survives, reports, and is JSON-safe.
    created = make_char(api, "Job Probe")
    sub = api.job_submit("catalog", created["id"])
    assert sub["ok"] is True and "job_id" in sub
    deadline = time.time() + 5
    status = api.job_status(sub["job_id"])
    while status.get("status") not in ("done", "cancelled", "error") and time.time() < deadline:
        time.sleep(0.02)
        status = api.job_status(sub["job_id"])
    assert status["status"] == "error"           # no_lora on a fresh character
    assert status["result"]["kind"] == "no_lora"
    json.dumps(status, allow_nan=False)          # never NaN/Infinity


def test_job_bridges_never_emit_nonfinite(api):
    created = make_char(api, "Job JSON")
    sub = api.job_submit("on_demand", created["id"], {"state": {"pose": "standing"}})
    json.dumps(api.job_status(sub["job_id"]), allow_nan=False)
    json.dumps(api.job_list(), allow_nan=False)


def test_avatar_job_runs_through_the_bridge(api):
    # 5.5d create-wizard reference step: N base candidates as a job. On the
    # sandbox (no GPU) it terminates with a structured engine result — proving
    # the avatar kind dispatches and the job survives + reports.
    created = make_char(api, "Avatar Probe")
    sub = api.job_submit("avatar", created["id"], {"count": 3})
    assert sub["ok"] is True and "job_id" in sub
    deadline = time.time() + 5
    status = api.job_status(sub["job_id"])
    while status.get("status") not in ("done", "cancelled", "error") \
            and time.time() < deadline:
        time.sleep(0.02)
        status = api.job_status(sub["job_id"])
    assert status["status"] == "error"
    assert status["result"]["kind"] == "engine"   # engine-unavailable, no CUDA
    json.dumps(status, allow_nan=False)


def test_identity_job_dispatches_through_the_bridge(api):
    created = make_char(api, "Identity Job")
    sub = api.job_submit("identity", created["id"], {"scale": 0.45})
    assert sub["ok"] is True and "job_id" in sub
    deadline = time.time() + 5
    status = api.job_status(sub["job_id"])
    while status.get("status") not in ("done", "cancelled", "error") \
            and time.time() < deadline:
        time.sleep(0.02)
        status = api.job_status(sub["job_id"])
    assert status["status"] == "error"
    # no reference set on a fresh character -> structured no_reference
    assert status["result"]["kind"] == "no_reference"
    json.dumps(status, allow_nan=False)


def test_image_frame_thumbnail_via_bridge(api):
    created = make_char(api, "Thumb Probe")
    # no frame yet -> thumbnail None, never an error
    res = api.image_frame_thumbnail(created["id"], "reference/nope.png")
    assert res["ok"] is True and res["thumbnail"] is None
    # unknown character is a structured not_found
    assert api.image_frame_thumbnail("ghost", "x.png")["kind"] == "not_found"
    # default args never raise
    assert api.image_frame_thumbnail()["kind"] == "invalid"
