import json

from app.config import DEFAULTS, Settings


def test_first_load_creates_file_with_defaults(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(path)
    assert path.exists()
    assert s.get("models.image.variant") == "default"
    assert s.get("safety.logging_enabled") is True


def test_set_persists_across_reload(tmp_path):
    path = tmp_path / "settings.json"
    s1 = Settings(path)
    s1.set("models.chat.variant", "heavy")
    s2 = Settings(path)
    assert s2.get("models.chat.variant") == "heavy"


def test_defaults_merge_adds_new_keys(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"models": {"image": {"variant": "heavy"}}}), encoding="utf-8")
    s = Settings(path)
    # user value preserved
    assert s.get("models.image.variant") == "heavy"
    # keys absent on disk arrive from defaults
    assert s.get("models.chat.variant") == "default"
    assert s.get("safety.logging_enabled") is True


def test_corrupt_file_backed_up_and_defaults_used(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    s = Settings(path)
    assert s.get("models.image.variant") == "default"
    assert (tmp_path / "settings.json.corrupt").exists()
    # and a fresh valid file was written
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_atomic_write_leaves_valid_json(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(path)
    for i in range(25):
        s.set("window.width", 1000 + i)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["window"]["width"] == 1024
    assert not (tmp_path / "settings.json.tmp").exists()


def test_get_missing_returns_default(tmp_path):
    s = Settings(tmp_path / "settings.json")
    assert s.get("no.such.key") is None
    assert s.get("no.such.key", 42) == 42


def test_as_dict_is_a_copy(tmp_path):
    s = Settings(tmp_path / "settings.json")
    d = s.as_dict()
    d["models"]["image"]["variant"] = "mutated"
    assert s.get("models.image.variant") == "default"


def test_defaults_shape_contains_swap_scaffold():
    assert set(DEFAULTS["models"].keys()) == {"active", "image", "chat"}
    assert DEFAULTS["models"]["active"] is None


def test_content_gate_ships_open_and_deep_merges(tmp_path):
    # 5.6a: the content gate defaults OPEN (user decision 2026-07-16) and
    # arrives via deep-merge on a pre-5.6 settings.json with no migration.
    assert DEFAULTS["content"]["gate_open"] is True
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"safety": {"logging_enabled": False}}),
                    encoding="utf-8")
    s = Settings(path)
    assert s.get("content.gate_open") is True
    s.set("content.gate_open", False)
    assert Settings(path).get("content.gate_open") is False
