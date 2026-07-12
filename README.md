# CharacterForge

Single-user, offline Windows app: deep character creator + consistent image
catalog + library + chat with persistent memory. See `DECISIONS.md` (frozen
spec) and `BUILD_PLAN.md` (stages/state). Stages 0–2 done (scaffold + safety
foundation, character data model, creator UI); next: **Stage 3 — Image
Pipeline**.

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
  data/options/*.json   bundled option definitions (drop-ins extend them)
  ui/
    shell.py            single pywebview window + JS↔Python bridge
    creator.py          Stage 2 — creator service (catalog → UI, payload → record)
    web/                HTML/JS front-end (app.js shell, creator.js creator)
data/                   settings.json, logs/, characters/, options/ (user drop-ins)
docs/CONTENT_POLICY.md  permitted-vs-prohibited line (v1 approved, frozen)
tests/                  isolation tests (filter, model, creator, bridge)
```

Later-stage GPU/model dependencies are catalogued in `requirements-full.txt`
and installed only at their stage.
