# IMAGE PIPELINE (Stage 3)

**Status:** Living. Stage 3a (base generation) landed; later sub-stages
(3b–3g) extend this document. Frozen decisions live in `DECISIONS.md`
(§3, §4, §6, §7, §11); the content line in `docs/CONTENT_POLICY.md`.

---

## 1. BASE CHECKPOINT PICK (deferred spec item — resolved at 3a)

**Pick: an Illustrious-XL-family checkpoint (SDXL architecture).**
Recommended concrete file: **WAI-NSFW-Illustrious-SDXL** (latest release at
download time) as the default variant; the heavy slot (§3) is free for a
larger or newer family merge.

Why this family (against then-current options, mid-2026):

- **Style class match (§4):** anime-derived, stylized/illustrative — the
  frozen rendering commitment.
- **Ecosystem center of gravity:** the Illustrious/NoobAI family superseded
  Pony V6 as the anime SDXL ecosystem during 2025 — the deepest current pool
  of LoRAs, guides, and tuned merges, and active development.
- **SDXL architecture** (hard requirement): IP-Adapter baselines (3b) and
  kohya-ss LoRA training (3d) target SDXL; Pony V7 moved to AuraFlow and
  FLUX remains rejected (§17) — both have weak identity/LoRA tooling at
  16 GB.
- **Adult + exotic range:** Danbooru-trained with full mature support and
  broad fantasy/non-human coverage (CONTENT_POLICY permits, §11 bounds).
- **Booru-tag conditioning** matches the option-file `prompt` fragment style
  the record already carries ("large breasts", "blue hair", "1girl").
- Fits the 16 GB floor in fp16 with headroom.

The *file* is swappable config (§4): any Illustrious-family `.safetensors`
drops in via settings with no code change. The checkpoint is user-placed on
the target machine — weights are never in the repo.

**Setup on the target machine:**

1. `pip install -r requirements-full.txt` (torch CUDA build first — see the
   header comment in that file).
2. Place the checkpoint, e.g. `models/image/wai-nsfw-illustrious.safetensors`
   (any path works; relative paths resolve against the app folder). Record
   which release/hash you downloaded — the sidecars record name + size, and
   reproducibility claims are per-file.
3. Point `models.image.checkpoint_path` at it in `data/settings.json`
   (`models.image.heavy_checkpoint_path` for the opt-in heavy variant).

**Offline posture (§2):** diffusers' single-file loader resolves the SDXL
component configs (tokenizer files, scheduler/UNet/VAE configs) from the
Hugging Face cache. Two supported modes:

- **Validation phase:** leave `models.image.pipeline_config_dir` unset; the
  *first* load performs a one-time online config warm into the HF cache
  (telemetry disabled by the engine), and every load after that is
  cache-local.
- **Fully offline:** place a local SDXL pipeline-config directory and set
  `models.image.pipeline_config_dir` to it — the engine then loads with
  `local_files_only=True` and never touches the network. Stage 7 bundles
  this config with the packaged app, which is what discharges the §2
  "no network at any point" guarantee for the shipped folder.

## 2. GENERATION SETTINGS (`image_gen` in settings.json)

| Key | Default | Notes |
|---|---|---|
| `width` × `height` | 832 × 1216 | ~1MP portrait, the SDXL sweet spot; multiples of 8, 512–2048 |
| `steps` | 28 | quality over speed is standing policy (§3) |
| `cfg_scale` | 5.5 | Illustrious guidance band is ~4.5–7 |
| `sampler` | `euler_a` | `euler_a` \| `euler` \| `dpmpp_2m` \| `dpmpp_2m_karras` |

The engine re-validates every request, so a hand-edited settings file
produces a structured error, not a crash.

## 3. PROMPT ASSEMBLY (3a — `app/imagegen/prompt.py`)

Record → positive/negative pair, fully data-driven:

1. **Quality preamble** — `app/imagegen/data/positive_quality.txt`.
2. **Subject anchor** — `solo` + `1girl`/`1boy`/`1other` mapped from
   `gender_presentation` (unset invents nothing: bare `solo`).
3. **Adult anchor** — the literal `adult` tag on every prompt, structural in
   code (P1); refined by the age group's `prompt_ranges` fragment
   ("young adult", "elderly", ...) when present.
4. **Option fragments** in catalog `(order, id)` order — option `prompt`
   fragments and slider `prompt_ranges`. Groups marked `render: false`
   (personality, voice) are chat-side context, not image tokens.
5. **`appearance_notes`** free text (the one image-relevant field;
   backstory/personality stay chat-side).

Exact-duplicate fragments dedupe; fragments join with `", "`. **CLIP
truncates around 75 tokens** — identity-critical fragments are ordered first
by design; keep appearance notes tight.

**Layer 1 (image side):** every fragment is gated in strict `prompt` context
*with provenance* (a blocked drop-in option fragment names its group), and
the cross-fragment boundary is gated by two further passes so a term formed
*across* the join cannot slip through:

- **edge-normalized join** — each fragment's leading/trailing punctuation is
  stripped, fragments are joined with a single space, and the result is
  gated. This defeats separator-overflow padding ("cute little…" + "girl" →
  "little girl") no matter how many punctuation chars pad a fragment edge,
  while preserving interior word spacing so ordinary prose is not
  concatenated into false positives ("…who shot a rival…" never folds to
  "shota").
- **zero-separator option pairs** — consecutive non-free-text fragments are
  concatenated with no separator and gated, catching a single word split
  across two option fragments ("sho"+"ta").

Any hit refuses generation with the offending source named, and audits.

*Honest boundary (§11 — the floor is not owner-proof):* a term deliberately
split across **three or more** adjacent option fragments each keeping ≥2
contiguous letters ("hi"|"gh"|"school") can still pass the join passes. This
requires hand-authoring several ordered option fragments — a §15 data-file
attack by the machine's owner, exactly the class the honest bar does not
claim to close deterministically. Layer 2 (semantic) and Layer 4 (review) are
the backstop; single-file authoring realism keeps it a non-issue in practice.

**Layer 2 (negative prompts):** every negative prompt leads with the
age-coded steer-away anchors (`negative_safety.txt` — deliberately exempt
from Layer 1: it names what generation must avoid) then the quality
negatives. The Layer-2 *classifier* attaches at 3c with the face-embedding
cull.

## 4. ENGINE + SWAP SCAFFOLD (3a — `app/imagegen/engine.py`)

- One heavy model holds VRAM at a time (§3). `load()` refuses while
  `models.active == "chat"`, then takes the slot; `unload()` (UI:
  `image_engine_release`) frees it. The sequenced swap manager is Stage 6a.
- diffusers `StableDiffusionXLPipeline.from_single_file`, fp16, CUDA-only by
  design (the engine refuses CPU: the floor machine has the GPU, §3).
- Seeds resolve before generation and land in the sidecar — every frame is
  reproducible from its JSON.

## 5. OUTPUT LAYOUT (3a)

Base renders are the §6 bootstrap candidates, so they land under the
character's reference dir; nothing mutates the record (promotion to
`identity.reference_image_path` is the 3b/3c confirmation step):

```
data/characters/<id>/reference/base-<utc-stamp>-<seed>.png
                              /base-<utc-stamp>-<seed>.json   # sidecar:
                              #   checkpoint, full request (prompt pair,
                              #   dims, steps, cfg, sampler, seed),
                              #   per-fragment provenance, record timestamp
```

Layer 4: every generation is audited with the full prompt pair
(`image_generated`), and every refusal with the offending source + matched
term (`filter_block` — assembly aborts at the first hit, so a refusal has no
finished pair to log).

## 6. HARDWARE VALIDATION — 3a CHECKLIST (pending)

On the 16 GB target machine, in order:

1. Install the Stage-3a slice (`requirements-full.txt` header) — CUDA torch.
2. Place the checkpoint; set `models.image.checkpoint_path`. Note the exact
   release downloaded.
3. Launch; confirm `image_engine_status` shows `torch_installed`,
   `diffusers_installed`, `checkpoint_exists` all true.
