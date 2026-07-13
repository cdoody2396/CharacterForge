# SCENES / PERSONAS / SCENARIOS / EVENTS + COMPOSITING (Stage 5 — DECISIONS.md §13)

**Status:** living companion to `BUILD_PLAN.md` Stage 5. The frozen decisions
are §13 (builders + character-over-background compositing) and §11 (the safety
layers Stage 5 attaches). This documents how they are implemented and which
knobs exist.

Stage 5 gives the user **authored context to interact within** — personas,
scenes, events, scenarios — plus **scene imagery** a character composites over.
It is the last piece before the Stage-6 chat loop.

---

## 1. The builder record (§13 "lighter structured builder")

`app/model/builder.py::BuilderRecord` — one dataclass, a `kind` discriminator
∈ `{persona, scene, event, scenario}`. The **same** tags + filtered-free-text
mechanism as the character engine, but **no** age / anatomy / sliders /
identity / LoRA (§13 "lighter, not the full character engine"). Fields: `id`,
`kind`, `name`, `selections`, `tags`, `free_text`, `consent` (scenario-only),
timestamps.

Invariants re-run on every construction **and on load** (a hand-edited
`builder.json` cannot enter in a prohibited state):

- **`id`** is a safe single path segment (`ensure_safe_id`).
- **`kind`** is one of the closed set — an unknown/hand-edited kind raises
  `BuilderKindError`, so a kind cannot be flipped to dodge the consent gate.
- **`consent`** is always `None` or an approved frame (below).
- **`name` + every free_text/selection/tag key & value** pass the Layer-1
  filter (`name` context for the name, `freetext` for prose, strict `prompt`
  context for the discrete tokens that head to scene image-prompt assembly).

Persistence: `app/model/builder_store.py::BuilderStore` — a parallel tree to
`characters/` (builders are character-independent — a scene is reusable across
characters):

```
data/builders/<id>/builder.json        the record (kind rides inside)
                  /background.json       the scene background manifest
                  /background/            generated background frames (scene)
```

## 2. Surfaces

| Surface | Where | What |
|---|---|---|
| `builder_describe(kind)` | `app/ui/builders.py` → bridge | Per-kind option catalog + free-text fields; for a scenario, the code-advertised approved consent frames. `kind` None → the kind list. |
| `builder_create` / `builder_update` | 〃 | Strict shape validation at the doorway; the record re-runs the content + consent + kind gates. `update` keeps `kind` fixed (a persona cannot become a consent-shedding scenario). |
| `builder_list` / `builder_get` | 〃 | Summary rows (kind, consent, scene background count, footprint) / one record in form shape. Unloadable records degrade to deletable error rows. |
| `builder_delete` | 〃 | Removes the whole `builders/<id>/` tree. Works on records that no longer load (deletion is the remedy). |
| `builder_reconcile` | 〃 | Sweeps orphaned scene-background frames — see §6. Also runs at every launch. |
| `scene_generate_background(scene_id, seed)` | `ImageService` | **[HARDWARE]** — see §4. |
| `scene_background_status` / `scene_clear_background` | 〃 | A scene's backgrounds + Layer-2 readiness / delete them. No GPU. |
| `image_composite(character_id, frame_ref, scene_id, background_ref, overrides)` | 〃 | **[HERE]** — see §3. Returns a PNG data-URI preview; persists nothing. |
| `image_matted_frames(character_id)` | 〃 | The character's matted (keyable RGBA) frames — the compositing UI's source list. |

The UI (`app/ui/web/builders.js`, the **Scenes** view) is data-driven from
`builder_describe`, mirroring the creator/library.

## 3. Character-over-background compositing (§13, all [HERE])

`app/imagegen/composite.py` — pure Pillow + arithmetic, unit-tested in the
sandbox (unlike matting, whose ONNX call is [HARDWARE]).

- `composite_geometry(bg_size, fg_size, config)` (pure) → the resized
  foreground box + paste position. Default: **bottom-center** anchor, foreground
  scaled to `scale`×background height (aspect preserved, refit to width,
  clamped inside the background).
- `composite_over(bg_rgb, fg_rgba, config)` → flattened RGB. The matte is
  **straight alpha, original RGB preserved** (Stage 3f), so it composites via
  `alpha_composite` — never premultiplied.
- **Only matted (RGBA) frames may be composited** — `load_rgba_matted` raises
  `NotMatted` on an alpha-less RGB frame (the §13 "matted frame" guard at the
  pixel boundary).

**Background on/off toggle:** `scene_id=None` → **transparent passthrough** (the
matted cutout, unchanged, on transparent alpha — the UI shows it on a neutral
checker backdrop, and it is exactly what Stage 6e avatars want). A scene id →
composite over that scene's most-recent (or a specified) background.

**The 3f edge residual, retired here** (no re-matte, tunable per composite):
`edge_choke` (alpha erosion, the same `MinFilter(3)` matting uses) + `feather_px`
+ `alpha_floor` kill matte halos over **bright and dark** composite backgrounds.

