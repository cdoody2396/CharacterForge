"""Stage 3d — the sandbox-verifiable core: the LoRA manifest, training-config
coercion, dataset preparation, preflight, and store helpers. No torch — the
training weight lives in the (faked) subprocess trainer."""

import json

import pytest

from app.config import Settings
from app.imagegen.lora import (
    TrainConfig,
    TrainItem,
    build_dataset,
    coerce_train_config,
    preflight_train,
)
from app.model import CharacterStore, LoraManifest


# -- LoraManifest ------------------------------------------------------------


def test_lora_manifest_round_trip():
    m = LoraManifest(
        character_id="abc", trigger="cfid12345678",
        lora_file="lora/identity.safetensors", base_checkpoint="wai.safetensors",
        base_checkpoint_bytes=123, network_dim=16, network_alpha=8.0, steps=1600,
        resolution=1024, learning_rate=1e-4, dataset_size=20, lora_bytes=999)
    restored = LoraManifest.from_dict(json.loads(json.dumps(m.to_dict())))
    assert restored.trigger == "cfid12345678"
    assert restored.lora_file == "lora/identity.safetensors"
    assert restored.network_dim == 16 and restored.dataset_size == 20


def test_lora_manifest_id_confined():
    from app.model.character import InvalidId
    with pytest.raises(InvalidId):
        LoraManifest(character_id="../escape", trigger="t", lora_file="lora/x")


# -- config coercion ---------------------------------------------------------


def test_coerce_train_config_defaults(tmp_path):
    cfg = coerce_train_config(Settings(tmp_path / "s.json"))
    assert cfg.network_dim == 16 and cfg.max_train_steps == 1600
    assert cfg.resolution == 1024 and cfg.mixed_precision == "fp16"


def test_coerce_train_config_survives_and_clamps_bad_hand_edits(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"image_gen": {"lora_train": {
        "network_dim": "big", "max_train_steps": 999_999, "timeout_seconds": 1e999,
        "learning_rate": "NaN", "resolution": -5, "mixed_precision": "int4",
        "optimizer": "Nonsense",
    }}}), encoding="utf-8")
    cfg = coerce_train_config(Settings(path))          # must not raise
    assert cfg.network_dim == 16                        # "big" -> default
    assert cfg.max_train_steps == 100_000               # 999999 -> clamped to hi
    assert cfg.timeout_seconds == 21600                 # inf (non-finite) -> default
    assert cfg.learning_rate == 1e-4                    # NaN -> default
    assert cfg.resolution == 256                        # -5 -> clamped to lo
    assert cfg.mixed_precision == "fp16"                # unknown -> default
    assert cfg.optimizer == "AdamW8bit"                 # unknown -> default


# -- preflight ---------------------------------------------------------------


def test_preflight_train_reports_missing_trainer(tmp_path):
    s = Settings(tmp_path / "s.json")
    assert preflight_train(s) == "trainer_unavailable"
    trainer = tmp_path / "sd-scripts"
    trainer.mkdir()
    s.set("models.image.lora_trainer_dir", str(trainer))
    assert preflight_train(s) == "trainer_unavailable"  # dir but no script
    (trainer / "sdxl_train_network.py").write_text("# kohya", encoding="utf-8")
    assert preflight_train(s) is None


# -- dataset preparation -----------------------------------------------------


def test_build_dataset_lays_out_kohya_folder(tmp_path):
    # three fake vetted images + a caption
    imgs = []
    for i in range(3):
        p = tmp_path / f"vetted-{i}.png"
        p.write_bytes(b"PNG" + str(i).encode())
        imgs.append(p)
    caption = "cfid12345678, elf, pointed ears, silver hair, adult"
    items = [TrainItem(image_path=p, caption=caption) for p in imgs]
    dataset = tmp_path / "dataset"
    count = build_dataset(dataset, items, TrainConfig(repeats=10))
    assert count == 3
    concept = dataset / "10_identity"          # <repeats>_identity
    assert concept.is_dir()
    pngs = sorted(concept.glob("img-*.png"))
    txts = sorted(concept.glob("img-*.txt"))
    assert len(pngs) == 3 and len(txts) == 3
    assert txts[0].read_text(encoding="utf-8") == caption
    assert pngs[0].read_bytes().startswith(b"PNG")


# -- store helpers -----------------------------------------------------------


