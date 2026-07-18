"""Identity LoRA promotion (3d): training, status, triggers, captions.

Mixin for ``ImageService`` (see service.py): methods run on the composed
class and share its instance state (``self._store``, ``self._engine``,
``self._settings``, …) plus the shared privates that stay on the base
(``_load_record``, ``_assemble``, ``_delete_quietly``, …) via the MRO.
"""

from __future__ import annotations


import os
from pathlib import Path

from ..model import CharacterRecord, LoraManifest
from . import lora as lora_mod
from .lora import TrainFailed, TrainItem, TrainRequest, TrainUnavailable
from .prompt import AssembledPrompt
from .service_shared import ARTIFACT_LOAD_ERRORS


class _LoraOps:

    # -- identity LoRA promotion (3d) -------------------------------------------

    def train_lora(self, character_id: object) -> dict:
        """Train a per-character identity LoRA on the confirmed vetted set (3c),
        store it, and flip the record's identity anchor (``has_lora`` +
        ``lora_path`` — the first record mutation the image pipeline makes since
        the 3b reference). Heaviest op: the image model is unloaded first so the
        trainer subprocess gets the whole GPU (§3), and the prior LoRA (if any)
        survives a failed re-train."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        vetted = self._load_vetted_manifest(record.id)
        if isinstance(vetted, dict):
            return vetted
        if vetted is None or vetted.count == 0:
            return {"ok": False, "kind": "no_vetted",
                    "error": "no confirmed vetted set to train on — run the "
                             "bootstrap and confirm a grid first (Stage 3c)"}
        checkpoint = self._engine.checkpoint_path()
        if checkpoint is None or not checkpoint.is_file():
            return {"ok": False, "kind": "engine",
                    "error": "no image checkpoint configured to train against"}
        missing = lora_mod.preflight_train(self._settings)
        if missing is not None:
            return {"ok": False, "kind": missing,
                    "error": self._trainer_missing_message(missing)}

        assembled = self._assemble(record)
        if isinstance(assembled, dict):
            return assembled
        trigger = self._lora_trigger(record)
        caption = self._lora_caption(trigger, assembled)

        # Resolve every vetted image under the char dir (containment) — a
        # hand-edited manifest path cannot pull a foreign file into training,
        # and a vetted entry must actually live under vetted/ (so a tampered
        # manifest can't feed e.g. character.json in as a training frame).
        items: list[TrainItem] = []
        vetted_dir = self._store.vetted_dir(record.id).resolve()
        for entry in vetted.entries:
            resolved = self._resolve_reference(record.id, entry.path,
                                               allow_absolute=False)
            if isinstance(resolved, dict):
                continue  # a missing/escaped vetted image is skipped
            abs_path = resolved[0]
            if vetted_dir != abs_path.parent and vetted_dir not in abs_path.parents:
                continue  # a vetted entry must be under vetted/
            items.append(TrainItem(image_path=abs_path, caption=caption))
        if not items:
            return {"ok": False, "kind": "no_vetted",
                    "error": "the vetted images are missing on disk"}

        config = lora_mod.coerce_train_config(self._settings)
        dataset_dir = self._store.lora_dataset_dir(record.id)
        output_dir = self._store.lora_dir(record.id) / "output"
        self._delete_tree_quietly(dataset_dir)
        self._delete_tree_quietly(output_dir)
        try:
            lora_mod.build_dataset(dataset_dir, items, config)
        except OSError as exc:
            self._delete_tree_quietly(dataset_dir)
            return {"ok": False, "kind": "io",
                    "error": f"could not prepare the training dataset: {exc}"}

        # VRAM (§3): free the image slot for the trainer subprocess, then mark
        # the slot busy for the duration; ALWAYS reset in the finally.
        self._engine.unload()
        produced: Path | None = None
        error: dict | None = None
        try:
            self._settings.set("models.active", "image")
        except OSError:
            pass
        try:
            trainer = self._trainer_factory(self._settings)
            request = TrainRequest(
                dataset_dir=dataset_dir, output_dir=output_dir,
                output_name="identity", base_checkpoint=checkpoint,
                trigger=trigger, config=config)
            produced = trainer.train(request)
        except TrainUnavailable as exc:
            error = {"ok": False, "kind": exc.kind,
                     "error": self._trainer_missing_message(exc.kind)}
        except TrainFailed as exc:
            error = {"ok": False, "kind": "train_failed", "error": str(exc)}
        except Exception as exc:
            error = {"ok": False, "kind": "train_failed",
                     "error": f"training failed: {exc}"}
        finally:
            try:
                if self._settings.get("models.active") == "image":
                    self._settings.set("models.active", None)
            except OSError:
                pass
            self._delete_tree_quietly(dataset_dir)

        if error is not None:
            self._delete_tree_quietly(output_dir)
            return error

        # Promote: move the LoRA to its final home, flip the record, write the
        # provenance manifest. The prior LoRA is only overwritten now (on success).
        lora_dir = self._store.lora_dir(record.id)
        final = lora_dir / "identity.safetensors"
        try:
            lora_dir.mkdir(parents=True, exist_ok=True)
            os.replace(produced, final)
        except OSError as exc:
            self._delete_tree_quietly(output_dir)
            return {"ok": False, "kind": "io",
                    "error": f"could not store the trained LoRA: {exc}"}
        self._delete_tree_quietly(output_dir)

        try:
            lora_bytes = final.stat().st_size
        except OSError:
            lora_bytes = 0
        lora_rel = "lora/identity.safetensors"
        try:
            ckpt_bytes = checkpoint.stat().st_size
        except OSError:
            ckpt_bytes = None
        # Write the provenance manifest FIRST (guarded) so a promotion always
        # has provenance and the footprint below counts lora.json too; a disk
        # failure here fails the promotion cleanly (the record isn't flipped —
        # the orphan .safetensors reads has_lora=False and a retrain overwrites).
        try:
            self._store.save_lora_manifest(LoraManifest(
                character_id=record.id, trigger=trigger, lora_file=lora_rel,
                base_checkpoint=checkpoint.name, base_checkpoint_bytes=ckpt_bytes,
                network_dim=config.network_dim, network_alpha=config.network_alpha,
                steps=config.max_train_steps, resolution=config.resolution,
                learning_rate=config.learning_rate, dataset_size=len(items),
                lora_bytes=lora_bytes))
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not save the LoRA provenance: {exc}"}
        record.identity.has_lora = True
        record.identity.lora_path = lora_rel
        record.identity.footprint = self._store.measure_footprint(record.id)
        record.touch()
        try:
            self._store.save(record)
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not save the promoted record: {exc}"}
        self._audit.log("lora_trained", character_id=record.id, trigger=trigger,
                        dataset_size=len(items), steps=config.max_train_steps,
                        lora_bytes=lora_bytes)
        return {"ok": True, "id": record.id, "lora_path": lora_rel,
                "trigger": trigger, "steps": config.max_train_steps,
                "network_dim": config.network_dim, "dataset_size": len(items),
                "lora_bytes": lora_bytes}

    def lora_status(self, character_id: object) -> dict:
        """Whether the character has a trained LoRA + its provenance. No GPU."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        anchor = record.identity
        manifest = self._load_lora_manifest(record.id)
        if isinstance(manifest, dict):
            return manifest
        # A valid promotion needs both the flag AND the file present.
        has_lora = bool(anchor.has_lora and anchor.lora_path)
        lora_ok = False
        if has_lora:
            resolved = self._resolve_reference(record.id, anchor.lora_path,
                                               allow_absolute=False)
            lora_ok = not isinstance(resolved, dict)
        return {
            "ok": True, "id": record.id,
            "has_lora": has_lora and lora_ok,
            "lora_path": anchor.lora_path if lora_ok else None,
            "trigger": manifest.trigger if manifest else None,
            "provenance": manifest.to_dict() if manifest else None,
            "footprint": anchor.footprint.to_dict(),
        }

    def clear_lora(self, character_id: object) -> dict:
        """Delete the LoRA + its provenance and un-promote the record."""
        record = self._load_record(character_id)
        if isinstance(record, dict):
            return record
        try:
            removed = self._store.clear_lora(record.id)
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not clear the LoRA: {exc}"}
        record.identity.has_lora = False
        record.identity.lora_path = None
        record.identity.footprint = self._store.measure_footprint(record.id)
        record.touch()
        try:
            self._store.save(record)
        except OSError as exc:
            return {"ok": False, "kind": "io",
                    "error": f"could not save the record: {exc}"}
        self._audit.log("lora_cleared", character_id=record.id)
        return {"ok": True, "id": record.id, "removed": removed}

    # -- LoRA internals ---------------------------------------------------------

    def _load_lora_manifest(self, character_id: str):
        try:
            return self._store.load_lora_manifest(character_id)
        except ARTIFACT_LOAD_ERRORS as exc:
            return {"ok": False, "kind": "lora_corrupt",
                    "error": f"the LoRA manifest is unreadable: {exc}"}

    @staticmethod
    def _lora_trigger(record: CharacterRecord) -> str:
        """Mint a stable identity trigger for a NEWLY trained LoRA. Derived
        from a HASH of the id so it is provably ``[a-z0-9]`` even for a
        hand-edited id (the id is path-safe but not content-gated), and won't
        collide on a short prefix.

        Six hex chars (~4 CLIP tokens) — the prior ``"cfid"`` + 12 hex was 16
        chars = 11 CLIP tokens, 14% of the whole 77-token budget (5.5b). Six
        hex preserves every 3d property: SHA1-derived, provably ``[0-9a-f]``,
        and no minor-coded substring is reachable from the hex alphabet.

        Called ONLY at train time. Generation reads the persisted trigger via
        :meth:`_generation_trigger` — re-deriving here would silently
        de-trigger every LoRA trained under a different derivation."""
        import hashlib

        digest = hashlib.sha1(str(record.id).encode("utf-8")).hexdigest()
        return digest[:6]

    def _generation_trigger(self, record: CharacterRecord) -> str:
        """The trigger a trained LoRA was ACTUALLY conditioned on — read from
        the persisted manifest, never re-derived. Re-deriving at generation
        time (the 5.5b defect) silently de-triggers every LoRA whose stored
        trigger differs from the current derivation: the weights load, the
        conditioned token is absent, identity weakens with no error. Falls
        back to the current derivation only when no trigger is recorded (an
        absent / pre-trigger / unreadable manifest), reproducing the
        historical behavior for those cases."""
        manifest = self._load_lora_manifest(record.id)
        if not isinstance(manifest, dict) and manifest and manifest.trigger:
            return manifest.trigger
        return self._lora_trigger(record)

    @staticmethod
    def _lora_caption(trigger: str, assembled: AssembledPrompt) -> str:
        """The training caption: the trigger + the record's gated identity
        description (dropping the booru composition anchors — quality/subject —
        which are generation-time, not identity)."""
        fragments = [trigger]
        for piece in assembled.pieces:
            if piece.source in ("quality", "subject"):
                continue
            fragments.append(piece.text)
        return ", ".join(fragments)