4. Create/pick a saved character; `image_prompt_preview` returns the pair.
5. `image_generate_base` → confirm: single window throughout (no console),
   VRAM fits fp16 at 832×1216, a coherent image matching the record's
   selections lands in `reference/` with its sidecar, `image_generated`
   audit line written. First run performs the one-time config warm (§1);
   if frames come out black, the merged-in VAE is misbehaving in fp16 —
   swap in the fp16-fix SDXL VAE.
6. Re-run with the sidecar's seed → same frame (deterministic on the same
   GPU/driver/library versions; cross-machine identity is not claimed).
7. Generate again after the warm with networking disabled (airplane mode) —
   must succeed, proving no per-generation network dependency.
8. `image_engine_release` frees VRAM (task manager check).

Result feeds the BUILD_PLAN hardware-validation flag for 3a.

## 7. STAGE 3B — IP-ADAPTER BASELINE IDENTITY (§6)

IP-Adapter steers generation by a **reference image** so a character looks
like *itself* across renders — the always-on baseline identity for the
quick-create path (LoRA promotion is the detailed-path upgrade at 3d). It
reuses the 3a gated prompt assembler and swap scaffold unchanged; it adds
image conditioning, nothing to the safety prompt.

### Flow

1. `image_generate_base` (3a) makes one or more candidate renders under
   `reference/`.
2. `image_set_reference(id, frame_path)` promotes a chosen candidate to the
   character's `IdentityAnchor.reference_image_path` (stored **char-relative**,
   e.g. `reference/base-….png`) — the *only* record mutation the image
   pipeline makes. `image_clear_reference` / `image_reference_status` manage it.
3. `image_generate_identity(id, seed?, scale?)` re-assembles + re-gates the
   same prompt and renders it IP-Adapter-steered by the stored reference, into
   `characters/<id>/identity/identity-<utc-stamp>-<seed>.png` (+ sidecar).

### Model (user-placed, offline — like the checkpoint)

A local **h94/IP-Adapter** mirror; point `models.image.ip_adapter.dir` at it.
`models.image.ip_adapter.variant` picks the weight from a code table
(`standard` | `plus`) — the weight ↔ image-encoder pairing is **not**
hand-editable, because that pairing is the one load-bearing footgun:

```
<ip_adapter.dir>/
  sdxl_models/
    ip-adapter_sdxl_vit-h.safetensors        # variant "standard"
    ip-adapter-plus_sdxl_vit-h.safetensors   # variant "plus" (stronger identity)
  models/
    image_encoder/                           # OpenCLIP ViT-H (dim 1024)
      config.json, model.safetensors, ...
```

Both variants are ViT-H, so `image_encoder_folder` is pinned to the **slash-
form** `"models/image_encoder"`. diffusers resolves a slashed value from the
repo root (→ ViT-H); the bare default `"image_encoder"` would resolve to
`sdxl_models/image_encoder` = ViT-**bigG** (dim 1280) and mismatch the ViT-H
projection (dim error / garbled identity). Keeping weight+encoder as code
constants behind the variant selector makes that mismatch unhittable.

### Steer strength

`image_gen.ip_adapter_scale` (default **0.55**; per-call override via
`image_generate_identity(scale=…)`). Engine bound is **[0, 1]**; useful band
~0.3–0.8 (advisory, surfaced in status). Low = loose steer (the prompt owns
pose/wardrobe); high (>0.8) approaches a rigid lock and the reference's own
pose/angle starts winning — reserved for the non-human end (§6 mitigation),
tuned on hardware. A bad hand-edit degrades to the default, never crashes.

### VRAM behavior (12 GB-class cards)

Hardware-measured (2026-07-12, RTX 4070 Super 12 GB): the fully-resident
identity stack (SDXL fp16 ~6.6 GB resident + ViT-H encoder + adapter
~1.9 GB) peaks **12.18 GB** (standard) / **12.32 GB** (plus) at 832×1216 —
past a 12 GB card's budget. The Windows (WDDM) driver silently spills to
system RAM instead of OOMing, roughly halving throughput (18.6 s/frame vs
9.7 base). Below `IDENTITY_RESIDENT_VRAM_MIN_GB` (14.0, `engine.py`) the
identity backend therefore uses accelerate **model-cpu-offload** instead of
a resident `.to("cuda")`: peak drops to ~the largest single component
(measured **6.58 GB** standard / **6.01 GB** plus) for a few seconds of
PCIe transfer per render (measured **12.0 s/frame** steady-state — faster
than the spilled resident path). At/above the floor the resident path is
unchanged. The predicate (`identity_needs_cpu_offload`) is pure and
sandbox-tested; base and catalog modes are untouched (base peaks 10.35 GB
and fits a 12 GB card without spill).

### Path safety (the security boundary)

`reference_image_path` lives in a hand-editable `character.json`, so at
generation time it is **untrusted**. `service._resolve_reference` validates
containment **twice** — at set-time (`allow_absolute=True`; the UI hands back
the frame's absolute path) and again at use-time (`allow_absolute=False`; a
stored absolute or `..` path is a tamper signal). It rejects `..`, absolute /
drive-relative / UNC components, and — after `resolve()` collapses symlinks —
anything landing outside `characters/<id>/`. Only a contained, existing file
is opened; only the char-relative form is persisted. The reference is
**path**-validated, not content-gated: it is a frame our own gated pipeline
produced, and the identity render re-runs the Layer-1 prompt gate + Layer-2
negative age anchors exactly as 3a. (The Layer-2 pixel/face classifier is 3c.)

### Output + provenance

```
data/characters/<id>/identity/identity-<utc-stamp>-<seed>.png
                             /identity-<utc-stamp>-<seed>.json   # sidecar adds:
                             #   "reference": char-relative reference path
                             #   "ip_adapter": {dir(basename), variant,
                             #     weight_name, image_encoder_folder, scale}
                             #   (never an absolute path)
```

Layer 4 events: `identity_generated`, `identity_reference_set`,
`identity_reference_cleared`, plus `filter_block` on a refused render.

## 8. HARDWARE VALIDATION — 3b CHECKLIST (pending)

On the 16 GB target machine (after the 3a checklist passes):

1. Place the local h94/IP-Adapter mirror (layout above); set
   `models.image.ip_adapter.dir`. Confirm `image_engine_status` shows
   `ip_adapter_configured`, `ip_adapter_weight_exists`,
   `ip_adapter_encoder_exists` all true.
2. Base-render a character, `image_set_reference`, then
   `image_generate_identity` → confirm a steered frame lands under
   `identity/` with an `ip_adapter` sidecar block, single window / no console.
3. **Encoder footgun check (diagnostic):** confirm the pinned
   `image_encoder_folder="models/image_encoder"` loads without a projection
   dim-mismatch; verify the on-disk weight filenames match the variant table
   (`.safetensors`, not `.bin`).
4. **VRAM:** peak at 832×1216 with ViT-H should fit fp16 with headroom (SDXL
   ~7 GB + ViT-H ~1.3 GB + adapter). Measure the `plus` variant too.
5. **Mode swap:** base → identity → base round-trip actually **releases** VRAM
   between modes (task-manager check) and a plain base render after an
   identity render is clean (no residual attn-processor/encoder state).
6. **Scale tuning:** 0.55 holds identity while the prompt still owns
   wardrobe/anatomy; 0.3 loose, 0.8 near-lock; non-human may need 0.7–0.85.
   Every number is a verified default, not a final constant (§16). Confirm the
   structural `adult` anchor + Layer-2 negative age anchors still hold at high
   scale (image conditioning injects into the same cross-attention).
7. **Fully offline:** with `pipeline_config_dir` + `ip_adapter.dir` set and
   airplane mode on, a steered generate completes (`local_files_only` +
   config-gated `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE`).
8. **Reproducibility:** re-render from a steered sidecar's seed + scale +
   reference + variant + checkpoint → same frame (same-GPU/driver/lib caveat).

Result feeds the BUILD_PLAN hardware-validation flag for 3b.

## 10. STAGE 3C — IDENTITY BOOTSTRAP + AUTO-FILTER (§6, §11)

Turns the single 3b reference into a vetted training set for the 3d LoRA:
generate a seed batch steered by the reference, auto-cull the drift/junk/
policy-violating frames, and let the user confirm a small machine-vetted grid.
The cull does the heavy lifting; user approval drops to confirming ~12 images.

### Flow (`ImageService` + the 5 `image_bootstrap*` / `image_confirm_vetted` bridges)