def test_store_lora_manifest_round_trip_and_absent(tmp_path):
    store = CharacterStore(tmp_path)
    assert store.load_lora_manifest("cid") is None
    store.save_lora_manifest(LoraManifest(
        character_id="cid", trigger="cfidcid", lora_file="lora/identity.safetensors"))
    assert store.load_lora_manifest("cid").trigger == "cfidcid"
    assert store.lora_dir("cid") == store.char_dir("cid") / "lora"
    assert store.lora_dataset_dir("cid") == store.char_dir("cid") / "lora" / "dataset"


def test_store_clear_lora(tmp_path):
    store = CharacterStore(tmp_path)
    store.lora_dir("cid").mkdir(parents=True)
    (store.lora_dir("cid") / "identity.safetensors").write_bytes(b"x")
    assert store.clear_lora("cid") is True
    assert not store.lora_dir("cid").exists()
    assert store.clear_lora("cid") is False  # already gone


def test_store_lora_paths_reject_crafted_ids(tmp_path):
    from app.model.character import InvalidId
    store = CharacterStore(tmp_path)
    for bad in ("../evil", "a/b", ".."):
        with pytest.raises((InvalidId, ValueError)):
            store.lora_dir(bad)


def test_write_config_toml_types_match_sd_scripts_contract(tmp_path):
    # Hardware-validation catch (2026-07-12, sd-scripts rev 0128ca00): toml
    # values bypass argparse type coercion (read_config_from_file puts them
    # straight onto the namespace), and sd-scripts unconditionally does
    # args.resolution.split(",") — so resolution MUST be a toml STRING; an
    # int AttributeErrors the trainer at startup.
    import tomllib

    from app.imagegen.lora import TrainConfig, TrainRequest, _KohyaSubprocessTrainer

    trainer = _KohyaSubprocessTrainer(tmp_path, "python")
    request = TrainRequest(
        dataset_dir=tmp_path / "ds", output_dir=tmp_path / "out",
        output_name="identity", base_checkpoint=tmp_path / "ckpt.safetensors",
        trigger="cfidabc123", config=TrainConfig(),
    )
    config_path = trainer._write_config(request)
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert isinstance(parsed["resolution"], str)        # .split(",") contract
    assert parsed["resolution"] == "1024"
    assert isinstance(parsed["max_train_steps"], int)   # argparse type=int
    assert isinstance(parsed["network_alpha"], float)
    assert isinstance(parsed["mixed_precision"], str)
    assert parsed["sdpa"] is True
    # UNet-only default (2026-07-12): standard SDXL practice + sidesteps the
    # diffusers 0.39 kohya te1/te2 converter regression at 3e load time.
    assert parsed["network_train_unet_only"] is True
    # Windows backslashes in the checkpoint path survive toml round-trip
    assert parsed["pretrained_model_name_or_path"] == str(tmp_path / "ckpt.safetensors")


def test_kohya_subprocess_pins_utf8_both_directions(tmp_path, monkeypatch):
    # Hardware-validation catch (2026-07-12): sd-scripts logs bilingual text;
    # on Windows a non-console pipe defaults the child to cp1252 (it dies
    # mid-run on a Japanese log line) and text=True decodes with the locale
    # codec in the parent. The subprocess must pin PYTHONUTF8=1 for the child
    # and utf-8/replace for the parent's capture.
    from app.imagegen.lora import TrainConfig, TrainRequest, _KohyaSubprocessTrainer

    (tmp_path / "sdxl_train_network.py").write_text("# stub", encoding="utf-8")
    trainer = _KohyaSubprocessTrainer(tmp_path, "python")
    request = TrainRequest(
        dataset_dir=tmp_path / "ds", output_dir=tmp_path / "out",
        output_name="identity", base_checkpoint=tmp_path / "ckpt.safetensors",
        trigger="cfidabc123", config=TrainConfig(),
    )
    captured = {}

    class _Proc:
        returncode = 0
        stderr = ""

    import subprocess

    def fake_run(cmd, **kwargs):
        captured.update(kwargs, cmd=cmd)
        (tmp_path / "out" / "identity.safetensors").write_bytes(b"\0")
        return _Proc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = trainer.train(request)
    assert out.name == "identity.safetensors"
    assert captured["env"]["PYTHONUTF8"] == "1"
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"
