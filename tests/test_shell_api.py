"""The JS↔Python bridge, tested headless (no window). The window itself is
exercised by launching the app; these tests pin the bridge's contract."""

import json

import pytest

from app.ui.shell import WEB_DIR, Api


@pytest.fixture()
def api(settings, audit, content_filter, creator, images) -> Api:
    return Api(settings, audit, content_filter, creator, images)


def test_ping(api):
    assert api.ping() == "pong"


def test_app_info_shape(api):
    info = api.app_info()
    assert info["version"]
    assert "Stage 3" in info["stage"]
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


def test_create_character_via_bridge(api, creator):
    res = api.create_character(
        {"mode": "quick", "name": "Bridge Test", "age": 25,
         "selections": {"race": "elf"}}
    )
    assert res["ok"] is True
    assert creator.store.exists(res["id"])


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
    created = api.create_character(
        {"mode": "quick", "name": "Render Probe", "age": 24,
         "selections": {"race": "elf", "gender_presentation": "feminine"}}
    )
    assert created["ok"] is True
    res = api.image_prompt_preview(created["id"])
    assert res["ok"] is True
    assert "solo, 1girl" in res["positive"]
    assert "elf, pointed ears" in res["positive"]
    assert "loli" in res["negative"]


def test_image_generate_base_via_bridge_reports_engine_unavailable(api):
    created = api.create_character(
        {"mode": "quick", "name": "Render Probe Two", "age": 24}
    )
    res = api.image_generate_base(created["id"])
    assert res["ok"] is False
    assert res["kind"] == "engine"  # sandbox: no checkpoint/GPU — structured


def test_image_engine_release_via_bridge(api):
    res = api.image_engine_release()
    assert res["ok"] is True and res["loaded"] is False


# -- identity reference + steered generation bridge (Stage 3b) -------------


def test_image_reference_status_via_bridge(api):
    created = api.create_character(
        {"mode": "quick", "name": "Ref Bridge", "age": 24}
    )
    res = api.image_reference_status(created["id"])
    assert res["ok"] is True and res["has_reference"] is False


def test_image_set_and_clear_reference_via_bridge(api, creator):
    created = api.create_character(
        {"mode": "quick", "name": "Ref Set Bridge", "age": 24}
    )
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
    created = api.create_character(
        {"mode": "quick", "name": "Ref Evil Bridge", "age": 24}
    )
    res = api.image_set_reference(created["id"], "../../secret.png")
    assert res["ok"] is False and res["kind"] == "reference_invalid"


def test_image_generate_identity_via_bridge_no_reference(api):
    created = api.create_character(
        {"mode": "quick", "name": "Ident Bridge", "age": 24}
    )
    res = api.image_generate_identity(created["id"])
    assert res["ok"] is False and res["kind"] == "no_reference"


# -- identity bootstrap bridge (Stage 3c) ----------------------------------


def test_image_bootstrap_status_via_bridge(api):
    created = api.create_character(
        {"mode": "quick", "name": "Boot Status", "age": 24}
    )
    res = api.image_bootstrap_status(created["id"])
    assert res["ok"] is True
    assert res["phase"] is None and res["has_vetted"] is False


def test_image_bootstrap_generate_via_bridge_no_reference(api):
    created = api.create_character(
        {"mode": "quick", "name": "Boot Gen", "age": 24}
    )
    res = api.image_bootstrap_generate(created["id"], 4)
    assert res["ok"] is False and res["kind"] == "no_reference"


def test_image_bootstrap_recull_and_confirm_without_bootstrap(api):
    created = api.create_character(
        {"mode": "quick", "name": "Boot None", "age": 24}
    )
    cid = created["id"]
    assert api.image_bootstrap_recull(cid)["kind"] == "no_bootstrap"
    assert api.image_confirm_vetted(cid, ["x"])["kind"] == "no_bootstrap"


def test_image_clear_bootstrap_via_bridge(api):
    created = api.create_character(
        {"mode": "quick", "name": "Boot Clear", "age": 24}
    )
    res = api.image_clear_bootstrap(created["id"], "all")
    assert res["ok"] is True and res["scope"] == "all"


# -- identity LoRA bridge (Stage 3d) ---------------------------------------


def test_image_lora_status_via_bridge(api):
    created = api.create_character(
        {"mode": "quick", "name": "Lora Status", "age": 24}
    )
    res = api.image_lora_status(created["id"])
    assert res["ok"] is True and res["has_lora"] is False


def test_image_train_lora_via_bridge_no_vetted(api):
    created = api.create_character(
        {"mode": "quick", "name": "Lora Train", "age": 24}
    )
    res = api.image_train_lora(created["id"])
    assert res["ok"] is False and res["kind"] == "no_vetted"


def test_image_clear_lora_via_bridge(api):
    created = api.create_character(
        {"mode": "quick", "name": "Lora Clear", "age": 24}
    )
    res = api.image_clear_lora(created["id"])
    assert res["ok"] is True and res["removed"] is False


# -- seed catalog bridge (Stage 3e) ----------------------------------------


def test_image_catalog_status_via_bridge(api):
    created = api.create_character(
        {"mode": "quick", "name": "Cat Status", "age": 24}
    )
    res = api.image_catalog_status(created["id"])
    assert res["ok"] is True and res["has_catalog"] is False and res["frames"] == 0


def test_image_generate_catalog_via_bridge_no_lora(api):
    created = api.create_character(
        {"mode": "quick", "name": "Cat Gen", "age": 24}
    )
    res = api.image_generate_catalog(created["id"])
    assert res["ok"] is False and res["kind"] == "no_lora"


def test_image_clear_catalog_via_bridge(api):
    created = api.create_character(
        {"mode": "quick", "name": "Cat Clear", "age": 24}
    )
    res = api.image_clear_catalog(created["id"])
    assert res["ok"] is True and res["removed"] is False


# -- matting bridge (Stage 3f) ----------------------------------------------


def test_image_matte_status_via_bridge(api):
    created = api.create_character(
        {"mode": "quick", "name": "Matte Status", "age": 24}
    )
    res = api.image_matte_status(created["id"])
    assert res["ok"] is True and res["has_catalog"] is False
    assert res["ready"] is False and res["missing"] == "matting_model_missing"


def test_image_matte_catalog_via_bridge_no_catalog(api):
    created = api.create_character(
        {"mode": "quick", "name": "Matte Gen", "age": 24}
    )
    res = api.image_matte_catalog(created["id"])
    assert res["ok"] is False and res["kind"] == "no_catalog"


def test_image_matte_bridges_default_args(api):
    # the bridge defaults (character_id=None) must map to structured invalid
    assert api.image_matte_catalog()["kind"] == "invalid"
    assert api.image_matte_status()["kind"] == "invalid"