1. `image_bootstrap_generate(id, batch?, more?)` — generate N candidates
   (vary **only the seed**; the identity prompt/reference/scale are fixed —
   §6: the LoRA needs a *tight* cluster, not pose variety) via the 3b
   `generate_identity`, then auto-filter. `more=True` appends a fresh-seed
   batch and re-culls the union.
2. `image_bootstrap_recull(id, overrides?)` — re-cull the persisted candidates
   with adjusted thresholds. **No image model, no regeneration** (§6 "adjust
   without regenerating").
3. `image_bootstrap_status(id)` — phase / counts / the proposed grid / vetted
   state. No GPU.
4. `image_confirm_vetted(id, candidate_ids)` — promote a selected subset into
   `vetted/` (the 3d input).
5. `image_clear_bootstrap(id, scope)` — delete `bootstrap`/`vetted`/both.

### The cull gate (`app/imagegen/cull.py`, per candidate, short-circuiting)

`decode → detect (informational) → CONTENT (Layer-2, hard, fail-closed) →
quality floor → identity similarity → aesthetic (soft rank)`. Then rank the
survivors by (similarity DESC, aesthetic DESC); the top `grid_size` become the
proposed grid. Four **fakeable** model abstractions (path-in / dataclass-out)
keep all of this sandbox-verifiable; only the real models are [HARDWARE]:

| Abstraction | Real backend | Role |
|---|---|---|
| `FaceEmbedder` | imgutils CCIP + anime face detect | 768-d character embedding + det_score/area/blur |
| `QualityScorer` | imgutils `anime_dbaesthetic` | soft aesthetic rank |
| `ContentClassifier` | imgutils WD14 tags ∩ `minor_coded_tags.txt` + rating/nudenet | **Layer-2 pixel gate** |
| `FaceSwapper` | InsightFace `inswapper_128` | optional identity lock |

**Identity similarity** = cosine (dot of unit embeddings) vs the reference;
default floor **0.50**. The embedding is the L2-normalized whole-image
**CCIP** feature (`_CcipEmbedder`), so the cull's cosine IS CCIP's own
character metric in disguise — `ccip_difference == (1 − cos) / 2`, verified
exactly on hardware. Hardware-measured on real 3b output (2026-07-12):
same-character frames score **0.63–0.82**, a different character **0.33** —
the 0.50 floor splits them with ~0.15 margin on both sides; CCIP's canonical
"same character" threshold (diff 0.1785) corresponds to a *tighter* cos
≈ 0.643 if the vetted set needs it. Every threshold is a hardware-tuned
default (§16), coerced defensively from `image_gen.bootstrap.*` (a bad
hand-edit degrades to the code default).

The detector's own gate mirrors the configured floor
(`detector_threshold()`, [0,1]-clamped): it feeds the anime face detector's
`conf_threshold` and, swap-only, insightface `prepare(det_thresh=…)` — a
detector default would otherwise silently drop faces *before* the cull's
floor ever saw them, making a tuned floor a dead knob (hardware-validation
catch, 2026-07-12).

**Embedder swap (2026-07-12, user-approved):** the original pick,
photo-trained `buffalo_l`/ArcFace, measured AT ITS MARGIN on the
WAI-Illustrious anime style — of six visually-identical steered renders,
three yielded **no detection even at det_thresh 0.20** (the rest det
0.25–0.39), and same-character ArcFace cosine measured 0.35–0.58, straddling
the 0.50 floor; as-calibrated the cull would have rejected essentially every
bootstrap candidate. Swapped to imgutils **CCIP + anime face detection**
behind the same `FaceEmbedder` Protocol (the abstraction was built for
exactly this): detection recovered to 8/8 at conf 0.83–0.89 and the same/
different separation above. The pure cull, fakes, and every floor knob are
byte-unchanged. `inswapper` face-swap (default OFF) still uses the buffalo_l
stack for its own detection — built only when `face_swap_enabled`; its
re-similarity check now runs through CCIP like everything else. Observed
face areas on real frames run 0.03–0.12 vs `face_area_min` 0.04 — watch at
the §11 calibration run.

### Layer-2 pixel gate — the safety-critical part (§11)

A clean adult-anchored *prompt* (3a) can still **render** a minor-appearing
face, which no deterministic prompt check can see. The `ContentClassifier` is
that pixel-side gate:

- **Hard** (reject + delete, never down-rank), runs on **every** frame
  *before* the quality/similarity gates (a no-face frame can still trip a
  whole-image minor-coded tag), and every block is audited `filter_block`
  (layer 2) for the Layer-4 leakage signal.
- **Fail-closed**: a missing/unconfigured classifier raises `CullUnavailable`
  at *preflight* (nothing is ever produced unclassified); a per-frame classify
  exception is treated as *blocked*.
- **Re-runs on the final pixels** inside `confirm_vetted` (the exact pixels 3d
  trains on, which may be swapped or hand-edited into the manifest).

Honest bar (§11): no single check is reliable on stylized anime (the style
renders adults with neotenous features). The value is stacked independent
checks + bias-to-block + Layer-4 review — defense in depth, **never a
guarantee**. `minor_coded_tags.txt` is the editable tuning surface.

### Face-swap (optional, default OFF)

Runs **strictly after** the similarity cull, on the kept set only — swapping
first would collapse the face-region similarity and *mask* drift. The
swapped (new) pixels are re-classified + re-similarity-checked (CCIP, like
every frame) fail-closed; a swap that fails falls back to its original.

### VRAM sequencing (§3, one heavy model at a time)

The image model generates the whole batch and is **unloaded in a `finally`**
(always frees the ~10-12 GB slot, even on an OOM at frame 30). Only *then* are
the light cull models built — CPU providers by default (zero VRAM). `recull` /
`status` / `confirm` / `clear` never touch the image model.

### `confirm_vetted` — no smuggling (§11)

`bootstrap.json` is hand-editable, so the selection is validated against the
**trusted manifest**: each id must be present with a confirmable status
(`proposed`/`kept`); the pixel path is taken from the manifest (never caller
input) and re-resolved through the 3b dual-containment check; and the final
pixels are re-classified fail-closed. A forged id, a `rejected_*` id, an
escaped manifest path, or a now-blocked frame cannot enter the training set.

### Model layout (user-placed, offline — like the checkpoint)

```
models.image.content_classifier_dir -> imgutils HF cache (WD14 tagger + rating + nudenet + aesthetic
                                       + CCIP identity + anime face detection — the WHOLE default cull)
models.image.face_recognition_dir   -> <dir>/models/buffalo_l/{det_10g,w600k_r50,...}.onnx  (SWAP-ONLY)
models.image.face_swapper_path      -> inswapper_128.onnx        (OPTIONAL — face_swap_enabled)
models.image.onnx_providers         -> ["CPUExecutionProvider"]  (zero VRAM; swap to CUDA after unload)
```

Since the 2026-07-12 embedder swap the default cull path needs ONLY the
imgutils HF cache; `preflight_cull` witnesses it via `content_classifier_dir`
(`classifier_unavailable` when absent) and requires the insightface stack
only when `face_swap_enabled`. `buffalo_l` root footgun (swap path):
InsightFace loads from `<root>/models/buffalo_l/`, so `face_recognition_dir`
is the dir that *contains* `models/`. FaceAnalysis has **no** `download=False`
flag and will silently fetch `buffalo_l.zip` if the dir is absent — so
preflight existence-checks it and refuses (`face_models_missing`) rather than
reaching the network. **Licenses:** CCIP (`deepghs/ccip_onnx`) is OpenRAIL,
anime face detection (`deepghs/anime_face_detection`) is MIT;
`inswapper_128` and `buffalo_l` are research/non-commercial — now confined to
the optional swap path, fine for this offline personal build, never
redistributed with the packaged app.

### Output + provenance

```
data/characters/<id>/bootstrap/candidates/cand-<utc>-<seed>.png (+ .json)   # append-only
                     /bootstrap/swapped/<cand>-swap.png                       # optional
                     /bootstrap/bootstrap.json   # BootstrapManifest: every candidate + cull scores + decision
                     /vetted/vetted-NN.png                                    # the 3d training set
                     /vetted/vetted.json         # VettedManifest
```

Only char-relative POSIX paths are persisted. Layer-4 events:
`bootstrap_generated` / `bootstrap_reculled` / `vetted_confirmed` /
`bootstrap_faceswapped` / `bootstrap_cleared`, plus `filter_block` (layer 2) on
every content reject.

## 11. HARDWARE VALIDATION — 3c CHECKLIST (pending)

On the 16 GB target machine (after the 3a/3b checklists pass):

1. Pre-warm the imgutils HF cache (`content_classifier_dir`) with the WD14 +
   aesthetic + **CCIP (`deepghs/ccip_onnx`) + anime face detection
   (`deepghs/anime_face_detection`)** models (done 2026-07-12 on the target).
   Only if enabling face-swap: place `buffalo_l` (`face_recognition_dir`) +
   `inswapper_128.onnx`. Confirm a bootstrap preflight sees them.
2. `image_bootstrap_generate` on a character with a reference → confirm the
   batch generates with the image model loaded, the model is **unloaded**
   before the cull runs (task-manager VRAM check), candidates + `bootstrap.json`
   land under `bootstrap/`, and a proposed grid comes back. Single window / no
   console throughout.
3. **similarity_floor calibration (CCIP):** pre-measured on real 3b frames
   (same-char cos 0.63–0.82, other-char 0.33 — 0.50 splits cleanly); confirm
   on the real 64-batch, and if the vetted set needs tightening use the
   CCIP-canonical ≈0.643. Watch `face_area_min` 0.04 vs observed anime-YOLO
   face areas 0.03–0.12 (§16).
4. **Layer-2 accuracy (safety):** verify the WD14/minor-coded ensemble on the
   actual style; tune `minor_coded_tags.txt`. Expect both FP and FN on stylized
   anime — the value is the stacked ensemble + bias-to-block + Layer-4 review,
   not any single leg. Confirm a deliberately minor-appearing render is caught
   and audited.
5. **Face-swap:** enable, confirm swaps run only post-cull, the swapped pixels
   are re-classified, and quality/seam is acceptable on tall 832×1216 portraits.
6. **Fully offline:** with the cache pre-warmed + airplane mode, a full
   bootstrap runs (`HF_HUB_OFFLINE`; the CCIP/detect/WD14 models must all
   resolve from cache; if swapping, FaceAnalysis must not trigger the silent
   `buffalo_l` auto-download).
7. **opencv:** exactly one cv2 (`opencv-contrib-python`, not `opencv-python`);
   confirm the Windows `insightface` wheel installs (swap path only).
8. Net keep-rate out of N=64 (does `more=True` get routinely needed to hit the
   floor?); confirm `confirm_vetted` copies the vetted set and 3d can read it.

Result feeds the BUILD_PLAN hardware-validation flag for 3c.

## 12. STAGE 3D — LoRA PROMOTION (§6)

Promotes the confirmed vetted set (3c) into a per-character **identity LoRA** —
the detailed-path identity mechanism (the quick path rides on the 3b IP-Adapter
reference). The LoRA teaches *identity only* (this face, this body); pose comes
from the base model at generation time (§6), so the seed batch was deliberately
identity-tight, not pose-varied.

### Flow (`ImageService` + 3 bridges)

1. `image_train_lora(id)` — the vetted images (`vetted/`) become a kohya
   training dataset (`lora/dataset/<repeats>_identity/img-NN.png` + a caption
   `.txt`), the trainer runs, the produced `.safetensors` is stored at
   `lora/identity.safetensors`, and the record's identity anchor is flipped
   (`has_lora=True`, `lora_path`). Provenance lands in `lora/lora.json`.
2. `image_lora_status(id)` — `has_lora` (true only when the flag AND the file
   are present) + trigger + provenance + footprint. No GPU.
3. `image_clear_lora(id)` — delete the LoRA + provenance and un-promote.

### Trainer (user-placed, offline, swappable — DECISIONS §6 spec-time pick)

**kohya-ss `sd-scripts`** driven as a **headless subprocess** (`CREATE_NO_WINDOW`
— no console popup, §2) behind a fakeable `LoraTrainer`. The app builds a config
+ dataset and invokes `sdxl_train_network.py`, then collects the `.safetensors`.
Point `models.image.lora_trainer_dir` at a sd-scripts checkout (its own env;
`models.image.lora_trainer_python` selects that interpreter, else the app's). A
diffusers/peft loop can replace `_default_trainer_factory` with no change above
`lora.py`. Hyperparameters (`image_gen.lora_train.*`, quality-max defaults:
dim 16 / alpha 8 / lr 1e-4 / 1024px / ~1600 steps / fp16, §16-tuned, coerced +
clamped) are hand-editable and degrade to defaults.

### Caption + trigger

Each image's caption = a stable per-character **trigger** (`cfid<id8>`) + the
record's *gated* identity description (the assembler's fragments minus the
booru composition anchors — quality/subject — which are generation-time, not
identity). Because the caption is built from the Layer-1-gated assembled prompt,
a blocked record refuses to train.

### VRAM (§3, one heavy model at a time)

Training is the heaviest op. The in-process image engine is **unloaded first**
so the trainer subprocess gets the whole GPU; `models.active` is marked busy
for the duration and reset in a `finally`. A **failed re-train never destroys
the prior LoRA** — the new `.safetensors` is `os.replace`d into place only on
success, and the record is flipped only then.

### Output + provenance

```
data/characters/<id>/lora/identity.safetensors   # the trained LoRA
                         /lora.json               # LoraManifest: trigger, base
                         #   checkpoint (name+bytes), dim/alpha/steps/resolution/
                         #   lr, dataset_size, lora_bytes, created_at
```

Only char-relative POSIX paths are persisted; each vetted image is
containment-resolved before it enters the dataset. Record mutation is confined
to `has_lora` + `lora_path` + `footprint` (the deferred **identity-tier marker**
is resolved here: `has_lora` + the vetted-manifest existence are the
authoritative promotion state — **no** separate record tier field; quick vs
detailed stays audited-not-persisted). Layer-4 events: `lora_trained`,
`lora_cleared`. **Using** the LoRA at generation is Stage 3e (the seed catalog);
3d's engine generate/load path is unchanged.

## 13. HARDWARE VALIDATION — 3d CHECKLIST (pending)

On the 16 GB target machine (after the 3a–3c checklists pass):

1. Place a kohya-ss `sd-scripts` checkout with its deps in an env; set
   `models.image.lora_trainer_dir` (+ `lora_trainer_python` if separate).
   Confirm `sdxl_train_network.py` is found (preflight). **Offline note
   (2026-07-12):** the trainer subprocess inherits the app's pinned
   `HF_HOME`/`HF_HUB_OFFLINE`, and sd-scripts' SDXL strategy loads the two
   CLIP **tokenizers** from the hub (`openai/clip-vit-large-patch14` +
   `laion/CLIP-ViT-bigG-14-laion2B-39B-b160k`) — prewarm both into the
   pinned cache (done on the target) or training fails at startup with a
   structured `train_failed` (fail-closed confirmed).
2. Confirm a vetted set exists (3c `confirm_vetted`); `image_train_lora` →
   verify the dataset lays out under `lora/dataset/<repeats>_identity/` with
   captions, the image engine is **unloaded** before training (task-manager
   VRAM check — training should have the whole GPU), training completes, and
   `lora/identity.safetensors` + `lora.json` land with `has_lora` flipped.
   Single window / no console throughout (the subprocess is headless).
3. **kohya config:** verify the generated `train_config.toml` keys match the
   installed sd-scripts version (SDXL LoRA: `sdxl=true`, `network_module=
   networks.lora`, `cache_latents`, `gradient_checkpointing`, `sdpa`); fix any
   key drift. Confirm `_toml_escape` handles the Windows backslash checkpoint
   path, and that the output-filename fallback matches the version's save name.
4. **VRAM fit (§3):** gradient-checkpointing + fp16 SDXL LoRA at 1024px must fit
   the 16 GB floor; if OOM, lower resolution/rank or enable more offload.
5. **Identity quality (§6):** ~15–30 vetted images, ~1600 steps — verify the
   trained LoRA holds identity without overfitting (drift makes it worse). Tune
   dim/alpha/steps/lr on the real checkpoint; every default is §16-tuned.
6. **Timeout + failure:** confirm a nonzero exit / timeout / no-output surfaces
   as a structured `train_failed` and the prior LoRA survives.
7. Confirm 3e can load `lora/identity.safetensors` + the `trigger` for catalog
   generation.

Result feeds the BUILD_PLAN hardware-validation flag for 3d.

## 14. STAGE 3E — SEED CATALOG GENERATION (§7)

Pre-renders the character's **core matrix** of common states — expressions ×
poses × the defined wardrobe — LoRA-steered and auto-filtered, so chat can
pick the nearest frame instantly (the "seed" of §7's seed-plus-grow; on-demand
"grow" is 3g). Identity comes from the 3d LoRA; pose/expression/outfit vary per
cell (unlike the identity-tight 3c bootstrap).

### Flow (`ImageService` + 3 bridges)

1. `image_generate_catalog(id)` — requires a trained LoRA (3d), the reference
   (3c, for the similarity cull), and the cull models. Builds the matrix,
   renders each cell LoRA-steered, auto-filters, and writes `catalog/` +
   `catalog.json` (the Stage-1 `CatalogManifest`).
2. `image_catalog_status(id)` — frame count / states / staleness. No GPU.
3. `image_clear_catalog(id)` — delete the frames + manifest.

### The matrix (`app/imagegen/catalog.py`)

`expressions × poses × outfits`, bounded by `image_gen.catalog.max_frames`.
Expressions and poses are editable data (`data/catalog_states.json`); outfits
are the character's wardrobe selections (or a single "as-is" when none). Each
cell's prompt = the **constant** gated identity (the assembler with the
wardrobe group excluded) + the LoRA **trigger** (lead) + the cell's
outfit/expression/pose (extra) — all Layer-1-gated; a blocked cell is skipped
and audited.

