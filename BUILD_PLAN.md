# PROJECT BUILD PLAN & STATE

**Status:** Living. This file updates as stages complete. Frozen design decisions live in `DECISIONS.md` — read that first, then this.

**How to use (each chat):**
1. Read `DECISIONS.md`, then this file.
2. Find the current stage under "Current State."
3. Build exactly that stage's scope — no more. One stage per working session where practical; large stages split into their sub-stages.
4. Produce runnable artifacts (code/config/schema), not descriptions.
5. Verify per the stage's verification location. Report observed behavior verbatim; do not soften failures.
6. On completion, update this file: mark the stage done, advance Current State, append to the Change Log.

**Update rules:**
- This file changes; `DECISIONS.md` does not (unless the user reopens a decision).
- Never mark a stage done without its Definition of Done met.
- "Code-here, validate-on-hardware" stages are *done here* when the code/config is complete and structurally verified; they carry a pending hardware-validation flag until the user confirms on the target machine.

---

## BUILD ENVIRONMENT CONSTRAINTS

The build sandbox has **no GPU and no bundled model weights.** Consequences:

- **Verifiable here:** data schemas, file formats, UI logic, management logic, memory store/retrieval logic, decay-model logic, filter/blocklist logic, compositing logic, packaging structure. Anything that does not require running a model on a GPU.
- **Code-here, validate-on-hardware:** anything invoking the image model or the LLM, LoRA training, embedding generation with the production model, live model-swapping, and the final packaged offline run. These stages produce complete, structurally-verified code and configuration; final validation is on the user's 16 GB target machine.

Each stage below is tagged **[HERE]** or **[HARDWARE]** accordingly. Some stages are mixed and say so.

---

## DEPENDENCY SPINE (BUILD ORDER)

```
Stage 0  Scaffold + Safety Foundation (Layer 1 + content-line draft)
   ↓
Stage 1  Character Data Model + Schemas (20+ gate lands here)
   ↓
Stage 2  Creator UI (quick + detailed, tags+text, categorical anatomy)
   ↓
Stage 3  Image Pipeline  [split 3a–3g, highest risk]
   ↓        base → IP-Adapter → bootstrap+cull → LoRA → seed catalog → matting → on-demand
   ├───────────────┐
Stage 4  Library    Stage 5  Scene/Persona/Scenario/Event Builders
   │                       │  (uses matting from 3f)
   └───────────┬───────────┘
Stage 5.5  Creator & Image UI  [split 5.5a–5.5g]
   ↓        jobs → prompt budget → §15 widgets → image UI → library scale → nav → residuals
   ↓        (Stage 3 is built and hardware-validated but UNREACHABLE from the window)
   ↓
Stage 6  Chat Loop  [split 6a–6e]
   ↓        swap manager → RAG store → decay model → turn assembly → avatar selection
   ↓
Stage 7  Packaging (single-launch folder, offline, one window)
```

**Safety is not a stage.** It is woven through the spine:
- Layer 1 (deterministic filter) is built in Stage 0 and *wraps every input/output* as later stages add them.
- The 20+ hard gate (Layer 3) lands *with* the data model in Stage 1.
- Image-side Layer 1 + Layer 2 attach across Stage 3.
- Chat-side Layer 2 + Layer 4 attach in Stage 6.
- The content-line policy is drafted in Stage 0 and must exist before any generation stage (3, 5, 6e).
- Stage 5.5 adds no new safety layer. It exposes existing gated paths to the UI; Layers 1–4 must be re-run on every path it surfaces, unchanged.

---

## STAGES

### Stage 0 — Scaffold + Safety Foundation  **[HERE]**
**Goal:** App skeleton and the deterministic safety layer everything routes through.
**Depends on:** nothing.
**Produces:**
- App folder structure + launcher stub + single-window shell (no console, no extra windows).
- Config/settings system, including the model-swap toggle scaffold (image + chat model selection).
- **Layer-1 deterministic filter module:** reusable input/output wrapper — blocklists, regex/classifier gates for prohibited categories, name slur-block. Built as a standalone module other stages import.
- **Content-line policy draft** (permitted vs prohibited), for user approval. Gates all generation stages.
**Definition of done:** shell launches to one window; settings persist; filter module rejects known bad inputs and passes clean ones in isolation tests; content-line draft delivered and approved by user.
**Safety attached:** Layer 1 (created), Layer 4 logging scaffold, content-line draft.

---

### Stage 1 — Character Data Model + Schemas  **[HERE]**
**Goal:** The record shape everything else reads and writes.
**Depends on:** Stage 0.
**Produces:**
- Character record schema: structured tag fields + filtered free-text fields + categorical anatomy fields + identity-anchor state (`has-LoRA`, reference image path, LoRA path, catalog manifest, footprint).
- **Option-definition data-file format** (§15): races, outfits, traits, anatomy categories, etc. — the format that makes options addable without a rebuild.
- Persistence layer (character records + catalog manifests on disk).
- **20+ hard gate (Layer 3):** age has no sub-20 representation and validates as a hard gate — under-20 is unconstructable.
- Name field wired to the Stage-0 slur-block (Layer 1).
**Definition of done:** a character record round-trips to disk and back; option data-files load and are enumerable; attempting a sub-20 character is structurally impossible (not merely rejected); a slur in the name field is blocked.
**Safety attached:** Layer 3 (age), Layer 1 (name).

---

### Stage 2 — Creator UI  **[HERE]**
**Goal:** The interface that writes character records. Rendering not yet wired.
**Depends on:** Stage 1.
**Produces:**
- **Quick-create** (minimal path — IP-Adapter target).
- **Detailed-create** (full path): progressive-disclosure, region-grouped anatomy; tags + filtered free text for backstory/personality; selection widgets (dropdowns/radials/wheels/segmented); sliders reserved for height/weight/muscle only.
- Categorical anatomy selectors (§12).
- Free-text fields routed through the Stage-0 Layer-1 filter (Layer 2 applies later, at generation).
- Reads option data-files (Stage 1); writes character records (Stage 1).
**Definition of done:** both create paths produce valid character records; anatomy is categorical with reserved sliders only where specified; adding a new option data-file surfaces new choices in the creator without code change; all free-text passes through Layer 1.
**Safety attached:** Layer 1 on all free-text input.

---

### Stage 3 — Image Pipeline  **[HARDWARE]** (highest risk; split)
**Goal:** Turn a character record into a consistent visual catalog.
**Depends on:** Stage 2 (a saved record to render).
**Safety across all sub-stages:** image-prompt Layer 1 at 3a; Layer 2 (negative prompts + content classifier) across 3a–3g; content-line policy must be approved before starting.

- **3a — Base generation.** Record → structured prompt → SDXL-derived model call. *Done here:* code + config complete and structurally sound. *Hardware:* produces a coherent image from a record. **DONE-HERE 2026-07-10 (hardware-validation flag PENDING).**
- **3b — IP-Adapter baseline identity.** Reference image → steered generation for immediate consistency (quick-create path). **DONE-HERE 2026-07-11 (hardware-validation flag PENDING).**
- **3c — Identity bootstrap + auto-filter.** Single strong reference → seed batch → face-embedding cull (ArcFace/InsightFace) + quality score → optional face-swap identity lock → small vetted grid for user confirmation. (§6) **DONE-HERE 2026-07-11 (hardware-validation flag PENDING).**
- **3d — LoRA promotion.** Train identity LoRA on the ~15–30 vetted set. Heavier/quality-max settings authorized. **DONE-HERE 2026-07-11 (hardware-validation flag PENDING).**
- **3e — Seed catalog generation.** Core matrix (expressions × poses × outfits) via the LoRA. (§7) **DONE-HERE 2026-07-11 (hardware-validation flag PENDING).**
- **3f — Matting / keyable output.** Background removal (or keyable-background generation) so frames composite cleanly. (§13) — **Stage 5 depends on this.** **DONE-HERE 2026-07-12 (hardware-validation flag PENDING).**
- **3g — On-demand generation + cache.** Novel states generate on demand, auto-filter, cache into the growing per-character library. (§7) **DONE-HERE 2026-07-12; hardware-VALIDATED same day (§19 all items PASS).**

**Definition of done (stage):** each sub-stage's code + config complete and structurally verified here; on hardware, the full path produces a consistent catalog for a test character with identity holding across the core matrix, and on-demand frames cache and matte correctly. Hardware-validation flag stays pending until the user confirms.

---

### Stage 4 — Library & Management  **[HERE]** (regeneration triggers depend on Stage 3 on hardware)  ✅ **DONE 2026-07-13**
**Goal:** Manage saved characters and their catalogs.
**Depends on:** Stage 1 (records), Stage 3 (catalogs exist to manage).
**Produces:**
- View / sort / filter / edit.
- Edit → **offers** regeneration + **marks catalog stale** (§14).
- Per-character footprint display (LoRA + catalog + cached frames).
- Deletion recommendation past threshold + **automatic LRU cap** backstop (evicted frames regenerate on demand). (§14)
**Definition of done:** characters list/sort/filter; editing marks stale and offers (not forces) regeneration; footprint displays accurately; LRU cap evicts correctly and the recommendation surfaces at threshold. (Actual regeneration invocation validated on hardware via Stage 3.)
**Safety attached:** none new.
**Outcome — all DoD MET (done-here; 819 tests passing, 1 skipped; live-window scripted smoke 22/22 PASS):**
- `app/ui/library.py` `LibraryService` (list/get/delete/thumbnail/reconcile) + `app/imagegen/manage.py` (`coerce_library_config` + pure `select_evictions`); edit path `CreatorService.update_character`; `ImageService.enforce_cache_cap`; 6 `library_*` bridges; startup reconcile in `main.run()`; `library.*` settings; front-end library view + creator edit mode. See `docs/LIBRARY.md`.
- **Both deferred items RESOLVED here** — the exact disk thresholds + LRU cap (§2 of the doc) and the catalog/bootstrap/cache↔manifest startup reconciliation sweep (§4). See the DEFERRED SPEC ITEMS annotations below.

---

### Stage 5 — Scene / Persona / Scenario / Event Builders  **[HERE]** + **[HARDWARE]** for rendering  ✅ **DONE-HERE + hardware-VALIDATED 2026-07-13** (RTX 4070 Super 12 GB; close-up-bust matte weakness folds into the 3f residual)
**Goal:** User-authored context to interact within, plus scene imagery.
**Depends on:** Stage 1 (builder record shape), Stage 3f (matting for compositing).
**Produces:**
- Lighter structured builder (tags + filtered free text) for personas/scenes/events/scenarios (§13).
- Background generation via the same image pipeline (**[HARDWARE]**).
- **Character-over-background compositing** using matted frames from 3f (compositing logic **[HERE]**).
- Background on/off toggle.
**Definition of done:** builders produce valid records via the same input model; compositing places a matted character frame over a generated background cleanly with the toggle working (compositing logic verified here; background generation validated on hardware).
**Safety attached:** Layer 1 on builder free-text; Layer 2 on background generation.
**Outcome — all [HERE] DoD MET (done-here; 921 tests passing, 1 skipped; +102 over Stage 4):**
- `app/model/builder.py` (`BuilderRecord`, one dataclass + `kind` discriminator; **code-anchored consent gate** `APPROVED_CONSENT_FRAMES` — a scenario without an approved affirmative-consent frame is unconstructable, the `age.py` Layer-3 pattern; `BackgroundManifest`) + `app/model/builder_store.py` (`BuilderStore`, parallel `data/builders/` tree) + `store.resolve_within` (shared containment, `resolve_contained` now delegates).
- Per-kind option catalogs `app/data/builders/{_shared,persona,scene,event,scenario}/` via `load_builder_catalog(..., include_bundled=False)` — the frozen §15 format is untouched, and §12's numeric-reservation check rejects any builder slider for free. Approved consent set (user-signed-off): `enthusiastic`, `established_relationship`, `negotiated_scene`, `romantic`.
- `PromptAssembler.assemble_scene` (scenery prompt, **reuses** `_gate`+`_gate_adjacency` — the HIGH-severity bypass surface — not a fork; no character identity; `scenery, no humans` anchor + `data/negative_scene.txt` people-steer). `app/imagegen/composite.py` (all-[HERE]: pure `composite_geometry` + real Pillow `composite_over`; `edge_choke`/`feather_px`/`alpha_floor` retire the 3f halo residual at composite time; transparent-passthrough when the background is off). `ImageService.generate_background` (**[HARDWARE]** SDXL leg + the new fail-closed **Layer-2** background classifier via `cull._default_classifier_factory`; reuses the generalized `_persist_image`), `composite_frame`, `matted_frames`, `background_status`, `clear_background`.
- `BuilderService` + `load_builder_guarded` + a builder reconcile sweep (the vouching model, wired into `main.run()`); 12 new bridges (`builder_*`, `scene_*`, `image_composite`, `image_matted_frames`); `image_gen.compositing.*` settings; the front-end **Scenes** view (`app/ui/web/builders.js` + editor + compositing studio). See `docs/BUILDERS.md`.
- **The 3f residual is now addressed** by the composite-time `edge_choke`/`feather_px`/`alpha_floor` (final tuning over bright/dark backgrounds rides the pending hardware pass).

---

### Stage 5.5 — Creator & Image UI  **[HERE]** + **[HARDWARE]** (split 5.5a–5.5g)

**Goal:** Make the image pipeline operable from the window, and make the creator survive option growth.

**Depends on:** Stages 0–5. Every image capability this stage exposes is already built and hardware-validated — it is unreachable, not absent.

**Rationale (verified against the tree at `66aa1e0`):** the `Api` exposes **26 `image_*` bridges; the front-end calls 4** (`image_generate_catalog`, `image_clear_cache`, `image_composite`, `image_matted_frames`). Unreachable from the window: base generation, prompt preview, reference set/clear, IP-Adapter identity generation, the entire bootstrap → cull → approve flow, LoRA train/status/clear, catalog status/clear, matting, and 3g on-demand generation. Every Stage-3 validation was driven by a scripted harness through the bridge. The image pipeline has never been operated from the UI.

**No `DECISIONS.md` change is required.** The §15 format extension is what the format exists for (its fourth backward-compatible extension); §12's slider reservation, §10's quick/detailed tiers, §6's IP-Adapter-then-optional-LoRA ordering, §2's one-window rule and §3's one-heavy-model rule are all honored as written.

**Safety attached:** none new. Layer 1 (creator free text, image prompt), Layer 2 (cull, matte, background), Layer 3 (age, consent), Layer 4 (audit) are unchanged and must be re-run on every path this stage exposes.

---

#### 5.5a — Long-running-job contract  **[HERE]** (timing **[HARDWARE]**)  ✅ **DONE-HERE 2026-07-13** (hardware-timing flag PENDING)

The prerequisite. Nothing else in the stage can be built on synchronous bridges.

**Outcome — all [HERE] DoD MET (done-here; 965 tests passing, 1 skipped; +43 over Stage 5):**
- `app/jobs/` — `JobRunner` (single daemon worker = the structural single GPU slot §3; bounded `queue.Queue`; `submit`/`status`/`cancel`/`list_jobs`/`wait_for`; per-state persistence to `data/jobs/<job_id>.json`), `CancelToken` + a **thread-local** `current_token()` seam, `JobCancelled` (subclasses `Exception` *directly* so no service loop's `except` tuple catches it), and `CancellableEngine` — a transparent engine proxy that checks the token before each `generate*` (raising `JobCancelled`, which unwinds through the loops' `finally: unload()`) and ticks per-frame progress, and is a **pure pass-through when no job is active** (the 922 legacy tests + every harness call on the main thread with no token → byte-identical).
- **Cancellation.** Cooperative for the in-process loops (bootstrap / catalog / on-demand, between frames) via the proxy; `Popen.terminate()` for the kohya subprocess — `_KohyaSubprocessTrainer.train` converted `subprocess.run`→`Popen`+`communicate` (kill+reap on timeout; guarded, de-registered terminate hook), the only [HARDWARE] trainer-backend edit (`train_lora` itself byte-unchanged). A terminated train → `TrainFailed` → returns before `os.replace` → **prior LoRA preserved** (3d invariant intact). `matte_catalog` (CPU ONNX, no engine) and single-frame `generate_background` run as pollable + reap-safe jobs but are **pre-flight** cancellable only — matching the spec's cancellation clause, which names only the four loop/subprocess paths.
- **Reap sweep** `JobRunner.reconcile()` mirrors the Stage-4/5 vouching model (own dir, `.json` only, corrupt→skip-not-delete): a fresh process owns no jobs, so any persisted non-terminal record is a dead orphan → marked terminal `interrupted`; terminal records past `jobs.retain_seconds` pruned. Wired into `main.run()` beside `library`/`builders` reconcile. **This closes the 3g item-10 hard-kill orphan-window residual** (documented, undrilled at 3g) at the job layer.
- **Wiring:** `build_image_service` wraps the engine; `main.run()` builds the `JobRunner` (release=idempotent `engine.unload` on every job end, keeping the slot free for §3 swapping) and reaps; `jobs.*` settings (queue bound + retention, defensively coerced — a hand-edited null never crashes launch); additive `job_submit`/`job_status`/`job_cancel`/`job_list` bridges with a six-kind dispatch. **The front-end is NOT rewired (5.5c–d owns that); the synchronous `image_*`/`scene_*` bridges are unchanged** and still call-through for tests + harness.
- **Adversarially verified:** red-team (20k-iteration `register`/`cancel` race hammer, path-traversal probes, Popen timeout/cancel drives — 0 defects survived), correctness review (one low-sev hand-edit `queue_size` crash — fixed + tested), DoD/scope audit (all items PASS, zero front-end/format scope creep).

**Finding:** zero `thread` / `async` / `queue` / `progress` / `evaluate_js` in `shell.py` or `main.py`. Every bridge is a plain synchronous method returning one dict at the end. Measured on the RTX 4070 Super: `image_train_lora` = **31.5 min**, `image_bootstrap_generate` (64 candidates) ≈ **15 min**, `image_generate_catalog` = **287 s** — and that last one is **already wired into `library.js`**, so the shipped app has a live five-minute silent hang.

**Produces:**
- `app/jobs/` — a `JobRunner`: `submit(kind, fn, **kw) → job_id`, a **single GPU slot** (§3 already forbids two heavy models resident; the runner makes it structural rather than incidental), a bounded queue, and cancellation.
- **Progress by polling, not push.** A `job_status(job_id)` bridge the UI polls at ~1 Hz. `window.evaluate_js` push is rejected: it can deadlock the bridge thread, it is fragile across view switches, and at 1 Hz over a 31-minute train the polling cost is nil.
- Job state persisted to `data/jobs/<job_id>.json` (kind, character_id, phase, progress, started_at, result | error). A hard kill leaves a recoverable record; the **Stage-4 reconcile sweep reaps it** under the same vouching rules (own artifact patterns, own directory, trusted manifest or nothing).
- **Cancellation.** Cooperative for in-process loops (bootstrap per-candidate, catalog per-cell, on-demand per-attempt); `Popen.terminate()` for the kohya subprocess. A cancelled train **must not destroy the prior LoRA** — 3d's `os.replace`-only-on-success invariant already guarantees this and must not be weakened. The VRAM slot is released in a `finally` on every cancel path.
- Job entry points wrap `image_bootstrap_generate`, `image_train_lora`, `image_generate_catalog`, `image_generate_on_demand`, `image_matte_catalog`, `scene_generate_background`. **The existing synchronous service methods stay** — 922 tests and every scripted harness call them. Wrap, do not rewrite.

**Definition of done:** a job survives a view switch and reports progress; cancel works on an in-process loop (bootstrap) and on a subprocess (train), releasing the VRAM slot and preserving the prior LoRA; a killed app leaves a job record the reconcile sweep reaps; the synchronous methods are byte-unchanged; suite green.

---

#### 5.5b — Prompt budget  **[HERE]** design, **[HARDWARE]** backend  ✅ **DONE-HERE 2026-07-13** (chunking A/B flag PENDING; token-count VALIDATED here on the local tokenizer)

