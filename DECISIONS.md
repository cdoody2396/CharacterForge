# PROJECT DECISIONS — FROZEN SPEC

**Status:** Frozen. This document records decisions that are locked. It does not track build progress — see `BUILD_PLAN.md` for that.

**How to use:** Any chat working on this project reads this file first. Everything here is decided and immutable unless the user explicitly reopens a specific decision. Do not re-litigate. Do not "improve" a locked decision without the user reopening it. The Rejected Alternatives section exists so settled tradeoffs are not re-argued.

---

## 1. PRODUCT DEFINITION

A single-user, offline Windows application combining:
- A deep, selection-driven character creator (fantasy / real / sci-fi / non-human, exhaustive customization).
- An image pipeline producing a consistent visual catalog of each character across poses, expressions, outfits, and scenes.
- A character library (view / edit / sort / filter / manage).
- A chat/dating interface where characters have persistent, human-like memory and an avatar that updates with the conversation.
- User-authored personas, scenes, events, and scenarios to interact within.

Personal use (creator + friends). Not a commercial release, but must function correctly as if it were.

All characters are 20+. This is a hard, structural, non-negotiable constraint (see §11).

---

## 2. PACKAGING & PLATFORM

- **Windows.**
- **Single-launch app folder** (Steam-style): one thing the user double-clicks. Launcher plus bundled data and models inside a self-contained folder. **Not** a literal single `.exe`.
- **Fully offline.** No network required at any point. No server-side anything.
- **One window.** No additional windows spawned. No console/terminal popups. Background model-serving processes run headless behind the single UI.

Rationale: local image generation and local chat require multi-GB model weights and headless serving processes. These fit behind one window but cannot be a literal single file at any reasonable quality. The app-folder form also makes updating and adding options far easier, which directly serves the extensibility requirement.

---

## 3. HARDWARE TARGET

- **Single tier.** No tier system. Weaker-machine floor sets the target; stronger machines run the same stack faster.
- **Floor:** 16 GB VRAM, 64 GB system RAM, 1 TB disk.
- **Model-swapping is architectural.** On a 16 GB floor the image model and chat model cannot co-reside. The app loads one heavy model at a time and swaps. This is assumed everywhere downstream.
- **Stronger hardware:** opt into a heavier image or chat model via a settings toggle. Same stack, no separate build.
- **No time constraints anywhere.** Slow is acceptable. Pre-loading, pre-rendering, and long generation/training times are all fine. Quality over speed is the standing priority — the user has explicitly authorized making processes *heavier* for quality.

---

## 4. RENDERING

- **Stylized / illustrative (anime-derived), on an SDXL-derived base model.**
- **One rendering style app-wide.** Genre is expressed through content, wardrobe, and setting — not through per-character rendering styles (which would mean multiple model stacks).
- Chosen because it is the only style spanning the full stated content range (fantasy, real-ish, sci-fi, non-human, exotic anatomy) with strong local consistency, mature/adult support, and the strongest local LoRA + identity ecosystem on a 16 GB floor.

Specific base-model checkpoint is a spec-time pick against then-current options and is swappable. The *style class* is the commitment.

---

## 5. CHARACTER RECORD MODEL

- **Prompt-driven with identity lock.** Not a composited art rig.
- The character record is **a structured prompt + a per-character identity anchor**, not a set of art-part IDs.
- Chosen because it carries the full content range and the "add options later" requirement (new options are new tokens / small data additions), which a composited rig cannot without enormous per-option art production.

---

## 6. IDENTITY PIPELINE

- **Layered hybrid: IP-Adapter baseline (always-on) + opt-in trained LoRA promotion.**
  - Quick-create → IP-Adapter reference for immediate consistency.
  - Detailed-create → optional LoRA promotion for any character that gets a full pose/outfit catalog.
  - The character record carries a "has-LoRA" state.
- **Quality-maximized.** Heavier pipeline is authorized: multi-stage culling, face-swap identity lock, higher training step counts, larger vetted sets where they help.