### LoRA-at-generation (the 3d payoff)

The engine gains a **catalog mode**: the load-key widens to
`(checkpoint, ip_config, lora)`, and a `_DiffusersLoraSDXLBackend` loads the
checkpoint + `load_lora_weights` (unfused; strength via `cross_attention_kwargs`
per-generate, `image_gen.catalog.lora_scale`). Base (3a) and identity (3b)
paths are unchanged; a different character's LoRA rides the same hardened
unload+reload swap branch (one heavy model at a time, §3).

### Auto-filter (§7 "same filter as training")

Every rendered frame runs the **same 3c cull** — content-classify (Layer-2,
hard, fail-closed, audited) → face-embedding similarity to the reference →
quality. A rejected frame is deleted and never shown; its cell is regenerated
(new seed) up to `max_attempts`. Only surviving frames enter the manifest.

### VRAM (§3) + prior-catalog safety

Each pass generates the whole (pending) matrix with the LoRA image model,
**unloads it**, then culls on the CPU toolkit. The new frames are staged in
`catalog.new/` and swapped over the live catalog **only on success** — a failed
re-generate (engine error, or nothing survives the cull → `catalog_empty`)
leaves the prior catalog intact.

### Output

```
data/characters/<id>/catalog/frame-<utc>-<seed>.png (+ .json sidecar)
                     /catalog.json   # CatalogManifest: one CatalogEntry per kept
                     #   frame — frame_id, char-relative path, state
                     #   {expression,pose,outfit}, on_demand=false, bytes
```

