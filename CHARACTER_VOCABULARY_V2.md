# CHARACTER VOCABULARY V2 — FROM-SCRATCH OPTION DESIGN (Rev 2)

**Status:** Built (Stage 5.6, 2026-07-17) — implemented into the option data files, the §15 format extensions (`visible_when`, `class`, `tier`), and the free-text swap. Retained as the design rationale + contract record; the contract flags below remain live.
**Rev 2 changes:** all sliders removed (species-relative categorical frame); single-setting world model (dual-world/isekai structure demoted to a `roots` backstory group); relationship-instance fields removed from the record (replaced by social dispositions; the instance is scenario-builder state); modern real-world coverage expanded across occupation, wardrobe, hobbies, tastes, skills, events, workplaces, living situations.

---

## 0. CONTRACT FLAGS — READ FIRST

1. **BUILD_PLAN deferral tension.** BUILD_PLAN defers personality/backstory/persona/event/scenario vocabulary to Stage 6 ("designing the vocabulary now produces a guess that gets rewritten after the first real conversation"). This document reopens that on user instruction. Consequence accepted: **Subsets C and D are Stage-6 design inputs whose `Mem` column is provisional** — a target contract for 6d persona injection and the §9 RAG seed, expected to be re-cut after the first live chat loop. Subsets A and B (render-side) are actionable now.
2. **Required set unchanged.** The 7 required/quick group ids (`race`, `gender_presentation`, `skin_tone`, `hair_color`, `hair_style`, `eye_color`, `body_type`) are kept **byte-identical as ids** — only labels and option lists change (data-only). No construction-gate contract change. All new groups are `required:false`.
3. **Sliders removed — §12 carve-out reopened (user, this revision).** The height/weight/muscle sliders are deleted. Reason: the model consumes categorical tags, not numbers; absolute metric bands are species-blind (a 152 cm dwarf is a *tall* dwarf, not a "short" character); a slider whose only output is one of four bands is pseudo-precision — the exact failure §12 rejects for anatomy. Replacement: species-**relative** categorical groups (B2 height band, B3 muscle definition); overall mass folds into the silhouette. §12's slider-reservation text needs a recorded amendment in `DECISIONS.md`; the `prompt_ranges`/imperial-display machinery goes dormant (kept in the format, unused); legacy records holding cm/kg load leniently and surface as a `validate_against` lint. The §12 principle — categorical over pseudo-precise — is *strengthened*, not weakened.
4. **No relationship instance on the record.** The character record never encodes who the user is to them. The record carries **social dispositions** (how they treat strangers, authority, people they lead, romantic interest — Subset D-v); the **instance** (relationship, shared history, opening scene, the user's persona) is scenario/persona-builder state (§13) layered on at chat time. One character reuses cleanly across any user persona and any scenario.
5. **Two §15 format deltas are needed** (fifth extension — plan, do not implement from this doc):
   - group `visible_when` — data-driven conditionality (species-class blocks, `roots`-conditional displacement, gated sub-fields). Fallback without it: always-visible groups with `none / n-a` defaults.
   - option `class` metadata on `race` options — the key `visible_when` conditions read.
6. **Age is not reopened.** The numeric ≥20 construction gate (§11 Layer 3) stays exactly as built. `apparent_age` is a render-facing band on top of the gate; every band is adult.
7. **Gating is structural.** Adult-only options live in separate gated data files (`wardrobe_intimate`, `anatomy_intimate`, gated placements) loaded only when the content gate is open — the ungated catalog simply lacks the entries (§11 Layer 3 pattern).
8. **Fragment authoring rule (3g lesson):** every `render:true` option ships a canonical Danbooru-register `prompt` fragment; every shipped fragment must pass the Layer-1 gate at assembly (existing test pattern). This doc enumerates **labels**; fragments are a data-file task with the token panel open.
9. **Excluded from the record by design:** expression, pose, outfit-variant (scene-time `catalog_states`), and anything user-shaped (flag 4).
10. **Free-text survivors — five slots**, all Layer-1 filtered: `name`, `nickname`, `catchphrase`, `signature_note` (visual), `companion_name` (conditional). Old appearance-notes / personality-notes / backstory free text is deleted in favor of enumeration. The user-facing custom nickname ("what they call you") is persona/scenario-time, not a record field.

---

## 1. LEGEND

Per-group metadata line: `id · Label · kind(count) → widget · Req · Cond · Img · Mem · Home`

- **kind → widget** follows the §15 derivation verbatim (swatch if any option carries `color` → segmented if single ≤5 → chips if ≤12 → picker). The slider branch never fires — no numeric groups remain.
- **Req:** `R` required (construction gate, ⟹ quick) · `O` optional · `O*` optional but on the **quick** path with a default.
- **Cond:** visibility condition (requires flag 5), else `—`.
- **Img (assembly tier, post-5.5b chunking):** chunking means nothing is dropped; **tier = window ordering + inclusion rule.**
  - `P0` first-window head, never displaced (LoRA trigger ~4 tok, subject-count, species core, presentation).
  - `P1` first-window body — render identity; must co-reside with P0 inside the 77-token first window (pooled embeds).
  - `P2` second window — strong visual detail. `P3` tail — weak-honor detail, style flavor.
  - `img:scene` only when scene state calls for it · `img:gated` only under an open gate · `—` never rendered.
- **Mem (provisional until 6d — flag 1):** `inject` persona card, every turn · `seed` §9 RAG store with salience metadata · `guard` seeded, resists disclosure until earned (secrets contract) · `scene` enters via active scenario state · `gated` only with the gate open · `latent` record-only, never auto-injected.
- **Home:** `record` · `builder` (§13 persona/scene/event/scenario catalogs) · `state` (`catalog_states.json`).
- Chips caps are selection caps (UI-enforced), not list sizes.

**Quick path** = 7 required groups + `name` + `age` + `O*` groups (`apparent_age`, `wardrobe`, `setting`) — 12 touches to a renderable, chattable character.

---

## 2. ARCHITECTURE — FOUR SUBSETS

| Subset | Contents | Consumer |
|---|---|---|
| **A. Identity & Origin** | name, age block, species block, presentation/pronouns, archetype | both pipelines |
| **B. Body & Render** | frame, skin, hair, face, species features, marks, anatomy (gated), wardrobe, aesthetic | image (render:true) |
| **C. Mind & Voice** | temperament, traits/flaws/quirks, values/fears/goals, skills, speech & voice, emotional profile | chat (render:false) |
| **D. Life & Bonds** | setting & roots, standing, occupation, backstory, daily life, tastes, social dispositions; scenario handoff | chat + §13 builders |

Creator presentation: four collapsible sections (existing `<details>` progressive disclosure); gated sub-regions inside B appear only with the gate open.

---

## 3. SUBSET A — IDENTITY & ORIGIN

**A1 · `name` · Name · free text (Layer-1 slur pass) · R · — · img:— · mem:inject · record** — existing behavior, unchanged.

**A2 · `nickname` · Goes by · free text, short · O · — · img:— · mem:inject · record**

**A3 · `age` · Age · numeric ≥20 (existing hard gate) · R · — · img:— (renders via A4 only) · mem:inject · record** — unchanged; §11 Layer 3.

**A4 · `apparent_age` · Apparent age · single(8) → chips · O* (default derived from A3) · — · img:P1 · mem:inject · record**
early 20s, mid-to-late 20s, 30s, 40s, 50s, 60s, elderly, ageless adult
*(Every band is adult. Exists for the elf/vampire/android case where looks ≠ years. Defaults to the band containing A3.)*

**A5 · `age_reality` · True age is… · single(7) → chips · O · — · img:— · mem:seed · record**
matches appearance, older by decades, centuries old, millennia old, unknown even to them, newly created, reborn into this life

**A6 · `race` · Species · single(~86) → picker · R (existing id) · — · img:P0 · mem:inject · record**
- *Human & near-human:* human, demigod, changeling, dhampir, half-elf, half-orc, cambion (half-demon), nephilim (half-angel), half-dragon, half-fae
- *Elven & fae:* high elf, wood elf, dark elf, snow elf, desert elf, sea elf, fae, fairy, pixie, nymph, dryad, sylph, satyr
- *Stout & green:* dwarf, gnome, halfling, goblin, hobgoblin, kobold, orc, ogre, troll, oni, half-giant
- *Beastfolk:* catfolk, kitsune, wolffolk, dogfolk, rabbitfolk, mousefolk, ratfolk, deerfolk, bearfolk, cowfolk, sheepfolk, batfolk, birdfolk, crowfolk (tengu), owlfolk, otterfolk, lizardfolk, snakefolk, sharkfolk, mothfolk, beefolk, mantisfolk
- *Monstrous body-plan:* lamia, merfolk, centaur, harpy, arachne, slime, gorgon, minotaur, alraune (plantfolk), scylla
- *Draconic:* dragonkin (scaled humanoid), dragon (anthro)
- *Fiend & celestial:* demon, imp, succubus/incubus, devil, tiefling, angel, fallen angel, aasimar, valkyrie, starborn celestial
- *Undead & spirit:* vampire, ghost, wraith, banshee, revenant, lich, preserved undead, skeletal undead, jiangshi, dullahan
- *Construct & synthetic:* android, cyborg, humanoid robot, clockwork automaton, golem (stone), golem (iron), golem (clay), living doll, animated puppet, AI hologram, vat-grown bioform
- *Elemental & cosmic:* flamekin, frostkin, stormkin, stonekin, verdantkin, shadowkin, lightkin, void-touched, eldritch-touched, alien (humanoid), alien (grey), alien (exotic), shade
*(Each option carries `class` metadata — flag 5 — driving Subset B conditional groups and B2's species-relative anchor.)*

**A7 · `hybrid_race` · Second heritage · single(same list) → picker · O · — · img:P2 · mem:seed · record**

**A8 · `gender_presentation` · Presentation · single(3) → segmented · R (existing id) · — · img:P0 · mem:inject · record**
feminine, masculine, androgynous

**A9 · `gender_identity` · Identity · single(6) → chips · O · — · img:— · mem:inject · record**
woman, man, nonbinary, genderfluid, agender, unlabeled

**A10 · `pronouns` · Pronouns · single(8) → chips · O (default from A8/A9) · — · img:— · mem:inject · record**
she/her, he/him, they/them, she/they, he/they, it/its, xe/xem, name only

**A11 · `archetype` · Archetype · single(~32) → picker · O · — · img:P2 · mem:inject · record**
warrior, knight, samurai, ninja, gunslinger, soldier, mercenary, ranger, monk, mage, sorcerer, witch, druid, priest, healer, bard, rogue, assassin, pirate, outlaw, detective, noble, royal, merchant, scholar, inventor, engineer, pilot, hacker, idol, courtesan, wanderer, commoner, adventurer
*(Narrative flavor only — the day job lives in D7 `occupation`. A goblin with archetype "warrior" and occupation "line cook" is the intended kind of collision.)*

---

## 4. SUBSET B — BODY & RENDER

### B-i · Frame  *(all categorical — flag 3)*

**B1 · `body_type` · Silhouette · single(12) → chips · R (existing id) · — · img:P1 · mem:inject (physique one-liner with B2/B3) · record**
petite, slim, willowy, lithe, average, athletic, curvy, voluptuous, stocky, muscular, heavyset, hulking
*(Silhouette now carries overall mass — the deleted weight slider's job.)*

**B2 · `height_band` · Height · single(7) → chips · O · — · img:P2 · mem:inject (physique line) · record**
very short, short, a touch short, average, a touch tall, tall, towering
*(**Species-relative** — measured against their kind's norm, not a metric scale. A tall dwarf is a tall dwarf; the species tag anchors absolute scale, the band modifies within it. Chat phrasing derives naturally: "tall for a goblin, about chest-height on you." Extremes are weak-honor on non-standard frames — §16 note. A per-species display-height table for concrete lore answers is open item 8.)*

**B3 · `muscle_def` · Muscle · single(5) → segmented · O · — · img:P2 · mem:inject (physique line) · record**
soft, toned, defined, powerfully built, massive
*(Weight group: **deleted**. Height/weight/muscle sliders: **deleted** — flag 3.)*

### B-ii · Skin

**B4 · `skin_tone` · Skin tone · color(28) → swatch · R (existing id) · — · img:P1 · mem:inject · record**
porcelain, fair, peach, light olive, olive, golden, tan, bronze, brown, deep brown, ebony, alabaster, ashen grey, slate grey, jet black, pearl white, blue, deep blue, teal, green, moss green, lavender, violet, pink, red, crimson, gold metallic, chrome

**B5 · `complexion` · Complexion · single(7) → chips · O · — · img:P3 · mem:— · record**
flawless, soft matte, dewy, luminous, weathered, rough, battle-worn

### B-iii · Hair

**B6 · `hair_color` · Hair color · color(25) → swatch · R (existing id) · — · img:P1 · mem:inject · record**
black, soft black, dark brown, brown, chestnut, auburn, ginger, red, crimson, strawberry blonde, blonde, platinum, white, silver, grey, pink, rose, orange, blue, navy, teal, green, mint, purple, lavender

**B7 · `hair_color_2` · Second hair color · color(same set) → swatch · O · — · img:P2 · mem:— · record**

**B8 · `hair_color_pattern` · Color pattern · single(6) → chips · C: B7 set · img:P2 · mem:— · record**
gradient, streaks, tips, split half-and-half, inner layer, roots showing

**B9 · `hair_length` · Length · single(8) → chips · O* (default mapped from B10) · — · img:P1 · mem:inject · record**
bald, buzzed, short, chin-length, shoulder-length, mid-back, waist-length, floor-length
*(NEW group — length extracted from the old style list; `hair_style` keeps its required id and becomes pure shape. Data-only edit.)*

**B10 · `hair_style` · Style · single(~25) → picker · R (existing id) · — · img:P1 · mem:inject · record**
loose straight, wavy, curly, coily, messy, slicked back, high ponytail, low ponytail, side ponytail, twin tails, single braid, twin braids, crown braid, bun, double buns, half-up, hime cut, bob, pixie, undercut, side shave, mohawk, dreadlocks, afro, wolf cut

**B11 · `bangs` · Bangs · single(7) → chips · O · — · img:P2 · mem:— · record**
none, blunt, side-swept, curtain, wispy, choppy, hime side-locks

**B12 · `facial_hair` · Facial hair · single(9) → chips · O (default none, available to every character) · — · img:P2 · mem:— · record**
none, stubble, mustache, goatee, van dyke, short beard, full beard, braided beard, mutton chops

### B-iv · Eyes & Face

**B13 · `eye_color` · Eye color · color(17) → swatch · R (existing id) · — · img:P1 · mem:inject · record**
brown, dark brown, hazel, amber, green, emerald, blue, ice blue, grey, violet, red, crimson, gold, yellow, pink, black, pupil-less white

**B14 · `eye_color_2` · Heterochromia · color(same set) → swatch · O · — · img:P2 · mem:seed · record** — a second color *is* the heterochromia flag.

**B15 · `eye_shape` · Eye shape · single(6) → chips · O · — · img:P2 · mem:— · record**
almond, round, upturned, downturned, narrow, wide

**B16 · `eye_features` · Eye features · multi(7, cap 3) → chips · O · — · img:P2 · mem:seed · record**
glowing eyes, slit pupils, no pupils, ringed iris, long lashes, dark circles, heavy-lidded

**B17 · `face_shape` · Face shape · single(7) → chips · O · — · img:P3 (weak honor — §16) · mem:— · record**
oval, round, heart, square, angular, soft, long

**B18 · `lips` · Lips · single(4) → segmented · O · — · img:P3 · mem:— · record**
thin, natural, full, very full

**B19 · `nose` · Nose · single(6) → chips · O · — · img:P3 (weak honor) · mem:— · record**
small, button, straight, aquiline, broad, upturned

**B20 · `eyebrows` · Eyebrows · single(5) → segmented · O · — · img:P3 · mem:— · record**
thin, natural, thick, arched, straight

**B21 · `makeup` · Makeup · single(8) → chips · O · — · img:P2 · mem:— · record**
none, natural, bold lips, smoky eyes, full glam, gothic, festival paint, war paint

### B-v · Species features (conditional on A6 `class` — flag 5)

**B22 · `ears` · Ears · single(14) → picker · O (default by species class) · — · img:P1 (non-human identity) · mem:inject · record**
human, pointed short, pointed long, feline, canine, fox, rabbit (upright), rabbit (lop), deer, round bear, mouse, bat, fin, mechanical

**B23 · `horns` · Horns · single(11) → chips · O · — · img:P1 non-human / P2 else · mem:inject · record**
none, small nubs, curved back, forward curve, ram curl, antlers, single oni horn, spiral pair, demon crown, broken horn, crystal

**B24 · `tail` · Tail · single(16) → picker · O · — · img:P1 non-human · mem:inject · record**
none, feline, fox, kitsune (two-tail), kitsune (nine-tail), canine, fluffy wolf, rabbit puff, deer, cow, lizard, spiked dragon, spade demon, fish, mechanical, ghost wisp

**B25 · `wings` · Wings · single(12) → chips · O · — · img:P1 non-human · mem:inject · record**
none, small feathered, large feathered, black feathered, bat, dragon, butterfly, dragonfly, fairy, mechanical, light/energy, tattered

**B26 · `fur_coverage` / **B27** `fur_color` (swatch) / **B28** `fur_pattern` · C: class beastfolk-mammal · img:P1–P2 · mem:inject(one-liner)**
coverage: accents only (ears/tail/paws), partial, full · pattern: solid, striped, spotted, calico, brindle, masked, gradient, tipped

**B29 · `scale_coverage` / **B30** `scale_color` (swatch) / **B31** `scale_sheen` · C: class reptilian/draconic/aquatic · img:P1–P2 · mem:inject(one-liner)**
coverage: accents, partial, full · sheen: matte, glossy, iridescent

**B32 · `feather_coverage` / **B33** `feather_color` (swatch) · C: class avian/harpy · img:P1–P2 · mem:inject(one-liner)**
coverage: accents, partial, full

**B34 · `chassis_finish` · Finish · single(9) → chips · C: class construct · img:P1 · mem:inject**
matte white, gloss white, matte black, gunmetal, brushed steel, chrome, ceramic, carbon weave, brass clockwork

**B35 · `chassis_seams` · Seams · single(4) → segmented · C: construct · img:P2 · mem:seed**
seamless synth-skin, visible joint lines, segmented plates, exposed actuators

**B36 · `faceplate` · Face · single(5) → segmented · C: construct · img:P1 · mem:inject**
fully human, synth-skin with seams, visor, screen face, sculpted mask

**B37 · `ethereal_opacity` / **B38** `glow_color` (swatch) · C: class spirit/incorporeal-undead/celestial/elemental · img:P1–P2 · mem:inject**
solid, faint shimmer, translucent, mostly transparent

**B39 · `lower_body` · Lower body · single(9) → chips · C: class monstrous (default fixed by species) · img:P0–P1 (body plan is identity) · mem:inject**
serpent coil, fish tail, spider abdomen, quadruped (horse), quadruped (deer), quadruped (big cat), quadruped (goat), slime mass, bird legs

**B40 · `elemental_marks` · Manifestation · multi(8, cap 2) → chips · C: class elemental/cosmic · img:P2 · mem:inject**
hair of the element, glowing veins, aura, crackling limbs, molten cracks, frost rime, blooming flowers, starfield skin

**B41 · `undead_state` · State · single(6) → chips · C: class undead · img:P1 · mem:inject**
pristine, pale, gaunt, stitched, partially skeletal, spectral

**B42 · `other_features` · Other · multi(5, cap 2) → chips · O · — · img:P2 · mem:seed**
halo, broken halo, third eye, gills, antennae

### B-vi · Marks & modifications

**B43 · `marks` · Distinctive marks · multi(18, cap 4) → picker · O · — · img:P2 · mem:seed · record**
freckles, beauty mark, mole under eye, dimples, vitiligo, birthmark, facial scar, scar across eye, torso scar, arm scars, burn scar, ritual scarring, stitches, glowing runes, subdermal cyber lines, serial barcode, visible fangs, gold tooth

**B44 · `tattoo_placement` · Tattoos · multi(9, cap 3) → chips · O · — · img:P2 · mem:seed · record**
none, left sleeve, right sleeve, both sleeves, back piece, chest, neck, face, hands, thigh

**B45 · `tattoo_motif` · Motif · single(10) → chips · C: B44 ≠ none · img:P2 · mem:seed · record**
tribal, floral, runic, geometric, irezumi, circuitry, script, minimalist line, gothic, nautical

**B46 · `piercings` · Piercings · multi(11, cap 4) → chips · O · — · img:P3 · mem:— · record**
ear studs, multiple ear rings, industrial, eyebrow, nose stud, septum, lip, labret, tongue, navel, dermal
*(Gated placements live in the gated file — flag 7.)*

### B-vii · Anatomy (§12 region grouping; intimate entries in gated files)

**B47 · `chest_size` · Chest · single(7) → chips · O · — · img:P2 (clothed-visible) · mem:gated · record**
flat, small, modest, medium, large, very large, huge

**B48 · `chest_shape` · Shape · single(6) → chips · C: B47 > flat, gate open · img:gated · mem:gated · record (gated file)**
natural, round, teardrop, perky, athletic, heavy

**B49 · `waist` · Waist · single(4) → segmented · O · — · img:P2 · mem:— · record**
narrow, average, thick, soft

**B50 · `hips` · Hips · single(4) → segmented · O (existing) · — · img:P2 · mem:— · record**
narrow, average, wide, very wide

**B51 · `rear` · Rear · single(5) → segmented · O · — · img:P3 / img:scene · mem:gated · record**
small, average, full, large, heavy

**B52 · `genitalia` · Configuration · single(5) → segmented · O (default unspecified) · gate open to edit beyond default · img:gated scenes only · mem:gated · record (gated file)**
unspecified, vulva, penis, both, none/featureless

**B53 · `genitalia_size` · Size · single(4) → segmented · C: B52 ∈ {penis, both}, gate open · img:gated · mem:gated · record (gated file)**
small, average, large, very large

**B54 · `grooming` · Grooming · single(4) → segmented · C: gate open · img:gated · mem:gated · record (gated file)**
bare, trimmed, natural, styled

**B55 · `body_hair` · Body hair · single(4) → segmented · O (existing) · — · img:P3 · mem:— · record**
smooth, light, moderate, heavy

### B-viii · Wardrobe & aesthetic

**B56 · `wardrobe` · Default outfit · single(~79) → picker · O* (default: casual streetwear; existing id) · — · img:P2 · mem:inject(one-liner) · record**
- *Everyday modern:* casual streetwear, hoodie & jeans, t-shirt & shorts, leggings & oversized hoodie, crop top & jeans, denim jacket casual, leather jacket & jeans, skater style, tracksuit, knitwear & coat, turtleneck & slacks, sundress, maxi dress, blouse & skirt, athletic wear, gym wear, beach casual, winter layers, hiking gear, pajamas, loungewear, bathrobe
- *Work modern:* business casual, blazer & slacks, office suit, suit without tie, pencil skirt & blouse, retail uniform, fast-food uniform, barista apron, chef whites & apron, diner uniform & apron, bartender vest, lab coat, medical scrubs, mechanic coveralls, hi-vis workwear, police uniform, military uniform, dress uniform, tactical gear, flight suit
- *Formal & date:* formal suit, cocktail dress, date-night smart, clubwear, evening gown, ball gown, noble finery, royal regalia, victorian dress, victorian suit
- *Fantasy & historical:* plate armor, leather armor, chainmail & tabard, battle-mage garb, mage robes, priest vestments, witch attire, shrine garb, kimono, yukata, hanfu-style robes, desert robes, northern furs, medieval peasant garb, tribal wraps, cowboy western wear
- *Subculture & styled:* gothic dress, gothic punk, punk leather, biker leathers, grunge flannel, cottagecore dress, bohemian layers, steampunk attire, cyberpunk techwear, neon streetwear, idol stage outfit, dancer costume, maid uniform, butler suit, spacer jumpsuit
- *Swim:* one-piece swimsuit, bikini
- *Gated file (`wardrobe_intimate` — flag 7):* lingerie, boudoir set, towel only, nude

**B57 · `outfit_palette` · Palette · color(multi, cap 2) → swatch · O · — · img:P2 · mem:— · record**

**B58 · `outfit_fit` · Fit · single(5) → segmented · O · — · img:P3 · mem:— · record**
loose, relaxed, fitted, form-fitting, skin-tight

**B59 · `outfit_condition` · Condition · single(5) → segmented · O · — · img:P3 · mem:— · record**
pristine, lived-in, worn, patched, tattered

**B60 · `neckline` · Coverage · single(5) → segmented · O · — · img:P3 · mem:— · record**
modest, standard, open collar, low-cut, revealing

**B61 · `accessories` · Accessories · multi(~61, cap 5; composer sends top 3 to image) → picker · O · — · img:P2 (first 3) · mem:seed · record**
round glasses, square glasses, half-rim glasses, sunglasses, monocle, eyepatch, choker, ribbon choker, pendant necklace, chain necklace, prayer beads, hoop earrings, stud earrings, ear cuffs, hairpin ornament, hair ribbon, flower in hair, hair beads, crown, circlet, tiara, beanie, cap, wide-brim hat, witch hat, cowboy hat, top hat, hood, veil, half mask, medical mask, fox mask, oni mask, scarf, bandana, long gloves, fingerless gloves, gauntlets, bracelets, watch, smartwatch, wireless earbuds, headphones, work lanyard, tote bag, crossbody bag, gym bag, belt pouches, satchel, backpack, holster, sheathed sword, sheathed dagger, carried staff, hip spellbook, belt wrench, tool belt, tail ring, ankle bracelet, umbrella, parasol

**B62 · `aesthetic` · Aesthetic · multi(24, cap 2) → picker · O (existing) · — · img:P3 (style tail) · mem:seed · record**
elegant, regal, ethereal, gothic, gothic punk, punk, grunge, cyberpunk neon, chrome futurist, vaporwave, pastel soft, cottagecore, bohemian, rustic, minimalist, military crisp, noir, baroque, tribal, industrial, sun-bleached, gloomy romantic, festival, scholarly tweed

**B63 · `signature_note` · Signature visual note · free text, filtered · O · — · img:P3 tail · mem:seed · record**

---

## 5. SUBSET C — MIND & VOICE  *(render:false — Stage-6 consumer; `Mem` provisional per flag 1)*

### C-i · Temperament

Five ordinal axes, each `single(5) → segmented`, all `O` with middle default, `img:—`, `mem:inject` (compressed to one persona-card line), `home:record`. Segmented ordinals, not sliders — flag 3 stands here too.

**C1 · `warmth`** — icy, cool, even, warm, radiant
**C2 · `energy`** — still, calm, steady, lively, exuberant
**C3 · `assertiveness`** — yielding, accommodating, balanced, assertive, domineering
**C4 · `candor`** — guarded, private, selective, open, unfiltered
**C5 · `impulse`** — deliberate, careful, balanced, spontaneous, impulsive

**C6 · `default_mood` · Resting mood · single(9) → chips · O · — · img:— · mem:inject · record**
sunny, content, neutral, wistful, gloomy, irritable, anxious, serene, brooding

### C-ii · Character

**C7 · `traits` · Traits · multi(~70, cap 6) → picker · O · — · img:— · mem:inject · record**
confident, shy, witty, dry-humored, playful, serious, loyal, fickle, ambitious, content, curious, protective, reckless, cautious, cynical, idealistic, romantic, pragmatic, adventurous, homebody, bookish, streetwise, flirtatious, brooding, cheerful, optimistic, pessimistic, anxious, serene, hot-tempered, patient, jealous, easygoing, forgiving, grudge-holding, honorable, sly, blunt, tactful, superstitious, skeptical, devout, irreverent, hedonistic, disciplined, greedy, generous, vain, humble, paranoid, trusting, melancholic, stoic, dramatic, aloof, clingy, independent, nurturing, ruthless, squeamish, fearless, timid, obsessive, laid-back, perfectionist, scatterbrained, cunning, naive, worldly, morbid, gallant

**C8 · `flaws` · Flaws · multi(28, cap 3) → picker · O · — · img:— · mem:inject · record**
quick temper, self-doubt, arrogance, recklessness, freezes under pressure, people-pleasing, workaholism, jealousy, possessiveness, small habitual lies, rigid oath-keeping, survivor's guilt, haunted by the past, fear of abandonment, fear of intimacy, commitment aversion, stubbornness, spitefulness, gullibility, indecision, perfection paralysis, self-sacrificing, martyr streak, vanity, greed, blunt to a fault, secret-keeping reflex, trust issues

**C9 · `quirks` · Quirks · multi(~36, cap 4) → picker · O · — · img:— · mem:seed · record**
hums while working, talks to self, terrible puns, quotes proverbs, collects trinkets, cracks knuckles, fidgets with hair, taps rhythms, doodles everywhere, always cold, always hungry, never swears, swears like a sailor, speaks to animals, superstitious rituals, insomniac, early riser, chronic napper, over-apologizes, laughs at own jokes, deadpan delivery, dramatic sighs, counts things, straightens crooked objects, hoards snacks, names inanimate objects, forgets names instantly, never forgets a face, whistles, chews pens, stretches constantly, feet up on furniture, sings when alone, bad with directions, licks thumb to turn pages, carries snacks for strays

**C10 · `vices` · Vices · multi(15, cap 3) → picker · O · — · img:— · mem:seed · record**
smoking, vaping, drinking, gambling, gossip, overspending, sweet tooth, caffeine dependence, oversleeping, brawling, doomscrolling, binge-watching, phone always in hand, vanity spending, petty shoplifting

**C11 · `values` · Values · multi(22, cap 3) → picker · O · — · img:— · mem:inject · record**
honor, freedom, family, knowledge, power, wealth, faith, love, loyalty, justice, mercy, beauty, craft mastery, fame, comfort, duty, nature, progress, tradition, survival, whimsy, truth

**C12 · `moral_compass` · Compass · single(5) → segmented · O · — · img:— · mem:inject · record**
principled, honorable-pragmatic, flexible, opportunistic, unscrupulous

**C13 · `fears` · Fears · multi(22, cap 3) → picker · O · — · img:— · mem:seed · record**
heights, deep water, fire, enclosed spaces, crowds, the dark, storms, insects, blood, needles, ghosts, abandonment, being forgotten, failure, commitment, their own power, the open sea, thunder, silence, being seen crying, hospitals, going home

**C14 · `near_goal` · Current goal · single(30) → picker · O · — · img:— · mem:inject · record**
pay off a debt, pay off the student loans, make rent this month, keep the job, get the promotion, pass the licensing exam, get their own place, save for a car, launch the business, find a missing person, win a competition, master a technique, open a shop, finish the great work, clear their name, find a way home, repair a relationship, protect someone, earn a rank, survive the season, court someone, leave town, settle down, uncover a truth, break a curse, buy back the family land, settle a small revenge, make a friend, be normal for once, save enough to travel

**C15 · `life_dream` · Life dream · single(20) → picker · O · — · img:— · mem:inject · record**
a place to belong, redemption, greatness, a quiet ordinary life, seeing the whole world, going home, true love, a family of their own, mastery of the craft, wealth beyond counting, freedom from their maker, becoming human, immortality, a worthy death, peace for their people, rebuilding what was lost, giving forgiveness, receiving forgiveness, forbidden knowledge, to matter to someone

**C16 · `lines_never_cross` · Never · multi(16, cap 3) → picker · O · — · img:— · mem:inject (stable behavioral anchors — drift-tripwire-relevant) · record**
betray a friend, abandon family, harm the innocent, break a sworn oath, lie to a loved one, steal from the poor, kill at all, kill the defenseless, forsake their faith, sell the craft's secrets, leave a debt unpaid, torture, work for the old enemy, give up on someone, break hospitality, abandon a post

### C-iii · Mind & skill

**C17 · `intellect_style` · Mind · single(8) → chips · O · — · img:— · mem:inject · record**
analytical, intuitive, scholarly, street-smart, cunning, absent-minded brilliant, simple and earnest, slow but deep

**C18 · `skills` · Skills · multi(~70, cap 6) → picker · O · — · img:— · mem:seed · record**
cooking, baking, brewing, bartending, mixology, first aid, medicine, herbalism, alchemy, swordplay, archery, marksmanship, hand-to-hand, tactics, stealth, lockpicking, pickpocketing, tracking, survival, hunting, fishing, sailing, riding, piloting, driving, mechanics, engineering, tinkering, home repair, smithing, tailoring, carpentry, hacking, coding, video editing, social media, electronics, chemistry, mathematics, history, languages, calligraphy, painting, sculpting, photography, makeup artistry, string instruments, keyboard instruments, wind instruments, singing, dancing, acting, storytelling, public speaking, persuasion, haggling, seduction, gambling, sleight of hand, teaching, animal handling, farming, gardening, navigation, cartography, elemental magic, healing magic, illusion magic, summoning magic, rune magic, accounting, law

**C19 · `signature_skill` · Best at · single (from the C18 selection) → chips · C: C18 non-empty · img:— · mem:inject (the one skill worth a persona-card line) · record**

### C-iv · Speech & voice

**C20 · `voice_timbre` · Timbre · single(20) → picker · O · — · img:— · mem:inject · record**
soft, gentle, bright, clear, warm, husky, sultry, deep, resonant, gravelly, raspy, melodic, lilting, monotone, sharp, breathy, booming, squeaky, synthesized, echoing

**C21 · `speech_pace` · Pace · single(5) → segmented · O · — · img:— · mem:inject · record**
laconic, measured, even, quick, rapid-fire

**C22 · `speech_register` · Register · single(6) → chips · O · — · img:— · mem:inject · record**
archaic-formal, formal, polite, casual, rough, crude

**C23 · `speech_patterns` · Patterns · multi(26, cap 3) → picker · O · — · img:— · mem:inject · record**
never uses contractions, uses honorifics for everyone, gives everyone nicknames, trails off mid-thought, answers questions with questions, thinks aloud, over-explains, one-word answers, poetic metaphors, dry sarcasm, gentle teasing, constant proverbs, technical jargon, modern slang, old-fashioned slang, mild oaths, heavy profanity, theatrical declarations, whispers when serious, laughs mid-sentence, self-deprecating asides, refers to self in third person, royal we, counts points on fingers, apologizes reflexively, switches languages when flustered

**C24 · `verbal_tic` · Tic · single(14) → picker · O · — · img:— · mem:inject · record**
"ya know", "like", "right then", "as it were", "I suppose", "hm?", "eh?", "…probably", "so it goes", "mark my words", "bless it", "tch", trailing "…yeah", "nya"

**C25 · `catchphrase` · Catchphrase · free text, one line, filtered (flag 10) · O · — · img:— · mem:inject · record**

**C26 · `accent_flavor` · Accent · single(14) → picker · O · — · img:— · mem:inject · record**
neutral, posh, rustic, clipped, lilting, brogue, drawl, sing-song, foreign-formal, old-world, dockside rough, courtly, backwater, synthetic-clipped

**C27 · `laugh` · Laugh · single(8) → chips · O · — · img:— · mem:seed · record**
soft giggle, snort-laugh, booming, silent shoulder-shake, wheeze, musical, rare and quiet, cackle

### C-v · Emotional profile

**C28 · `expressiveness` · Face card · single(5) → segmented · O · — · img:— · mem:inject · record**
stone-faced, reserved, readable, expressive, wears everything

**C29 · `temper_fuse` · Fuse · single(5) → segmented · O · — · img:— · mem:inject · record**
unshakeable, long fuse, average, short fuse, hair trigger

**C30 · `affection_style` · Shows care by · multi(10, cap 2) → chips · O · — · img:— · mem:inject · record** *(written general — how they treat anyone they care for, per flag 4)*
words of praise, teasing, acts of service, small gifts, protective hovering, physical closeness, cooking for them, quality time, quiet presence, loyalty shown publicly

**C31 · `comfort_ritual` · Comfort ritual · single(14) → picker · O · — · img:— · mem:seed · record**
brewing tea, black coffee at dawn, whetstone and blade care, prayer beads, rooftop stargazing, long baths, tending plants, polishing the chassis, journaling, an old photo, humming an old song, cleaning the workspace, feeding strays, watching the rain

---

## 6. SUBSET D — LIFE & BONDS  *(render:false — Stage-6 consumer; `Mem` provisional)*

### D-i · Setting & roots  *(one setting group; other-world origin is one option, not a subsystem)*

**D1 · `setting` · Setting · single(31) → picker · O* (default: modern day) · — · img:— (genre reaches the image only through wardrobe and scene, per the §4 one-style rule) · mem:inject · record**
modern day, modern urban fantasy, near-future city, cyberpunk megacity, high fantasy kingdom, dark fantasy realm, fairy-tale land, mythic ancient world, feudal eastern realm, desert empire, frozen north, island nations and sail, gaslamp victorian, wild frontier, noir city, wartime era, post-apocalyptic wasteland, space colony, deep-space station, starship fleet, far-future utopia, far-future decline, solarpunk commune, steampunk skyways, eldritch-haunted coast, superhero metropolis, spirit realm, the fae wilds, a pocket dimension, virtual world, unknown

**D2 · `roots` · From · single(9) → chips · O (default: born and raised here) · — · img:— · mem:seed · record**
born and raised here, from a small town, from the big city, from the countryside, from another country, from another world, from the future, from the past, nobody knows
*(This is the whole isekai mechanism now: one chip, plus D13 entries — "crossed over from another world", "died in another world" — when it deserves backstory weight.)*

**D3 · `locale` · Locale · single(24) → picker · O · — · img:— · mem:inject · record**
megacity core, downtown, quiet suburb, college town, strip-mall sprawl, small town, village, farmstead, frontier outpost, port district, slums, luxury district, university quarter, industrial zone, underground city, mountain hold, forest settlement, desert oasis, coastal cliffs, floating isle, space-station ring, starship berth, nomad caravan, hidden enclave

### D-ii · Standing & work

**D4 · `social_standing` · Standing · single(7) → chips · O · — · img:— · mem:inject · record**
outcast, underclass, working class, middle class, well-off, elite, nobility

**D5 · `reputation` · Reputation · single(6) → chips · O · — · img:— · mem:seed · record**
unknown, local fixture, well-known locally, infamous, famous, legendary

**D6 · `legal_status` · With the law · single(7) → chips · O · — · img:— · mem:seed · record**
clean, minor record, watched, wanted, fugitive, pardoned, above the law

**D7 · `occupation` · Occupation · single(~125) → picker · O · — · img:— (uniform reaches the image via B56 wardrobe) · mem:inject · record**
- *Modern & everyday:* office worker, retail clerk, cashier, rideshare driver, truck driver, real-estate agent, personal trainer, hairstylist, barber, nail tech, flight attendant, hotel front desk, EMT, firefighter, social worker, therapist, lawyer, paralegal, IT support, sysadmin, game developer, graphic designer, streamer, warehouse worker, construction worker, plumber, landscaper, veterinarian, dental hygienist, bank teller, DJ, postal worker
- *Food & service:* line cook, chef, baker, barista, bartender, waiter, innkeeper, street vendor, food-truck owner, diner cook
- *Trades & labor:* blacksmith, carpenter, mechanic, electrician, miner, farmer, rancher, fisher, dockworker, courier, delivery rider, tailor, jeweler, mason, shipwright
- *Combat & security:* soldier, guard, mercenary, bodyguard, bouncer, beast hunter, monster hunter, knight, duelist, gladiator, police detective, private eye, ranger
- *Arcane & faith:* court mage, hedge witch, alchemist, enchanter, priest, shrine keeper, exorcist, oracle, ritualist, healer
- *Tech & science:* engineer, roboticist, programmer, hacker-for-hire, lab researcher, field scientist, medic, doctor, nurse, pharmacist, pilot, starship engineer, drone operator, netrunner
- *Knowledge & art:* scholar, librarian, archivist, teacher, professor, scribe, journalist, novelist, poet, painter, sculptor, musician, street performer, idol, actor, dancer, photographer, tattoo artist
- *Commerce & rule:* merchant, shopkeeper, trader, banker, accountant, clerk, bureaucrat, diplomat, estate administrator, guildmaster, union rep, political aide
- *Shadow:* smuggler, thief, fence, information broker, con artist, retired assassin, spy, bounty hunter, black-market dealer
- *Other:* adventurer, explorer, caravan guide, sailor, gravekeeper, lighthouse keeper, park ranger, beast-keeper, stablehand, gardener, housekeeper, butler, unemployed, between jobs, university student, retired, of independent means, drifter

**D8 · `workplace` · Workplace · single(38) → picker · O · — · img:— · mem:seed + scene · record**
office floor, startup loft, retail floor, shopping mall, chain coffee shop, corner café, greasy diner, fast-food kitchen, upscale restaurant, tavern, gym, salon, warehouse, construction site, hospital ward, clinic, school, streaming studio, nightclub, hotel lobby, guild hall, castle court, temple, academy, library, workshop, garage, forge, farm, docks, market stall, corporate tower, precinct, starship crew, station deck, back alleys, laboratory, no fixed workplace

**D9 · `job_feeling` · Feels about it · single(5) → segmented · O · — · img:— · mem:inject · record**
hates it, tolerates it, content, proud of it, it's a cover

### D-iii · Backstory

**D10 · `origin_story` · Raised · single(20) → picker · O · — · img:— · mem:seed (high salience) · record**
loving family, poor but warm, strict household, noble upbringing, temple-raised, apprenticed young, military family, merchant family on the road, orphaned young, raised by a grandparent, raised by a stranger, street kid, wild-raised, sheltered and secluded, raised in a lab, manufactured, summoned into being, raised among another species, youngest of many, only child

**D11 · `family_now` · Family now · single(8) → chips · O · — · img:— · mem:inject · record**
close-knit, loving at a distance, strained, estranged, complicated, gone, unknown, found family instead

**D12 · `siblings` · Siblings · single(5) → segmented · O · — · img:— · mem:seed · record**
none, one, a few, many, unknown

**D13 · `defining_events` · Defining events · multi(~52, cap 4) → picker · O · — · img:— · mem:seed (salience-weighted; the primary §9 RAG seed) · record**
lost someone dear, survived a disaster, survived a war, betrayed by a friend, betrayed someone, escaped captivity, failed a great task, achieved fame young, lost fame, exiled, discovered hidden lineage, made a forbidden bargain, cursed, blessed and chosen, lost their memory, faked their own death, great heartbreak, raised a sibling alone, lost the family fortune, won and lost a fortune, committed a crime, wrongly accused, saved by a stranger, saved a stranger, died in another world, crossed over from another world, first love lost, mentor died, broke an oath, kept an impossible promise, power awakened late, gained feelings, decommissioned and reactivated, abandoned by their maker, sole survivor, plague survivor, tournament champion, deserted an army, freed from servitude, ended a feud, started a feud, walked away from a throne, lived another whole life, crossed the sea alone, watched the old world end, laid off, went viral once, dropped out, a divorce, got sober, moved back home, a bad car accident

**D14 · `secrets` · Secrets · multi(18, cap 3) → picker · O · — · img:— · mem:guard (retrievable, resists disclosure until earned — 6d design target) · record**
hidden identity, royal blood, on the run, double agent, a past crime, hidden power, forbidden love, failing health, not human and passing, owes a dangerous debt, knows a dangerous truth, memories are implanted, a prophecy names them, living under an alias, keeping a promise to the dead, a body buried, heir to an enemy house, the accident wasn't one

**D15 · `turning_point` · Why now · single(16) → picker · O · — · img:— · mem:inject · record**
fresh start in a new town, on the run, searching for someone, sent on assignment, recently exiled, just arrived from elsewhere, rebuilding after loss, retired from the old life, undercover, a debt came due, the curse has a deadline, an inheritance with strings, the shop just opened, last of their order, one last job, following a rumor home

### D-iv · Daily life & tastes

**D16 · `living_situation` · Lives · single(17) → picker · O · — · img:— · mem:seed · record**
studio apartment, shared flat, parents' basement, dorm room, family home, house they own, above the shop, workshop loft, rented room at the inn, barracks, ship bunk, station quarters, temple dormitory, grand estate, caravan, van life, no fixed home

**D17 · `finances` · Finances · single(6) → chips · O · — · img:— · mem:seed · record**
destitute, struggling, getting by, comfortable, wealthy, absurdly rich

**D18 · `companion` · Companion animal · single(15) → picker · O · — · img:— (compositing scope creep otherwise) · mem:seed · record** + **`companion_name` free text (flag 10) · C: D18 ≠ none**
none, cat, dog, raven, owl, snake, fox, rabbit, dragon whelp, spirit wisp, tiny golem, drone, rat, horse, a stray they feed

**D19 · `hobbies` · Hobbies · multi(~60, cap 5) → picker · O · — · img:— · mem:seed · record**
cooking for fun, baking, gardening, houseplants, stargazing, fishing, fishkeeping, hiking, camping, road trips, tinkering, model-building, painting, sketching, poetry, journaling, novels, history books, true-crime podcasts, anime and manga, chess, card games, board games, tabletop RPGs, video games, cosplay, birdwatching, people-watching, coin collecting, curio collecting, figurine collecting, sneaker collecting, thrifting, pressed flowers, calligraphy, social dancing, martial-arts practice, weight training, running, swimming, cycling, yoga, rock climbing, skateboarding, surfing, singing, an instrument, karaoke, live concerts, festivals, tea ceremony, wine tasting, motorcycles, photography, astronomy, home brewing, foraging, embroidery, whittling, language learning, volunteering

**D20 · `fav_food` · Favorite food · single(36) → picker · O · — · img:— · mem:seed · record**
rich stew, fresh bread, spicy noodles, ramen, curry, dumplings, grilled fish, sushi, barbecue, anything fried, fried chicken, pizza, smash burgers, tacos, hot wings, mac and cheese, instant noodles, diner pancakes, strong coffee, bubble tea, sweet pastries, chocolate, ice cream, fresh fruit, cheap street skewers, honey tea, black tea, herbal tonics, aged wine, cold beer, whiskey neat, energy drinks, meat pies, pickled things, machine oil (acquired taste)

**D21 · `disliked_food` · Won't eat · multi(14, cap 3) → picker · O · — · img:— · mem:seed · record**
anything bitter, anything sweet, seafood, spice, mushrooms, olives, organ meat, milk, crowded restaurants, cheap liquor, synthetic food, vegetables, surprises in food, food going to waste

**D22 · `music_taste` · Music · single(22) → picker · O · — · img:— · mem:seed · record**
quiet preferred, folk ballads, tavern songs, classical strings, choral hymns, jazz, blues, rock, metal, punk, indie, electronic, synthwave, lo-fi, hip-hop, r&b, country, k-pop, pop idols, street drums, sea shanties, lullabies

**D23 · `pet_peeves` · Pet peeves · multi(20, cap 3) → picker · O · — · img:— · mem:seed · record**
loud chewing, tardiness, liars, being ignored, condescension, mess, small talk, unsolicited touching, interrupting, bad tippers, wasted food, dull blades, sloppy work, whining, being pitied, nicknames from strangers, slow walkers, phone at the dinner table, spoilers, speakerphone in public

### D-v · Social dispositions  *(flag 4 — character-intrinsic. The record defines how this character treats kinds of people; the specific user relationship is instantiated per chat by the §13 scenario builder. Nothing here names the user.)*

**D24 · `with_strangers` · First impression · single(9) → chips · O · — · img:— · mem:inject · record**
cold, wary, aloof, politely distant, courteous, friendly, instantly warm, gruff but kind, flirts with everyone

**D25 · `warming_pace` · Opens up · single(5) → segmented · O · — · img:— · mem:inject · record**
never fully, glacially, slowly, steadily, quickly

**D26 · `with_friends` · Once close · single(10) → chips · O · — · img:— · mem:inject · record**
fiercely devoted, easy and teasing, mother-hens them, brutally honest, ride-or-die quiet, playfully cruel, endlessly generous, protective, leans on them, keeps some walls up

**D27 · `toward_authority` · Authority · single(6) → chips · O · — · img:— · mem:inject · record**
deferent, respectful, indifferent, chafing, openly defiant, becomes the authority

**D28 · `in_conflict` · Conflict · single(7) → chips · O · — · img:— · mem:inject · record**
avoids it, de-escalates, goes cold and silent, sharp words, meets it head-on, explodes then apologizes, holds the grudge quietly

**D29 · `trust` · Trust is · single(5) → segmented · O · — · img:— · mem:inject · record**
never fully given, earned slowly, earned by deeds, given until broken, given freely

**D30 · `when_interested` · When interested in someone · single(8) → chips · O · — · img:— · mem:inject (escalation beyond disposition stays governed by CONTENT_POLICY + the builder consent set at runtime) · record**
oblivious to their own feelings, denies it, quiet pining, shy signals, warmer teasing, direct about it, bold pursuit, flirts but never follows through

**D31 · `attachment_behavior` · Attachment · single(5) → segmented · O · — · img:— · mem:inject · record**
steady, slow to open, hot and cold, clingy, fiercely independent

**D32 · `jealousy` · Jealousy · single(5) → segmented · O · — · img:— · mem:inject (upper bands are drift-tripwire-relevant context, not exempt from it) · record**
none, mild, noticeable, strong, consuming

**D33 · `address_habits` · Addresses people · single(8) → chips · O · — · img:— · mem:inject · record**
formal titles for everyone, surnames, first names immediately, nicknames everyone, pet names for anyone they like, rank or role, avoids names, mirrors how they're addressed

**D34 · `avoided_topics` · Deflects on · multi(14, cap 3) → picker · O · — · img:— · mem:inject (pairs with D14 guard behavior — deflection is intrinsic, whoever asks) · record**
their past, their family, the war, the scar, the old name, their maker, the person they lost, why they left, their debt, what they did, the prophecy, their people's customs, money, their feelings

### D-vi · Scenario-builder handoff  *(home:builder — the per-chat layer, enumerated for coverage. At chat start: character record (standing dispositions) + these (current state) = the instantiated dynamic. This is where the user's self-defined role lives.)*

- **`user_persona`** · builder record: name (free text), role (the relationship below, seen from the user's side), one-line self-description (free text, filtered) · mem:scene
- **`relationship`** · single → picker: strangers, new acquaintances, neighbors, regular customer, coworkers, fellow students, old friends, best friends, childhood friends reunited, rivals, friendly rivals, character mentors the user, user mentors the character, character employs the user, user employs the character, character is the user's bodyguard, client and professional, roommates, amicable exes, messy exes, courting, engaged, married, it's complicated · mem:scene → inject at instantiation
- **`how_met`** · single → picker · when relationship ≠ strangers: grew up together, met at work, met at school, through a friend, they rescued the user, the user rescued them, a bad first impression, a bar fight, online first, arranged by families, hired on, a chance downpour, seated together on a long trip, a misdelivered package, a summoning, a duel · mem:seed
- **`meeting_scene`** · single → picker · when relationship = strangers (fires at chat start): at their workplace, they save the user, the user saves them, wrong delivery, seated together in transit, both hiding from rain, the user summons them, found injured, new neighbor, hired for a job, mistaken identity, last table at the café, lost and asking directions, a marketplace haggle, caught mid-pickpocket, festival crowd · mem:scene
- **`scene_tone`** · single → chips: cozy slice-of-life, comedy, drama, mystery, adventure, action, light horror, slow-burn romance, forward romance, *gated:* mature *(mature scenes run under the approved consent set: enthusiastic, established_relationship, negotiated_scene, romantic)* · mem:scene
- **`time_weather`** · chips: dawn, midday, dusk, night, rain, snow, storm, festival day · mem:scene · also feeds scene-image generation (§13 background pipeline)
- **`event_seeds`** · picker: a letter arrives, an old face returns, the shop floods, a festival begins, a debt collector calls, a storm strands you together, a rival appears, an award ceremony, a theft, a confession interrupted · mem:scene

---

## 7. MIGRATION FROM THE CURRENT VOCABULARY

| Current | Disposition |
|---|---|
| `race` 13 options | same id, grows to ~86 grouped options + `class` metadata (flag 5) |
| `archetype` 10 | same id, grows to ~32; the day job splits out to D7 `occupation` |
| `gender_presentation` 3 | unchanged; A9/A10 add identity + pronouns beside it |
| `skin_tone` 10 | same id (B4), grows to 28 swatches |
| `hair_color` 11 | same id (B6), grows to 25; two-tone moves to B7/B8 |
| `hair_style` 9 (mixed length + shape) | same id (B10), becomes pure shape (25); length extracted to new B9 `hair_length` |
| `eye_color` 10 incl. heterochromia | same id (B13), 17 colors; heterochromia becomes the B14 second swatch |
| `body_type` 8 | same id (B1), 12 silhouettes; now also carries overall mass |
| height / weight / muscle sliders | **removed** — replaced by species-relative `height_band` (B2) + `muscle_def` (B3); §12 carve-out reopened (flag 3) |
| `appearance_notes` free text | survives as B63 `signature_note` (filtered, P3 tail) |
| chest / hips-rear / genitalia / body-hair / marks | restructured into B-vi + B-vii; intimate entries move to gated files (flag 7) |
| `core_disposition` 9 | replaced by C1–C5 axes + C6 mood |
| `personality_traits` 14 | C7, grows to ~70 with cap 6 |
| `voice` 6 | C20 timbre (20) + the C21–C27 speech block |
| `personality_notes` free text | **deleted** — replaced by C8–C16 enumerations |
| `wardrobe` 12 incl. lingerie/nude | same id (B56), ~75 ungated + 4 in the `wardrobe_intimate` gated file |
| `aesthetic` 7 | same id (B62), 24, cap 2 |
| `backstory` free text | **deleted** — replaced by D-i through D-iv enumerations |
| *(never shipped)* relationship-to-user concept | never enters the record — D-v dispositions + §13 builder instance (flag 4) |

---

## 8. TOTALS & BUDGET NOTE

~140 record groups (A11 + B63 + C31 + D34) plus 7 builder-catalog kinds; ~1,300 options. Quick path: 12 touches. A maximum-detail render-side selection lands ~45–60 fragments; post-5.5b chunking this encodes fully, but **P0+P1 must still fit the first 77-token window** (pooled embeds) — the tier column is the assembly-order contract that guarantees it. The persona-injection card (`mem:inject`, fully loaded) estimates 250–400 LLM tokens against the §8 context budget; this is the number 6d tuning re-cuts (flag 1). At chat time the LLM receives the record's social dispositions as **standing behavior** and the builder's relationship + user persona as **current state** — the pairing never hardens into the record.

---

## 9. OPEN ITEMS

1. `visible_when` + option `class` — the fifth §15 extension this design needs; route through a BUILD_PLAN delta before any data files are authored.
2. DECISIONS §12 amendment — one line striking the height/weight/muscle slider reservation (user edit; the document is frozen to everyone else).
3. Legacy records carrying `height`/`weight`/`muscle` keys — lenient load already covers them; decide whether the editor surfaces retired values read-only or drops them with a lint.
4. Whether five free-text slots (flag 10) is acceptable, or `catchphrase` should also become a picker.
5. Per-option Danbooru fragment authoring + the Layer-1 assembly test for every new render:true option — data-file work with the token panel, not covered here.
6. RAG seed composition — which Subset C/D groups seed one document each vs. composed documents (origin_story + defining_events as one "past" doc) — 6d design.
7. Weak-honor render fields (B17 face_shape, B19 nose, B2 extremes on non-standard frames) — keep at P3/P2 or cut after the first hardware render pass (§16 honesty).
8. Per-species display-height table — concrete lore numbers behind the B2 bands ("about chest-height on you"), chat-side only, a later data add.
9. Builder relationship/scenario vocabulary (D-vi) — full enumeration belongs to the §13 builder catalogs at Stage 6; the lists here are the seed.
