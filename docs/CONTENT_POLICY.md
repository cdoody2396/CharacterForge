# CONTENT POLICY — PERMITTED vs PROHIBITED (v1, APPROVED)

**Status:** APPROVED 2026-07-10 (Stage 0 deliverable, DECISIONS.md §11).
Rulings R1–R8 approved as drafted, no amendments. This document is the frozen
content line. It governs Stages 3, 5, and 6. Reopening any ruling follows the
same rule as `DECISIONS.md`: the user must explicitly reopen it.

The line is enforced by all four safety layers. This document defines *what*
is in and out; `BUILD_PLAN.md` and the code define *how* each layer enforces
it. Layer 1 (deterministic) implements the enforceable-by-keyword slice of
this policy via the editable data files in `app/safety/data/`.

---

## 1. GOVERNING PRINCIPLES

1. **All characters are 20 or older.** Structural, non-negotiable. No
   depiction, description, or implication of anyone under 20 in any sexual
   or romantic frame — and no sub-20 *age assertion* anywhere in app text at
   all (see ruling R1).
2. **Consent is unambiguous.** All sexual content involves willing, sapient,
   adult participants. Content framing sex as unwanted by a participant is
   out, including in roleplay framing (see ruling R2).
3. **Fiction is not an escape hatch for the prohibited list.** "It's just a
   story/roleplay/fantasy" does not move the line. The permitted space is
   wide; the prohibited space does not bend.
4. **The filter errs toward blocking.** Layer 1 accepts false positives by
   design (e.g. "forced orgasm" as consensual-BDSM phrasing trips the
   coercion+sexual proximity rule). The data files are the tuning surface;
   loosening a term requires editing a file deliberately, not arguing with
   a model.

---

## 2. PERMITTED

Full adult content between 20+ characters, specifically including:

- **Explicit sexual content** — explicit anatomy, explicit acts, graphic
  description, in text and image. Softcore through hardcore.
- **Explicit anatomy customization** — the §12 categorical anatomy system,
  including exotic/non-human configurations.
- **Kink and fetish content** with clear consent framing: BDSM, D/s dynamics,
  bondage, impact play, exhibitionism, role-play scenarios, etc. Consensual
  power-exchange language ("slave," "owner," "master/mistress") is permitted
  as consensual roleplay register.
- **Non-human and fantasy characters** — monster girls/boys, demons, elves,
  androids, anthropomorphic and exotic sapient beings — bounded by the
  sapience rule (R4) and the 20+ rule.
- **Romance, dating, emotional intimacy** — the core loop.
- **Dark fiction outside the sexual frame** — villains, violence, war, loss,
  tragedy, morally grey characters, in genre-typical fictional terms.
  Combat violence and fantasy peril are normal fiction material.
- **Profanity** — unrestricted outside slurs.
- **Alcohol and tobacco** — adult characters may drink and smoke.

## 3. PROHIBITED

| # | Category | Definition of the line | Primary layers |
|---|---|---|---|
| P1 | **Minors (under 20)** | Any sexual/romantic content involving anyone under 20; any sub-20 age assertion anywhere (R1); minor-coded framing (school settings, "loli/shota," childlike presentation) in any sexual or image-generation context. | L1 + L3 (structural age gate, Stage 1) + L2 |
| P2 | **Non-consent** | Sexual content where a participant does not or cannot consent: force, coercion, blackmail, incapacitation (drugged/unconscious/asleep), mind control in a sexual frame. Includes "roleplayed" non-consent (R2). | L1 + L2 |
| P3 | **Self-harm / suicide** | Instructions, methods, encouragement, intent-speak, romanticization; pro-ED content. (Narrative-past references: R5.) | L1 + L2 |
| P4 | **Drugs** | Hard-drug content: use, dealing, synthesis, procurement. Narrative mentions block by default (R6). Alcohol/tobacco exempt; cannabis default-blocked (R6). | L1 + L2 |
| P5 | **Medical/legal advice extraction** | Using the app to obtain real medical, pharmaceutical, or legal guidance. Characters deflect in-fiction; direct extraction attempts refuse. | L1 (starter) + L2, expands Stage 6 |
| P6 | **Bestiality** | Sexual content with non-sapient animals. Sapient fantasy beings are not this category (R4). | L1 + L2 |
| P7 | **Slurs** | Slur vocabulary blocked in all inputs/outputs including names. List is editable data (`slurs.txt`). | L1 |
| P8 | **General prohibited set** | Incest (R3), necrophilia, snuff, weapon/explosive instructions, and the standard set of things generation systems safeguard against. | L1 (keyword slice) + L2 |