`matted_path` starts null — the 3f matte pass (§16) fills it — and `on_demand`
stays false (the seed set; 3g grows the cache on demand). Record mutation:
**none** — 3e only reads `has_lora`/`lora_path`/`reference_image_path`.
Layer-4 events: `catalog_generated`, `catalog_cleared`, plus `filter_block`
(layer 2) on every rejected frame.

## 15. HARDWARE VALIDATION — 3e CHECKLIST (pending)

On the 16 GB target machine (after 3a–3d pass):

1. On a fully-promoted character (LoRA + reference + vetted done),
   `image_generate_catalog` → confirm the matrix renders LoRA-steered (identity
   holds via the LoRA + trigger), each frame auto-filters (task-manager: the
   image model **unloads** before the cull), and `catalog/` + `catalog.json`
   land with char-relative paths. Single window / no console throughout.
2. **LoRA call:** confirm `load_lora_weights(dir, weight_name=...)` loads the
   kohya SDXL `.safetensors` and the per-generate `cross_attention_kwargs`
   scale is honoured on the unfused adapter (else switch to `set_adapters` +
   an explicit adapter name). Verify checkpoint + LoRA fit fp16 at 832×1216.
3. **Auto-filter reuse:** confirm the same 3c thresholds cull off-model LoRA
   frames (similarity to the reference) and the Layer-2 gate still fires on
   pixels; a rejected cell regenerates and a persistently-bad cell is dropped
   (catalog is best-effort core coverage).
4. **Matrix size vs "slow is fine":** the default cap (48 frames) at ~30 s each
   is ~24 min — acceptable (§3). Tune `max_frames`/`max_attempts` per taste.
5. **Prior-catalog safety:** kill a re-generate mid-run → confirm the prior
   catalog + manifest survive and no `catalog.new/` staging dir is orphaned.

Result feeds the BUILD_PLAN hardware-validation flag for 3e.

## 16. STAGE 3F — MATTING / KEYABLE OUTPUT (§7, §13)

Background-removes the 3e seed-catalog frames into **keyable RGBA cutouts**
(straight alpha, original RGB preserved) under `catalog/matted/`, filling
`CatalogEntry.matted_path` — the artifact Stage 5's character-over-background
compositing consumes (§13). Works on the already-generated, already-culled
catalog: matting is deterministic post-processing, so it never re-rolls
identity or discards the vetting those exact pixels passed.

### Method (deferred spec item — RESOLVED at 3f)

**Direct-ONNX reimplementation of rembg's ISNet pipeline** on the
already-installed `onnxruntime` + `pillow` (+ transitive numpy) stack, with a
**user-placed** model file. No new pip dependencies, no runtime downloads.

- **Not `pip install rembg`** — the historical "hard-depends on
  opencv-python-headless" objection is STALE (gone upstream ~2.0.72); the live
  objections are its unconditional `pymatting`/`scikit-image`/`scipy` deps
  (imported at module top even though their paths default off), a `pooch`
  runtime model downloader, `numpy>=2.3`/`pillow>=12.1` floors, and an
  `onnxruntime>=1.23.2` extra — none needed for the ~30-line recipe below
  (rembg is MIT; the port is attributed in `app/imagegen/matte.py`).
- **Not `transparent-background`** — hard dep on `opencv-python`, a second
  cv2 distribution (forbidden beside `opencv-contrib-python`), plus a
  torch/timm/kornia/albumentations/gdown stack.
- **Not `imgutils.segment`** (same SkyTNT model, already installed) — it
  loads via huggingface_hub (runtime download unless the cache is pre-seeded),
  violating the user-placed-weights rule. Useful as a parity reference only.
- **Not keyable-background *generation* / chroma key** — regenerating the
  catalog on a flat key would discard the 3e vetting and re-roll identity, and
  SDXL does not render trustworthy flat keys (spill, key-colored hair or
  costumes); a chroma key still needs despill + soft edges.

### Model layout (user-placed, offline — like the checkpoint)

```
models.image.matting_model_path -> isnet-anime.onnx   (default variant)
models.image.onnx_providers     -> reused (CPU default = zero VRAM)
```

| variant (`image_gen.matting.variant`) | file | size | provenance / license |
|---|---|---|---|
| `isnet_anime` (default) | `isnet-anime.onnx` | ~176 MB | SkyTNT/anime-segmentation, Apache-2.0 |
| `isnet_general` | `isnet-general-use.onnx` | ~170 MB | xuebinqin/DIS, Apache-2.0 |
| `birefnet` | rembg's BiRefNet ONNX exports | ~973 MB (lite ~214 MB) | ZhengPeng7/BiRefNet, MIT |

All are assets of `https://github.com/danielgatis/rembg/releases/download/
v0.0.0/<name>.onnx`. Docs-only integrity md5s (no runtime hashing):
isnet-anime `6f184e756bb3bd901c8849220a83e38e`, isnet-general-use
`fc16ebd8b0c10d971d3513d564d01e29`, BiRefNet-general
`7a35a0141cbbc80de11d9c9a28f52697`. No non-commercial restriction on any
(unlike inswapper/buffalo_l).

