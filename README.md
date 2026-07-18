# CharacterForge

Single-user, offline Windows app: deep character creator + consistent image
catalog + library + chat with persistent memory. See `DECISIONS.md` (frozen
spec) and `BUILD_PLAN.md` (stages/state). Stages 0–5.7 done (scaffold + safety
foundation, character data model, creator UI, image pipeline, library at
scale, builders + compositing, Vocabulary V2, optimization + Create/Library
overhaul); next: **Stage 6 — Chat Loop**.

## Launch

Double-click `CharacterForge.pyw` (relaunches itself into `.venv` under
`pythonw` — one window, no console).

From a terminal instead:

```
.venv\Scripts\python.exe CharacterForge.pyw
```

## Development

```
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pytest tests -q
```

## Layout

```
CharacterForge.pyw      launcher stub (Stage 7 replaces with packaged launcher)
app/
  main.py               entry point; wires services
  config/settings.py    JSON settings, atomic writes, model-swap scaffold
  audit/audit.py        Layer 4 — local JSONL audit log
  safety/
    layer1.py           Layer 1 — deterministic content filter (the floor)
    normalize.py        obfuscation-resistant text normalization
    data/*.txt          editable blocklists (the tuning surface)
  model/
    age.py              Layer 3 — the 20+ structural gate
    character.py        character record + identity anchor + content gates
    options.py          §15 option-definition format + resilient loader
    store.py            per-character persistence (atomic, path-confined)
    builder.py          scene/persona/event/scenario records
    builder_store.py    builder persistence
    bootstrap.py        3c bootstrap manifest model
    lora.py             3d LoRA manifest model
  imagegen/             Stage 3 image pipeline (SDXL engine, prompt assembly,
                        bootstrap+cull, LoRA training, catalog, matting,
                        scene compositing, on-demand cache)
  jobs/                 background job runner (cancellable, persisted)
  data/options/*.json   bundled option definitions (drop-ins extend them)
  data/options_gated/   adult-only options, loaded only while the gate is open
  data/builders/        scene/persona/event/scenario option data
  ui/
    shell.py            single pywebview window + JS↔Python bridge
    creator.py          creator service (catalog → UI, payload → record)
    library.py          library service (list/summary/thumbnails/delete)
    builders.py         builders service (scenes, personas, events, scenarios)
    web/                HTML/JS front-end (app.js shell, creator.js, library.js,
                        profile.js, builders.js, jobs.js)
data/                   settings.json, logs/, characters/, options/ (user drop-ins)
docs/                   CONTENT_POLICY.md (frozen v1), IMAGE_PIPELINE.md,
                        BUILDERS.md, LIBRARY.md
tests/                  isolation tests (filter, model, creator, imagegen,
                        library, builders, bridge)
```

Later-stage GPU/model dependencies are catalogued in `requirements-full.txt`
and installed only at their stage.
