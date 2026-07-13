# LIBRARY & MANAGEMENT (Stage 4 — DECISIONS.md §14)

**Status:** living companion to `BUILD_PLAN.md` Stage 4. The frozen decisions
are §14; this documents how they are implemented and which knobs exist.

---

## 1. Surfaces

| Surface | Where | What |
|---|---|---|
| `library_list` | `app/ui/library.py` → bridge | One summary row per stored character: name/age/timestamps, identity flags (`has_lora`, `has_reference` — containment-checked, not just non-null), catalog/cache frame counts + staleness, MEASURED footprint, §14 deletion recommendation. Unloadable records degrade to error rows (still deletable), never hide. |
| `library_get` | 〃 | A record serialized back into the creator-form shape + `issues` (the §15 soft lint against the live option catalog). |
| `library_update` | `app/ui/creator.py` `update_character` | The edit path — see §3. |
| `library_delete` | `app/ui/library.py` | Removes the whole `characters/<id>/` tree. Requires a valid id only, NOT a loadable record: deletion is the remedy for corrupt/policy-blocked records. Audited. |
| `library_thumbnail` | 〃 | The identity reference image as a ≤256 px JPEG data URI (CSP allows `img-src data:` only). Missing/corrupt/escaped reference → `null`, never an error. |
| `library_reconcile` | 〃 | The startup reconciliation sweep — see §4. Also runs at every app launch (`app/main.py run()`), fail-safe. |

Sorting/filtering is client-side (`web/library.js`) over the list payload —
the rows carry every axis (name, timestamps, footprint, staleness, flags).

## 2. Disk thresholds + LRU cap (the resolved deferred item)

Settings section `library.*` (defensively coerced: bad hand-edit → code
default; clamped to [8 MB, 1 TB]):

| Key | Default | Meaning |
|---|---|---|
| `cache_cap_bytes` | 268435456 (256 MB) | §14 automatic per-character LRU cap on the on-demand cache — the backstop. ~115 cached states at the measured ~2.2 MB/state (frame + matte). |
| `recommend_cache_bytes` | 201326592 (192 MB) | §14 deletion recommendation threshold on the cache — the user-facing signal, deliberately BELOW the cap so deliberate management is suggested before the backstop bites. |

**Eviction** (`ImageService.enforce_cache_cap`, pure pick in
`app/imagegen/manage.py::select_evictions`): when the measured `cache/` tree
exceeds the cap, least-recently-used entries (the 3g `last_used` stamp; a
missing stamp reads as oldest; `frame_id` tiebreak) are purged — frame +
sidecar + matte, under the same containment trust rules as every other cache
purge — until back under the cap. The single most-recently-used entry is
never evicted (a cap below one frame's cost must not thrash
generate→evict→regenerate; the tree is still bounded by cap + one state).
Runs after every on-demand cache insert (best-effort — never fails the
generation) and per character during the reconcile pass. Audited
(`cache_evicted`). Evicted states simply regenerate on demand (§14).

The seed catalog is NEVER evicted — the cap governs only the grown cache.

## 3. Editing (§14: offers, not forces)

`CreatorService.update_character(id, payload)`:

- Same strict shape validation as creation; the record is **rebuilt**, so the
  Layer-1 content gates and the structural 20+ age gate re-run on every
  channel — an edit cannot smuggle in what creation would refuse.
- Preserved across the edit: `id`, `created_at`, and the whole
  `IdentityAnchor` (reference, LoRA state, footprint). Editing never touches
  trained identity.
- The payload **replaces** the selection/tag/slider/free-text sets wholesale
  (the form submits its full state — omitting a field unsets it).
- **Render-change detection:** the old and new records are assembled through
  the Stage-3 prompt assembler and compared on the positive prompt — the
  single source of render truth. A personality-notes or name edit renders
  identically → catalog NOT marked stale (a false stale would cheapen the
  signal); an appearance/wardrobe/slider/age edit differs → the catalog AND
  cache manifests are marked `stale` (refreshing `updated_at`, which
  correctly invalidates the 3f/3g optimistic tokens). If either side fails
  to assemble (e.g. a blocklist tightened since creation), the comparison is
  inconclusive and conservatively reads as changed.
- Regeneration is **offered** by the UI (edit-flow offer + a per-row
  "Regenerate catalog" action on stale rows) and never invoked by the edit
  itself.
- Audited (`character_updated`, with `render_changed` + stale marks).

## 4. Startup reconciliation sweep (the resolved deferred item)

`LibraryService.reconcile()` — runs at every launch before the window opens
(fail-safe: any fault is audited and never blocks the launch), and on demand
from the bridge. Per character:

1. **Stale staging/backup dirs** — `catalog.old`, `catalog.new`,
   `cache.new`, `vetted.new` are only ever populated mid-run, so at startup
   they are hard-kill leftovers by definition and are removed. (This
   includes the 3e double-fault `catalog.old` recovery copy — per the
   deferred item's "drop `*.old` orphans"; frames are regenerable from the
   surviving LoRA and the removal is audited with its byte count.)
2. **Bootstrap-candidate orphans** — files under `bootstrap/candidates/`
   that `bootstrap.json` does not vouch for (the 3c mid-batch-kill class).
   An absent manifest vouches for nothing; a **corrupt manifest sweeps
   nothing** (orphanhood cannot be proven against a corrupt witness — noted
   and reported instead).
3. **Manifest verification** — catalog + cache entries whose frames no
   longer exist (or whose recorded paths escape the character dir) are
   dropped; dangling `matted_path` pointers are cleared (they re-matte/heal
   later). Saved only when something changed, so the optimistic token is
   never gratuitously invalidated.
4. **Cache-artifact orphans** — files under `cache/` and `cache/matted/`
   the (verified) manifest does not vouch for — the named 3g kill-window
   class (frame+sidecar(+matte) between survivor-move and manifest-save).
5. **The §14 LRU cap** (per §2 above).

Deletion discipline: only files matching our own artifact patterns
(`*.png`, `*.json`, `*.png.tmp`), only as direct children of our own
artifact dirs, only when a trusted manifest fails to vouch for them. A
user's stray `notes.txt` is never touched. Idempotent — a second run finds
nothing. Audited per character (`library_swept`) + a run summary
(`library_reconciled`).

## 5. Safety posture

Nothing new attaches at Stage 4 (per the build plan). The edit path re-runs
Layer 1 + Layer 3 by construction (§3 above); Layer 4 audits cover update,
delete, sweep, and eviction; the thumbnail read is containment-checked
exactly like every other stored-path use. The bridge keeps the structured-
result contract on every new method.
