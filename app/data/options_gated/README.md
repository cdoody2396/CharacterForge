# Gated option definitions (5.6a — §11 Layer-3 pattern)

Adult-only option files (`*.json`, same §15 format as `app/data/options/`)
live here. This directory is passed to the option loader **only while the
content gate is open** (`content.gate_open` in `data/settings.json`); with
the gate closed the catalog *structurally lacks* these entries — the creator
cannot offer them and the prompt assembler (which consumes the same catalog)
never sees them. Nothing filters.

Authoring rules:
- Gated files load **after** every ungated directory (bundled → `data/options`
  → here → `data/options_gated`). They should only append groups/options —
  do not carry scalar overrides of ungated groups.
- Same fragment rule as everywhere: every `render:true` option ships a
  canonical Danbooru-register `prompt` fragment, and every shipped fragment
  must pass the Layer-1 gate at assembly.
- The vocabulary itself (wardrobe_intimate, anatomy_intimate, gated
  placements) is authored in Stage 5.6c per `CHARACTER_VOCABULARY_V2.md`
  flag 7; nothing ships here in 5.6a.

Runtime drop-ins belong in `data/options_gated/` (created on demand), which
loads after this directory.