### Identity LoRA facts (locked, corrects a common misconception)
- Identity LoRA teaches **identity only** — this face, this body. It does **not** require pose or angle variety. Pose comes from the base model at generation time.
- Practical training set: **~15–30 vetted, on-model images.** Tight and consistent beats large and scattered. Drifted images make identity *worse*, not better.
- **Bootstrap flow:** single strong reference image → generate a seed batch steered by it → auto-filter (face-embedding similarity via ArcFace/InsightFace + aesthetic/quality scoring) → optional face-swap to lock the identity face onto the batch → user confirms a small pre-vetted grid → train. Automated culling does the heavy lifting; user approval drops to confirming ~12 machine-vetted images.

### Identity LoRA ≠ pose/scene catalog
- The identity LoRA and the pose/scene catalog are **separate artifacts**.
- The catalog is **output** generated *using* the LoRA — it is not training input.
- Optional pose/scene LoRAs (e.g. a reusable "sitting" LoRA) are **character-independent** and shared across characters. They are an enhancement, not part of per-character identity training.

**Known limit:** IP-Adapter identity strength is weakest on heavily non-human anatomy (the fantasy-creature end). LoRA promotion is the mitigation — this is a reason both mechanisms exist rather than IP-Adapter alone.

---

## 7. IMAGE CATALOG MODEL

- **Seed-plus-grow.**
  - **Seed:** at character creation, pre-render a core matrix of common states (expressions × poses × outfits, including the defined wardrobe), locked via the character's identity anchor.
  - **Grow:** states outside the core generate on demand and cache into a growing per-character library.
- Chat selects the nearest core frame instantly for the common case; novel states generate on demand (model-swap + generation, absorbed by "slow is fine").
- **Auto-filter** (face-embedding + quality score) rejects malformed on-demand frames and regenerates rather than showing them. Same filter as the training pipeline.

**Disk:** a growing per-character catalog needs a disk budget and eviction policy (see §15).

---

## 8. CHAT MODEL

- **Mid-class quantized LLM (≈12–24B).**
- Largest class that clears the persona-depth bar *and* still fits with the image model swapped out. Slower token rate is absorbed by "slow is fine"; context budget is tight when loaded.
- Small (7–9B) rejected as too shallow for the stated character-depth goal. Large (27–70B) rejected as fighting the 16 GB floor at quality-eroding quantization.
- Specific model is a spec-time pick against then-current options, swappable via the same settings toggle as the image model. The *class* is the commitment.

**Swap cost note:** the chat→image model swap lands mid-experience and sets the rhythm of chat. This is accepted, not a defect.

---

## 9. MEMORY ARCHITECTURE