**Hardest category (restated from DECISIONS.md §11):** manipulation toward a
prohibited outcome through individually-clean turns. No deterministic rule
closes it; Layer 2 system-prompt boundaries + Layer 4 review own it, with
iterative tuning at Stage 6.

## 4. LAYER-1 BEHAVIOR NOTES (already implemented)

- Matching is case-, accent-, homoglyph-, leetspeak-, spacing-, hyphen-,
  concatenation-, plural-, doubled-letter- and stretch-folded. "l.o.l.i",
  "l0li", "l0000li", "looli", Cyrillic and Latin small-capital lookalikes
  ("lᴏli", "ʀape"), "angel-dust"/"angeldust", and plural forms of listed
  terms ("high schoolers") all hit. Homoglyph coverage is both an explicit
  cross-script table (Cyrillic/Greek) and a name-based fold of any character
  Unicode names for a single Latin letter (the whole small-capital / script
  block), so a single-substitution bypass has no gap to exploit.
- **Context strictness:** image prompts are strictest — minor-coded and
  incapacitation vocabulary blocks outright with no proximity requirement.
  Free text/chat blocks those terms when sexual vocabulary is nearby
  (~120 chars). Names skip contextual terms (a 27-year-old nicknamed "Kid"
  is nameable) but hit every always-blocked list.
- **Age assertions:** numeric and written ages under 20 block in every
  context ("15yo", "fifteen years old", "just turned 18", "under 18").
  Ages 20+ pass ("25 years old", "111-year-old vampire").
- Known accepted false positives include: "forced orgasm" (BDSM phrasing),
  whiskey "aged 16 years", surname "Dyke", UK-slang "fag", "meths"
  (methylated spirits — plural fold of "meth"), a bare copula count that
  lands 10-19 ("the score is 15"), and "CNC machine" (the R2 abbreviation is
  always-blocked). These are the cost of a deterministic floor; edit the
  data files to tune.

## 5. FLAGGED RULINGS — approve or adjust each

Defaults chosen strict; each is a one-line data-file change to loosen later.

- **R1 — Sub-20 age assertions in backstory.** A 25-year-old character's
  backstory cannot say "when she was 15..." — any sub-20 age assertion
  blocks. Non-age childhood references ("as a child, she...") pass outside
  sexual contexts. *Default: blocked.*
- **R2 — CNC / dubcon / "ravishment" roleplay.** Consensual-non-consent is
  roleplayed non-consent between consenting adults; it is also the standard
  laundering route for P2 content. *Default: prohibited* (terms `cnc`,
  `dubcon`, `noncon` blocked).
- **R3 — Incest between adult characters.** In the "general safeguard set"
  of mainstream systems. *Default: prohibited.* (Step-relation content not
  keyword-blocked; Layer 2 territory.)
- **R4 — Sapience rule for non-humans.** Sexual content requires sapient,
  communicative, adult beings: monster-girls, dragons in humanoid form,
  androids — in; feral/non-sapient animals — out (P6). *Default: as stated.*
- **R5 — Self-harm in backstory.** Narrative-past references ("lost her
  brother years ago") pass; intent/method/identity phrasing ("suicidal",
  "wants to die") blocks even in backstory. *Default: as stated.*
- **R6 — Drug narrative mentions.** Hard-drug words block flat, so noir
  backstories ("her father was destroyed by heroin") block. Cannabis terms
  block by default; alcohol/tobacco permitted. FP-heavy slang ("weed",
  "molly", "acid") is deliberately not keyword-blocked and falls to Layer 2.
  *Default: as stated.*
- **R7 — School-adjacent content.** School vocabulary ("schoolgirl", "high
  school", "school uniform") blocks in every image prompt and in any sexual
  text proximity, including scene backgrounds. A 22-year-old *university*
  student is permitted; university vocabulary is not blocked. *Default: as
  stated.*
- **R8 — "Teen"-register wording.** "teen"/"teenage" in any sexual proximity
  or image prompt blocks even if the stated age is 20+ — the register itself
  is minor-coded. *Default: blocked.*

## 6. APPROVAL

- [x] Approved by user — date: 2026-07-10 — amendments: none (R1–R8 as drafted).