### Close-up-bust escalation (5.5g — the 3f residual, un-parked)

`isnet_anime` leaves a **translucent full-frame pane** on tight close-up busts:
the character fills 85–94 % of the frame, so there is almost no background to
key out, and the per-image min-max stretch holds that near-empty "background"
at high alpha. (Measured on the RTX 4070 target: wide/full-body frames matte
cleanly at ~0.18–0.28 solid-alpha coverage; busts sit at ~0.93–0.996.) 6e's
avatar **is** a bust, so this matters.

The fix is a **per-frame re-matte with a second (BiRefNet) model, routed by the
coverage the gate already computes** — no new architecture (BiRefNet is the
existing constants-only `birefnet` variant). Configure a second, user-placed
model:

```
models.image.matting_escalation_model_path -> a BiRefNet .onnx  (None = OFF)
image_gen.matting.escalation_variant       -> "birefnet" (any known variant)
image_gen.matting.escalation_coverage      -> 0.85  (escalate when primary >= this)
```

Policy (`_apply_escalation` + `_MatteEscalation`): after the primary matte, a
frame whose **primary solid-alpha coverage ≥ `escalation_coverage`** is
re-matted with the escalation model; the escalated cutout is **promoted only if
it passes the SAME degenerate gate AND keys strictly more out (lower coverage)
than the primary** — the never-worse rail, so escalation can never ship a matte
worse than the primary. `escalation_coverage = 0.85` sits above the clean-frame
ceiling (~0.28) and below `coverage_max` (0.98), so in-band busts (0.93–0.98)
that currently *pass* the gate still get the BiRefNet re-matte.

**Byte-for-byte no-op when unset:** with no escalation model path,
`coerce_escalation_config` returns `None` — no second session, no extra factory
call, no manifest keys. The escalation toolkit is built **lazily** (on the first
frame that crosses the threshold, so a wide-frame-only catalog never loads the
~973 MB model) and applies to all matte paths (`matte_catalog`, on-demand cache,
heal). A missing/corrupt escalation model **degrades to disabled** (the primary
matte is used) — it never raises. When on, `manifest.matting` gains
`escalation_variant` / `escalation_model` (basename only) / `escalated` (count).

### The recipe (`app/imagegen/matte.py`, `_OnnxMatter`)

One shared codepath; the per-variant constants (verified verbatim from rembg
2.0.76 sessions) are the ONLY differences:

| variant | input | mean | std | sigmoid in code |
|---|---|---|---|---|
| `isnet_anime` | 1024² | (0.485, 0.456, 0.406) | (1, 1, 1) | no |
| `isnet_general` | 1024² | (0.5, 0.5, 0.5) | (1, 1, 1) | no |
| `birefnet` | 1024² | (0.485, 0.456, 0.406) | (0.229, 0.224, 0.225) | yes (graph emits logits) |

RGB → LANCZOS to 1024² (aspect-distorted square, per rembg) → scale by the
image's own max pixel (**rembg quirk, reproduced for parity — NOT /255**) →
per-channel mean/std → NCHW float32, input name read dynamically from the
graph → first output, channel 0 → optional sigmoid → per-image **min-max
stretch** (second reproduced quirk; deviation: epsilon-guarded — upstream
divides by zero on a constant output) → uint8 L-mask → LANCZOS back to
832×1216 → optional `erode_px` MinFilter passes + `feather_px` Gaussian →
**`putalpha`** (deviation from rembg's default `naive_cutout`, which blends
edge RGB toward black and re-composites with dark fringes). Never binarized
(rembg's `post_process_mask` destroys the anti-aliased edge).

### Flow (`ImageService` + 2 bridges)

1. `image_matte_catalog(id, force=False)` — re-screens + mattes every manifest
   entry (below). `force` re-mattes frames that already have a valid matte.
2. `image_matte_status(id)` — matted/unmatted counts (a `matted_path` only
   counts when it containment-resolves into `catalog/matted/`), readiness
   (`preflight_matte`), and the last run's provenance. No GPU, no models.

Per entry: containment-resolve `entry.path` (untrusted — the manifest is
hand-editable) and require it to be a **direct `*.png` child of `catalog/`**
(the matte output is keyed by the source STEM, so same-stem/other-extension
hand-placed sources would collide onto one matte file — .png-only makes
collisions structurally impossible) → **Layer-2 classify the source pixels,
fail-closed, BEFORE the skip check** → skip if a valid matte exists (unless
`force`) → matte to `*.png.tmp` (a temp namespace no promoted final can
carry) → degenerate coverage gate (`coverage_min`/`coverage_max` on the
solid-alpha fraction: an empty or keyed-nothing-out mask is deleted at tmp,
the prior matte survives) → atomic `os.replace` promote → record the
char-relative `matted_path`. Per-frame failures (`invalid_path` / `missing` /
`matte_failed` / `matte_empty` / `matte_full`) never abort the run; a run
with **no matted and no skipped frames and at least one `matte_failed`**
escalates to a top-level `matte_failed` (the wrong-model-file signal). Every
result shape — ok, `matte_failed`, `catalog_changed`, save-`io` — carries the
run tallies (`frames`/`matted`/`skipped`/`blocked`/`failed`) + per-frame
`results`.

Top-level kinds: `no_catalog` | `catalog_corrupt` | `matting_model_missing` |
`classifier_unavailable` | `matte_unavailable` | `matte_failed` |
`catalog_changed` | `io` (+ the record-load kinds `invalid` / `not_found` /
`blocked` / `age`).

**Why re-classify 3e-vetted pixels:** the file at `entry.path` at matte time
is not provably the file 3e classified (hand-editable), and classifier drift
must catch previously-passed frames at the next processing boundary. A
blocked frame is purged — pixels + sidecar + prior matte + manifest entry —
and audited (`filter_block`, layer 2, context `image.matte.frame`); accepted
consequence: a blocklist tightening can shrink a catalog (3e regeneration
recovers). The RGBA output is NOT separately classified: under putalpha its
RGB is byte-identical to the just-classified source and alpha only removes.

**Manifest round-trip safety (3f is the first flow that loads, mutates, and
re-saves `catalog.json`):** a manifest whose `character_id` mismatches the
requested id is `catalog_corrupt` (`save_catalog` routes by the manifest's
own id — a hand-edit could clobber another character); an **optimistic
concurrency token** (`updated_at`) aborts with `catalog_changed` rather than
clobber a manifest a concurrent 3e regeneration swapped in mid-matte —
best-effort, NOT a lock (second-granularity token, a check-to-save window
remains; acceptable for a single-window app with no background regeneration);
an all-skipped run saves nothing (true no-op). Only `matted_path`s and the
`matting` provenance block mutate — **zero record mutation**, engine never
touched (CPU ONNX beside a loaded SDXL, the `confirm_vetted` posture; users
flipping `onnx_providers` to CUDA should `image_engine_release` first and own
the contention).

### Output

```
data/characters/<id>/catalog/matted/<frame-stem>.png   # RGBA, straight alpha
                     /catalog.json    # matted_path per entry + a `matting`
                                      #   provenance block (variant, model
                                      #   basename+bytes, providers, knobs,
                                      #   matted count, complete, timestamp)
```

Inside `catalog/` deliberately: a 3e regeneration swap destroys derived
mattes with their source frames, `footprint.catalog_bytes` counts them, and
`clear_catalog` removes them for free (no separate clear op; a mattes-only
redo is `force=True`). Layer-4 events: `catalog_matted` (tallies + variant +
model basename; on a `catalog_changed`/save-`io` abort the same event carries
`aborted=<kind>` so a run that purged frames always leaves a run-level
trail), plus `filter_block` per purge.

### Settings (`image_gen.matting.*`, coerced + clamped, §16-tuned defaults)

`variant` (`isnet_anime`) · `erode_px` 0 · `feather_px` 0 (both `[0, 8]`;
defaults = exact rembg parity — the halo-mitigation knobs) · `coverage_min`
0.02 · `coverage_max` 0.98 (`[0, 1]`; a min>max hand-edit resets both).

## 17. HARDWARE VALIDATION — 3F CHECKLIST (pending)

On the 16 GB target machine (after the 3e checklist passes):

1. Place `isnet-anime.onnx`, set `models.image.matting_model_path` (optional
   md5 self-check above), confirm `image_matte_status` reports `ready`.
2. Full-catalog `image_matte_catalog` → RGBA files under `catalog/matted/`,
   every `matted_path` filled, the `matting` provenance block written; single
   window, no console, **zero VRAM** (task-manager: the SDXL slot untouched).
3. **Parity diff** (guards the transcribed constants): in a THROWAWAY venv,
   `rembg` (isnet-anime, `putalpha=True`) vs `matte.py` on the same frame —
   alphas should be near-identical. Also confirm the real model's input is
   `(1, 3, 1024, 1024)` float32 on `onnxruntime>=1.17` CPU.
4. **Edge quality:** composite mattes over bright AND dark backgrounds;
   inspect hair wisps + source-background halos; tune `erode_px` 0→1→2 and
   `feather_px`; escalate to `variant=birefnet` (CUDA providers only after
   `image_engine_release`) if insufficient.
5. **Degenerate floors:** confirm `coverage_min=0.02`/`coverage_max=0.98`
   don't false-trip on tight portraits or frame-filling full-body cells (a
   false trip silently leaves `matted_path` null).