- **RAG + human-decay memory dynamics.** This is the project's differentiating system, structurally enabled by being local and file-based.
- Per-character persistent memory store (files / embeddings). The character record plus conversation events are written to disk.
- **Per turn:** retrieve only the relevant slice (this character's traits + past events bearing on the current moment) and inject it. Context stays lean; the full record is never dumped every turn.
- **Decay model:** memories carry metadata — recency, emotional salience, reinforcement count. A scoring function models human forgetting: recent and emotionally-charged memories surface easily; old trivial ones fade and drop from retrieval unless reinforced by being brought up again.
- **Tunable and defeasible:** decay rate, salience weighting, and similar are exposed knobs, not hardcoded constants. The decay model can be tuned or toggled off entirely without structural change (toggling it off yields plain RAG — the fallback tier, with no wasted work).

**Flags:**
- The decay model is the one part here that can *feel wrong* if mistuned. It needs a tuning pass against real conversation. Knobs must be exposed.
- Memory retrieval and the image-model swap contend for VRAM when both fire on one turn. **Sequence them:** retrieve on the chat model, then swap to the image model. This ordering is a hard constraint on the chat loop.

---

## 10. CHARACTER CREATION — INPUT MODEL

- **Structured tags + filtered free text.** General mechanism for backstory, personality, and the scene/persona/scenario/event builders.
  - Tags give the model reliable structured signal (vibe).
  - Free text adds specifics the tags cannot.
  - The character pulls vibe from tags and specifics from text.
- **Free text is filtered input, not raw.** It passes the Layer-1 deterministic filter and the Layer-2 semantic gate (see §11). The original "no free text" rule is now "structured tags + filtered free text," and the safety architecture accounts for the change.
- **Names:** free text with a deterministic slur/blocklist pass (Layer 1); reject-and-prompt on a hit. Naming is the one near-forced text exception.
- Selection methods otherwise: dropdowns, radials, color wheels, lists, segmented pickers — whatever fits the section.
- **Quick-create vs detailed-create** are different identity tiers, not just different form lengths: quick → IP-Adapter; detailed → optional LoRA promotion.

---

## 11. SAFETY ARCHITECTURE

**Four-layer defense-in-depth.** Each layer covers what the others cannot; dropping any opens a class of failure the others don't close.

1. **Hard-coded pre-filters (deterministic, no model).** Input/output blocklists, regex/classifier gates on the prohibited categories, image-prompt filtering before generation, name slur-block. Un-promptable, model-independent. The floor. Non-negotiable — the only layer that does not depend on a model choosing to comply.
2. **Model-level gating.** System-prompt boundaries, refusal behavior on the LLM; negative prompts + content classifiers on the image side. Catches *semantic* intent the keyword layer misses. Softest layer (prompt-based).
3. **Structural enforcement.** The data model makes prohibited states *unrepresentable*, not merely blocked. Cleanest example: character age has no sub-20 option and is validated as a hard gate at creation — under-20 is not "caught," it cannot be constructed. Extend the principle wherever a constraint can move from checked to structurally impossible (consent framing in scenario templates, category boundaries in scene/event builders).
4. **Logging & review (local accountability).** Every generation and conversation can be logged locally. Not enforcement, but makes boundary-testing visible/reviewable and gives a tuning signal for where other layers leak. Cheap given the file architecture.

### Honest bar (accepted by the user)
A local model whose weights are on the user's disk **cannot be made unbypassable by a determined operator who owns the machine.** The design goal is **defense in depth**: normal use is safely bounded, accidental drift is caught, deliberate circumvention must defeat several mechanisms at once. The goal is **not** adversary-proof-against-the-owner — that is unachievable locally and would be a false claim.

### Prohibited categories
Under-20 (hard/structural), non-consent, self-harm/suicide, drugs, extraction of medical/legal advice, bestiality, and the general set of things AI normally safeguards against.

### Permitted content
Full adult content is permitted, including explicit anatomy and adult scenarios. The exact permitted-vs-prohibited line is a **spec-time deliverable drafted for user approval** — it is not left to the model's turn-by-turn judgment. **Approved 2026-07-10:** the governing content line is `docs/CONTENT_POLICY.md` (v1, rulings R1–R8 approved as drafted). It is now frozen on the same terms as this document — reopening a ruling requires the user to explicitly reopen it.

**Hardest category (flagged):** manipulation toward a prohibited outcome through otherwise-clean turns — a conversation that trips no keyword but steers somewhere prohibited. Only Layer 2 (semantic) and Layer 4 (review) touch it, and Layer 2 is the soft one. This needs explicit spec attention and iterative tuning; it cannot be fully closed with deterministic code.

---

## 12. ANATOMY SELECTION

- **Categorical selectors, not continuous sliders, for discrete anatomy.** Presence / type / build / proportion / broad size categories as discrete options.
- **Grouped by body region, progressive disclosure** (expand a region to reach detailed options) — keeps the exhaustive option set navigable.
- **Explicit anatomy is permitted** (bounded by §11 safety layers; the 20+ gate and prohibited-category filters are unaffected).
- **Consistency is a LoRA property, not a creator-field property.** The creator sets the categorical target; the LoRA promotion pass makes anatomy consistent across the catalog. The creator defines, the LoRA enforces.
- **Continuous sliders are reserved for axes the model honors continuously** — height, weight, muscle mass (broad visual ranges). Discrete anatomy uses categorical controls.

**Known capability limit (user-approved):** the model renders *categorical* anatomy reliably and *fine dimensional* specification unreliably. Pseudo-precise anatomical sliders would imply control the pipeline cannot deliver, so they are not used. This is a property of how these models consume conditioning, not a tuning gap.

---

## 13. SCENES / PERSONAS / SCENARIOS / EVENTS

- **Lighter structured builder** (not the full character engine), using the same tags + filtered-free-text mechanism.
- **Backgrounds/scenes are generated by the same image pipeline** (same base model, same tag+text builder) and catalogued.
- **Character-over-background compositing**, not character-in-scene single-pass. Generating a character correctly *inside* a specific scene in one pass fights identity consistency; compositing an already-consistent character frame over a separately-generated background sidesteps that. This is the correct architecture, not a convenience.
- **Background on/off toggle:** character alone (transparent/neutral) or composited over a scene.
- **Requires matting** (background removal on catalog frames, or generating frames on a keyable background) for clean compositing rather than pasted cutouts.

---

## 14. LIBRARY & MANAGEMENT

- View / edit / sort / filter.
- **Editing a character offers regeneration** (does not force it) and **marks the catalog stale** so the user knows frames no longer match the edited record. Forced regeneration on every tweak is rejected as punishing given training time.
- **Per-character footprint display:** each character shows disk usage (LoRA + catalog + cached on-demand frames).
- **Deletion recommendation** surfaced when a character's cache grows past a threshold.
- **Automatic LRU cap per character** as a backstop: evicted on-demand frames regenerate on demand if needed again, so a never-cleaned character cannot grow unbounded. User-facing recommendation for deliberate management; automatic cap as the safety net.

---

## 15. IN-GAME OPTION EXTENSION

- **Data-file format first, and correctly.** The app reads option definitions (races, outfits, traits, anatomy categories, etc.) from structured files at startup. Drop in a new definition file — it appears in the creator, no rebuild. This is the real mechanism and directly satisfies "easy to add options later."
- **In-app editor UI is a later layer** on top of that format — a friendlier way to author the same files. It is a convenience, not the mechanism. Committing to the format now makes the editor cheap later, with no rework either way.

---

## 16. CAPABILITY LIMITS (HONEST BOUNDARIES)

Stated plainly so they are not discovered late:

- **Anatomy control is categorical, not dimensionally precise** (§12).
- **IP-Adapter identity weakens on heavily non-human anatomy**; LoRA promotion mitigates (§6).
- **Local safety cannot be made owner-proof**; the bar is layered defense-in-depth (§11).
- **The decay model can feel wrong if mistuned**; it requires a tuning pass and exposed knobs (§9).
- **GPU/weight-dependent components cannot be fully exercised in the build sandbox** (no GPU, no bundled weights). Those stages produce verified code + config; final validation happens on the target machine. See `BUILD_PLAN.md` for which stages are which.

---

## 17. REJECTED ALTERNATIVES (do not re-argue)

| Decision point | Rejected | Reason |
|---|---|---|
| Packaging | Literal single `.exe` | Buys nothing but a slower, more fragile version of the same app-folder; harder to update/extend. |
| Hardware | Tier system | Multiplies config + test surface for no gain on a personal build. |
| Rendering | Photoreal | Fights fantasy/exotic range; consistency degrades badly on non-human subjects. |
| Rendering | Painterly/semi-real | Weaker tooling for tight consistency; smaller ecosystem. |
| Rendering base | FLUX | Weak LoRA-training/identity tooling at 16 GB; thin adult/fantasy support. |
| Character record | Composited art rig | Enormous per-option art burden; fights fantasy/exotic range and "add options later." |
| Identity | IP-Adapter only | Drops reliable trained catalogs; identity drifts at the exotic end. |
| Identity | LoRA only | Forces ~15–40 min training on every throwaway character. |
| Catalog | Strictly finite | Any unrendered state is simply unavailable to chat. |
| Chat model | Small (7–9B) | Too shallow for stated character depth. |
| Chat model | Large (27–70B) | Fights the 16 GB floor at quality-eroding quantization. |
| Memory | Static system prompt | No cross-window memory; a chatbot, not a living character. |
| Memory | Plain RAG (as target) | Kept as fallback only; drops the distinctive forgetting behavior. |
| Safety | Model refusal alone | The single most-bypassable layer; the exact single-layer approach that fails locally. |
| Anatomy | Continuous precision sliders | Model cannot honor fine dimensional control; overpromises. |
| Scenes | Character-in-scene single-pass | Fights identity consistency vs. compositing. |
| Options | Editor UI first | Format is the real mechanism; editor is a convenience layer on top. |