**Outcome — all [HERE] DoD MET (done-here; part of the 965-passing suite):**
- **Trigger defect FIXED first.** `generate_catalog` + `generate_on_demand` now read the trigger from the persisted `LoraManifest.trigger` via `_generation_trigger` (falls back to derivation only for an absent / empty / unreadable manifest); derivation (`_lora_trigger`) happens ONLY at train time. A LoRA trained before the change keeps its old trigger and still fires — tested (`test_generation_reads_trigger_from_manifest_not_derivation`). This is the two-line surgical change inside the otherwise byte-unchanged sync methods.
- **Trigger shortened** to 6 hex chars (`sha1(id)[:6]`, ~4 CLIP tokens) from the prior 16-char `cfid`+12hex (11 tokens = 14% of the budget); every 3d property preserved (SHA1-derived, provably `[0-9a-f]`, no minor-coded substring reachable from hex).
- **Chunked long-prompt encoding** in `engine.py` (`encode_chunked` + `_comma_windows`, used by all three backends): split the assembled positive/negative on commas into ≤75-content-token windows, `encode_prompt` each, `torch.cat` the embeds along the sequence axis, pooled from window 0, both chunk-lists padded to a common `k` so `prompt_embeds`/`negative_prompt_embeds` are equal-length by construction (the diffusers CFG requirement). API locked from diffusers 0.39 source. **No new dependency** (`compel` rejected — 3f precedent). A short prompt yields one window → behaviourally identical to the old string path. Empirically confirmed here: tokenizer-1 (ViT-L) and tokenizer-2 (bigG) content counts are identical on the model's real BPE, so windowing on `pipe.tokenizer` does not under-count the bigG path.
- **Token accounting** — `clip_token_counter` (engine, the model's own `CLIPTokenizer` from `<pipeline_config_dir>/tokenizer`, lazy + offline; `None`→honestly unavailable, no vendored second BPE) + `token_report` (prompt: total, per-piece marginal cost + cumulative, the 77-boundary index). Surfaced through the previously-uncalled `image_prompt_preview` bridge under a `tokens` key (5.5c wires it into the creator). **Validated here** — the local `models/sdxl_config/tokenizer/` files are present, so the real-count path is exercised in the suite (`test_clip_token_counter_returns_real_counts`); the structured-unavailable path is the sandbox posture.

**Finding (measured with the real CLIP BPE tokenizer against the real option catalog):**

| Record shape | Assembled | Model sees |
|---|---|---|
| Quick-create (7 selections — the shape every hardware validation used) | 57–69 tok | all of it |
| **Fully-detailed, base (3a)** | **106 tok** | 77 — outfit tags, style tags, appearance notes **dropped** |
| **Fully-detailed, catalog/on-demand cell (3e/3g)** | **137 tok** | 77 — distinctive features, outfit, style, free text, **and the cell's own outfit / expression / pose** dropped |

The truncation has not bitten because **no fully-detailed character has ever been rendered.** It bites on the first one, and again on every option added.

**Produces:**
- **Chunked text encoding** in `engine.py`: split the assembled positive on commas into ≤77-token windows, `encode_prompt` each, concatenate `prompt_embeds` along the sequence axis, take `pooled_prompt_embeds` from the first window; pad the negative to a matching sequence length (diffusers requires equal lengths under CFG). **No new dependency** — the 3f precedent (direct reimplementation over a dependency with conflicting pins) governs; `compel` is rejected because it drags `transformers`/`diffusers` pins into a stack running transformers 5.13. The exact diffusers surface is unexecutable in the sandbox and gets the same research-then-implement treatment 3b and 3f received.
- **Token accounting** on `AssembledPrompt`: total tokens, per-piece cost, and the index of the 77-boundary. Backed by the model's own `CLIPTokenizer` loaded from the local `models/sdxl_config/tokenizer/` (already on disk from the 3b offline posture), lazily imported like every other heavy import. On the sandbox it reports honestly unavailable — no vendored second tokenizer that can drift from the model's.
- **LoRA trigger cost.** The 3d SHA1 trigger `cfidafa4efa8344b` costs **11 CLIP tokens — 14% of the entire budget.** Shorten to 6 hex chars (~4 tokens), preserving 3d's property (SHA1-derived, provably `[a-z0-9]`, no minor-coded substring).
- **Latent defect this exposes — fix first.** `service.py` **re-derives** the trigger from the record at generation time (`_lora_trigger(record)` at `generate_catalog` and `generate_on_demand`) rather than reading it from the persisted `LoraManifest.trigger`. Any change to the derivation silently de-triggers every previously-trained LoRA: the weights load, the token they were conditioned on is absent, identity weakens with no error. **The generation path must read the trigger from the manifest; derivation happens only at train time.** This is correct independent of the shortening, and it is what makes the shortening safe for existing LoRAs.

**Definition of done:** a fully-detailed record renders with its outfit / style / free-text / pose fragments present in the conditioning (hardware: A/B the same record and seed with and without chunking — the previously-dropped fragments must visibly appear); `image_prompt_preview` returns a real token count on hardware and a structured unavailable on the sandbox; the trigger costs ≤5 tokens; a LoRA trained before the change still triggers correctly; suite green.

---

#### 5.5c — §15 format delta + creator widgets  **[HERE]**  ✅ **DONE-HERE 2026-07-13** (token-panel real-count VALIDATED here)

**Outcome — all DoD MET (done-here; 981 tests passing, 1 skipped; +16 over 5.5b):**
- **§15 fourth extension** (`app/model/options.py`): group `required` + `widget` (closed enum `segmented|chips|swatch|picker|slider`, unknown → load-time `OptionFormatError`) + option `image`. `_check_required_quick` enforces **required ⟹ quick** on both the new-group and merge paths (a required-but-not-quick group, or a merge flipping quick off while required stays, is a load-time error). `derive_widget` is the sole widget authority (slider if numeric → swatch if any colour → segmented if single ≤5 → chips if ≤12 → picker) and `OptionCatalog.required_group_ids()` is the catalog-derived required set. The 7 render-identity groups (`race`/`gender_presentation`/`skin_tone`/`hair_color`/`hair_style`/`eye_color`/`body_type`) are marked `required:true` in the data files; muscle's meaningless `%` unit dropped.
- **Required-selection construction gate** (`app/model/character.py`): `MissingRequiredSelection` + `CharacterRecord.create(required_groups=…)` — a NEW character (create OR edit) cannot be constructed without the render-identity minimum, enforced at the `.create()` boundary (the `age.py` "construction is the gate" pattern) driven by the catalog's required set. **Load (`from_dict`) stays lenient** — a legacy record loads and the gap surfaces as a `validate_against` soft lint (the §15 record-is-source-of-truth stance; safety-vs-completeness split — age re-gates on load because it is safety-critical, required-selections do not because they are a drift/quality concern). This closes the 3g "validation record pins no eye color → drift" residual: the two on-disk records still load, and the incomplete one now lints its missing skin_tone/hair_style/eye_color.
- **Creator service** (`app/ui/creator.py`): passes the catalog required set into every construction; `_group_payload` emits the resolved `widget` + `required` + `prompt_ranges`; `_resolve_option_image` containment-resolves each option `image` to a bounded, CSP-displayable `data:` URI via `resolve_within` (a hostile `..`/absolute/symlink path yields no thumbnail, never a raise or out-of-tree read); `MissingRequiredSelection` maps to a structured `kind:"required"` on create + update. `builders.py` shares `_group_payload` with a null image resolver (builder options carry no thumbnails).
- **Front-end** (`web/creator.js` + `app.css`): the old `<select>` is **gone** — the backend-derived widget drives five renderers (segmented / chips / swatch tiles / searchable-tiled-virtualized picker / slider). The **picker** holds ~200 options (search + filter, image/colour/label tiles, capped render with a "N more — refine" tail) so a drop-in 60-option file becomes a searchable picker with no code change. Sliders drop the `%`, show the live `prompt_ranges` band label + imperial-at-display (cm→ft/in, kg→lb; storage stays metric). The **live prompt panel** reads `image_prompt_preview` (assembled positive + per-fragment provenance + CLIP token count + the 77-boundary marker; refreshed on entering edit mode and after every save — the bridge loads a saved record). Content-filter status shows **only on a block** (no per-keystroke "passes" line). Sections are collapsible `<details>` (progressive disclosure generalized), and the layout is a responsive two-column (form + sticky prompt panel) using the window at width. Required groups carry a `*` marker + a client-side pre-save check (the backend gate is the truth).

**Produces — three optional keys (the format's fourth backward-compatible extension):**

- group **`required: true`** — record construction fails without it, enforced in `CharacterRecord` (the `age.py` "construction is the gate" pattern), not merely in the UI. **The required set is the quick set:** `race`, `gender_presentation`, `skin_tone`, `hair_color`, `hair_style`, `eye_color`, `body_type` — exactly the render-identity minimum. A `required` group that is not `quick` is a **load-time format error** (quick-create would be unsatisfiable). This closes the 3g residual "the validation record pins no eye color, so it drifts frame-to-frame."
- group **`widget: "segmented" | "chips" | "swatch" | "picker" | "slider"`** — optional override of the derivation. Closed enum; an unknown value is a load-time format error.
- option **`image: "<relative path>"`** — optional thumbnail for picker tiles, containment-resolved under `app/data/options/` via `store.resolve_within`. Nothing uses it yet; it is taken now because adding it later touches every option file, and a visual character creator whose race and outfit pickers show examples is the better result.

**Widget derivation** replaces `options.length > 8 && !hasColors → <select>`:

```
kind slider|number         → slider
any option carries `color` → swatch
kind single, ≤5 options    → segmented
kind single|multi|tags, ≤12 → chips
otherwise                  → picker
```

An explicit `widget` overrides it. **`<select>` is deleted from the creator.**

- **`picker`** — searchable, filterable, tiled, virtualized; built to hold 200 options. Renders `image` thumbnails when present, `color` swatches when present, labels otherwise. This is the component that keeps §15's promise ("drop in a file, no rebuild") alive as the catalog grows: a new 60-option race file becomes a searchable picker automatically, with no code change.
- **Sliders.** Drop the meaningless `%` unit on muscle. Render the live `prompt_ranges` band label under every slider ("Toned", "Tall", "Lean frame") — the semantic already exists in the data and is what the model is actually told. **Imperial at display only**, derived from the existing `unit` (`cm` → ft/in, `kg` → lb). **Storage stays metric** — the `prompt_ranges` bands are metric (152/167/182/197 cm; 52/75/100 kg) and every stored record holds cm/kg; rewriting the numbers breaks both.
- **Live prompt panel**, off the existing-but-uncalled `image_prompt_preview` bridge: the assembled positive, per-fragment provenance, the token count, and the 77-boundary marker. The single change that makes every other option decision visible.
- Content-filter status shown **only on a block** (`creator.js:206` currently reports "Passes the content filter" on every keystroke).
- Sections collapsible — generalize the anatomy-region progressive disclosure to every section.
- Responsive layout — the form must use the window at maximized width.

**Definition of done:** a drop-in 60-option file surfaces as a searchable picker with no code change; a record missing a required group cannot be constructed; a `required`-but-not-`quick` group is a load-time error; the token panel shows the real count on hardware; no `<select>` remains in the creator; suite green.

---

#### 5.5d — Character image UI  **[HERE]** wiring, **[HARDWARE]** operation  ✅ **DONE-HERE 2026-07-13** (hardware acceptance run rides with 5.5g)

**Outcome — all [HERE] DoD MET (done-here; 1004 tests passing, 1 skipped; +23 over 5.5c/5.5f; JS node-syntax-checked + DOM-id cross-check clean; adversarially reviewed):**
- **The image pipeline is now operable from the window.** New `web/profile.js` — the character profile reached from a Library card's **Open** — surfaces every character-image capability over one saved character: **identity** (avatar candidates → pick → `image_set_reference`; IP-Adapter render at the 3b plus-band scale slider [0.3–0.6, default 0.45]; clear reference), **promotion** (bootstrap → the machine-vetted grid with checkbox approval → `image_confirm_vetted` → train; re-cull / discard; clear LoRA — explicit, never part of creation, §17), **catalog** (generate / matte / clear), **on-demand posing** (an `{expression, pose, outfit}` id-triple picker off `image_catalog_states` → `image_generate_on_demand`), and **storage** (footprint / staleness / cache clear / delete). Every heavy op runs through the **job contract** (`web/jobs.js`: submit → poll-at-1 Hz → terminal, with a reusable progress + **cancel** widget) — never a synchronous image bridge.
- **Create wizard, final optional step** (`web/creator.js`): after a character saves (without a reference), it OFFERS avatar candidates (`avatar` job → `image_frame_thumbnail` grid → pick → `image_set_reference`) or a skip-to-Library — the §10 quick-create IP-Adapter tier.
- **Latent 287-s hang CLOSED** in both places §6/§10 determined: `creator.js` (`showUpdateOffer`) and `library.js` (`doRegenerate`) no longer call `image_generate_catalog` synchronously — both submit `job_submit("catalog", …)` with progress + cancel.
- **Enabling reads + job kinds** (`service.py` / `shell.py`): `image_frame_thumbnail` (a bounded JPEG data URI for ANY char-owned frame — the CSP forbids showing disk paths; missing/escaped/corrupt → None), `image_catalog_states` (ids-only pose space), and two job kinds — `avatar` (`generate_base_candidates`, batch base renders, sets nothing, engine unloaded in `finally`) and `identity` (one IP-Adapter render, off the bridge thread). All UI numbers clamp (`_coerce_thumb_px`, `_coerce_candidate_count`) — a hand-edited Infinity degrades, never crashes.
- **Bridge reachability:** all four bridges orphaned by 5.5f (`image_bootstrap_recull` / `image_clear_bootstrap` in the promotion panel; `image_engine_status` / `image_engine_release` re-homed in a Settings "Image engine" diagnostic) are reachable again. The one bridge without a named caller — `image_generate_base` — is **intentionally superseded** by the avatar batch (`generate_base_candidates`); the base-render capability is reachable, the singular bridge is not needed.
- **[HERE]/[HARDWARE] split honored:** every generation leg returns structured engine-unavailable on the sandbox and is wired for the 5.5g acceptance run; no new safety layer, Layers 1–4 re-run on every surfaced path (avatar/identity/on-demand assemble through the gated prompt path; the pose picker sends ids only), new UI disk-read paths go through `resolve_within` containment.

The 22 unreachable bridges get a surface, in the two places §6 and §10 already determine.

**Create wizard, final step — the reference image.** Generate N base candidates (`image_generate_base`, varied seeds) → pick one → `image_set_reference`. **Offered, not mandatory:** the character saves without it; the step invites it. This is the quick-create identity tier (IP-Adapter, §6/§10). Runs as a job (5.5a).

**Character profile (inside the Library) — everything else.**
- **Identity panel** — reference set/clear/regenerate; IP-Adapter identity renders; scale control (`standard` default 0.55; `plus` band 0.3–0.6, default 0.45 — the 3b hardware finding).
- **Promotion flow (§6, never had a UI):** bootstrap → the machine-vetted grid → `confirm_vetted` → train. Bootstrap and train run as jobs with progress and cancel; the grid is the approval surface (`image_bootstrap_status` → grid, `image_confirm_vetted` → the training set). Promotion is an explicit action, never part of creation — §17 rejects LoRA-on-every-character because of **throwaway characters**, not because of the 31.5 minutes.
- **Catalog** — browse the seed matrix, regenerate, matte, clear.
- **On-demand posing** — an `{expression, pose, outfit}` id-triple picker → `image_generate_on_demand`. This is "posable through prompts"; `resolve_cell` already implements it and `data/catalog_states.json` is the editable state space (§15).
- Footprint, staleness, LRU cache status, delete.

**Definition of done:** on hardware, create → avatar → bootstrap → approve → train → catalog → novel pose → composite over a scene, **entirely from the window**, with progress and cancellation working. No script.

---

#### 5.5e — Library at scale  **[HERE]**  ✅ **DONE-HERE 2026-07-13**

**Outcome — all DoD MET (done-here; part of the 1004-passing suite):**
- **Footprint caching** (`ImageService.refresh_footprint`): reloads the record FRESH, `measure_footprint`s lora/catalog/cache, and caches into the existing `IdentityAnchor.footprint` (NOT a new field) — never raising, never `touch()`ing (a derived-artifact change is not a record edit and must not reorder "recently updated"), and re-loading fresh so a 287-s catalog job or 31-min train can never clobber a concurrent creator edit (only the footprint field is overwritten). Wired at every byte-changing op (`generate_catalog` / `clear_catalog` / `matte_catalog`[matted∨removed] / `generate_on_demand` / `clear_cache`; train/clear_lora already persisted it) and into the Stage-4 **reconcile** sweep (also the migration path for pre-cache records). `LibraryService._summary_row` now READS the cached value for OK rows — the ~10k-`stat()`-per-refresh disk walk at 200 characters is gone; broken rows still measure directly (the walk stays off the hot path).
- **Tag filtering:** `library_list` carries `_tag_labels` (record.tags → catalog labels, unknown option id → raw id, §15) covering archetype / distinctive-features / traits / wardrobe; the UI filters with AND-match tag chips.
- **Layout toggle + virtualized list** (`web/library.js`): a grid⇄list toggle where the list layout is virtualized (only the visible window is in the DOM — `ROW_H`-based, bounded indices), so 200+ characters stay responsive.
- **Profile view:** the `#view-profile` container 5.5d lives in, reached from a Library card's Open; `get_character` returns the cached footprint for its header.

- **`list_characters` cost.** It calls `measure_footprint()` per character, which `rglob("*")` + `stat()`s the whole character directory (114 MB LoRA, 20 catalog frames, 20 mattes, cache). At 200 characters that is ~10k `stat()` calls per refresh. Cache the footprint into the record's existing `Footprint` field on artifact change; recompute on demand and at reconcile. "Slow is acceptable" (§3) authorizes model work that buys quality — it does not license a disk walk that buys nothing.
- **Tag-based filtering** — records carry `archetype` / `distinctive_features` / `traits` / `wardrobe` tags; `library_list` must return them.
- Layout toggle (grid / list); virtualized list.
- **Profile view** — the container 5.5d lives in.

---

#### 5.5f — Navigation  **[HERE]**  ✅ **DONE-HERE 2026-07-13**

**Outcome — all items MET (done-here; part of the 981-passing suite; JS node-syntax-checked + DOM-id cross-check clean):**
- **Home view deleted.** Its status card + model/logging settings + the Layer-1 filter harness moved behind a **gear → Settings view** (`#view-settings`); the filter tester is now a collapsible **Developer tools** `<details>` panel there. **Library is the landing view** (`app.js` opens on it), and **Create is a button on the Library toolbar** (`#lib-create` → `Creator.beginCreate()` → the create view; the Create nav item is gone). The create view gained the two-column form + live-prompt layout.
- **Scenes split.** **Scene** is now the primary/default builder kind (`builders.js` `currentKind="scene"`, tab reordered first) beside its compositing studio — the real hardware-validated image consumer. **Persona / event / scenario** stay as frozen tabs with unchanged vocabulary (chat-context records with no reader until Stage 6d); the view hint states the split. No builder record shape or option vocabulary was touched.
- `STAGE` advanced to "Stage 5.5 — Creator & Image UI" (shown in the new Settings view + the sidebar).

#### 5.5f (spec) — Navigation

- **Delete the Home view.** It is build scaffolding: a status card, a settings block, and a Layer-1 filter test harness. **Library becomes the landing view** (characters are the app's nouns), Create is a button on it, settings move behind a gear, and the filter tester becomes a dev panel inside settings.
- **Split Scenes.** **Scene** has a real, hardware-validated consumer (background generation + compositing) and moves beside the image system. **Persona / event / scenario** are chat-context records with **no reader anywhere in the codebase** — their UI is **frozen as-is** and is designed against 6d's persona-injection format in Stage 6.

---

#### 5.5g — Promoted residuals  **[HARDWARE]**

- **3f BiRefNet escalation for close-up busts — un-parked.** It was filed low priority on the reasoning that "full-scene composites use the wide frames." 6e's avatar **is** a bust, and busts key at 0.4–7% transparent. `birefnet` is already a constants-only variant sharing the `_OnnxMatter` codepath: this is model placement plus a per-frame variant selection routed by the coverage reading the matte gate already computes, not new code.
- **3g states-file canonical tags.** `over_shoulder`'s "looking over shoulder" → the canonical Danbooru tag "looking back". Audit every fragment in `catalog_states.json` against canonical booru tags — the checkpoint is Illustrious-family and booru-conditioned. (Truncation was tested as an alternative explanation and **refuted**: on the sparse validation record the pose fragment sits at tokens 56–66 and reached the model. The bad-tag diagnosis stands.)
- **3c Layer-2 minor-appearance recall check** + the optional face-swap leg. User-directed safety residuals; pairs with the 3f blocked-frame purge drill. Scheduled here, not parked.

---

### Deferred out of this stage

**Personality, backstory, persona, event, and scenario vocabulary → Stage 6.** These groups are `render:false` and have **no reader in the codebase** — Stage 6d's persona injection is what reads them, and backstory seeds the §9 RAG store. Their structure is not merely unbuilt; it is unknowable without a live chat loop (§9 already concedes the decay model needs a tuning pass against real conversation, and persona injection has the same property). Designing the vocabulary now produces a guess that gets rewritten after the first real conversation.

### Stage definition of done

- Full suite green (922 + new).
- Live-window scripted smoke, all pass, one window throughout.
- **Hardware acceptance:** a **fully-detailed** character created, avatar generated, bootstrap run, grid approved, LoRA trained, seed catalog generated, a novel pose generated on demand, and composited over a scene background — **entirely from the window** — with progress and cancellation working, and with the detailed record's outfit / style / free-text fragments verifiably present in the conditioning.

### Suggested session split

| Session | Scope | Ends with |
|---|---|---|
| 1 | 5.5a jobs + 5.5b prompt budget & trigger fix | suite green; sync methods unchanged — **DONE 2026-07-13** |
| 2 | 5.5c format delta, widgets, creator + 5.5f nav | drop-in 60-option file → picker, no code change — **DONE 2026-07-13** |
| 3 | 5.5d image UI + 5.5e library at scale | every bridge reachable from the window |
| 4 | **[HARDWARE]** 5.5g residuals + the acceptance run | the full path, from the window |

---

### Stage 6 — Chat Loop  **[HARDWARE]** (memory/decay/selection logic partly **[HERE]**; split)
**Goal:** Interact with characters; persistent human-like memory; avatar updates with conversation.
**Depends on:** Stages 1–5.
**Safety across:** Layer 2 (system-prompt boundaries + refusal) at 6d; Layer 4 logging across chat + generation; explicit attention to the manipulation-toward-prohibited-outcome category at 6d + Layer 4 review (§11).

- **6a — Model load/swap manager.** Chat ↔ image, **sequenced** to avoid VRAM contention (§9). Code **[HERE]**; swap behavior **[HARDWARE]**.
- **6b — RAG memory store.** Per-character embed/store/retrieve/rank. Store/retrieve/rank logic **[HERE]**; production embedding **[HARDWARE]**.
- **6c — Decay model.** Metadata (recency/salience/reinforcement), scoring function, exposed knobs, toggle-off → plain RAG (§9). Logic **[HERE]**; tuning **[HARDWARE]** against real conversation.
- **6d — Persona injection + turn assembly.** Traits + retrieved memories + rolling window → prompt. **[HARDWARE]** for live generation.
- **6e — Avatar-frame selection.** Map conversation state → catalog frame; miss → on-demand via 3g. Selection logic **[HERE]**; generation **[HARDWARE]**.
**Definition of done:** swap manager sequences correctly (verified here; timing on hardware); memory store/retrieve/rank and decay scoring behave correctly in isolation with knobs exposed and toggle working; on hardware, a multi-turn conversation shows persistent memory, correct forgetting behavior after tuning, and an avatar that updates from the catalog with on-demand fallback. Manipulation-category handling reviewed.
**Safety attached:** Layer 2 (chat), Layer 4 (logging).

---

### Stage 7 — Packaging  **[HARDWARE]** (final offline run on target machine)
**Goal:** Assemble the shippable single-launch app folder.
**Depends on:** all prior stages.
**Produces:**
- Single-launch app-folder assembly (§2).
- Model/weight bundling.
- One-window wrapper — no console, no additional windows.
- Offline verification (no network calls).
**Definition of done (final):** on the target machine, double-click launches to one window, fully offline, no stray windows/console; a character can be created, cataloged, managed, and chatted with end-to-end. This is the final acceptance test and happens on the user's hardware.
**Safety attached:** full stack present and active.

---

## CURRENT STATE

**Current stage:** **Stage 5.5 — Creator & Image UI** (in progress; split 5.5a–5.5g).
**Sessions 1 (5.5a jobs + 5.5b prompt budget), 2 (5.5c format delta + creator widgets
+ 5.5f nav), and 3 (5.5d image UI + 5.5e library at scale) are DONE-HERE** (2026-07-13;
**1004 tests passing, 1 skipped**). **Next: session 4 — 5.5g promoted residuals +
the full [HARDWARE] acceptance run.** The image pipeline is now **operable from the
window**: a character **profile** (reached from a Library card's Open) surfaces identity
(avatar candidates → reference; IP-Adapter render + scale), the promotion flow (bootstrap
→ machine-vetted grid → confirm → train), catalog (generate/matte/clear), on-demand posing,
and storage/delete — every heavy op running through the 5.5a job contract with progress +
cancel; the create wizard gained its optional final reference step; and the shipped 287-s
synchronous-catalog hang is closed in both creator.js and library.js. The library survives
scale (5.5e): the per-row `measure_footprint()` disk walk is replaced by a footprint cached
into the record on artifact change + at reconcile, `library_list` returns tags for a chip
filter, and the list has a grid⇄list toggle with a virtualized list. The creator already
survived option growth (5.5c: §15 `required`/`widget`/`image` → five data-derived widgets,
render-identity construction gate, live prompt panel). Navigation is reorganized (5.5f:
Library landing, Create button, gear settings, Scene split). Stages 0–5 are complete.
Stage 3 — Image Pipeline is **COMPLETE** (3a–3g done-here + hardware-validated on the
RTX 4070 Super 12 GB). Stage 6 — Chat Loop follows 5.5g.
**Completed stages:** Stage 0 — Scaffold + Safety Foundation (**DONE** 2026-07-10);
Stage 1 — Character Data Model + Schemas (**DONE** 2026-07-10);
Stage 2 — Creator UI (**DONE** 2026-07-10);
Stage 3a — Base generation (**DONE-HERE** 2026-07-10; **hardware-VALIDATED** 2026-07-12);
Stage 3b — IP-Adapter baseline identity (**DONE-HERE** 2026-07-11; **hardware-VALIDATED** 2026-07-12);
Stage 3c — Identity bootstrap + auto-filter (**DONE-HERE** 2026-07-11; **hardware MOSTLY-VALIDATED** 2026-07-12 post-CCIP-swap — two named items remain, see pending flags);
Stage 3d — LoRA promotion (**DONE-HERE** 2026-07-11; **hardware-VALIDATED** 2026-07-12);
Stage 3e — Seed catalog generation (**DONE-HERE** 2026-07-11; **hardware-VALIDATED** 2026-07-12);
Stage 3f — Matting / keyable output (**DONE-HERE** 2026-07-12; **hardware MOSTLY-VALIDATED** 2026-07-12 — two named residuals, see pending flags);
Stage 3g — On-demand generation + cache (**DONE-HERE** 2026-07-12; **hardware-VALIDATED** 2026-07-12 — §19 all items PASS, same-day build→review→validate; see the change log);
Stage 4 — Library & Management (**DONE** 2026-07-13 — all DoD MET; 819 tests passing; live-window smoke 22/22; both deferred items resolved; see the change log);
Stage 5 — Scene / Persona / Scenario / Event Builders (**DONE-HERE** + **hardware-VALIDATED** 2026-07-13 — all DoD MET; 921 tests passing; background generation + Layer-2 + compositing confirmed on the RTX 4070 Super 12 GB; close-up-bust matte weakness folds into the 3f residual; see the change log);
Stage 5.5a — Long-running-job contract (**DONE-HERE** 2026-07-13 — all [HERE] DoD MET; 965 tests passing; adversarially reviewed; hardware-timing flag pending, see below);
Stage 5.5b — Prompt budget (**DONE-HERE** 2026-07-13 — all [HERE] DoD MET; trigger defect fixed + shortened; chunked encoding; token count validated on the local tokenizer; chunking A/B on real weights pending, see below);
Stage 5.5c — §15 format delta + creator widgets (**DONE-HERE** 2026-07-13 — all DoD MET; 981 tests passing; `required`/`widget`/`image` §15 keys + the render-identity construction gate + five data-derived widgets + the live prompt panel; token-panel real-count validated here on the local tokenizer);
Stage 5.5f — Navigation (**DONE-HERE** 2026-07-13 — all items MET; Library landing + Create button + gear settings + Scene split; JS syntax + DOM-id cross-checked);
Stage 5.5d — Character image UI (**DONE-HERE** 2026-07-13 — all [HERE] DoD MET; 1004 tests passing; the profile view + job-driven identity/promotion/catalog/posing panels + the create-wizard reference step; the 287-s sync-catalog hang closed; adversarially reviewed; **[HARDWARE] acceptance run pending, rides with 5.5g**);
Stage 5.5e — Library at scale (**DONE-HERE** 2026-07-13 — all DoD MET; footprint caching into the record + reconcile recompute; tag chip filter; grid/list toggle + virtualized list; profile container).
**Pending hardware-validation flags:**
- **Stage 5.5a** — the job MECHANISM is fully [HERE]-tested (cancel/progress/reconcile/
  bridge contract, 965 green). **REMAINING on the target:** a real 31.5-min train and
  ~15-min bootstrap driven as jobs, cancelled mid-run — confirming `Popen.terminate`
  kills kohya, the VRAM slot frees, the prior LoRA survives, and progress polls at ~1 Hz;
  plus a hard-kill → next-boot `jobs.reconcile()` reap of a real `data/jobs/*.json`. The
  live-window job smoke (submit → poll → cancel, one window) rides the 5.5d acceptance run.
- **Stage 5.5b** — the token count is **VALIDATED here** (the local CLIP tokenizer is on
  disk). **REMAINING on the target:** the A/B — render the SAME fully-detailed record +
  seed with and without chunking; the previously-dropped outfit / style / free-text / pose
  fragments must visibly appear (and the shortened trigger must still fire the LoRA on real
  weights). This lands with the 5.5d/5.5g acceptance run.
- **Stage 5.5d** — the wiring is fully [HERE]-tested (every image_* bridge reachable from
  the window; job dispatch for avatar/identity/bootstrap/train/catalog/matte/on-demand;
  thumbnails + state-space; structured engine-unavailable on the sandbox). **REMAINING on
  the target (the 5.5g acceptance run):** drive the full create → avatar candidates → pick
  reference → bootstrap → approve the machine-vetted grid → train → catalog → matte → novel
  pose → composite over a scene **entirely from the window**, with the job progress polling
  at ~1 Hz and Cancel killing an in-process loop (bootstrap) and the kohya subprocess
  (train) mid-run — no script.
- **Stage 3a** — **VALIDATED 2026-07-12** (all eight §6 items PASS: first
  render + VRAM 10.35 GB + sidecar/audit; offline generate proven under a
  hard socket block with `models/sdxl_config` + `pipeline_config_dir` now
  set; base same-seed re-render across a full release/reload pixel-identical;
  release → 0.01 GB resident).
- **Stage 3b** — **VALIDATED 2026-07-12** (all eight §8 items PASS on the RTX
  4070 Super 12 GB; see the change-log entry — scripted real-services runs,
  like the 3a first-render). Residual observations: `plus` at the global 0.55
  scale over-steers (color cast) — its advisory band is 0.3–0.6 with code
  default 0.45; the ArcFace/buffalo_l anime-face calibration finding gates 3c
  (below).
- **Stage 3c** — **MOSTLY VALIDATED 2026-07-12** after the user-approved
  CCIP embedder swap (§11 items 1–3, 6–8 PASS: full 64-batch bootstrap,
  100% keep-rate, similarity floor calibrated on the measured CCIP gap,
  unload-before-cull live at 0.01 GB resident, socket-blocked offline run,
  single-cv2 install, `confirm_vetted` → 20-frame vetted set readable by 3d;
  see the change-log entry). **REMAINING:** item 4's false-negative side —
  the safety-critical minor-appearance recall check needs a deliberately
  minor-appearing render caught + audited, a user-directed test (the
  false-positive side is validated: 0 false blocks across 64 adult frames);
  and item 5 face-swap (default OFF) — if ever enabled, note the buffalo_l
  stack's anime-style margin applies to ITS detector too.
- **Stage 3d** — **VALIDATED 2026-07-12** (all §13 items on the RTX 4070
  Super 12 GB: full 1600-step train 31.5 min, peak 9.86 GB, LoRA holds
  identity across the 3e matrix; three real contract catches fixed — toml
  `resolution` string, UTF-8 subprocess pipes, CLIP-tokenizer prewarm — plus
  the `network_train_unet_only` default; see the change-log entry).
- **Stage 3e** — **VALIDATED 2026-07-12** (§15: full 20-cell matrix with the
  trained LoRA, 20/20 kept, 287 s, VRAM peak 10.51 GB, per-generate scale
  honored; two catches — `peft` was never pinned (load_lora_weights refuses
  without it) and diffusers 0.39's kohya te1/te2 converter regression (now a
  UNet-only engine fallback + the UNet-only trainer default; see the
  change-log entry). Custom `lora_scale` values remain tune-at-will.
- **Stage 3f** — **MOSTLY VALIDATED** (2026-07-12 entries: constants parity
  bit-identical, offline, throughput ~1.1–1.2 s/frame CPU, idempotence — now
  re-confirmed on the REAL 20-frame LoRA catalog, 20/20 matted).
  **REMAINING:** edge-quality tuning over bright AND dark composite
  backgrounds (halo knobs / BiRefNet escalation — naturally lands with
  Stage-5 compositing), and the blocked-frame purge drill (needs a
  deliberately blocked frame — pairs with the 3c Layer-2 recall check,
  user-directed).
- **Stage 3g** — **VALIDATED 2026-07-12** (all ten §19 items PASS on the
  RTX 4070 Super 12 GB; see the change-log entry). Residual observations,
  neither a code defect: (1) prompt-adherence tuning on the editable states
  file — `over_shoulder`'s fragment "looking over shoulder" was not honored
  on the validation record (the canonical booru tag is "looking back";
  `data/catalog_states.json` is drop-in-editable, §15); (2) the validation
  record pins no eye color, so it drifts frame-to-frame (a record-completeness
  matter — the UNet-only identity LoRA carries it only weakly). The item-10
  hard-kill orphan window (survivor-move → manifest-save) is documented, not
  drilled — it lands with the Stage-4 reconciliation sweep.
- **Stage 5** — **hardware-VALIDATED 2026-07-13** on the RTX 4070 Super 12 GB
  (scripted real-services measurement harness + a visual pass on the
  composites). **Background generation:** both a BRIGHT and a DARK scene
  rendered coherent, people-free settings (the `scenery, no humans` anchor +
  `negative_scene.txt` people-steer hold), first render 15–20 s incl. base SDXL
  load, **steady-state ~10.6 s/frame** @ 832×1216/28; **VRAM device-wide peak
  ~11.9 / 12.3 GB** (idle baseline ~1.78 GB → ~10.1 GB app-side; torch
  max-allocated 11.1 GB), slot released clean. **Layer-2:** the real WD14
  classifier ran on both generated backgrounds and passed clean scenery (0
  false blocks — the false-positive side; the minor-coded recall side still
  pairs with the open 3c item). **Compositing:** transparent-OFF passthrough +
  ON over both backgrounds, bottom-center @ scale 0.85, ~0.17 s/composite —
  clean on full-body / wide frames (verified visually). **NAMED RESIDUAL (folds
  into the 3f residual):** isnet_anime leaves a translucent full-frame pane on
  tight CLOSE-UP BUST frames (character fills ~85–94% of the frame → almost no
  background to key → the per-image min-max stretch keeps it at high alpha;
  composite-time `alpha_floor` up to 192 + `edge_choke` cannot remove it —
  confirmed by an alpha-distribution scan of all 20 matted frames: solid-alpha%
  spans 16–94%, wide/full-body frames matte cleanly at 72–82% transparent,
  busts at 0.4–7%). Fix when wanted: a BiRefNet / isnet-general re-matte for
  close-ups (the residual's own "BiRefNet escalation"). Low priority —
  full-scene composites use the wide frames, which are clean.

**Stage 2 DoD — all MET (378 tests passing; live-window scripted smoke ALL PASS):**
- Both create paths produce valid character records — quick (name/age +
  `quick`-flagged groups) and detailed (full sections, anatomy by region,
  free text) both persist via `CreatorService.create_character` →
  `CharacterRecord.create` (hard gates re-run) → `CharacterStore.save`;
  round-trip + `validate_against(catalog) == []` asserted in tests and
  exercised end-to-end in the live window.
- Anatomy is categorical with reserved sliders only where specified —
  structural: a numeric option group is a load-time format error unless its
  field is in the §12 closed list (height/weight/muscle, plus the age
  bounds); regioned (anatomy) numeric groups doubly rejected. Option files
  apply atomically, so a malformed fragment cannot half-merge a slider into
  an anatomy region.
- Adding a new option data-file surfaces new choices without code change —
  the form renders entirely from `creator_catalog()` (sections, quick
  membership, regions, widgets all data-driven); drop-ins surface at startup
  and live via "Reload options"; stale UI state prunes on reload.
- All free text passes through Layer 1 — live `check_text` feedback while
  typing (UX) plus the record-level gate on save (the boundary); selection/
  tag values are gated in strict prompt context (discrete prompt-bound
  tokens); slider keys gated (closing a Stage-1 gap).

**Creator (`app/ui/creator.py` + `web/creator.js`):** `describe()`/`reload()`
serialize the catalog for the UI; `create_character(payload)` does strict
shape validation (unknown groups/options rejected, sliders clamped and
finite, free text limited to the fixed field set) and returns structured
errors (`invalid`/`blocked`/`age`) the UI maps onto fields. §15 format gained
`section`, `quick`, and option `color` (all optional, backward compatible).

**Stage 3a DoD — MET (done-here; 452 tests passing; live-window scripted
smoke ALL PASS):**
- Checkpoint pick made and recorded — Illustrious-XL-family SDXL checkpoint
  (`docs/IMAGE_PIPELINE.md` §1), style-class-committed and file-swappable via
  `models.image.checkpoint_path` (§4); heavy variant + optional local
  pipeline-config dir wired.
- Record → structured prompt assembly (`app/imagegen/prompt.py`) is fully
  data-driven: quality preamble → subject anchor (code-derived from
  `gender_presentation`) → structural adult anchor + age-range fragment →
  option `prompt` fragments + slider `prompt_ranges` in catalog order →
  filtered `appearance_notes`. Groups gain a `render` flag; personality/voice
  and gender_presentation are `render:false` (chat-side / code-anchored). A
  drop-in option file changes rendering with no code change (§15), verified
  end-to-end via the live catalog.
- Image-prompt **Layer 1** attached: every fragment gated in strict `prompt`
  context with provenance (a blocked drop-in fragment names its group), plus
  an edge-normalized adjacency gate + zero-separator option-pair gate closing
  the cross-fragment join surface (a red-team HIGH: one-char separator
  overflow). **Layer 2** negative prompts carry age-coded steer-away anchors
  (`app/imagegen/data/`), positive prompt asserts adulthood structurally.
- Model call behind the swap scaffold (`app/imagegen/engine.py`): one heavy
  model at a time (§3), refuses while chat holds the slot, heavy-variant
  toggle honored, seeds resolved+recorded, all heavy imports lazy so the
  build sandbox imports clean and returns structured engine-unavailable
  errors. Generation + reproducibility sidecar persist under
  `characters/<id>/reference/` (the §6 bootstrap candidate location; no record
  mutation). Every generation + refusal audited (Layer 4).

**Adversarial verification (3-agent workflow):** red-team (prompt-gate
bypass + crash + path/ID + bridge-contract), correctness code review, and a
DoD audit. Execution-confirmed and all fixed: **HIGH** separator-overflow
join bypass (padding a fragment edge pushed a cross-fragment blocked term
past the join gate — now an edge-normalized + zero-sep-pair gate); **HIGH**
settings-persist `OSError` inside `load()`/`unload()` escaping the bridge
(now the backend is assigned before the persist and slot-writes are guarded);
**MEDIUM** `Infinity`/`1e999` in `image_gen` settings crashing the bridge via
`int(inf)` `OverflowError` (now finiteness-guarded); **MEDIUM** idempotent
`load()` + settings-derived sidecar checkpoint could record the wrong model
after a variant flip (now `load()` swaps on change and the sidecar records
the *actually-loaded* checkpoint); **MEDIUM** `_load_record` mapping
content/age/corrupt-file loads to `not_found` with no audit (now
`blocked`/`age`/`io` with a Layer-4 trail); R7 minor-coded school-scene terms
added to the contextual blocklist; L1 blank heavy-path fallback; L3 stale
VRAM slot reset at startup; M3 pipeline `close()` gc-then-empty-cache +
best-effort teardown; H2 offline posture (local `pipeline_config_dir` +
`local_files_only`, telemetry/progress-bar disabled before heavy import).

**Stage 3b DoD — MET (done-here; 516 tests passing, 1 skipped; live-window
scripted smoke ALL PASS):**
- Reference → steered generation, end-to-end: `set_reference` promotes a
  chosen in-character frame to `IdentityAnchor.reference_image_path` (stored
  char-relative — the ONLY record mutation the image pipeline makes);
  `generate_identity` re-assembles + re-gates the same 3a prompt and renders
  it IP-Adapter-steered by the stored reference, into `characters/<id>/
  identity/` with an `ip_adapter` provenance sidecar block. `clear_reference`
  + `reference_status` round out the surface.
- Checkpoint pick for the deferred IP-Adapter item (`docs/IMAGE_PIPELINE.md`
  §7): local **h94/IP-Adapter** mirror, ViT-H, `standard`|`plus` variant
  selector. The weight ↔ image-encoder pairing (the one load-bearing footgun)
  is a code constant behind the selector, so a hand-edit cannot unpair them;
  `image_encoder_folder` pinned to the slash-form `models/image_encoder`.
- IP-Adapter call behind the swap scaffold: a SEPARATE identity backend built
  and torn down through the hardened swap branch (no in-place
  `load_ip_adapter`/scale-0 toggling — that stateful path is hardware-only
  and a no-image call raises). Load-key widened to `(checkpoint, ip_config)`
  with identity preconditions checked BEFORE the idempotency short-circuit;
  one heavy model at a time (§3); heavy-checkpoint variant still honored.
- Safety unchanged and re-run on every steered frame: Layer-1 prompt gate +
  Layer-2 negative age anchors + structural adult anchor. The reference is
  **path**-validated, not content-gated (the Layer-2 pixel classifier is 3c);
  `_resolve_reference` dual-containment-checks the stored path at set-time and
  again at use-time (it lives in hand-editable `character.json`). Layer-4
  audits `identity_generated` / `identity_reference_set|cleared` / refusals.
- `ip_adapter_scale` default 0.55, engine-bound `[0,1]`, per-call override;
  a bad hand-edit degrades to the default, never crashes. Fully-offline
  posture extended (local_files_only + config-gated `HF_HUB_OFFLINE`/
  `TRANSFORMERS_OFFLINE`). Zero new dependency pins; imports clean without torch.

**Adversarial verification (design + review workflows).** A research+design
workflow first nailed the diffusers IP-Adapter SDXL API (the `load_ip_adapter`
sequence, the ViT-H `image_encoder_folder` slash-form footgun, that
`ip_adapter_image` is required once loaded so scale-0 is not a substitute for
unload) and synthesized the spec two independent designs were graded into.
Implementation caught its own bug via tests (identity-mode `ip_config=None`
was indistinguishable from base-mode `None` in the load-key, so an
unconfigured identity request could be served by a resident base backend —
fixed by checking identity preconditions before the idempotency short-circuit).
A 16-agent review workflow (red-team + code-review + DoD, each finding
adversarially verified) then surfaced exactly one confirmed defect that
survived verification: **MEDIUM** — a NUL byte in a stored reference path made
`Path.resolve()` raise `ValueError` (not `OSError`), escaping the resolver's
guard and breaking the bridge on the ordinary preview path; fixed with an
explicit up-front NUL reject plus broadening the guard to `(OSError, ValueError)`
(matching the sibling `_load_record`/`char_dir` boundaries). Confirmed
non-escape (stat faults before any out-of-dir open) and regression-tested
across all four callers. Everything else refuted or accepted-by-design (the
[HARDWARE] TOCTOU, the additive base-sidecar `stage` key).

**Stage 3c DoD — MET (done-here; 577 tests passing, 1 skipped; live-window
scripted smoke ALL PASS):**
- Seed batch from the single reference: `bootstrap_generate` reuses 3b
  `generate_identity` unchanged, varying ONLY the seed (fixed identity prompt/
  reference/scale — §6 needs a tight cluster, not pose variety), persisting
  append-only candidates under `bootstrap/candidates/`.
- Auto-filter behind four **fakeable** abstractions (`app/imagegen/cull.py`:
  `FaceEmbedder`/`QualityScorer`/`ContentClassifier`/`FaceSwapper`, path-in/
  dataclass-out) so the whole pure cull is sandbox-verified with fakes; only the
  real InsightFace/imgutils/inswapper backends are [HARDWARE]. Cull order:
  decode → detect → **content (Layer-2, hard, fail-closed)** → quality floor →
  identity similarity (ArcFace cosine ≥ 0.50) → aesthetic rank; survivors ranked
  and the top `grid_size` proposed.
- **Layer-2 image content classifier attaches here** (§11): hard-reject +
  delete + `filter_block`(layer 2) audit on every candidate, BEFORE quality/
  similarity, fail-closed (missing model → `CullUnavailable` at preflight so
  nothing is produced unclassified; a classify exception is a block), and
  re-run on the FINAL pixels in `confirm_vetted`. `minor_coded_tags.txt` is the
  editable tuning surface. Honest bar documented (defense-in-depth, not a
  guarantee).
- Optional face-swap (`inswapper`, default OFF) runs STRICTLY after the
  similarity cull on survivors only, re-classified + re-similarity-checked
  fail-closed with fallback to the original.
- Confirmation flow: `bootstrap_status` (grid/counts/phase), `confirm_vetted`
  (promote a subset → `vetted/` = the 3d input), `bootstrap_recull` (re-cull
  persisted candidates, NO image model), `clear_bootstrap`. `confirm_vetted`
  validates the selection against the TRUSTED manifest (membership + status),
  takes pixel paths from the manifest (not caller input), re-resolves
  containment, and re-classifies — no forged id / escaped path / blocked frame
  can enter the training set.
- §3 VRAM: `bootstrap_generate` unloads the image model in a `finally` (always
  frees the slot) and builds the CPU cull toolkit only afterward. §2 offline:
  models user-placed, `local_files_only`/`HF_HUB_OFFLINE`, no network. Zero
  record mutation (the vetted-manifest existence is the source of truth; no
  `has_lora`/`lora_path`/tier flag — that is 3d). New `BootstrapManifest`/
  `VettedManifest` on disk; all paths char-relative. Requirements 3c slice
  (`insightface`/`onnxruntime`/`dghs-imgutils`/`opencv-contrib-python`, dropping
  `opencv-python`); non-commercial license note.

**Adversarial verification (design + review workflows).** A research+design
workflow first nailed the InsightFace API for the unexecutable backends
(`FaceAnalysis(buffalo_l)` + `normed_embedding` cosine, the `<root>/models/
buffalo_l/` root footgun, `inswapper.get(img, target, source)` arg order,
offline loading, the research/non-commercial license) and graded two designs
into one spec. A 20-agent review workflow (red-team + code-review + DoD, each
finding adversarially verified) then confirmed the safety-critical properties
INTACT (no content-gate bypass, no un-vetted smuggling, VRAM sequencing correct,
fail-closed works) and surfaced 6 confirmed defects, all fixed: **A1** a
hand-edited manifest `candidate_id` could escape `characters/<id>/` at the
face-swap write (now `ensure_safe_id` at manifest load + a basename guard);
**A2** bridge methods could raise instead of returning structured errors — a
non-`CullUnavailable` toolkit-build failure (missing insightface import,
undecodable reference) and a corrupt/hand-edited manifest (JSON/`InvalidId`/
`TypeError`) — now `cull_unavailable`/`bootstrap_corrupt`; **A3** the `batch`
knob bypassed its `[1,256]` clamp on the default path (now clamped in the
coercion, the one knob without downstream re-validation); **A4** `confirm_vetted`
deleted the prior vetted set before copying (now a staged temp-then-`os.replace`
so a mid-copy `OSError` preserves it); **A5** the face-swap service path and
corrupt-manifest handling were untested (now covered). Regression-tested; the
[HARDWARE] backends, VRAM sequencing, and the content-gate flow were verified
correct and left unchanged.

**Stage 3d DoD — MET (done-here; 607 tests passing, 1 skipped; live-window
scripted smoke ALL PASS):**
- Trains a per-character identity LoRA on the confirmed vetted set (3c
  `VettedManifest`): each vetted image is containment-resolved (and must live
  under `vetted/`), captioned with a stable trigger + the record's *gated*
  identity description (dropping the booru composition anchors), laid out as a
  kohya dataset (`build_dataset`), trained, and the produced `.safetensors`
  collected (tolerating a suffixed output filename).
- Trainer behind a **fakeable** `LoraTrainer` (`app/imagegen/lora.py`), injected
  like the engine/cull factories, so the whole promotion flow is sandbox-
  verified with a fake trainer; the real backend is **kohya `sd-scripts` as a
  headless subprocess** (`CREATE_NO_WINDOW`, §2), user-placed + swappable. No
  heavy imports at module top; `import app.imagegen.lora` is clean without torch.
- Stores `lora/identity.safetensors` + a `LoraManifest` provenance sidecar and
  **flips `IdentityAnchor.has_lora` + `lora_path`** (the first record mutation
  since 3b's reference) + footprint. `lora_status` reports `has_lora` only when
  the flag AND the file are present; `clear_lora` fully un-promotes.
- §3 VRAM: the in-process image engine is **unloaded before** the trainer
  subprocess runs (so it gets the whole GPU), the slot is marked busy for the
  duration and reset in a `finally`, and a **failed re-train never destroys the
  prior LoRA** (the new file is `os.replace`d and the record flipped only on
  success). §2 offline: user-placed sd-scripts, no bundled weights, no new pip
  pins; every bridge method returns structured errors on the sandbox.
- **Deferred identity-tier-marker question RESOLVED:** `has_lora` + the
  vetted-manifest existence are the authoritative promotion state — **no**
  separate record tier field is added; quick vs detailed stays audited-not-
  persisted (see `docs/IMAGE_PIPELINE.md` §12).

**Adversarial verification (3 review subagents — ultracode off, so individual
agents, not a workflow).** Red-team + correctness code-review + DoD audit, each
running executed repros. They confirmed the clean bills (VRAM sequencing, prior-
LoRA safety, promotion consistency, fail-closed error taxonomy, no scope creep)
and surfaced findings, all fixed: **HIGH** a valid-JSON manifest missing a
required key raised `KeyError` (a `LookupError`) straight through the bridge —
the `_load_*_manifest` guards omitted it (now caught, across the lora, vetted,
AND 3c bootstrap loaders); **MEDIUM** `save_lora_manifest`'s `OSError` was
unwrapped and could escape after promotion (now the provenance manifest is
written first, guarded → `io`, which also fixes a footprint under-count); **LOW**
the LoRA trigger derived from the path-safe-but-not-content-gated id (now a hash
→ provably `[a-z0-9]`); **LOW** a tampered vetted entry could feed an in-dir
non-image (e.g. `character.json`) into training (now vetted entries must live
under `vetted/`); **[HARDWARE]** the kohya config used `xformers` (→ `sdpa`, no
extra dep) and exact-name output collection (→ newest-`.safetensors` fallback).
Regression-tested; the [HARDWARE] subprocess backend was verified structurally
and left otherwise unchanged.

**Stage 3e DoD — MET (done-here; 644 tests passing, 1 skipped; live-window
scripted smoke ALL PASS):**
- Renders the core matrix (expressions × poses × the character's wardrobe, or
  an as-is dimension when none) LoRA-steered, bounded by `max_frames`. Each
  cell's prompt = the constant *gated* identity (assembler with the wardrobe
  group excluded) + the LoRA trigger (lead) + the cell's outfit/expression/pose
  (extra); a blocked cell is skipped + audited. Expressions/poses are editable
  data (`data/catalog_states.json`).
- **Engine gains LoRA-at-generation** (the 3d payoff): a catalog mode with the
  load-key widened to `(checkpoint, ip_config, lora)` and a
  `_DiffusersLoraSDXLBackend` (checkpoint + `load_lora_weights` unfused,
  per-generate `cross_attention_kwargs` scale). Base (3a) and identity (3b)
  paths are byte-unchanged; a different LoRA rides the hardened swap branch.
- **Auto-filter = the same 3c cull** ("same filter as training", §7):
  content-classify (Layer-2, hard, fail-closed, audited) → similarity to the
  reference → quality. A rejected frame is deleted and its cell regenerated up
  to `max_attempts`; only survivors enter the manifest. (The face-area floor is
  relaxed *for the catalog only* — pose-varied frames have small faces — while
  the safety content gate + similarity stay at the 3c values.)
- Fills the **Stage-1 `CatalogManifest`/`CatalogEntry`** under `catalog/`
  (`frame_id`, char-relative `path`, `state={expression,pose,outfit}`,
  `on_demand=False`, `bytes`). §3 VRAM: each pass generates with the LoRA image
  model, **unloads it**, then culls on the CPU toolkit; the new frames are
  staged and swapped over the prior catalog **only on success** (rollback-safe,
  so a failed re-generate preserves the prior catalog + manifest). **Zero
  record mutation** — 3e only reads `has_lora`/`lora_path`/`reference`.

**Adversarial verification (3 review subagents — red-team, code-review, DoD).**
They confirmed the clean bills (the widened load-key + mode preconditions, base/
identity paths unchanged, VRAM sequencing, the literal-3c-cull reuse, no 3f/3g
scope creep, zero record mutation) and — notably — that the unfused
`cross_attention_kwargs` scale IS honoured on the diffusers ≥0.31 PEFT backend
for both UNet and text encoders (no `set_adapters` change needed). Findings, all
fixed: **MEDIUM** `_finalize_catalog`'s swap wasn't rollback-safe — a mid-swap
`OSError` could leave `catalog.json` disagreeing with the frames on disk (now a
rename-aside + restore-on-failure, so any failure preserves a consistent prior
catalog); **LOW** `load_catalog_states` raised `AttributeError` on valid-but-non-
object JSON (`[]`/`null`) escaping the bridge (now guarded); **LOW/tuning** the
identity-tight cull systematically rejected pose-varied catalog frames (relaxed
`face_area_min` for the catalog, content gate unchanged). Regression-tested
(no_states, malformed states, partial-success `incomplete>0`, finalize rollback,
relaxed-area). The [HARDWARE] LoRA backend was verified structurally correct.

**Stage 3f DoD — MET (done-here; 680 tests passing, 1 skipped; live-window
scripted smoke ALL PASS):**
- **Resolves the deferred matting/keying approach:** a direct-ONNX
  reimplementation of rembg's ISNet pipeline (~30 lines, MIT, attributed) on
  the already-installed `onnxruntime`+`pillow` slice, with a **user-placed**
  `isnet-anime.onnx` (SkyTNT/anime-segmentation, Apache-2.0 provenance,
  ~176 MB) as the default; `isnet_general` and `birefnet` are constants-only
  config variants sharing one codepath. **No new pip deps, no runtime
  downloads.** rembg itself NOT installed (the old opencv-conflict rationale
  is stale — dropped upstream ~2.0.72; live objections: unconditional
  pymatting/scikit-image/scipy deps, a pooch runtime downloader,
  numpy/pillow/onnxruntime floor pins); transparent-background rejected
  (second cv2 distribution); keyable-background *generation* rejected
  (discards the 3e vetting, re-rolls identity, SDXL renders no trustworthy
  flat key).
- New `app/imagegen/matte.py` behind a **fakeable `Matter` Protocol** +
  injected `MatteFactory` (the cull.py idiom): `preflight_matte` (model +
  Layer-2 classifier — deliberately NOT the face models),
  `coerce_matte_config` (variant/erode/feather/coverage knobs,
  degrade-never-crash), the pure `evaluate_matte` coverage gate, and the
  `[HARDWARE]` `_OnnxMatter` with the research-verified per-variant rembg
  constants (reproduced quirks: divide-by-image-max, per-image min-max
  stretch; deviations: epsilon guard, **putalpha keyable output** — original
  RGB + straight soft alpha, never binarized; optional erode/feather halo
  knobs). Sandbox-clean imports (no numpy/PIL/onnxruntime at module level).
- `matte_catalog(id, force)`: per entry, containment + **direct-`.png`-child
  of `catalog/`** residency (stem-keyed outputs ⇒ .png-only makes collisions
  structurally impossible) → **Layer-2 re-screen fail-closed BEFORE the skip
  check** every run (blocked ⇒ purge pixels + sidecar + recorded matte +
  manifest entry, audited) → skip valid mattes unless `force` → `*.png.tmp`
  (a temp namespace no final can carry) → coverage gate → atomic promote →
  char-relative `matted_path`. Per-frame failures never abort; every result
  shape carries the tallies. **Optimistic `updated_at` token** aborts
  `catalog_changed` rather than clobber a concurrent 3e regen; a
  `character_id`-mismatched manifest is `catalog_corrupt` (`save_catalog`
  routes by the manifest's own id); an all-skipped run saves nothing. Mattes
  live INSIDE `catalog/` (die with 3e swaps, counted by `catalog_bytes`,
  removed by `clear_catalog`). **Zero record mutation; engine untouched**
  (CPU ONNX, zero VRAM, the confirm_vetted posture).
- 2 bridges (`image_matte_catalog`/`image_matte_status`); settings
  `models.image.matting_model_path` + `image_gen.matting.*`;
  `CatalogManifest` gains an optional backward-compatible `matting`
  provenance block; `store.matted_dir`; requirements 3f slice (no pins);
  `docs/IMAGE_PIPELINE.md` §16–§17 (+ KNOWN LIMITS renumbered 16→18).

**Adversarial verification (research+design + review workflows — ultracode
on).** A research workflow first locked the rembg ISNet/BiRefNet pre/post
constants verbatim from source (incl. the divide-by-image-max and
unguarded-min-max-stretch hazards, the sigmoid-in-code split for BiRefNet,
licenses/md5s/URLs, and the CORRECTION that rembg's opencv dep is gone — the
exclusion rationale was updated, not parroted), and a judge merged two
independent designs into one spec (itself catching a nonexistent
`manifest.touch()`, a gate-after-skip contradiction, and the cross-character
`save_catalog` routing hazard). A 16-agent review workflow (red-team +
code-review + DoD; every finding independently re-executed by a skeptic — 12
confirmed, 0 refuted, 1 accepted-by-design) returned 31 clean bills
(containment incl. a 47-probe hand-edit sweep with zero tracebacks,
gate-before-skip, rollback/no-op/concurrency semantics, sandbox cleanliness,
VARIANTS re-verified against upstream) and findings, all fixed: **HIGH** a
hand-edited `"bytes": Infinity` in catalog.json raised `OverflowError`
through both new bridges — `int(inf)` is not a `ValueError`, the documented
`_generation_settings` hazard on the manifest channel (now caught across ALL
seven service loader guards, incl. the 3e catalog/record loaders, per the 3d
fix-across-loaders precedent); **MEDIUM** the blocked-frame purge deleted
only the canonical matte name while the skip check trusts ANY `matted_path`
into `matted/` — a hand-renamed matte of just-blocked pixels survived (the
purge now covers the recorded path under the same trust rule); **LOW**×2
(same root) hand-placed same-stem/other-extension sources collided onto one
matte file and the `*.tmp.png` sweep could eat a promoted final whose source
stem ended in `.tmp` (sources now `.png`-only; temp namespace now
`*.png.tmp`); **LOW** the all-failed escalation + abort dicts dropped the run
tallies and aborts left no run-level audit (tallies on every shape; aborts
log `catalog_matted` with `aborted=<kind>`); **LOW** a non-finite coverage
reading shipped an invalid-JSON `NaN` to the JS bridge, which would hang the
promise (finite-or-None now); **LOW** the factory closer freed nothing — the
matter held the live session ref (`_OnnxMatter.close()` added); plus doc/test
gaps (best-effort-token caveat + top-level kind list documented;
degenerate-under-force, default-arg-bridge, and write-then-raise-tmp test
arms added). Accepted-by-design residual: the optimistic token's
check-to-save TOCTOU window (best-effort, not a lock — documented; no
concurrent writer exists in the single-window app). Regression-tested
(**680 passing**).

**Stage 3g DoD — MET (done-here + hardware-validated; 736 tests passing,
1 skipped; live-window scripted smoke 14/14 PASS; §19 all items PASS):**
- **Novel states generate on demand:** `generate_on_demand(id, state)` takes
  a full `{expression, pose, outfit}` **id triple** — ids only, validated by
  the pure `resolve_cell` (`catalog.py`) against `data/catalog_states.json` +
  the record's wardrobe (plus the always-valid `asis` base look), so the
  bridge cannot inject prompt text (`invalid`/`unknown_state` on anything
  else); prompts come only from the editable data (§15 — new states extend
  the on-demand space with no code change). A covered state serves
  **instantly** (cache-then-catalog lookup under the 3f residency rule —
  containment-resolved direct `*.png` child; dangling/escaped entries read
  as novel); a novel state generates LoRA-steered via the **parameterized 3e
  passes** (3e callers byte-identical), staged in `cache.new/` (in-process
  failures leave zero orphans), survivor moved by O_EXCL-reserving
  `_move_unique`.
- **Same auto-filter:** the literal 3c cull per generated frame
  (content-first fail-closed Layer 2 + CCIP similarity + quality, catalog
  `face_area_min` relaxation), rejected frames deleted + regenerated up to
  `max_attempts`, then structured `frame_rejected`. **Mattes via the 3f
  `Matter`** best-effort at generation (fresh pixels NOT re-classified —
  culled seconds earlier in the same run); a matte gap **heals on the next
  hit**, and the heal — unbounded pixel age — re-classifies fail-closed
  first: a blocked frame is purged (pixels + sidecar + matte + entry, 3f
  trust rules) + audited (`image.cache.heal`) and the state regenerates.
- **Caches into the growing library:** `cache/` + `cache/matted/` + a
  `cache.json` reusing the `CatalogManifest` shape — `on_demand=true`, at
  most one entry per state (replacement purges the prior artifacts), and the
  new **`last_used`** field (additive) stamped at creation + every cache hit
  = the §14 LRU signal Stage 4 consumes; `footprint.cache_bytes` counts it
  all, separately from the catalog. Deliberately a **sibling** of `catalog/`:
  a 3e regeneration swap replaces the seed catalog while the grown cache
  survives (hardware-proven). Serve-path bookkeeping (last_used, healed
  matted_path) rides the 3f optimistic token, best-effort — never fails a
  hit, never clobbers a concurrently swapped manifest. Zero record mutation;
  zero engine changes; no new settings (the 3e knobs verbatim) and no new
  dependencies. 3 bridges (`image_generate_on_demand`/`image_cache_status`/
  `image_clear_cache`); `docs/IMAGE_PIPELINE.md` §18–§19 (KNOWN LIMITS → §20).

**Adversarial verification (3 review subagents — red-team, code-review,
DoD/scope audit, each executing repros).** Clean bills: bridge fuzz (every
3g input shape → structured), purge containment (crafted `matted_path`/
`path` cannot delete outside `cache/`·`cache/matted/`), cross-channel
manifest routing guarded, state/outfit fragments always Layer-1-gated,
VRAM sequencing (unload-in-finally before the CPU cull; heal never touches
the engine), 3e parameterization byte-identical, zero record mutation, DoD
items all MET with no scope creep (no Stage-4 LRU eviction, no 6e mapping).
Findings, all fixed + regression-tested: **HIGH** the new
`CatalogEntry.last_used` read reordered `from_dict` evaluation (`.get`
before the `["frame_id"]` subscript), turning the previously-guarded
TypeError for a non-dict manifest entry (`"entries": [null]` — a natural
hand-edit) into an **AttributeError in no loader guard tuple** — a raw
traceback through every manifest bridge incl. pre-existing 3e
`catalog_status` + 3f `matte_status` (now an isinstance→ValueError in
`from_dict` AND a shared `ARTIFACT_LOAD_ERRORS` guard tuple across all nine
loader sites incl. `_load_record`, per the 3d fix-across-loaders precedent);
**MEDIUM** a hand-edited `Infinity`/`NaN` value inside an entry's `state`
rode verbatim into the `cache_status`/serve-hit payloads — invalid JSON that
hangs the JS promise (state now str-normalized `{str: str}` at the from_dict
choke point, the record `__post_init__` stance); **LOW** `RecursionError`
from pathologically nested manifest JSON escaped every loader (now in the
shared tuple); **LOW** doc kinds-list mis-attributed `blocked` to
record-load only (a Layer-1-blocked cell is also top-level `blocked`);
**LOW** footprint test didn't pin mattes-count-as-cache_bytes (now does).
Accepted observations (within the 3f best-effort bar): per-call `*.png.tmp`
sweep, raw-but-containment-validated `entry.path` echoed in responses,
subset state matching on hand-extended entries.

**Next action (when resumed):** **begin Stage 5.5 — Creator & Image UI** (split 5.5a–5.5g:
long-running-job contract → prompt budget → §15 format delta + widgets → character image
UI → library at scale → navigation → promoted residuals). It is a **[HERE]**-dominant stage
with a **[HARDWARE]** acceptance run. It requires **no `DECISIONS.md` change**. It promotes
three residuals into scope (3f BiRefNet escalation for close-up busts; 3g canonical booru
tags in `catalog_states.json`; the 3c Layer-2 recall check + face-swap leg) and defers the
personality / backstory / persona / event / scenario vocabulary to Stage 6, where the only
reader of those `render:false` groups is built.
**Stage-5 hardware validation (when on the target, RTX 4070 Super 12 GB):**
generate a background for a scene builder (real SDXL scenery render + Layer-2
screening + block→purge drill), then `composite_frame` the live character
`c517663a…`'s real matted catalog over it — confirm the toggle and that
`edge_choke`/`feather_px`/`alpha_floor` retire the inherited **3f edge
residual** over a BRIGHT and a DARK background (this closes the named 3f
residual). **Other residual hardware items (none gating Stage 6):** 3c Layer-2
false-negative recall check + optional face-swap leg (pairs with the Stage-5
purge drill); 3g states-file prompt tuning (drop-in data edit).

---

## DEFERRED SPEC ITEMS / OPEN QUESTIONS

Carried forward; resolve at the relevant stage:

- **Specific model picks** — image base checkpoint (Stage 3) and chat LLM (Stage 6), chosen against then-current options; both swappable.
- **Decay-model knobs + defaults** — finalized during Stage 6c tuning.
- **Permitted-vs-prohibited content line** — drafted in Stage 0 for user approval; governs Stages 3, 5, 6.
- **Matting/keying approach** — **RESOLVED at Stage 3f (2026-07-12):** direct-ONNX reimplementation of rembg's ISNet pipeline on the existing onnxruntime stack, user-placed `isnet-anime.onnx` default with `isnet_general`/`birefnet` config variants; rembg/transparent-background not installed (dependency conflicts + runtime downloaders); keyable-background generation rejected (discards 3e vetting, re-rolls identity). (`docs/IMAGE_PIPELINE.md` §16.)
- **Exact disk thresholds + LRU caps** — **RESOLVED at Stage 4 (2026-07-13):** `library.cache_cap_bytes` = 256 MB (the §14 automatic per-character LRU backstop on the on-demand cache; ~115 cached states at the measured ~2.2 MB/state) and `library.recommend_cache_bytes` = 192 MB (the deletion-recommendation threshold, deliberately below the cap so deliberate management is surfaced before the backstop bites). Both hand-editable + defensively coerced (`app/imagegen/manage.py::coerce_library_config`, clamped to [8 MB, 1 TB]). The cap governs ONLY the grown cache (the seed catalog is never evicted); eviction is LRU by the 3g `last_used` signal, measured against RECORDED artifact bytes (orphans are the sweep's job, not eviction's), never evicts the just-inserted frame (`select_evictions`, `enforce_cache_cap`). (`docs/LIBRARY.md` §2.)
- **Editor UI for option data-files** — later layer on the Stage-1 format; not scheduled, added when wanted.
- **Catalog manifest ↔ frames startup reconciliation** — **RESOLVED at Stage 4 (2026-07-13):** `LibraryService.reconcile()` runs at every launch (before the window opens, fail-safe — a fault is audited, never blocks the launch) and on demand from the `library_reconcile` bridge. Per character it (1) removes the staging/backup dirs `catalog.old`/`catalog.new`/`cache.new`/`vetted.new` (only ever populated mid-run — at startup they are hard-kill leftovers, incl. the 3e double-fault `catalog.old` recovery copy → the deferred "drop `*.old` orphans"), (2) drops manifest entries whose frames no longer exist and clears dangling `matted_path` pointers (→ "verify manifest frames exist"), (3) sweeps `bootstrap/candidates/` files absent from `bootstrap.json` (the 3c mid-batch-kill class), (4) sweeps `cache/`·`cache/matted/` files absent from `cache.json` (the 3g kill-window class), and (5) runs the §14 LRU cap. Deletion discipline: only our own artifact patterns (`*.png`/`*.json`/`*.png.tmp`), only as direct children of our own dirs, only when a TRUSTED manifest fails to vouch — a corrupt manifest sweeps NOTHING on its channel (orphanhood unprovable), an absent manifest vouches for nothing. Idempotent. (Original context, now historical: the 3e catalog swap renames the frames dir and writes the sibling `catalog.json` in two non-atomic steps; a hard kill in that window could leave a `catalog.old/` orphan or a momentarily-disagreeing manifest; in-process failures are fully rolled back and self-heal on the next successful `generate_catalog` — the sweep closes the kill-window residue for catalog, bootstrap-candidate, and cache orphans alike.) (`docs/LIBRARY.md` §4.)
- **Identity-tier marker on the record** — **RESOLVED at Stage 3d (2026-07-11):** no separate record-level tier field. `IdentityAnchor.has_lora` + `lora_path` (plus the vetted-manifest existence) are the authoritative promotion state; quick vs detailed creation stays audited (Layer 4), not persisted. (`docs/IMAGE_PIPELINE.md` §12.)
- **Long-running-job contract** — **OPENED at Stage 5.5a.** `shell.py` and `main.py` contain
  zero `thread` / `async` / `queue` / `progress` / `evaluate_js`; every bridge is a plain
  synchronous method returning one dict at the end. Measured: `image_train_lora` 31.5 min,
  `image_bootstrap_generate` (64) ≈ 15 min, `image_generate_catalog` 287 s — and
  `image_generate_catalog` is **already wired into `library.js`**, so the shipped app has a
  live five-minute silent hang. Resolution: a `JobRunner` with a single GPU slot, a
  `job_status` **polling** bridge (not `evaluate_js` push — it can deadlock the bridge
  thread), job records persisted to `data/jobs/` and reaped by the Stage-4 reconcile sweep,
  and cooperative cancellation that preserves 3d's `os.replace`-on-success LoRA invariant.
  The existing synchronous service methods are wrapped, never rewritten (922 tests call them).
- **CLIP 77-token prompt budget** — **OPENED at Stage 5.5b.** Measured with the real CLIP BPE
  tokenizer against the real option catalog: a fully-detailed record assembles to **106 tokens**
  in base mode and **137 tokens** in a catalog/on-demand cell, against a hard 77-token model
  limit. The dropped tail is outfit tags, style tags, distinctive features, appearance free
  text — **and, in catalog mode, the cell's own outfit, expression, and pose.** It has not
  bitten only because **no fully-detailed character has ever been rendered**: every Stage-3
  validation ran a quick-create-shaped record (57–69 tokens). Resolution: chunked text
  encoding in `engine.py` (≤77-token windows, concatenated `prompt_embeds`, pooled from the
  first window, negative padded to match), **no new dependency** — the 3f precedent. Was
  `IMAGE_PIPELINE.md` §20 KNOWN LIMIT; update it on close.
- **LoRA trigger re-derivation (latent defect)** — **OPENED at Stage 5.5b.** `service.py`
  **re-derives** the trigger from the record at generation time (`_lora_trigger(record)` in
  `generate_catalog` and `generate_on_demand`) instead of reading the persisted
  `LoraManifest.trigger`. Any change to the derivation silently de-triggers every
  previously-trained LoRA — the weights load, the token they were conditioned on is absent,
  identity weakens with no error. The generation path must read the trigger from the
  manifest; derivation belongs only at train time. (Discovered while costing the trigger: the
  16-char SHA1 hex trigger consumes **11 of the 77 CLIP tokens** — 14% of the budget. Shortened
  to 6 hex chars, ~4 tokens, preserving 3d's provably-`[a-z0-9]` property.)
- **Creator widget vocabulary** — **RESOLVED at Stage 5.5c.** The current derivation
  (`options.length > 8 && !hasColors → <select>`) does not scale: race (13 options) and scene
  location (20) already fall into a dropdown, and every option added makes it worse. Replaced
  by a five-widget closed set (`segmented` / `chips` / `swatch` / `picker` / `slider`) derived
  from kind + cardinality + intrinsic visual, with an optional `widget` override in the §15
  format. `picker` (searchable, filterable, tiled, virtualized, holds 200) is what keeps §15's
  drop-in-no-rebuild promise alive as the catalog grows. A palette of hand-picked widgets was
  rejected: a widget is *code*, so an author picking from a palette can only pick what someone
  already wrote — a new 60-option file would silently stay a dropdown.

---

## CHANGE LOG

- *(init)* Documents created. All decisions Q1–Q13 codified in `DECISIONS.md`. Build plan drafted. No stages started.
- *(Stage 0 build)* Scaffolded the app: `app/` package (Python 3.11 `.venv`), single-window pywebview shell + JS↔Python bridge + web UI, `CharacterForge.pyw` launcher (relaunches into `.venv` under `pythonw`, `CREATE_NO_WINDOW`, MessageBox on fatal error), JSON settings with atomic/thread-safe writes and the model-swap toggle scaffold, Layer-4 append-only JSONL audit log, and the Layer-1 deterministic content filter (`app/safety/`: obfuscation-resistant `normalize.py` + `layer1.py` matching engine + editable `data/*.txt` blocklists across 8 prohibited categories). Delivered `docs/CONTENT_POLICY.md` (draft, rulings R1–R8, awaiting sign-off). Isolation test suite added.
- *(Stage 0 verification)* Ran a multi-agent adversarial workflow (red-team bypass/false-positive lenses + backend/UI code review + DoD audit). It surfaced execution-confirmed Layer-1 bypasses (incomplete homoglyph table, no hyphen/concatenation/plural tolerance, doubled-letter and leet+separator gaps, missing written/copula/ordinal age forms), false positives (compound adult ages like "twenty-two years old", "lol i"→loli, "shot a"→shota, "mounted her horse", innocent proximity anchors), and backend/UI defects (audit `json.dumps` outside try, settings temp-file race, set_setting persistence-failure contract, audit re-enable ordering, external-link/one-window hardening, unsafe geometry parse). All fixed: rewrote the matching engine (complete homoglyph table + name-based Latin-letter fold, joiner/punct/spread families with ASCII edge guards, doubled-letter + post-leet folding, automatic plural tolerance), extended age regexes, retuned data files, and patched the backend/UI. A second adversarial round found only the residual small-capital-block and multiword-plural classes, both then closed structurally. Test suite: **236 passing**; live window smoke re-confirmed (1 window).
- *(Stage 0 sign-off — 2026-07-10)* User approved `docs/CONTENT_POLICY.md` v1 (R1–R8 as drafted, no amendments). Content line frozen into `DECISIONS.md` §11. **Stage 0 marked DONE.** Per user request, paused before starting Stage 1.
- *(Stage 1 build — 2026-07-10)* Built the `app/model/` package: `Age` value type (structural 20+ gate, §11 Layer 3), `CharacterRecord` (structured tags + filtered free-text + region-grouped categorical anatomy + reserved height/weight/muscle sliders + `IdentityAnchor` has-LoRA/reference/LoRA/footprint + `CatalogManifest`), the §15 option-definition data-file format + merging loader, and a persistence layer. Added 7 bundled option files (25 groups) and an isolation test suite.
- *(Stage 1 verification — 2026-07-10)* Ran a multi-agent adversarial workflow (attack lenses on the age gate, content gates, option loader, and persistence + code review + DoD audit). It surfaced execution-confirmed defects: post-construction age mutation and free-text-KEY / selection-value / tag-value channels bypassing the gates and persisting to disk; **path traversal via a crafted `record.id`** (save/delete/catalog escaping the store, incl. `rmtree` of external dirs); and option-loader fragility (UTF-8 BOM rejection, no per-file isolation so one bad drop-in bricked the creator, uncoerced numeric bounds crashing at use time, alias/tag string-explosion). All fixed: `__setattr__`-enforced age + safe-id invariants, a single normalization/gate choke point covering every key and value on every channel, `ensure_safe_id` confining all store paths, and a BOM-tolerant, per-file-isolated, type-coercing loader with an `errors` list. A re-run confirmed all 30 attack reproductions now blocked. **321 tests passing.** **Stage 1 marked DONE.**
- *(Stage 2 build — 2026-07-10)* Built the creator: `app/ui/creator.py` (`CreatorService` — catalog description for the UI, live `reload()`, strict payload validation → record → store, structured `invalid`/`blocked`/`age` errors, Layer-4 audit of creations and blocks), bridge methods on the shell `Api` (`creator_catalog`/`creator_reload_options`/`create_character`), and a fully data-driven front-end (`web/creator.js`): quick + detailed paths, section cards, anatomy as collapsible body-region groups (§12 progressive disclosure), chips/swatch-chips/dropdown/slider widgets, live Layer-1 feedback on name + free text, field-level error highlighting. §15 format extended (backward-compatible): group `section` + `quick`, option `color`; bundled files annotated. Structural §12 rule added to the loader: numeric groups are a closed list (height/weight/muscle + age bounds) and can never carry a region.
- *(Stage 3a build — 2026-07-10)* Built the image pipeline base-generation slice: new `app/imagegen/` package — `prompt.py` (record → gated structured positive/negative prompt, data-driven from option `prompt` fragments + slider `prompt_ranges` + filtered `appearance_notes`, code-derived subject anchor, structural adult anchor, image-side Layer 1 with provenance + cross-fragment adjacency gate, Layer 2 negative-prompt anchors), `engine.py` (SDXL-derived diffusers call behind the §3 swap scaffold — lazy heavy imports, CUDA-only real backend, VRAM-slot sequencing against `models.active`, checkpoint/variant/config resolution, seed handling, request validation), `service.py` (bridge-facing orchestration: load→gate→generate→persist frame + reproducibility sidecar under `characters/<id>/reference/`→audit; structured `{ok:...}` results), and editable `data/*.txt` prompt files. Recorded the deferred checkpoint pick (Illustrious-XL-family SDXL) with rationale in new `docs/IMAGE_PIPELINE.md`. Extended the §15 option format with a backward-compatible `render` flag (default true; personality/voice + gender_presentation set false); added `image_gen` settings + `models.image.pipeline_config_dir`; wired `ImageService` through `main.build_services` and five `image_*` bridge methods on the shell `Api`; uncommented the Stage-3a slice of `requirements-full.txt` (install on target only). Startup now resets a stale persisted VRAM slot.
- *(Stage 3a verification — 2026-07-10)* Ran a three-agent adversarial workflow (red-team on the prompt gate / crashes / path-ID / bridge contract; correctness code review; DoD audit). Execution-confirmed and all fixed: **HIGH** separator-overflow join bypass (one trailing punctuation char pushed a cross-fragment blocked term past the join gate and reached real generation, logged as clean — closed with an edge-normalized adjacency gate + zero-separator option-pair gate; the residual 3-way-word-split is documented under the §11 honest bar); **HIGH** settings-persist `OSError` inside `load()`/`unload()` escaping every image bridge method raw (backend now assigned before the persist; slot writes guarded; teardown best-effort); **MEDIUM** `Infinity`/`1e999`/`-Infinity` in `image_gen` settings crashing the bridge via `int(inf)` `OverflowError` outside the try (now finiteness-guarded, never raises); **MEDIUM** idempotent `load()` + settings-time sidecar checkpoint recording the wrong model after a variant flip (load now swaps on change; sidecar records the actually-loaded checkpoint + size); **MEDIUM** `_load_record` collapsing content-blocked / underage / corrupt-file loads into `not_found` with no Layer-4 trail (now `blocked`+audit / `age` / `io`); **MEDIUM/LOW** R7 minor-coded school-scene backgrounds (classroom/chalkboard/blackboard/school desk/school hallway added to `minors_contextual.txt`); **LOW** blank heavy-checkpoint path not falling back; **LOW** stale persisted VRAM slot after a crash; plus M3 `close()` gc-then-empty-cache and the H2 offline/no-console posture (local `pipeline_config_dir` + `local_files_only`; `HF_HUB_DISABLE_TELEMETRY`/`_PROGRESS_BARS` and `diffusers` progress bar disabled before the heavy import, so a tqdm write under `pythonw` cannot fail the load). Clean bills: path/store confinement (all crafted ids → structured `not_found`/`invalid`, no escape from `reference/`), option `aliases`/`tags`/`label` do not leak into prompts, atomic O_EXCL frame-name reservation (no same-second overwrite; concurrency test), the age gate and negative-prompt exemption. **452 tests passing; scripted live-window smoke (create → engine status → prompt preview → structured engine-unavailable generate → slot release → cleanup) ALL PASS, one window throughout.** **Stage 3a marked DONE-HERE (hardware-validation flag pending).**
- *(Stage 3b build — 2026-07-11)* Built IP-Adapter baseline identity on the 3a pipeline. Ran a research+design workflow first to lock the diffusers IP-Adapter SDXL API before writing the unexecutable [HARDWARE] backend (confirmed the `load_ip_adapter(dir, subfolder, weight_name, image_encoder_folder, local_files_only)` → `set_ip_adapter_scale` → `pipe(..., ip_adapter_image=)` sequence; the ViT-H `image_encoder_folder="models/image_encoder"` slash-form footgun; that `ip_adapter_image` is required once loaded so `set_ip_adapter_scale(0)` is not a substitute for `unload_ip_adapter`), then graded two independent designs into one spec. `engine.py`: `GenerationRequest.ip_adapter_scale` (validated `[0,1]`, omit-if-None so base sidecars are unchanged); `IPAdapterConfig` + an `IP_ADAPTER_VARIANTS` code table (`standard`/`plus`, both ViT-H) so the weight↔encoder pairing is unhittable by hand-edit; a separate `_DiffusersIPAdapterSDXLBackend` that loads the adapter in `__init__` and is torn down whole on a mode switch (no in-place toggling); `load(mode)` with the load-key widened to `(checkpoint, ip_config)` and identity preconditions checked before the idempotency short-circuit; `generate_identity(request, reference)`; `status()` IP-Adapter availability block. `service.py`: `_resolve_reference` dual-containment resolver (set-time + use-time, since the stored path is hand-editable), `set_reference`/`clear_reference`/`reference_status`/`generate_identity`, `_persist` refactored to a parameterized `_persist_frame` writing steered frames + an `ip_adapter` sidecar block under `characters/<id>/identity/`, `_ip_adapter_scale` coercion, `preview_prompt.has_reference`. Settings gained `models.image.ip_adapter.{dir,variant}` + `image_gen.ip_adapter_scale`; four `image_*` bridge methods; `docs/IMAGE_PIPELINE.md` §7–§8 (model layout, footgun, path-safety, output, 3b hardware checklist). Zero new dependency pins (the IP-Adapter weights + ViT-H encoder are user-placed, like the checkpoint). Implementation caught its own bug via a failing test — identity-mode `ip_config=None` (unconfigured) was indistinguishable from base-mode `None` in the load-key, so an unconfigured identity request could be silently served by a resident base backend; fixed by checking identity preconditions before the idempotency return.
- *(Stage 3b verification — 2026-07-11)* Ran a 16-agent review workflow (red-team + correctness code-review + DoD audit → each of the 12 raised findings adversarially verified by an independent skeptic → triage). Exactly one defect survived verification (raised independently by two dimensions): **MEDIUM** — a NUL byte in a stored `reference_image_path` makes `Path.resolve()` raise `ValueError` (not `OSError`), which escaped the resolver's `except OSError` guard and propagated raw out of every bridge caller, including the ordinary `preview_prompt`→`has_reference` path (a §2 one-window/no-console contract break). Fixed with an explicit up-front NUL reject plus broadening the guard to `(OSError, ValueError)` — matching the sibling `_load_record`/`char_dir` boundaries — and regression-tested across all four callers (set-time + the three use-time paths). Confirmed it is a robustness/bridge-contract break only, NOT a containment escape (`stat()` faults before any out-of-dir open; the dual-containment traversal/absolute/`..`/symlink defenses are intact). Everything else refuted or accepted-by-design: the [HARDWARE] load/generate reference TOCTOU (single-user offline, no adversary between check and use), the additive base-sidecar `stage` key, reference-is-path-validated-not-content-gated (the Layer-2 pixel classifier is 3c), and lazy heavy imports. Clean bills: the widened load-key + identity-precondition ordering, `to_dict` omit-if-None, `unload()` ip_config reset, the offline posture, and no scope creep (no 3c cull/FaceID, no 3e catalog, no 3g cache, no LoRA; the record's only new mutation is `reference_image_path`). **516 tests passing (1 skipped: symlink-escape test needs OS symlink privilege); scripted live-window smoke (create → reference status → no-reference generate → set-reference stored char-relative → has-reference → structured engine-unavailable steered generate → path-traversal rejected → clear → status) ALL PASS, one window throughout.** **Stage 3b marked DONE-HERE (hardware-validation flag pending).**
- *(Stage 3c build — 2026-07-11)* Built identity bootstrap + auto-filter on the 3b steer. Ran a research+design workflow first to lock the InsightFace/imgutils/inswapper APIs before writing the unexecutable [HARDWARE] backends (`FaceAnalysis(name="buffalo_l", root=<dir-containing-models/>, allowed_modules=["detection","recognition"])` → `app.get(bgr, max_num=0)` → `.normed_embedding` unit-cosine; `get_wd14_tags` ∩ `minor_coded_tags.txt`; `anime_dbaesthetic`; `inswapper.get(img, target, source, paste_back=True)`; offline via pre-placed files + `local_files_only`/`HF_HUB_OFFLINE`; the research/non-commercial license), then graded two designs into one spec. New `app/model/bootstrap.py` (`BootstrapCandidate`/`BootstrapManifest`/`VettedEntry`/`VettedManifest`, pure data, `ensure_safe_id`-confined ids); `store.py` bootstrap/vetted path helpers + save/load/clear. New `app/imagegen/cull.py` (sandbox-clean): `CullUnavailable`, the four `FaceEmbedder`/`QualityScorer`/`ContentClassifier`/`FaceSwapper` Protocols + dataclasses + `CullConfig`, the pure `score_candidate` (content-first, fail-closed) + `cull_and_rank`, `preflight_cull`, `coerce_cull_config`, and the lazy-import real backends behind a `ToolkitFactory` injected like the engine's backend factory. `service.py` gained `bootstrap_generate`/`bootstrap_recull`/`bootstrap_status`/`confirm_vetted`/`clear_bootstrap` with generate→unload-in-finally→CPU-cull VRAM sequencing, the Layer-2 gate wired hard+fail-closed+audited on candidates and confirm-time final pixels, and confirm-subset validation against the trusted manifest. Settings: `models.image.{face_recognition_dir,content_classifier_dir,face_swapper_path,onnx_providers}` + `image_gen.bootstrap.{...}`; 5 `image_*` bridges; `minor_coded_tags.txt`; requirements 3c slice (added `insightface`/`onnxruntime`/`dghs-imgutils`/`opencv-contrib-python`, dropped `opencv-python`; license note); `docs/IMAGE_PIPELINE.md` §10–§11. Zero record mutation (§6). A failing test caught its own bug during build (kept-count vs floor); the aesthetic-tiebreak ranking was corrected.
- *(Stage 3c verification — 2026-07-11)* Ran a 20-agent review workflow (red-team + code-review + DoD, each of 16 findings adversarially verified → triage). The verifiers confirmed the safety-critical invariants INTACT — no content-gate bypass, no un-vetted/forged-id smuggling into the vetted set, VRAM sequencing correct (image model unloaded in `finally` before the CPU cull), offline posture correct, classifier fail-closed — and surfaced 6 confirmed defects, all fixed: **A1 (safety/path)** a hand-edited manifest `candidate_id` with `..` could escape `characters/<id>/` at the optional face-swap write (now `ensure_safe_id` at `BootstrapCandidate.from_dict` + a basename guard in `_apply_face_swap`; confirmed the escaped file still can't reach the vetted set — rejected by the confirm-time containment check); **A2 (MEDIUM, §2)** bridge methods could raise instead of returning `{ok:false,kind}` — a non-`CullUnavailable` toolkit-build failure (missing `insightface` import, undecodable reference) → now `cull_unavailable` at both call sites, and a corrupt/hand-edited manifest (`JSONDecodeError`/`InvalidId`/`TypeError`) → now `bootstrap_corrupt` via guarded load helpers, plus `OSError` guards on save/`rmtree`; **A3 (LOW)** `image_gen.bootstrap.batch` bypassed its `[1,256]` clamp on the `batch=None` path → now clamped in `coerce_cull_config` (the one knob with no downstream per-request re-validation; the verifiers confirmed the other per-image settings are correctly crash-guard-only like 3a/3b); **A4 (LOW)** `confirm_vetted` cleared the prior vetted set before the copy loop → now staged into `vetted.new/` and `os.replace`d only after the full build, so a mid-copy `OSError` preserves the prior set; **A5** the entire `_apply_face_swap` body and corrupt-manifest handling were untested → added service tests (swap re-classify/re-similarity + fallback, tampered/corrupt manifest, arbitrary factory exception, atomic-copy-failure). All CONFIRMED findings reproduced-then-fixed; severity corrections from the verifiers were honored (not over-escalated). Clean/left-alone by design: the [HARDWARE] backends (lazy imports; `import app.imagegen.cull` clean without torch/insightface/onnxruntime/cv2/imgutils), the content-gate flow, and scope (no 3d LoRA / 3e catalog / 3f matting / 3g cache; zero record mutation). **577 tests passing (1 skipped: symlink-escape needs OS privilege); scripted live-window smoke (status → no-reference generate → no-bootstrap recull/confirm → clear → set-reference → face-models-missing generate) ALL PASS, one window throughout, every path structured.** **Stage 3c marked DONE-HERE (hardware-validation flag pending).**
- *(Stage 3d build — 2026-07-11)* Built LoRA promotion: the confirmed vetted set (3c) → a per-character identity LoRA. New `app/model/lora.py` (`LoraManifest` provenance, char-relative, `ensure_safe_id`-confined) + store helpers (`lora_dir`/`lora_dataset_dir`/`lora_manifest_path`, `save/load_lora_manifest`, `clear_lora`). New `app/imagegen/lora.py` (sandbox-clean — no in-process heavy imports; the training weight is in the subprocess): `TrainConfig`/`coerce_train_config` (quality-max §16 defaults, finite+clamped), `TrainRequest`/`TrainItem`, the `LoraTrainer` Protocol + `TrainerFactory` (injected like the engine/cull factories), pure `build_dataset` (kohya `<repeats>_identity/` layout + captions), `preflight_train`, and the [HARDWARE] `_KohyaSubprocessTrainer` (builds `train_config.toml`, runs `sdxl_train_network.py` headless via `CREATE_NO_WINDOW`, collects the `.safetensors`). `service.py` gained `train_lora`/`lora_status`/`clear_lora`: resolve+containment-check each vetted image (must be under `vetted/`), build the trigger (`cfid`+hash) + the *gated* caption (from `_assemble`, dropping the booru composition anchors), prep the dataset, **unload the image engine so the trainer gets the GPU** (§3, slot reset in `finally`), train, `os.replace` the LoRA into place + write provenance + flip `has_lora`/`lora_path`/footprint — all only on success (a failed re-train preserves the prior LoRA). Settings: `models.image.lora_trainer_dir`/`lora_trainer_python` + `image_gen.lora_train.*`; 3 `image_*` bridges; requirements 3d slice (no new pip pins — user-placed sd-scripts); `docs/IMAGE_PIPELINE.md` §12–§13. The deferred identity-tier-marker question was resolved (no record tier field). Zero engine generation changes (LoRA-at-generation is 3e). A test caught a config-clamp expectation bug during build (1e999→default vs a finite→clamp).
- *(Stage 3d verification — 2026-07-11)* Ran three individual review subagents (ultracode off → the Agent tool, not a workflow): red-team, correctness code-review, DoD/scope audit, each executing repros. They confirmed the clean bills — VRAM sequencing (engine unloaded before the trainer; `models.active` ends `None` on every path), prior-LoRA-survives-failed-retrain, promotion consistency (only `has_lora`/`lora_path`/footprint mutate), fail-closed error taxonomy, no scope creep (engine generate path unchanged; no 3e catalog/3f matting/3g cache) — and surfaced findings, all fixed: **HIGH** a valid-JSON manifest missing a required key raised `KeyError` (a `LookupError`, not in the guard tuples) straight through the bridge — fixed across the lora/vetted/**bootstrap** loaders (self-verified the `KeyError` escape first); **MEDIUM** `save_lora_manifest`'s `OSError` was unwrapped and could escape *after* the record was promoted — now the provenance manifest is written first (guarded → `io`), which also fixes the DoD-flagged footprint under-count (footprint now counts `lora.json`); **LOW** the trigger derived from the path-safe-but-not-content-gated id — now a SHA1 hash → provably `[a-z0-9]`, no minor-coded substring, no short-prefix collision; **LOW** a tampered vetted manifest could feed an in-dir non-image (`character.json`) into training — now a vetted entry must resolve under `vetted/`; **[HARDWARE]** the kohya TOML forced `xformers` (→ `sdpa`, no extra dep) and collected the output by exact name (→ newest-`.safetensors` fallback for sd-scripts step/epoch suffixes). Regression-tested (corrupt-missing-key manifests, `save_lora_manifest` OSError→io, hashed-trigger, footprint-includes-manifest, non-vetted-path skip). **607 tests passing (1 skipped: symlink-escape needs OS privilege); scripted live-window smoke (lora status → no-vetted train → clear → forge a vetted set → structured precondition refusal) ALL PASS, one window throughout, every path structured.** **Stage 3d marked DONE-HERE (hardware-validation flag pending).**
- *(Stage 3e build — 2026-07-11)* Built seed catalog generation. **Engine LoRA-at-generation** (the 3d payoff): `GenerationRequest.lora_scale`, a `_DiffusersLoraSDXLBackend` (checkpoint + `load_lora_weights` unfused + per-generate `cross_attention_kwargs` scale), the `_default_backend_factory` widened to 4-arg dispatch, `load(mode='catalog', lora=...)` with the load-key widened to `(checkpoint, ip_config, lora)` (catalog preconditions before the idempotency short-circuit), `generate_catalog`, and `loaded_lora`/`loaded_mode` status — base (3a) and identity (3b) paths byte-unchanged. New `app/imagegen/catalog.py` (pure, sandbox-clean): `CatalogConfig`/`coerce_catalog_config`, `load_catalog_states` (from editable `data/catalog_states.json`), `record_outfits` (wardrobe or as-is), `build_cells` (the capped matrix). Extended `PromptAssembler.assemble` with `exclude_groups`/`lead`/`extra` (all gated + deduped + adjacency-checked) so a catalog cell = constant gated identity minus wardrobe + the LoRA trigger + the cell's outfit/expression/pose. `service.py` gained `generate_catalog`/`catalog_status`/`clear_catalog` with the generate→unload→cull-per-pass VRAM sequence, the **same 3c cull** as the auto-filter (content fail-closed + similarity + quality; rejected cells regenerated up to `max_attempts`), a staged `catalog.new/` swap that preserves the prior catalog on failure, and the Stage-1 `CatalogManifest`/`CatalogEntry` filled under `catalog/`. Store gained `catalog_frames_dir`/`clear_catalog`; settings `image_gen.catalog.*`; 3 `image_*` bridges; `docs/IMAGE_PIPELINE.md` §14–§15. Zero record mutation; no 3f/3g surface. A test caught its own arg-order bug during build.
- *(Stage 3e verification — 2026-07-11)* Ran three individual review subagents (red-team, code-review, DoD/scope). They confirmed the clean bills — the widened `(checkpoint, ip_config, lora)` load-key + catalog-preconditions-before-idempotency (an unconfigured catalog request can't be masked by a resident base backend), base/identity engine paths unchanged, VRAM sequencing (image model unloaded before the CPU cull each pass; `models.active` ends `None`), the auto-filter is the *literal* 3c `score_candidate`+`coerce_cull_config`, and no scope creep (`matted_path` stays None, `on_demand` False, zero record mutation) — and, resolving a cross-agent question, that the unfused `cross_attention_kwargs` scale IS honoured on the diffusers ≥0.31 PEFT backend for both UNet and text encoders (no `set_adapters` change needed; default scale 1.0 is safe regardless). Findings, all fixed: **MEDIUM** `_finalize_catalog`'s swap was not rollback-safe — a mid-swap `os.replace`/`save_catalog` `OSError` could leave `catalog.json` disagreeing with the frames on disk (phantom manifest); now the prior catalog is renamed aside and RESTORED on any failure, so every failure path leaves a consistent prior catalog; **LOW** `load_catalog_states` raised `AttributeError` on valid-but-non-object JSON (`[]`/`null`/`42`) escaping the `image_generate_catalog` bridge (self-verified, now `isinstance(dict)`-guarded); **LOW/tuning** the identity-tight cull (`face_area_min=0.04`) systematically rejected the deliberately pose-varied catalog (full-body/over-shoulder = small faces) → a catalog-only relaxed `face_area_min` (0.01) while the Layer-2 content gate + similarity floor stay at the 3c values. A late red-team re-run confirmed both fixes hold and surfaced one **LOW residual** — if the rollback's OWN restore `os.replace` also fails (a double disk-fault), the manifest could be left dangling; now the dangling manifest is dropped so `catalog_status` reports a consistent "no catalog" (the prior frames remain in `catalog.old/` for recovery, self-healing on the next run). Regression-tested (no_states, malformed states→empty, partial-success `incomplete>0`, finalize rollback preserves the prior catalog, the double-fault drops the dangling manifest, relaxed-area keeps small-face frames). Accepted residual (deferred, §below): a *hard process-kill* in the microsecond window between the two-step frame rename + the manifest write is not journaled/reconciled at startup — self-healing on the next successful run. **645 tests passing (1 skipped: symlink-escape needs OS privilege); scripted live-window smoke (catalog status → no-lora generate → clear → forge has_lora+reference → structured no-checkpoint refusal) ALL PASS, one window throughout, every path structured.** **Stage 3e marked DONE-HERE (hardware-validation flag pending).**
- *(Stage 3f build — 2026-07-12)* Built matting / keyable output. Ran a research+design workflow first (5 web researchers → 2 independent designs → a merging judge) to lock the unexecutable [HARDWARE] facts from source before coding: rembg's exact ISNet/BiRefNet pre/post constants (1024² LANCZOS, per-variant mean/std, divide-by-image-MAX not /255, first-output-channel-0, unguarded per-image min-max stretch, sigmoid-in-code only for BiRefNet, dynamic input-name reading), model provenance/licenses/md5s (isnet-anime = SkyTNT/anime-segmentation, Apache-2.0, ~176 MB, rembg v0.0.0 release asset), and the dependency picture — including the CORRECTION that rembg's opencv-python-headless dep is gone upstream (~2.0.72), so the deferred-item resolution cites the live objections (pymatting/scikit-image/scipy hard deps, pooch runtime downloader, numpy≥2.3/pillow≥12.1/ort≥1.23 floors) instead. **Method pick (deferred item RESOLVED):** direct-ONNX reimplementation on the already-installed onnxruntime+pillow slice, user-placed `isnet-anime.onnx` default, `isnet_general`/`birefnet` constants-only variants, putalpha keyable output (original RGB + straight soft alpha; rembg's naive_cutout black-fringes on re-composite), epsilon-guarded stretch, optional erode/feather halo knobs — no new pip pins, no downloads; keyable-background *generation* rejected (discards 3e vetting, re-rolls identity, no trustworthy SDXL flat key). New `app/imagegen/matte.py` (fakeable `Matter` Protocol + `MatteFactory` + `MatteToolkit` with the Layer-2 classifier, `preflight_matte`, `coerce_matte_config`, pure `evaluate_matte`, `[HARDWARE]` `_OnnxMatter`); `service.py` `matte_catalog`/`matte_status` + `_load_catalog_manifest` (with a cross-character `character_id`-mismatch guard — 3f is the first flow that round-trips catalog.json, and `save_catalog` routes by the manifest's own id) — per-frame: containment + direct-`.png`-child residency → Layer-2 re-screen fail-closed BEFORE the skip check (blocked ⇒ purge + de-manifest + audit) → skip/force → `*.png.tmp` → coverage gate → atomic promote → char-relative `matted_path`; optimistic `updated_at` token → `catalog_changed`; all-skipped = true no-op; mattes inside `catalog/` (die with 3e swaps, footprint-counted, cleared free). `CatalogManifest.matting` provenance (backward-compatible); `store.matted_dir`; 2 bridges; `models.image.matting_model_path` + `image_gen.matting.*`; requirements 3f slice; `docs/IMAGE_PIPELINE.md` §16–§17 + KNOWN LIMITS renumber. Zero record mutation; engine untouched; no 3g surface.
- *(Stage 3f verification — 2026-07-12)* Ran a 16-agent review workflow (red-team + correctness code-review + DoD/spec audit → every raised finding independently re-executed by a skeptic: 12 CONFIRMED, 0 refuted, 1 accepted-by-design). 31 clean bills: containment + a 47-probe hand-edit sweep (settings × manifest × path oddities) with zero bridge tracebacks, gate-before-skip re-screen semantics, prior-artifact/rollback/no-op/concurrency behavior, VARIANTS re-verified verbatim against upstream rembg, sandbox cleanliness, zero record mutation. Findings, all fixed: **HIGH** hand-edited `"bytes": Infinity` in catalog.json raised `OverflowError` through both new bridges (`int(inf)` is not a `ValueError` — the codebase's own documented `_generation_settings` hazard, missed on the manifest channel; now caught across ALL seven service loader guards incl. the 3e catalog + record loaders, per the 3d fix-across-loaders precedent); **MEDIUM** the blocked-frame purge deleted only the canonical matte name while the skip check trusts ANY `matted_path` resolving into `matted/` — a hand-renamed matte of just-blocked pixels survived (purge now covers the recorded path under the same trust rule); **LOW** stem-keyed outputs let hand-placed same-stem/other-extension sources collide onto one matte (silent pixel swap / cross-entry purge) — sources now `.png`-only, collisions structurally impossible; **LOW** the `*.tmp.png` sweep could destroy a promoted final whose hand-placed source stem ended in `.tmp`, breaking failed-re-matte-keeps-prior (temp namespace now `*.png.tmp`, which no final can carry); **LOW** the all-failed escalation + `catalog_changed`/save-`io` aborts dropped the run tallies and left no run-level audit (tallies on every result shape; aborts log `catalog_matted` with `aborted=<kind>`); **LOW** a non-finite coverage reading shipped a bare `NaN` into the bridge payload — invalid strict JSON that would hang the JS promise on `JSON.parse` (finite-or-None guard); **LOW** the factory closer nulled a local while `_OnnxMatter` held the live session ref (a real `close()` now drops it); plus documented the best-effort concurrency caveat + full top-level kind list in §16 and added the degenerate-under-force / default-arg-bridge / write-then-raise-tmp test arms. Accepted-by-design: the optimistic token's check-to-save TOCTOU window (labeled best-effort in code + docs; no concurrent writer in a single-window app). **680 tests passing (1 skipped); scripted live-window smoke (create → no-catalog status/refusal → forged catalog → matting_model_missing → dummy files → structured matte_unavailable → escaped matted_path untrusted → clear) ALL PASS, one window throughout, every path structured.** **Stage 3f marked DONE-HERE (hardware-validation flag pending).**
- *(Hardware install + first validation — 2026-07-12)* Full `requirements-full` install on the target machine — **RTX 4070 Super, 12 GB VRAM** (note: the plan's VRAM assumptions were written against a 16 GB floor; SDXL fp16 generation fits, 3d LoRA training becomes the tightest fit and a first-class validation item). Installed: torch 2.13.0+cu126 (CUDA verified live) + torchvision 0.28, diffusers 0.39 / transformers 5.13 / accelerate 1.14, insightface 1.0.1 (prebuilt wheel — no compile; `FaceAnalysis`/`model_zoo` API surface verified compatible with the 3c code), onnxruntime 1.27, dghs-imgutils 0.19, opencv-contrib-python 4.11 + numpy 1.26.4 (a transitive `opencv-python` dep re-created the forbidden dual-cv2 state — caught and removed, contrib-only reinstalled; numpy<2 is imgutils' hard pin, torch runs on it). User-placed weights live in repo-local `models/` (now gitignored): `isnet-anime.onnx` (md5-verified), the buffalo_l pack, and the imgutils classifier cache prewarmed into `models/classifier_cache`. **Wiring fix found at validation:** `content_classifier_dir` was a preflight witness only — imgutils resolves the HF cache via `HF_HOME`, which freezes at the first hub import (the engine's, in the normal flow), so the configured dir was never actually consulted; added `cull.pin_hf_cache()` called at app startup (+ factory `setdefault` backstops + unit test; **681 tests passing**). **3f hardware validation (§17): items 1–3 and 8–9 PASS** — the transcribed-constants parity diff vs real rembg on two real anime frames came back **bit-identical (max alpha diff 0)**; a real end-to-end `matte_catalog` (real ISNet + WD14 Layer-2, `HF_HUB_OFFLINE=1`) matted 2/2 frames with provenance + an idempotent second-run skip at ~1.2 s/frame CPU; buffalo_l detects the anime test face at det=0.583 (just above the 0.5 floor — recorded as a 3c calibration signal). Remaining §17 items (edge-quality tuning over composite backgrounds, the purge drill, lifecycle) queue behind the first real 3a–3e catalog. **Checkpoint placed the same day:** `models/waiIllustriousSDXL_v150.safetensors` (WAI-Illustrious SDXL v15.0, 6,938,040,682 bytes, SHA256 `befc694a296f75e996488ebf9f9db8a1493bd059b6e704b975829e87d5aeb4fa`) wired to `checkpoint_path`. **First real 3a render PASS** (scripted, real services): gated prompt → coherent on-record frame (silver-haired elf, adult anchors held) at seed 12345; first render 22.1 s incl. load + one-time config warm, **steady-state 9.7 s/frame** at 832×1216/28 steps, **VRAM peak 10.35 / 12.0 GB** (base generation fits the 12 GB card with ~1.6 GB headroom), slot released clean. Observed: the assembled prompt ran 115 tokens vs CLIP's 77 — the documented KNOWN-LIMITS truncation limit (IMAGE_PIPELINE §18 then, §20 after the 3g renumber; safety anchors lead the prompt by design, so the tail-loss is style fragments); flagged for prompt-budget awareness at 3e where cell fragments append.
- *(Stage 3b hardware validation — 2026-07-12)* Ran the full §8 checklist on the target machine (RTX 4070 Super 12 GB), scripted real-services runs. **Mirror fetched + wired:** local h94/IP-Adapter under `models/ip_adapter/` — `ip-adapter_sdxl_vit-h.safetensors` (698,391,064 B), `ip-adapter-plus_sdxl_vit-h.safetensors` (847,517,512 B), ViT-H `models/image_encoder/` (`model.safetensors` 2,528,373,448 B + config; hidden 1280 → projection 1024 confirmed ViT-H); **all three SHA256s bit-match the HF LFS metadata**; `ip_adapter.dir` set; status booleans all true (item 1). **Items 2–3 PASS:** steered frame under `identity/` with a correct `ip_adapter` sidecar block + char-relative reference; the pinned slash-form encoder folder loaded with no projection dim-mismatch. **Items 4–5 → a 12 GB finding + engine tuning:** the fully-resident identity stack peaked **12.18 GB (standard) / 12.32 GB (plus)** — past the card, silently WDDM-spilling to system RAM at **18.6 s/frame** (vs 9.7 base; base's 10.35 GB fits clean); the identity→base swap correctly freed the identity extras (−1.83 GB ≈ ViT-H+adapter) and release ends at 0.01 GB. Fix: below `IDENTITY_RESIDENT_VRAM_MIN_GB=14.0` the identity backend now uses accelerate **model-cpu-offload** (adapter loaded before device placement, diffusers' documented order) — re-measured peak **6.58 GB std / 6.01 GB plus, 12.0 s/frame** steady-state (faster than the spilled resident path); pure predicate `identity_needs_cpu_offload` unit-tested; base/catalog paths untouched. **Item 6 PASS (visual):** identity holds across 0.30/0.55/0.80/0.95 (same character by eye at every scale), the prompt owns pose/wardrobe at ≤0.55, 0.95 approaches the documented near-lock (reference composition wins, mild color-fringe); **the structural adult anchor + Layer-2 negative age anchors hold at 0.95** — every frame unambiguously adult; 0.55 default confirmed; observed: `plus` at the global 0.55 over-steers (color cast) — its band is 0.3–0.6/default 0.45. **Items 7–8 PASS:** with every Python socket hard-blocked (stricter than airplane mode) the full base→reference→steered path completed — after fetching the SDXL **pipeline-config skeleton** (stabilityai/stable-diffusion-xl-base-1.0 configs+tokenizers, 3.1 MB, no weights) into `models/sdxl_config/` and setting `pipeline_config_dir` (pre-stages the Stage-7 bundling item; the first socket-blocked run correctly failed structured `{ok:false,kind:'engine'}` while `pipeline_config_dir` was unset, proving both the documented caveat and the bridge contract); re-render from the steered sidecar's seed+scale+reference across a full release/reload came back **pixel-identical** (also proves offload-path determinism). **Two wiring/calibration catches (the pin_hf_cache class):** (1) insightface `prepare()` used its default `det_thresh=0.5`, silently dropping faces BEFORE the configured `det_score_floor` — any floor tuned below 0.5 was a dead knob; now `detector_threshold()` mirrors the coerced floor ([0,1]-clamped at the use site), unit-tested. (2) **The 3c-gating finding:** photo-trained buffalo_l/ArcFace is at its margin on the WAI-Illustrious anime style — the reference detected at det 0.745 while of six steered same-character frames (visually confirmed identical) **three yielded no detection even at det 0.20**, the rest det 0.25–0.39, and same-character ArcFace cosine measured **0.35–0.58** vs the 0.50 same-person floor: as-calibrated the 3c cull would reject essentially every bootstrap candidate on this style. Candidate resolution recorded in `docs/IMAGE_PIPELINE.md` §10 (swap the `FaceEmbedder` real backend to imgutils CCIP + anime face detection behind the same Protocol — the abstraction was built for this); decision surfaced to the user before the 3c run. Docs: §7 VRAM-behavior section, §10 det-thresh + calibration notes. Closing the last §6 item the same day: base-mode same-seed re-render across a full release/reload came back **pixel-identical** — **Stage 3a flag CLEARED** with all eight §6 items PASS. **683 tests passing (1 skipped).** **Stage 3b hardware-validation flag CLEARED.**
- *(3c CCIP embedder swap + hardware validation — 2026-07-12)* Acting on the 3b calibration finding, the user approved swapping the `FaceEmbedder` real backend from buffalo_l/ArcFace to **imgutils CCIP + anime face detection** (option graded against keep-buffalo_l-and-tune and a hybrid). **Feasibility probed BEFORE rewiring, on the exact frames that broke ArcFace:** anime-YOLO detection 8/8 at conf 0.83–0.89 (buffalo_l: 3/6 no-detect); CCIP same-character cosine **0.63–0.82 vs 0.33** for a different-character control — the checked-in 0.50 floor splits the gap with ~0.15 margin on both sides, and `ccip_difference == (1 − cos)/2` EXACTLY on every measured pair, so the pure cull's cosine machinery, fakes, and floor knobs are all byte-unchanged; the swap is confined to the [HARDWARE] backend + factory + preflight (`_CcipEmbedder`; buffalo_l/FaceAnalysis now built ONLY when `face_swap_enabled`; preflight witnesses the default path via the classifier cache alone — `classifier_unavailable` before `face_models_missing`; licenses: ccip_onnx OpenRAIL, anime_face_detection MIT, the non-commercial insightface pair now confined to the optional swap path). `import app.imagegen.cull` stays sandbox-clean. **A second freeze-at-import offline leak found and fixed mid-validation (the pin_hf_cache class):** the BASE backend never set `HF_HUB_OFFLINE` (only the 3b identity backend did), so in the normal flow — first heavy import = base render — huggingface_hub froze OFFLINE=False process-wide and the bootstrap cull's cached-model resolutions made live etag requests (observed unauthenticated-hub warning). Now `engine.pin_hf_offline` runs at startup: hub pinned offline whenever the §2 posture is configured (`pipeline_config_dir` set), warm path preserved when unset; unit-tested, and the warning disappeared from all subsequent runs. **§11 validation (real checkpoint, real CCIP/WD14, scripted):** full 64-candidate bootstrap on a fresh character (8 + 28 + 28 via `more=True` accumulation) → **64/64 keep-rate** (similarity 0.613–0.836, zero content/quality/similarity/det/area rejects), grid of 12 proposed, VRAM 0.01 GB resident during every cull (unload-before-cull live), ~13–14.5 s/steered-frame incl. loads; top-ranked frames visually confirmed same-character and unambiguously adult; `confirm_vetted` promoted grid+top-kept = **20 frames** into `vetted/` (in the §6 15–30 band, `below_floor=False`, final-pixel re-screen passed) and the 3d dataset contract reads it; a socket-blocked end-to-end bootstrap (generate → unload → CCIP/WD14 cull) completed fully offline. `more=True`'s answer to §11 item 8: NOT routinely needed — keep-rate is ~100% on this style, the 64 default over-provisions comfortably. A killed mid-batch run also confirmed the crash posture (stale `models.active` reset at next startup; append-only candidates; the Stage-4 reconciliation deferred item gained a candidates-orphan sweep addendum). **REMAINING (named in the pending flags):** the Layer-2 false-negative recall check (user-directed) and the optional face-swap leg. **684 tests passing (1 skipped).**
- *(3d + 3e hardware validation — 2026-07-12)* Ran the §13 and §15 checklists end-to-end on the target machine. **3d setup:** kohya `sd-scripts` cloned to `models/sd-scripts` (rev `0128ca00`, 2026-07-08) with its OWN uv venv (its pins — diffusers 0.32/transformers 4.54 — are incompatible with the app venv; `lora_trainer_python` exists for exactly this) + torch 2.13+cu126 + bitsandbytes 0.49. **Three real [HARDWARE] contract catches, all fixed + regression-tested:** (1) the generated toml wrote `resolution` as an int, but toml values bypass argparse coercion and sd-scripts unconditionally `args.resolution.split(",")`s — now a quoted string; (2) sd-scripts logs bilingual text and a Windows non-console pipe defaults the child to cp1252 while `text=True` decodes with the locale codec in the parent — the subprocess now pins `PYTHONUTF8=1` + `encoding="utf-8", errors="replace"`; (3) the trainer inherits the app's pinned offline HF posture and sd-scripts loads the two CLIP **tokenizers** from the hub — prewarmed into the pinned cache (§13 item 1 documented); the failure surfaced as a structured `train_failed` fail-fast, live-proving §13 item 6's path. **Training:** 40-step smoke PASS (105 s; dataset laid out + cleaned by design; `has_lora` flipped; trigger `cfidafa4efa8344b`), then the full **1600-step quality run: 31.5 min, VRAM peak 9.86 of 12 GB (~2.4 GB headroom)** — THE 12 GB stress test clears at the §16 quality-max defaults, 114 MB LoRA `os.replace`d over the smoke artifact. **3e:** first catalog run surfaced two more catches — `peft` was never pinned (diffusers' `load_lora_weights` refuses without it; now in requirements-full 3a slice) and **diffusers 0.39's kohya converter has a te1/te2 regression** (empty text-encoder rank_dict → `IndexError` on a TE-carrying kohya LoRA). Resolution, both sides: the trainer toml now sets `network_train_unet_only = true` (standard SDXL identity practice, lower VRAM, kills the fragile surface) AND the engine's catalog backend degrades to the UNet-only key subset when the full load fails (legacy/foreign LoRAs; the UNet slice was hardware-verified to carry the identity — a 12-step probe render reproduced the character from a minimal prompt). **Full §15 run with the trained LoRA: 20/20 matrix cells kept (zero rejects, `incomplete=0`), 287 s, VRAM peak 10.51 GB, slot 0.01 GB after; identity visually confirmed across portrait/standing/sitting × expressions** — the CCIP cull kept full-body cells that ArcFace would have no-detected. **3f on the real catalog: 20/20 matted at ~1.1 s/frame CPU, second run fully idempotent (0/20 skipped-all)** — closing §17's real-catalog items; the two residuals (edge-quality over composite backgrounds → Stage 5; blocked-frame purge drill → pairs with the user-directed Layer-2 recall check) are named in the pending flags. **686 tests passing (1 skipped).** **Stage 3d and 3e hardware-validation flags CLEARED; 3f mostly-validated.**
- *(Stage 3g build — 2026-07-12)* Built on-demand generation + cache — the "grow" of §7's seed-plus-grow, closing Stage 3. `app/imagegen/catalog.py` gained the pure `resolve_cell` + `STATE_KEYS`: the caller supplies an `{expression, pose, outfit}` **id triple** only (creator-payload strictness: exactly three keys, non-empty strings, known ids → `invalid`/`unknown_state`), prompts come solely from `data/catalog_states.json` + the option catalog (drop-in states extend the on-demand space, §15), `asis` always valid. `service.py` gained `generate_on_demand`/`cache_status`/`clear_cache` + internals (`_find_state_frame`, `_serve_cached`, `_heal_matte`, `_matte_one`, `_purge_state_entries`, `_save_manifest_quietly`, `_move_unique`): covered states serve instantly (cache-then-catalog lookup, 3f residency rule, no models); novel states ride the **parameterized 3e passes** (subdir/rel/stage/kind + on_demand/context params, 3e defaults byte-identical) — generate LoRA-steered → unload-in-finally (§3) → the literal 3c cull → retry to `max_attempts` → `frame_rejected`; staging `cache.new/` (zero in-process orphans), survivor moved via O_EXCL-reserving `_move_unique`, matted best-effort via the 3f `Matter` (fresh pixels not re-classified — culled same-run; a gap **heals on the next hit**, and the heal re-classifies fail-closed first: blocked ⇒ purge + audit `image.cache.heal` + regenerate). Cache manifest = the `CatalogManifest` shape at `cache.json` (`store.save_cache`/`load_cache`/`clear_cache`, `cache_frames_dir`/`cache_matted_dir`), entries `on_demand=true` + the new additive `CatalogEntry.last_used` (§14 LRU signal: stamped at creation + every cache hit; catalog entries stay null), one entry per state (replacement purges prior artifacts under the 3f trust rules), serve-path bookkeeping best-effort behind the 3f optimistic token (never fails a hit, never clobbers a swapped manifest). `cache/` is a deliberate **sibling** of `catalog/` — a 3e regen swap replaces the seed catalog, the grown cache survives; `footprint.cache_bytes` separates it. Zero record mutation; zero engine changes; **no new settings** (3e catalog knobs verbatim) and **no new dependencies**; 3 bridges; `docs/IMAGE_PIPELINE.md` §18–§19 (+ KNOWN LIMITS renumbered → §20).
- *(Stage 3g verification — 2026-07-12)* Ran three review subagents (red-team + correctness code-review + DoD/scope audit, each executing repros). Clean bills: full bridge fuzz structured on every 3g input shape; purge containment (crafted `path`/`matted_path` cannot delete outside `cache/`·`cache/matted/`; hand-renamed mattes of blocked pixels die under the recorded-path trust rule); cross-channel manifest routing guarded (`character_id` mismatch ⇒ `cache_corrupt` before any save); state/outfit fragments always Layer-1-gated incl. `lead`/`extra`; VRAM sequencing (cull toolkit built with the slot free — `active_at_build == [None]`; heal never touches the engine); 3e parameterization defaults diffed byte-identical; zero record mutation (byte-compare test); DoD all MET, no scope creep (no Stage-4 eviction, no 6e mapping, engine/cull/matte/prompt untouched). Findings, all fixed + regression-tested: **HIGH** the new `last_used` read in `CatalogEntry.from_dict` ran `.get` before the `["frame_id"]` subscript, so a non-dict manifest entry (`"entries": [null]`, a natural hand-edit) raised **AttributeError — in no loader guard tuple** — a raw traceback through every manifest bridge including pre-existing 3e `catalog_status` and 3f `matte_status` (fixed both ways: isinstance→ValueError in `from_dict`, plus a shared `ARTIFACT_LOAD_ERRORS` tuple replacing all nine loader guards incl. `_load_record`, per the 3d fix-across-loaders precedent); **MEDIUM** a hand-edited `Infinity`/`NaN` inside an entry's `state` rode verbatim into the `cache_status`/serve-hit bridge payloads — invalid JSON that hangs the JS promise (the creator-slider hazard on a new channel; `state` now str-normalized `{str: str}` at the `from_dict` choke point, also fixing the pre-existing 3e `catalog_status` channel); **LOW** `RecursionError` from pathologically nested manifest JSON escaped every loader (now in the shared tuple); **LOW** docs §18 kinds-list mis-attributed `blocked`; **LOW** the footprint test didn't pin mattes-count-as-`cache_bytes`. Accepted observations (within the 3f best-effort bar): per-call tmp sweep, containment-validated-but-raw `entry.path` echoed in responses, subset state matching. **736 tests passing (1 skipped); scripted live-window smoke 14/14 PASS (ping → create → empty cache status + matte readiness → invalid/unknown_state/no_lora/unknown-key/default-args → forged-LoRA precondition chain → corrupt cache manifest → clear), one window throughout.**
- *(Stage 3g hardware validation — 2026-07-12)* Ran the full §19 checklist on the target machine (RTX 4070 Super 12 GB) against the live character `c517663a…`, scripted real-services runs — **all ten items PASS**, Stage 3 hardware track complete. (1) Novel state (`sultry`/`over_shoulder`/`asis` — both ids capped out of the 3e matrix) generated end-to-end in **29.9 s** (gen + CCIP/WD14 cull + ISNet matte), device-wide VRAM peak 11.99 GB ≈ **10.8 GB app-side** (desktop baseline 1.18 GB; consistent with 3e's 10.51), slot + torch allocation released clean (a first-pass "resident 1.25 GB" flag was the meter reading device-wide usage — baseline-confirmed benign). (2) Same triple again: **0.01 s** cache hit, zero model loads, `last_used` bumped; a seed-covered triple served from the catalog equally instantly. (3) Identity + gates on the real pixels (visually verified + alpha-verified): same character, unambiguously adult, matte keyable (bg alpha 0 / subject 254, coverage 0.70). (4) Deleted matte healed on access in **1.7 s** (WD14 re-screen + re-matte, audited). (5) `force=True` regenerated + replaced (old frame/sidecar/matte gone, one entry per state). (6) Similarity floor cranked to 0.999 → real frames rejected → structured `frame_rejected` after 2 attempts (38.7 s), nothing cached, slot released. (7) `unknown_state` structured. (8) Full 3e regen (289 s, 20/20) — **the cache survived the swap and served the identical frame**; footprint separated (cache 4.5 MB / catalog 20.2 MB / LoRA 114 MB); fresh catalog re-matted 20/20 in 21 s. (9) Socket-hard-blocked end-to-end on-demand run (generate → cull → matte) fully offline in 28.9 s + an instant offline hit. (10) A planted stale `cache.new/` swept at the next run; the kill-window orphan class is documented → the Stage-4 sweep addendum. Residual tuning notes (data-level, not code): `over_shoulder`'s fragment "looking over shoulder" wasn't honored on this record (canonical booru tag is "looking back" — editable states file, §15) and the record pins no eye color so it drifts (record-completeness). **Stage 3g flag CLEARED; Stage 3 — Image Pipeline COMPLETE (3a–3g done-here + hardware-validated; residuals: 3c recall check + face-swap leg, 3f edge-quality + purge drill, 3g states-file tuning — none gating Stage 4/5).**
- *(Stage 2 verification — 2026-07-10)* Ran a three-agent adversarial pass (backend red-team executing live attacks, front-end static review, DoD audit). Execution-confirmed findings, all fixed: **non-atomic option merge** let a malformed drop-in half-mutate a bundled group into a regioned anatomy slider with widened clamp bounds (fixed: files now apply atomically via staged copy — a bad file has zero effect); **uncaught `OverflowError`** from a huge JSON slider integer escaping to the bridge (fixed + isfinite guards both sides of clamp); **loader crash-to-startup-brick** from deeply-nested JSON (`RecursionError`) or a directory/unreadable file named `*.json` (fixed: resilient load catches broadly, skips non-files); **slider-KEY channel unfiltered** (fixed: record gate now covers slider keys); **contextual terms ("child", "forced") persisting as selection/tag values** because lone tokens can't trip proximity logic (fixed: discrete values now gated in strict prompt context); non-finite option bounds, merge type-coercion drift, option-override reordering, `prompt_ranges` validated at load. Front-end: reload now prunes stale state (vanished groups/options/kind flips), save has an in-flight guard (double-click created duplicates), number-input empty-string guard, client-side required checks, live-check response sequencing, anatomy-region open-state preserved across re-renders. Clean bills: CSP (no inline/eval/innerHTML), one-window rule, XSS discipline via `textContent`, age gate unbypassable (all 15+ probe variants), no path/store influence from any creator input, no partial files on failed create. DoD audited item-by-item: all MET. **378 tests passing; scripted live-window smoke (quick create → detailed create with free text → blocked-name rejection → disk verification) ALL PASS.** **Stage 2 marked DONE.**
- *(Stage 4 build — 2026-07-13)* Built Library & Management (§14). New `app/imagegen/manage.py` (sandbox-clean, stdlib+internal only): `LibraryConfig` + `coerce_library_config` (the resolved disk-threshold deferred item — `library.cache_cap_bytes` 256 MB / `library.recommend_cache_bytes` 192 MB, coerced+clamped [8 MB, 1 TB]) and the pure `select_evictions` (LRU by the 3g `last_used` signal, missing stamp = oldest, `frame_id` tiebreak, MRU never evicted, `protect_id` pins the just-inserted frame against same-second ties). New `app/ui/library.py` `LibraryService`: `list_characters` (one summary row per stored id — identity flags with containment-checked `has_reference`, per-channel catalog/cache frame-count+staleness, measured footprint LoRA/catalog/cache, §14 deletion recommendation; unloadable records degrade to still-deletable error rows), `get_character` (record → creator-form payload + `validate_against` soft issues), `delete_character` (id-only, works on records that no longer load — the remedy for corrupt/blocked), `thumbnail` (containment-resolved reference → ≤256 px JPEG data URI, None on any failure), `reconcile` (the startup sweep — the resolved reconciliation deferred item — staging dirs + bootstrap/candidate orphans + 3g cache orphans + dangling manifest entries + the §14 LRU cap; corrupt-manifest-sweeps-nothing, own-artifact-patterns-only, per-character fault becomes a skipped entry). Shared `load_record_guarded`/`resolve_contained` mirror the `ImageService` taxonomy + `_resolve_reference` use-time rules. `ImageService.enforce_cache_cap` (measures RECORDED artifact bytes, evicts via `select_evictions` under the cache purge trust rules, saves+audits `cache_evicted`) + a best-effort post-insert hook in `generate_on_demand` (passes the fresh `frame_id` as `protect_frame_id`; response gains `evicted`); `_purge_state_entries` refactored to share `_cache_entry_paths`/`_purge_entry_artifacts`/`_entry_disk_cost`. `CreatorService.update_character` (the edit path — same strict validation, record REBUILT so Layer-1 + 20+ gates re-run, id/created_at/identity preserved, §15 unknown-group values carried forward, `_render_changed` compares assembled positive prompts so a name/personality edit doesn't falsely mark stale, `_mark_stale` sets `stale` on catalog+cache manifests best-effort). A hand-edited non-finite slider now reads as corrupt at `_norm_number` (closing the Infinity/NaN-into-bridge-payload class on the record channel). 6 `library_*` bridges; `main.build_services` wires the service + runs `reconcile()` at startup (fail-safe); front-end library view (`web/library.js`: list/sort/filter, footprint, badges, two-step inline delete, stale→regenerate offer, lazy thumbnails, persistent status line) + creator edit mode (`creator.js`: `beginEdit`/`endEdit`, offered-not-forced regeneration gated on `has_lora`); `library.*` settings; `docs/LIBRARY.md`. Zero record-schema change (reused the Stage-1 `Footprint`/`stale`/`last_used` fields), zero new dependencies, no engine/generation change beyond the cap hook. `STAGE` advanced to "Stage 4 — Library & Management".
- *(Stage 4 verification — 2026-07-13)* Ran three review subagents (red-team executing repros, correctness code-review executing repros, DoD/scope audit). **Red-team: CLEAN — zero CRITICAL/HIGH/MEDIUM reproduced.** Execution-confirmed clean bills: `thumbnail` never reads outside `characters/<id>` (10 hostile reference paths + a real Windows directory-junction escape all → None); the reconcile sweep never deletes recorded/innocent files and defangs hostile manifest `path`/`matted_path` (`../`, absolute, `cache/matted/../../character.json`) via basename-only keep-sets + `iterdir`-scoped deletion; corrupt/id-mismatched manifests sweep nothing; ~100-case bridge fuzz on all 6 methods → every result a strict-JSON dict, never a raise; `update_character` ignores injected `id`/`identity`/`created_at` and re-runs gates (blocked/under-age/obfuscated all refused, nothing persisted, audited); Infinity/NaN on every channel → structured error, no non-strict JSON to a bridge; LRU coercion over 14 hostile settings values never raised. **DoD: all four clauses MET, scope clean** (no Stage-5/6 creep, zero schema change, no new deps, engine delta = the cap hook + a behavior-preserving purge refactor). **Code-review** raised no HIGHs; findings applied: (1) **MEDIUM** edit silently dropped values whose option group was unloaded while the UI claimed they were kept — now carried forward from the stored record (§15 source-of-truth), UI message true; (2) **MEDIUM** library action feedback was written to a detached (re-rendered) DOM node — routed to a persistent `#lib-status` line; (3) **MEDIUM** `enforce_cache_cap` measured the whole cache tree (orphans included) and could over-evict good frames to pay for bytes it can't free — now measures RECORDED artifact bytes; (4) **MEDIUM** `beginEdit` no-op'd on an in-flight catalog load (blank Create form) — `ensureStarted` now returns a shared awaitable; (5) **LOW/MED** `refresh()` wiped in-flight `busy`/confirm state and could no-op a post-action refresh — busy/confirm now survive a re-list, refreshes coalesce; (6) **LOW** same-second `last_used` tie could evict the just-generated frame — `protect_frame_id` pins it; (7) **LOW** startup sweep reclaims `catalog.old` (comment amended — it is a documented staging orphan); (11) **LOW** `_render_changed`'s lazy `PromptAssembler()` was unguarded — now degrades to conservatively-changed on OSError; (12) **LOW** reconcile `bytes_freed` over-reported on a locked rmtree + the bridge could raise from a deep-fs fault — now counts only reclaimed bytes and a per-character fault becomes an audited skip; plus the DoD gaps (regen offer gated on `has_lora`; new tests: edit-never-invokes-generation, `build_services` library wiring, `protect_id`, recorded-bytes cap, unknown-group preservation, reconcile-never-raises). **819 tests passing (1 skipped); scripted live-window smoke 22/22 PASS (ping → create → list/get → forge catalog → render-relevant edit marks stale + offers-not-forces → non-visual edit no render change → thumbnail none/data-uri → reconcile sweeps staging+orphans → traversal/fuzz/blocked/under-age all structured → delete → one window throughout).** **Stage 4 marked DONE (both deferred items resolved; no hardware-validation flag — regeneration invocation is a Stage-3 path already validated).**
- *(Stage 5 build — 2026-07-13)* Built Scene / Persona / Scenario / Event builders + character-over-background compositing (§13). Planned in plan mode (2 Explore agents mapped the roadmap + codebase, 1 Plan agent pressure-tested the design; three user decisions locked: build all four kinds now, the approved consent set, transparent-passthrough for background-off). **Model:** `app/model/builder.py` — one `BuilderRecord` with a `kind` discriminator ∈ {persona,scene,event,scenario} (the lighter §13 record: same tags + filtered-free-text as the character engine, **no** age/anatomy/sliders/identity/LoRA); `__post_init__` re-runs the Layer-1 gates on every channel + a **code-anchored Layer-3 consent gate** — `APPROVED_CONSENT_FRAMES` is a code constant and `consent` is a dedicated typed field (the `age.py` "construction is the gate" pattern), so a scenario without an approved affirmative-consent frame is *unconstructable* and a §15 drop-in can only advertise the ids, never widen the gate (approved set, user-signed-off: `enthusiastic`/`established_relationship`/`negotiated_scene`/`romantic`); the `kind` is validated against the closed set so a hand-edit can't flip a scenario to shed consent; `BackgroundManifest` (scene imagery witness). `app/model/builder_store.py` `BuilderStore` — a parallel `data/builders/<id>/` tree; `store.resolve_within(base_dir, raw)` extracted as the shared containment rule (`library.resolve_contained` now delegates). **Options:** `app/data/builders/{_shared,persona,scene,event,scenario}/` loaded per-kind via new `load_builder_catalog(kind, data_dir, include_bundled=False)` — the frozen §15 format untouched, and §12's numeric-reservation check now *rejects any builder slider for free* (reinforcing "no sliders"); scene groups (`location`/`time_of_day`/`lighting`/`weather` + shared `mood`) carry `render:true` scenery prompts, the rest `render:false` chat-side. **Scene prompt:** `PromptAssembler.assemble_scene` **reuses** the same `_gate` + `_gate_adjacency` (the HIGH-severity 3a separator-overflow surface — not a fork) + `_dedupe_pieces`; no character identity (no subject/adult anchor, no LoRA/IP-Adapter), a `scenery, no humans` anchor + a new editable `data/negative_scene.txt` people-steer, safety negatives kept (R7 school-vocab blocks on scene backgrounds too). **Compositing (all-[HERE], PIL is in the .venv):** `app/imagegen/composite.py` — pure `composite_geometry` (bottom-center anchor, scale-to-bg-height, aspect+width-clamped) + real Pillow `composite_over` (straight-alpha, original RGB preserved — never premultiplied); `load_rgba_matted` raises `NotMatted` on an alpha-less frame (the §13 guard at the pixel boundary); `edge_choke`/`feather_px`/`alpha_floor` retire the inherited **3f matte-halo residual at composite time** (no re-matte); `coerce_composite_config` defensively clamped. **Service:** `ImageService.generate_background` (**[HARDWARE]** scenery SDXL render + a **new fail-closed Layer-2** background classifier via `cull.ClassifierToolkit`/`_default_classifier_factory`/`preflight_classifier` — block→purge→audit; reuses a generalized `_persist_image` extracted from `_persist_frame` so 3a/3b are byte-unchanged), `composite_frame` (background on → composite; off → transparent-passthrough; PNG data-URI preview, **persists nothing** — avatar caching is 6e), `matted_frames`, `background_status`, `clear_background`. **UI/wiring:** `app/ui/builders.py` `BuilderService` + `load_builder_guarded` + a builder reconcile sweep (the library vouching model — corrupt manifest sweeps nothing; wired into `main.run()` fail-safe); 12 bridges (`builder_*`, `scene_*`, `image_composite`, `image_matted_frames`); `image_gen.compositing.*` settings; front-end **Scenes** view (`web/builders.js`: kind tabs → data-driven form + live Layer-1 checks + the required consent control, builder list, a scene background panel, a compositing studio with the on/off toggle + placement/edge controls; `index.html` nav enabled; `app.css`). `STAGE` advanced to "Stage 5 — …". Zero new dependencies. `docs/BUILDERS.md`. **921 tests passing (1 skipped; +102 over Stage 4)** — new `test_builder`/`test_builder_store`/`test_composite`/`test_builder_service`/`test_scene_service`/`test_scene_prompt` + shell-API bridge tests + conftest `builders` fixture; every [HARDWARE] leg (background gen + Layer-2) exercised GPU-less behind injected fakes. **Scripted live-window smoke 24/24 PASS** through the real `build_services()` Api bridge (describe/create all four kinds → Layer-3 consent gate + Layer-1 name gate refused → list/get/update-keeps-kind → `scene_generate_background` structured `classifier_unavailable` on the sandbox → `image_matted_frames` → composite background OFF transparent-passthrough + ON over a hand-placed sample background with `edge_choke` honoured → unmatted/non-scene refused → both reconcile sweeps → delete). **Stage 5 marked DONE-HERE; hardware-validation flag PENDING on background generation + Layer-2 (the compositing + builder logic is fully verified here).**
- *(Stage 5 verification — 2026-07-13)* Ran a 14-agent adversarial review workflow (6 dimension finders — safety-gates / path-containment / bridge-robustness / correctness-service / correctness-builder-ui / DoD-scope — each finding then independently re-executed by a skeptic prompted to REFUTE; then a triage synthesis). 7 findings raised → **4 CONFIRMED (2 distinct defects after dedup), 3 REFUTED, 0 uncertain.** Triage verdict: **no hardware-blocking must-fix** — neither defect crosses a safety (L1–L4), containment, or data boundary. Both fixed + regression-tested: **D-1 (MEDIUM, §2 bridge contract)** — `composite_frame`'s two PIL decode sites caught only `except (OSError, ValueError)`, but PIL's `DecompressionBombError` is a *bare* `Exception` subclass (verified in-venv, Pillow 12.3), so a hand-placed oversized/bomb frame or background (>178 MP; `frame_ref`/`background_ref` are the §2 hand-editable-untrusted inputs, and the background-OFF passthrough loads the frame first so it's the common path) escaped the bridge as a raw traceback → hung UI under pythonw; the sibling `library.thumbnail` already guards the identical case with `except Exception`. Fixed by broadening both decode guards (+ the OFF-path `encode_png_data_uri`) to `except Exception → {ok:False,kind:'io'}`, mirroring `thumbnail`; regression test lowers `Image.MAX_IMAGE_PIXELS` and asserts a structured refusal, not a raise. **D-2 (MEDIUM, UI correctness)** — `builders.js doSave` re-rendered the editor only for scenes after a create, so for persona/event/scenario the button kept reading "Create" while its dispatch had silently switched to `builder_update` (via the now-set `editing` id) — creating two personas in a row would *rename* the first instead of making a second; fixed by re-rendering the editor for every kind after a create (button flips to "Save changes" + Delete appears, matching the dispatch). **REFUTED (held under attack):** (a) a claimed consent-gate bypass via in-memory `__setattr__` kind-flip — refuted: the persistence/load path (`from_dict → __post_init__`) re-gates, and a live in-memory mutation is not a persistence vector (the `character.py`/`age.py` stance); (b) the DecompressionBomb reframed as a *containment* escape — refuted: path resolution is correct, it's an unguarded-decode issue (already covered by D-1); (c) a "§12 rejects any builder slider" over-claim — refuted as a code defect (a numeric group on a §12-reserved field would load), but the three docstrings were tightened to state the precise rule. **Clean bills (attacked, held):** the L3 consent gate + kind gate, L2 fail-closed on backgrounds, path/containment (`resolve_within`/`_resolve_reference`), reuse-not-fork (`_persist_image`/`resolve_within`/`assemble_scene`), and the other 11 Api bridges' `{ok}`/strict-JSON discipline. **922 tests passing (1 skipped; +1 regression test); scripted live-window smoke re-run 24/24 PASS after the fixes.** Stage 5 remains DONE-HERE; the pending hardware-validation flag (background generation + Layer-2 + edge-tuning over bright/dark backgrounds) is unchanged.
- *(Stage 5 hardware validation — 2026-07-13)* Ran a scripted real-services measurement harness on the RTX 4070 Super 12 GB (real base SDXL + real WD14 Layer-2 + Pillow compositing) plus a visual pass on the composites. **Stage 5 hardware-VALIDATED.** Background generation rendered coherent, people-free BRIGHT + DARK scenes (the `scenery, no humans` anchor + `negative_scene.txt` steer hold), first render 15–20 s incl. base load, **steady ~10.6 s/frame**, **VRAM device-wide peak ~11.9 / 12.3 GB** (idle 1.78 → ~10.1 GB app-side; torch max-alloc 11.1 GB), slot released clean. The real Layer-2 WD14 classifier passed both clean backgrounds (0 false blocks). Character-over-background compositing is clean on full-body / wide frames (transparent-OFF + ON, bottom-center @ 0.85, ~0.17 s/composite; verified visually on the live character `c517663a…` over the dark neon-street scene). **Residual (folded into 3f):** the first bust frame I tested showed a translucent full-frame pane — traced to a matte-quality limit, not a compositing bug: isnet_anime keeps tight CLOSE-UP busts (character fills ~85–94% of the frame → little background to key) at high alpha, and composite-time `alpha_floor` up to 192 + `edge_choke` cannot remove it (verified). An alpha-distribution scan of all 20 matted frames confirmed it is composition-dependent: wide/full-body frames matte cleanly (72–82% transparent), busts do not (0.4–7%). Fix when wanted: a BiRefNet / isnet-general re-matte for close-ups (the 3f residual's "BiRefNet escalation") — low priority, since full-scene composites use the wide frames. **Stage 5 hardware-validation flag CLEARED**; the close-up-bust matte item lives on as part of the open 3f residual.
- *(Stage 5.5 planning — 2026-07-13)* Paused before Stage 6 for a creator/UI/options pass;
  the pass was reframed into a stage after a repo audit at `66aa1e0` found the image pipeline
  **unreachable from the UI** (26 `image_*` bridges, 4 called). Verified findings entered as
  deferred items: the missing long-running-job contract (synchronous bridges; a live 287-second
  silent hang already shipped in `library.js`); the CLIP 77-token budget (a fully-detailed
  record assembles to 106/137 tokens — measured with the real CLIP BPE tokenizer — and has
  never bitten only because every hardware validation used a quick-create-shaped record); the
  LoRA-trigger re-derivation defect (generation re-derives instead of reading
  `LoraManifest.trigger`, so any derivation change silently de-triggers existing LoRAs); the
  11-token trigger cost; the non-scaling widget derivation; `list_characters`' per-refresh
  `rglob`+`stat` of every character directory; and the fact that only `name` and `age` are
  required, which is the root of the 3g "record pins no eye color, so it drifts" residual.
  An alternative explanation for the 3g `over_shoulder` residual (truncation rather than a
  non-canonical booru tag) was tested and **refuted** — on the sparse validation record the pose
  fragment sits at tokens 56–66 and reached the model; the bad-tag diagnosis stands. Three
  residuals promoted into Stage 5.5 (3f BiRefNet escalation — un-parked because 6e's avatar
  *is* a bust and busts key at 0.4–7% transparent; 3g canonical booru tags; 3c Layer-2 recall
  check). Personality / backstory / persona / event / scenario vocabulary deferred to Stage 6
  — those `render:false` groups have no reader, and their format is unknowable without a live
  chat loop. **No `DECISIONS.md` change required.** Stage section inlined into this file
  between Stage 5 and Stage 6; the DEPENDENCY SPINE, CURRENT STATE, and DEFERRED SPEC ITEMS
  updated to match.
- *(Stage 5.5a + 5.5b build — 2026-07-13)* **Session 1 of Stage 5.5: the long-running-job contract + the prompt budget.** Planned in plan mode (3 Explore agents mapped the bridge/shell/reconcile, service/engine/prompt, and test surfaces; 1 Plan agent adversarially traced the cancellation seam before writing). **5.5a — jobs.** New `app/jobs/` package: `JobRunner` (a single daemon worker draining a bounded `queue.Queue` — the structural single GPU slot §3; `submit(kind, fn, target_id, total)→job_id`, `status`/`cancel`/`list_jobs`/`wait_for`, per-state-change persistence to `data/jobs/<job_id>.json`, and a `reconcile()` reap sweep mirroring the Stage-4/5 vouching model — own dir, `.json` only, corrupt→skip-not-delete, non-terminal→`interrupted`, terminal→prune past `jobs.retain_seconds`); `CancelToken` (event + progress counter + a guarded, de-registerable subprocess terminate hook) published on a **thread-local** `current_token()`; `JobCancelled` subclassing `Exception` *directly* so none of the byte-frozen service loops' `except` tuples catch it; and `CancellableEngine`, a transparent engine proxy that — **only when a job's token is set** — checks cancellation before each `generate*` (raising `JobCancelled`, which unwinds through the loops' `finally: self._engine.unload()`, freeing the VRAM slot) and ticks per-frame progress, and is otherwise a pure pass-through (so the 922 legacy tests + every hardware harness, which call on the main thread with no token, are byte-identical). Cooperative cancel covers the in-process loops (bootstrap/catalog/on-demand, between frames); `_KohyaSubprocessTrainer.train` converted `subprocess.run`→`Popen`+`communicate` (kill+reap on `TimeoutExpired`; terminate hook `token.register`ed then de-registered on every exit path) so a train cancel `Popen.terminate`s kohya → nonzero exit → `TrainFailed` → `train_lora` returns before its `os.replace` → **the prior LoRA is preserved** (3d invariant untouched). `matte_catalog` (CPU ONNX, no engine call) + single-frame `generate_background` are pollable + reap-safe jobs, pre-flight-cancellable only (matching the spec's cancellation clause). Wired: `build_image_service` wraps the engine; `main.run()` builds the runner (`release`=idempotent `engine.unload` after every job, keeping the slot free for §3 swaps) and reaps at startup beside library/builders; a defensively-coerced `jobs.*` settings namespace (a hand-edited null `queue_size` degrades to the default, the `_safe_int` posture — never a silent pythonw launch crash); additive `job_submit`/`job_status`/`job_cancel`/`job_list` bridges with a six-kind dispatch (`bootstrap`/`train`/`catalog`/`on_demand`/`matte`/`background`). **The synchronous `image_*`/`scene_*` bridges and the six service methods are byte-unchanged** (git-diff-confirmed: the only edits inside those methods are 5.5b's two trigger-read lines); the front-end is deliberately NOT rewired (5.5c–d). **5.5b — prompt budget.** Fixed the trigger de-trigger defect first: `generate_catalog` + `generate_on_demand` now read `LoraManifest.trigger` via new `_generation_trigger` (falls back to derivation only for an absent/empty/unreadable manifest), and `_lora_trigger` (train-time only) shortened 16→**6 hex chars** (11→~4 CLIP tokens; SHA1-derived, provably `[0-9a-f]`, no minor-coded substring). Chunked long-prompt encoding in `engine.py` (`encode_chunked`+`_comma_windows`, used by all three backends): comma-split into ≤75-content-token windows, `encode_prompt` each, `torch.cat` embeds along the sequence axis, pooled from window 0, pos/neg chunk-lists padded to a common `k` so they are equal-length under CFG (API locked from diffusers 0.39 source; **no new dependency** — `compel` rejected per the 3f precedent; a short prompt → one window, identical to the old path). Token accounting: `clip_token_counter` (the model's own `CLIPTokenizer` from `<pipeline_config_dir>/tokenizer`, lazy+offline, `None`→honestly-unavailable, no vendored second BPE) + `token_report` (total, per-piece marginal+cumulative cost, the 77-boundary index), surfaced through the previously-uncalled `image_prompt_preview` bridge under a `tokens` key (5.5c wires it into the creator). **Adversarially verified before hand-off** (red-team + correctness + DoD/scope agents, each finding independently re-executed by a skeptic prompted to refute): red-team found **0 surviving defects** (a 20 000-iteration `register`/`cancel` race hammer, path-traversal probes on `job_id`, and Popen timeout/cross-thread-cancel drives all held; it also empirically confirmed tokenizer-1 and tokenizer-2 content counts are identical on the model's real BPE, refuting a bigG-truncation concern); correctness review found **one low-sev defect** (a hand-edited `jobs.queue_size` crashing launch) — **fixed + regression-tested**; DoD/scope audit: **all 5.5a + 5.5b DoD items PASS, zero front-end/format scope creep.** **965 tests passing, 1 skipped (+43 over Stage 5):** new `test_jobs.py` (22: runner/cancel/progress/reconcile/queue/bridge-contract), `test_prompt_tokens.py` (chunk windows + `encode_chunked` orchestration on real torch + `token_report` + real-tokenizer count, which is **validated here** — the local `models/sdxl_config/tokenizer/` is on disk), plus trigger-from-manifest, Popen-trainer contract + terminate-on-cancel, and shell job-bridge tests. **DONE-HERE; hardware-timing (5.5a) + chunking-A/B (5.5b) flags pending** (see the pending-flags list). The 3g item-10 hard-kill orphan window is now closed at the job layer by `jobs.reconcile()`.
- *(Stage 5.5c + 5.5f build — 2026-07-13)* **Session 2 of Stage 5.5: the §15 format delta + creator widgets (5.5c) and the navigation reorganization (5.5f).** **§15 fourth backward-compatible extension** (`app/model/options.py`): group `required` + `widget` (closed enum `segmented|chips|swatch|picker|slider`, unknown → load-time `OptionFormatError`) + option `image`; `derive_widget` is the sole widget authority (slider if numeric → swatch if any option carries a colour → segmented if single ≤5 → chips if ≤12 → picker) so the old `<select>` heuristic is gone; `_check_required_quick` enforces **required ⟹ quick** on the new-group AND merge paths (a required-but-not-quick group — including a merge flipping quick off while required stays true — is a load-time error); `OptionCatalog.required_group_ids()` exposes the catalog-derived required set. The 7 render-identity groups marked `required:true` in the data files; muscle's meaningless `%` unit dropped. **Render-identity construction gate** (`app/model/character.py`): `MissingRequiredSelection` + `CharacterRecord.create(required_groups=…)` — a NEW character (create OR edit) is unconstructable without the required set, enforced at the `.create()` boundary (the `age.py` "construction is the gate" pattern) driven by the catalog, defaulting empty so raw/internal construction is ungated; **load (`from_dict`) stays lenient** and `validate_against` soft-lints the gap (the §15 record-is-source-of-truth stance, and the safety-vs-completeness split — age re-gates on load because it is safety-critical; a missing eye-colour is a drift/quality concern, so legacy records still load and lint). This closes the 3g "validation record pins no eye color → drift" residual (the two on-disk records still load; the incomplete one now lints its missing skin_tone/hair_style/eye_color). **Creator service** (`app/ui/creator.py`): threads the catalog required set into every construction, maps `MissingRequiredSelection` to a structured `kind:"required"` on create + update, emits the resolved `widget`/`required`/`prompt_ranges` per group, and containment-resolves each option `image` to a bounded CSP-displayable `data:` URI via `resolve_within` (a hostile `..`/absolute/symlink path yields no thumbnail, never a raise or out-of-tree read; `builders.py` shares `_group_payload` with a null image resolver). **Front-end** (`web/creator.js` rebuilt + `app.css`): five widget renderers replace `<select>` — segmented / chips / colour-swatch tiles / a searchable-filterable-tiled-capped **picker** (holds ~200 options; a drop-in 60-option file becomes a searchable picker with no code change) / slider; sliders drop `%`, render the live `prompt_ranges` band label + imperial-at-display (cm→ft/in, kg→lb; storage stays metric); a **live prompt panel** reads the previously-uncalled `image_prompt_preview` (assembled positive + per-fragment provenance + real CLIP token count + the 77-boundary marker, refreshed on entering edit mode and after every save — the bridge loads a saved record, so it is live-on-save); the content-filter status shows **only on a block**; sections are collapsible `<details>` and the layout is a responsive two-column (form + sticky prompt panel) at window width; required groups carry a `*` + a client-side pre-save check. **5.5f navigation**: the Home view is deleted — its status card + model/logging settings + the Layer-1 filter harness move behind a **gear → Settings view** (the filter tester now a collapsible Developer-tools panel); **Library is the landing view** and **Create is a button on its toolbar**; **Scene** is split out as the primary/default builder kind beside the compositing studio (`builders.js` `currentKind="scene"`) while persona/event/scenario stay frozen (no vocabulary change). `STAGE` advanced to "Stage 5.5 — Creator & Image UI". Zero new dependencies, zero record-schema change, no image-pipeline surface added (5.5d owns the 22 unwired bridges). **The token-panel real-count path is validated here** (a fully-detailed record → 91/75 CLIP tokens, boundary at fragment 23 of 25) on the local `models/sdxl_config/tokenizer/`.
- *(Stage 5.5d + 5.5e build — 2026-07-13)* **Session 3 of Stage 5.5: the image pipeline made operable from the window (5.5d) + the library made to survive scale (5.5e).** **5.5e backend first.** `ImageService.refresh_footprint` — reloads the record FRESH, `measure_footprint`s lora/catalog/cache, and caches the result into the existing `IdentityAnchor.footprint` (not a new field) — never raising and never `touch()`ing (a derived-artifact change is not a record edit, so it must not reorder the "recently updated" view, and re-loading fresh means a 287-s catalog job or 31-min train cannot clobber a concurrent creator edit; only the footprint field is overwritten). Wired at every byte-changing op (`generate_catalog`, `clear_catalog`, `matte_catalog` behind `if matted or removed`, `generate_on_demand`, `clear_cache`; `train_lora`/`clear_lora` already persisted it) and into the Stage-4 `reconcile` sweep (step 6 — also the migration path for records written before the cache, whose stored footprint reads 0 until the first recompute). `LibraryService._summary_row` now READS the cached footprint for OK rows (the ~10k-`stat()`-per-refresh disk walk at 200 characters is gone; degraded/broken rows still measure directly — the walk stays off the hot path), and carries `_tag_labels` (record.tags → catalog option labels, unknown id → raw id, §15) covering archetype/distinctive-features/traits/wardrobe; `get_character` returns the footprint for the profile header. **5.5d wiring.** Two read bridges enable the visual surface: `image_frame_thumbnail(id, path, max_px)` (a bounded JPEG data URI for ANY char-owned frame, containment-resolved via `_resolve_reference`, missing/escaped/oversized/corrupt → `thumbnail:None` — the CSP forbids showing disk paths; `_coerce_thumb_px` clamps [64,1024]) and `image_catalog_states(id)` (the ids-only `{expression, pose, outfit}` space for the on-demand picker, `_humanize`d labels, prompt text resolved server-side — the on-demand injection-safety stance). Two new `Api._build_job` kinds: `avatar` (`generate_base_candidates` — a batch of base renders with the base backend resident across the batch, ticking the CancellableEngine per frame, unloaded in a `finally`, setting NO reference; `_coerce_candidate_count` clamps [1,8]) and `identity` (one IP-Adapter render — it loads the model, so it runs off the bridge thread, never synchronously). **Front-end:** `web/jobs.js` (the submit→poll-at-1 Hz→terminal client + a reusable progress + **cancel** widget every panel mounts; a `{ok:false}` submit resolves as a terminal error, never a hang); `web/profile.js` (the 5.5d container reached from a Library card's **Open** — identity [avatar candidates→pick→`set_reference`; IP-Adapter render with the 3b plus-band scale slider 0.3–0.6 default 0.45; clear], promotion [bootstrap→the machine-vetted grid with checkbox approval→`confirm_vetted`→train; re-cull; discard; clear LoRA — explicit, never part of creation, §17], catalog [generate/matte/clear], on-demand posing [id-triple selects→on_demand], storage/footprint/staleness/LRU/delete); `web/library.js` rewritten for scale (a grid⇄list toggle, a VIRTUALIZED list — only the visible window in the DOM, bounded indices — a tag-chip AND-filter, "Open"→profile, and catalog regeneration routed through the job contract); `web/creator.js` gained the create wizard's optional final step (offer avatar candidates→pick→`set_reference`, the character having already saved) and routed its stale-catalog regen offer through a job. **Latent 287-s hang CLOSED** in both places §6/§10 named: `creator.js` (`showUpdateOffer`) and `library.js` (`doRegenerate`) no longer call `image_generate_catalog` synchronously — both `job_submit("catalog", …)` with progress + cancel. `index.html` gained the `#view-profile` container + the library toolbar controls (layout toggle, tag filter, `#lib-job`) + a Settings "Image engine" diagnostic + the jobs/profile script includes; `app.css` the job-widget / profile / tag-chip / grid-list / virtualization styles; `app.js` re-homed `image_engine_status`/`image_engine_release` in Settings (orphaned when 5.5f deleted Home). **[HERE]/[HARDWARE] split honored** — every generation leg returns structured engine-unavailable on the sandbox and is wired for the 5.5g acceptance run; no new safety layer; Layers 1–4 re-run on every surfaced path (avatar/identity/on-demand assemble through the gated prompt path; the pose picker sends ids only); new UI disk-read paths go through `resolve_within` containment; zero new dependencies. **1004 tests passing, 1 skipped (+23 over 5.5c/5.5f):** new `test_imagegen` avatar-candidates/frame-thumbnail/refresh-footprint-reloads-fresh/catalog-caches-footprint/state-space, `test_library` cached-footprint-read + reconcile-recompute + tag-labels, `test_shell_api` avatar/identity job dispatch + frame-thumbnail bridge + jobs/profile asset checks; the on-demand "zero record mutation" test became "mutates only the footprint" (the deliberate 5.5e relaxation). JS node-syntax-checked; DOM-id cross-check clean.
- *(Stage 5.5d + 5.5e verification — 2026-07-13)* Ran three parallel adversarial review agents (red-team backend / front-end correctness / DoD-scope), each finding re-executed by the agent as a skeptic prompted to refute. **Red-team backend: 0 defects survived refutation** — every new surface (`refresh_footprint` + its five call sites, `frame_thumbnail`+`_coerce_thumb_px`, `generate_base_candidates`+`_coerce_candidate_count`, `catalog_state_space`+`_humanize`, the shell bridges + `avatar`/`identity` kinds, `_summary_row`/`_tag_labels`) confirmed never-raise, never-non-finite-JSON, containment intact; notably a hand-edited `Infinity` in the record's cached footprint raises in `Footprint.from_dict` → `store.load` doesn't catch → `load_record_guarded` maps it via `ARTIFACT_LOAD_ERRORS` to a **degraded row** (which measures a finite footprint), so no non-finite value can reach the OK-row cached path. **DoD/scope: all 5.5d + 5.5e DoD MET, no scope violations, no weakened gates** (avatar/identity/on-demand gate through `_assemble`; the pose picker sends ids only; `IdentityAnchor.footprint` reused not added). It flagged **5 orphan `image_*` bridges** — all addressed: `image_bootstrap_recull` + `image_clear_bootstrap` surfaced in the promotion panel (Re-cull / Discard candidates), `image_engine_status` + `image_engine_release` re-homed in the Settings "Image engine" panel, and `image_generate_base` documented as **intentionally superseded** by the avatar batch (the base-render capability is reachable; the singular bridge is redundant). **Front-end correctness: exactly one confirmed bug — fixed:** in `profile.js vettedGrid` the per-tile click handler toggled selection but never updated the "Confirm N for training" button, so an individually-picked subset couldn't be submitted (only "Select all" re-rendered) — now the toggle calls a shared `updateConfirm()` that recomputes the button's label + disabled state in place, called once on build for the initial state. Everything else in the front-end checklist held (job poll termination + cancel-before-id + single-settle; `busy` serializes the single §3 slot; sub-state survives re-render; feedback persists across `render()`; the virtualizer indices are bounded; refresh coalescing intact). **1004 tests passing, 1 skipped after the fixes; JS re-parsed clean; every referenced DOM id present.** **Stage 5.5d + 5.5e marked DONE-HERE; the [HARDWARE] acceptance run (the full create→avatar→bootstrap→approve→train→catalog→pose→composite-from-the-window drive with progress + cancel) rides with 5.5g.**
- *(Stage 5.5c + 5.5f verification — 2026-07-13)* Ran an adversarial red-team + correctness + DoD/scope pass on the in-scope 5.5c/5.5f files, every Python finding re-executed against real code and every JS bridge name cross-checked against `shell.py`. **Exactly one defect survived — fixed + regression-tested: MEDIUM** — `_coerce_prompt_ranges` (`options.py`) coerced the `min`/`max` bounds with a bare `float()` and no finiteness guard, unlike every sibling coercer (`_coerce_number`/`_norm_number`/`_check_sliders`). This became newly reachable in 5.5c because `_group_payload` now emits `prompt_ranges` on the `creator_catalog()` bridge (the live slider band label), so a §15 hand-edited `Infinity`/`NaN` bound loaded cleanly — *not even recorded in `catalog.errors`* — then rode verbatim into the bridge payload as invalid strict JSON, rejecting the `JSON.parse` and bricking the **entire** creator (defeating the resilient-load contract twice: silent load + total brick). Fixed with the same `math.isfinite` guard the numeric-scalar path uses (+ `OverflowError` on the `float()` like `_coerce_number`); regression test asserts strict load rejects it AND resilient load records the skip while `describe()` stays strict-JSON-safe. Clean bills (all re-executed): missing-required construction rejected with `kind:"required"` on create + update (empty `required_groups` stays ungated); the required set is exactly the 7 render-identity groups and all `quick`; hostile option `image` paths (`..`, absolute POSIX/Windows, nested traversal, missing, wrong-extension) yield **no image, no raise, no out-of-tree read** with `describe()` staying strict JSON; required-but-not-quick raises on new-file + both merge-flip paths (recorded as a skip under resilient load); unknown widget raises on new-group + merge; `derive_widget` matches the spec table across every bundled group + the 5/6/12/13-option and multi-with-colours edges; legacy `from_dict` loads a required-incomplete record while `.create()` with the catalog set rejects and `validate_against` lints; `builders.describe()` works for all four kinds with the null image resolver; the JS required client-check only gates UX (the backend `required_groups` is always passed) and picker/single/multi state mutation is correct; option-image data-URIs are always finite base64. No scope creep (no image-pipeline bridges wired — 5.5d's job; `builders.js` retains a `<select>` for its own >10-option Scenes lists, outside the "no `<select>` in the creator" DoD). **981 tests passing, 1 skipped (+16 over 5.5b): options widget-derivation/required-quick/widget-enum/image-containment/finite-prompt_ranges, the character required-gate + lenient-load + lint, the creator drop-in-60→picker + image-data-URI-containment + missing-required, and the bridge required-set/widget shape. Stage 5.5c + 5.5f marked DONE-HERE.**