6. **putalpha semantics:** transparent-area RGB is preserved (inspect edge
   pixels) — no dark fringe in Stage-5-style composites.
7. **Layer-2 re-screen:** hand-place a policy-tripping PNG at a manifest
   path → the run purges frame+sidecar+matte, drops the entry, audits
   `filter_block`.
8. **Offline:** airplane mode end-to-end (model user-placed; the classifier's
   imgutils HF cache pre-warmed; the factory pins HF_HUB_OFFLINE et al.).
9. **Throughput + idempotence:** 48 frames × ~1–3 s CPU is acceptable (§3);
   `force` re-runs; a third run skips all (note: an all-skip run still
   re-classifies every frame with WD14 — confirm acceptably fast). BiRefnet
   on CPU is seconds-per-frame — prefer the lite export or GPU providers.
10. **Lifecycle:** `footprint.catalog_bytes` grows by the matte bytes at the
    next measurement; a 3e re-generate leaves ZERO stale mattes (they die
    with the swapped `catalog/`) and status reports the new catalog unmatted;
    a mid-run kill leaves only a swept-on-next-run `*.png.tmp`.

Result feeds the BUILD_PLAN hardware-validation flag for 3f.

## 18. STAGE 3G — ON-DEMAND GENERATION + CACHE (§7)

The "grow" of §7's seed-plus-grow: a requested state already covered by a
valid seed-catalog or cache frame is served **instantly** (no models, no
GPU); a novel state generates LoRA-steered, runs the **same 3c auto-filter**
as 3e ("same filter as training", §7), is matted best-effort via the 3f
`Matter`, and caches under `cache/` with `on_demand=true`. Stage 6e's
avatar-frame selection consumes this surface (state → frame; miss →
generate); Stage 4's LRU cap + footprint management consume its metadata.

### Flow (`ImageService` + 3 bridges)

1. `image_generate_on_demand(id, state, force=False)` — `state` is a full
   `{expression, pose, outfit}` id triple. Hit → served from cache-then-
   catalog (a forced regeneration shadows the seed frame); miss → generate +
   cull + matte + cache. `force=True` skips the lookup and regenerates,
   replacing any same-state **cache** entry (the seed catalog is never
   touched).
2. `image_cache_status(id)` — frames / per-state rows (incl. `last_used`) /
   matte coverage + readiness. No GPU.
3. `image_clear_cache(id)` — delete `cache/` + `cache.json`; evicted states
   regenerate on demand if asked for again (§14).

### The state vocabulary (ids only — no prompt injection surface)

The caller picks **ids**; every prompt fragment comes from the editable
states file / the option catalog, never from the bridge (and the Layer-1
gate re-runs on the assembled cell regardless). `resolve_cell`
(`app/imagegen/catalog.py`) enforces the creator-payload strictness: exactly
the three keys, all non-empty strings — a malformed shape is `invalid`, an
unknown id is `unknown_state`. `expression`/`pose` must exist in
`data/catalog_states.json` (so dropping in new states extends the on-demand
space with no code change, §15); `outfit` must be one of the record's
wardrobe selections or the literal `asis` (render the base look — always
valid, even when a wardrobe exists). The novel-state space = every valid
triple the capped 3e matrix did not pre-render, plus anything added to the
states file later.

### Serving vs processing (the 3c/3f re-screen stance)

Serving a hit is a **read**: like `bootstrap_status`/`catalog_status`, it
does not re-classify pixels — re-screening happens at every *processing*
boundary. A hit only counts when the entry's path containment-resolves to a
direct `*.png` child of its own frames dir (the 3f residency rule); a
dangling/escaped/hand-edited entry silently reads as novel. The one
processing a hit can trigger is the **heal** path: a hit whose matte is
missing/invalid (e.g. matted before the matting model was placed, or a 3f
coverage trip) is re-matted on access — and because those pixels' age is
unbounded and the manifest hand-editable, the heal **re-classifies the
source fail-closed first** (a classify exception is a block). A blocked
frame is purged (pixels + sidecar + matte + manifest entry, under the 3f
purge trust rules) + audited (`filter_block`, layer 2, context
`image.cache.heal`), and the run falls through to fresh generation. A fresh
3g frame is NOT re-classified before its same-run matte: the cull
content-classified those exact pixels seconds earlier (content-first,
fail-closed) — unlike 3f, where source age is unbounded.

### Generation (reuses the 3e machinery verbatim)

Preconditions mirror 3e: trained LoRA (3d) + reference (similarity cull) +
checkpoint + `preflight_cull` — all checked BEFORE any GPU work. The single
cell rides the parameterized 3e passes: generate (engine catalog mode,
`image_gen.catalog.lora_scale`) → **unload in a finally** (§3) → CPU cull
(`image_gen.catalog.face_area_min` relaxation; Layer-2 gate + similarity at
the 3c values) → retry up to `image_gen.catalog.max_attempts`; nothing
surviving is a structured `frame_rejected`. **No separate 3g knobs** — the
cache is the catalog, grown. Frames stage in `cache.new/` (an in-process
failure leaves zero orphans; a hard kill leaves only a swept-on-next-run
staging dir), and only the culled survivor moves into `cache/` (O_EXCL
name reservation — never clobbers). Sidecar `stage: 3g-cache`,
`kind: cache`.

A matte failure/degenerate-trip never discards the culled frame — it caches
unmatted (`matte_status` reports why) and the next hit heals the gap.

### The cache manifest (`cache.json`)