## 4. Background generation (§13, [HARDWARE])

`ImageService.generate_background` mirrors `generate_base` for a **scene** only:

1. `PromptAssembler.assemble_scene` — a **scenery** prompt from the scene's
   `render:true` groups + `setting_notes`, with a `scenery, no humans` anchor
   and **no** character identity (no subject/adult anchor, no LoRA/IP-Adapter),
   so the background is an empty setting the character composites over.
2. Plain base-backend SDXL render.
3. **Layer-2 pixel gate** — a **new** requirement over `generate_base` (§11).
   The `cull.ClassifierToolkit` (CPU ONNX, coexists with the loaded engine)
   screens the generated pixels fail-closed; a block purges the frame + audits.
4. Persist under `background/` (reusing `_persist_image`'s atomic naming +
   reproducibility sidecar) + a `BackgroundManifest` entry.

## 5. Safety (the four layers Stage 5 attaches)

| Layer | Where | Mechanism |
|---|---|---|
| **1 — deterministic** | all builder text | `BuilderRecord` gates every channel on construction + load. The scene prompt reuses `PromptAssembler._gate` + `_gate_adjacency` (the cross-fragment gate that closed a HIGH-severity 3a bypass) — the scene channel is not a separate, weaker path. R7 school-vocabulary blocks on scene backgrounds too. |
| **2 — pixel classifier** | generated backgrounds | Fail-closed WD14 minor-coded classifier, purge-on-block. |
| **3 — structural (consent)** | scenarios | `APPROVED_CONSENT_FRAMES` is a **code constant** and `consent` is a **dedicated typed field** (the `age.py` pattern). A scenario without an approved affirmative-consent frame is **unconstructable**; a drop-in option file only *advertises* the ids and can neither widen nor rename the set. The approved set (user-signed-off): `enthusiastic`, `established_relationship`, `negotiated_scene`, `romantic`. This makes "a consent-less scenario" impossible — non-consent *phrasing* remains Layer 1's job on every kind. |
| **4 — logging** | all paths | create/update/delete/generate/block/composite audited. |

The 20+ protection needs no `Age` field on builders: a sub-20 assertion in any
builder text is blocked by Layer 1.

## 6. Option files + the §12 free ally

Builder options load **per kind** (`load_builder_catalog(kind, data_dir)`,
`include_bundled=False`) from `app/data/builders/{_shared,<kind>}/` plus user
drop-ins under `<data>/builders/<kind>/` — so a scene form never shows character
races, and a drop-in file extends a kind with no rebuild (§15). Because the same
loader runs, **§12's numeric-reservation check rejects any builder slider** at
load — reinforcing the "no sliders" decision for free.

Per-kind free-text fields (code-defined): scene `setting_notes` (the one that
feeds the scene image prompt), scenario `situation_notes`, persona
`persona_notes`, event `event_notes`.

## 7. Reconciliation

`BuilderService.reconcile` (at every launch via `app/main.py run()`, fail-safe;
and on demand via `builder_reconcile`) sweeps orphaned scene-background frames
— a killed generation leaves a frame+sidecar the manifest never recorded (the
3g kill-window analogue). Same **vouching model** as the character library: only
our own `*.png`/`*.json` patterns, directly inside `background/`, and only when
a **trusted** `background.json` fails to vouch (a corrupt manifest sweeps
nothing; an absent one vouches for nothing). Idempotent.

## 8. Settings

`image_gen.compositing.*` (defensively coerced/clamped; a bad hand-edit → the
code default):

| Key | Default | Meaning |
|---|---|---|
| `anchor` | `bottom_center` | placement anchor (`bottom_center`/`center`/`bottom_left`/`bottom_right`/`top_center`) |
| `scale` | `0.85` | foreground height / background height, (0, 1] |
| `margin` | `0.0` | gap from the anchored edge, fraction of bg height |
| `edge_choke` | `0` | alpha erosion passes (halo choke), int [0, 8] |
| `feather_px` | `0` | Gaussian soften after the choke, int [0, 8] |
| `alpha_floor` | `0` | clamp alpha below this to 0 (halo fringe), int [0, 254] |

Scene generation reuses the `image_gen.width/height/steps/cfg_scale/sampler`
knobs and the base backend — no new generation knobs.

## 9. What is [HERE] vs [HARDWARE]

- **[HERE]** (verified in-sandbox): the whole builder framework, the consent +
  kind gates, scene prompt assembly + its gates, compositing (real Pillow),
  matted-frame listing, the reconcile sweep, all bridge wiring.
- **[HARDWARE]** (pending flag): `generate_background`'s SDXL render + the
  Layer-2 classifier on real pixels, and final `edge_choke`/`feather` tuning
  over bright/dark composite backgrounds (the inherited 3f residual). The
  surrounding logic is verified here behind injected fakes.