Reuses the `CatalogManifest` shape at `characters/<id>/cache.json`; entries
carry `on_demand=true` + **`last_used`** (additive field — the §14 LRU
signal, stamped at creation and on every cache hit; seed-catalog entries
leave it null; eviction itself is Stage 4). At most one cache entry per
state triple: a replacement (force, or regen after a heal purge) deletes the
prior frame + sidecar + matte under the purge trust rules. Bookkeeping
writes on the serve path (last_used, healed matted_path) ride the 3f
**optimistic token** and are best-effort: on a token mismatch / unreadable
current manifest / save error nothing is written and the hit still serves —
pixels on disk are authoritative and the next access re-links idempotently.
A manifest whose `character_id` mismatches is `cache_corrupt` (`save_cache`
routes by the manifest's own id, the 3f hazard).

Top-level kinds: `invalid` | `unknown_state` | `cache_corrupt` |
`catalog_corrupt` | `no_lora` | `lora_missing` | `engine` | `config` |
`no_reference` | `reference_invalid` | `reference_missing` |
`face_models_missing` | `classifier_unavailable` | `cull_unavailable` |
`no_faces` | `frame_rejected` | `blocked` (a Layer-1-blocked cell prompt, or
a blocked stored record at load) | `io` (+ the record-load kinds
`not_found` / `age`). Matte outcomes are NOT top-level (best-effort): the ok
result's `matte_status` carries `matted` / `matte_failed` / `matte_empty` /
`matte_full` / `matting_model_missing` / `classifier_unavailable` /
`matte_unavailable`.

### Output

```
data/characters/<id>/cache/frame-<utc>-<seed>.png (+ .json sidecar)
                     /cache/matted/<frame-stem>.png   # RGBA, straight alpha
                     /cache.json    # CatalogManifest shape; on_demand=true,
                                    #   last_used per entry
```

`cache/` is deliberately a **sibling** of `catalog/` (not inside it): a 3e
regeneration swap replaces the seed catalog but the grown cache survives it
(same LoRA, same filter — still valid), `footprint.cache_bytes` counts it
separately (§14's management view), and `clear_cache`/Stage-4 LRU manage it
independently. Zero record mutation; the engine gains nothing (the 3e
catalog mode is reused as-is). Layer-4 events: `cache_generated` (state +
attempts + replaced + matte outcome; `aborted=<kind>` on a post-generation
abort), `cache_matted` (heal outcomes), `cache_cleared`, plus
`filter_block` per cull reject / heal purge.

Known crash window (accepted, mirrors 3c/3e): the survivor move + manifest
save are separate steps — a hard kill between them leaves an orphan frame
pair in `cache/` (footprint-counted, invisible to lookup). The Stage-4
startup reconciliation sweep (deferred item) covers it alongside the
bootstrap-candidates and `catalog.old` orphans.

## 19. HARDWARE VALIDATION — 3G CHECKLIST (pending)

On the target machine (after the 3a–3f checklists pass, with the live
end-to-end character):

1. `image_generate_on_demand` with a novel triple (e.g. an expression ×
   pose combination the capped 3e matrix skipped) → generates LoRA-steered,
   culls (task-manager: the image model **unloads** before the cull), mattes
   (CPU), lands `cache/frame-*.png` + `cache/matted/*.png` + `cache.json`
   with `on_demand=true` + `last_used`. Single window, no console.
2. **Instant hit:** repeat the same triple → served with `cached=true`,
   `source="cache"`, **no model loads** (sub-second), `last_used` bumped.
   A triple the seed catalog covers → `source="catalog"`, equally instant.
3. **Identity + gates hold:** the cached frame is the same character by eye
   (CCIP similarity ≥ floor logged in the cull) and unambiguously adult;
   `filter_block` audits fire on any reject.
4. **Heal:** delete one cache matte file → next hit re-classifies + re-mattes
   it (audit `cache_matted`), fills `matted_path` back in.
5. **force=True** replaces the same-state cache frame (old frame + sidecar +
   matte gone, one entry per state) and shadows a catalog-covered state.
6. **frame_rejected path:** with a deliberately hostile state (or a
   tightened similarity floor override at the settings level), confirm the
   structured `frame_rejected` after `max_attempts`, nothing cached, prior
   cache intact.
7. **Unknown vocabulary:** an unknown expression id → structured
   `unknown_state`; a drop-in edit to `data/catalog_states.json` makes the
   new id generable with no code change (§15).
8. **3e regen survival:** re-run `image_generate_catalog` → the cache
   survives the swap (sibling dir) and its frames still serve; footprint
   shows `cache_bytes` separately from `catalog_bytes`.
9. **Offline:** socket-blocked end-to-end on-demand run (generate → cull →
   matte) completes fully offline.
10. **Crash posture:** kill mid-generation → `cache.new/` staging swept on
    the next run; a kill between move and manifest-save leaves one orphan
    pair (the documented Stage-4 sweep item), nothing served corrupt.

Result feeds the BUILD_PLAN hardware-validation flag for 3g.

## 20. STAGE 5.5a — LONG-RUNNING-JOB CONTRACT (§3)

Every heavy image op is slow ([HARDWARE]: train 31.5 min, bootstrap ~15 min,
catalog 287 s). Stage 3 shipped them as **synchronous** bridges, so
`image_generate_catalog` — already wired into `library.js` — was a live
five-minute silent UI hang. `app/jobs/` backgrounds them without touching the
synchronous service methods (922+ tests + every harness call them):

- **`JobRunner`** — one daemon worker draining a bounded `queue.Queue`. One
  heavy job at a time = the structural single GPU slot (§3). `submit(kind, fn,
  target_id, total) → job_id` returns immediately; `status(job_id)` is a
  non-blocking read the UI polls at ~1 Hz (**never** `window.evaluate_js` push —
  it can deadlock the bridge thread and is fragile across view switches). Each
  record persists to `data/jobs/<job_id>.json` on every state change.
- **Cancellation** rides a `CancelToken` published on a thread-local
  (`current_token()`). `CancellableEngine` wraps the engine and, only when a job
  is active, checks the token before each `generate*` (raising `JobCancelled` —
  which subclasses `Exception` *directly*, so no service loop's `except` tuple
  catches it; it unwinds through the loops' `finally: unload()`, freeing VRAM)
  and ticks per-frame progress. **When no job is active it is a pure
  pass-through** — the synchronous path is byte-identical. Train cancels via
  `Popen.terminate()` (`_KohyaSubprocessTrainer.train` is now `Popen`+
  `communicate`, kill+reap on timeout); a terminated train raises `TrainFailed`
  and returns before `os.replace`, so the **prior LoRA is preserved** (the 3d
  invariant). `matte_catalog` / single-frame `generate_background` are pollable +
  reap-safe but pre-flight-cancellable only.
- **Reap sweep** `JobRunner.reconcile()` (wired into `main.run()`) mirrors the
  Stage-4/5 vouching model: own dir, `.json` only, corrupt→skip (never delete);
  a fresh process owns no jobs, so any persisted non-terminal record is a
  hard-kill orphan → marked `interrupted`; terminal records past
  `jobs.retain_seconds` are pruned. This closes the 3g item-10 orphan window.
- **Bridges:** `job_submit(kind, target_id, options)` / `job_status` /
  `job_cancel` / `job_list`, dispatching the six kinds to the unchanged
  `bootstrap_generate` / `train_lora` / `generate_catalog` /
  `generate_on_demand` / `matte_catalog` / `generate_background`. The front-end
  is wired to these in 5.5c–d.


## 21. STAGE 5.5b — PROMPT BUDGET (77-token CLIP window)

- **LoRA trigger.** The trigger is minted once at train time (`_lora_trigger`,
  now `sha1(id)[:6]` — 6 hex chars, ~4 CLIP tokens, down from the 16-char
  `cfid`+12hex = 11 tokens) and persisted in `LoraManifest.trigger`. Generation
  reads it from the manifest (`_generation_trigger`), **never re-derives** —
  re-deriving silently de-triggers a LoRA whose stored trigger differs (weights
  load, the conditioned token is absent, identity weakens, no error). Fallback
  to derivation only for an absent / empty / unreadable manifest, so a
  pre-change 16-char LoRA still fires.
- **Chunked encoding** (`engine.encode_chunked`, all three backends). SDXL's
  CLIP caps at 77 tokens and diffusers truncates a longer prompt silently — a
  fully-detailed record assembles to 106–137 tokens (dropping outfit / style /
  free-text / pose). The fix splits the assembled positive on commas into
  ≤75-content-token windows, `encode_prompt`s each, and concatenates the embeds
  along the sequence axis (pooled from window 0; the negative is padded to the
  same length — the diffusers CFG equal-length requirement — by encoding both
  chunk-lists padded to a common `k`). No new dependency (`compel` rejected, 3f
  precedent). A short prompt → one window, identical to the old path.
- **Token accounting.** `clip_token_counter` loads the model's own
  `CLIPTokenizer` from `<pipeline_config_dir>/tokenizer` (lazy, offline);
  `token_report` gives total / per-piece / 77-boundary. Surfaced through
  `image_prompt_preview` under `tokens` (structured `available: false` when the
  tokenizer is not on disk — never a vendored second BPE). 5.5c wires it into
  the creator's live prompt panel.


## 22. KNOWN LIMITS (restating §16 for the image side)

- Categorical anatomy renders reliably; fine dimensional precision does not
  (§12) — the pipeline never promises it.
- IP-Adapter identity (3b) weakens at the heavily non-human end; LoRA
  promotion (3d) is the mitigation (§6).
- Prompt-token budget: CLIP encodes ~75 content tokens per 77-slot window.
  **Chunked encoding (§21) removes the single-window truncation** — a long
  prompt is split into windows and concatenated, so nothing is dropped; a lone
  comma-free fragment longer than a window is still truncated (nothing shorter
  is possible without splitting a word).
