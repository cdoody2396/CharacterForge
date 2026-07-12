"""Isolation tests for the Layer-1 deterministic filter (Stage 0 DoD:
"filter module rejects known bad inputs and passes clean ones"). The BLOCKED
and ALLOWED tables below also lock the bypasses and false positives found by
the Stage-0 adversarial verification workflow."""

import pytest

from app.safety.layer1 import CONTEXTS, FilterResult

# --- known-bad inputs: (context, text, expected category) --------------------

BLOCKED = [
    # minors — always terms + obfuscations
    ("freetext", "loli", "minors"),
    ("freetext", "L0li", "minors"),
    ("freetext", "l.o.l.i", "minors"),
    ("freetext", "looooli", "minors"),
    ("freetext", "lоli", "minors"),          # Cyrillic 'о'
    ("freetext", "ӏoli", "minors"),          # U+04CF confusable 'l'
    ("prompt", "lᴏli", "minors"),            # small-capital O (name-fold)
    ("prompt", "shoᴛa", "minors"),           # small-capital T
    ("freetext", "ᴘedo", "minors"),          # small-capital P
    ("freetext", "looli", "minors"),         # doubled letter
    ("freetext", "lolli", "minors"),
    ("freetext", "l.0.l.i", "minors"),       # leet + separators combined
    ("freetext", "l0000li", "minors"),       # digit-stretched leet
    ("freetext", "l*li", "minors"),          # masked vowel
    ("freetext", "shota", "minors"),
    ("freetext", "sh0ta", "minors"),
    ("freetext", "underage", "minors"),
    ("freetext", "under-age", "minors"),
    ("freetext", "jailbait", "minors"),
    ("freetext", "she looks barely legal", "minors"),
    ("freetext", "pedophile", "minors"),
    ("freetext", "p3do stuff", "minors"),
    ("freetext", "peԁo", "minors"),          # U+0501 confusable 'd'
    ("freetext", "lolicons gallery", "minors"),   # plural
    ("freetext", "paedophiles", "minors"),
    ("freetext", "paedos", "minors"),
    ("chat", "let's do some ageplay", "minors"),
    ("chat", "age-regression play", "minors"),
    ("name", "Loli", "minors"),
    # minors — age assertions (all contexts, 20+ line)
    ("freetext", "she is 15 years old", "minors"),
    ("freetext", "a 19-year-old elf", "minors"),
    ("freetext", "15yo", "minors"),
    ("freetext", "15 y.o.", "minors"),
    ("freetext", "15 y/o", "minors"),
    ("freetext", "15 yrs", "minors"),
    ("freetext", "9 yr old", "minors"),
    ("freetext", "aged 16", "minors"),
    ("freetext", "age: 14", "minors"),
    ("freetext", "age:15", "minors"),
    ("freetext", "she just turned 18", "minors"),
    ("freetext", "just turned eighteen", "minors"),   # written turned
    ("freetext", "turning sixteen", "minors"),
    ("freetext", "fifteen years old", "minors"),
    ("freetext", "a seventeen-year-old", "minors"),
    ("freetext", "twelve years old", "minors"),
    ("freetext", "twelve yo", "minors"),
    ("freetext", "fifteen y/o", "minors"),
    ("freetext", "she is under 18", "minors"),
    ("freetext", "under eighteen only", "minors"),    # written under
    ("freetext", "age fifteen", "minors"),
    ("freetext", "aged fifteen", "minors"),
    ("freetext", "her 16th birthday", "minors"),      # ordinal
    ("freetext", "18-25 years old", "minors"),        # range starting <20
    ("freetext", "19.0 years old", "minors"),         # decimal
    ("chat", "when she was 15", "minors"),            # R1 flagship example
    ("chat", "she is 17", "minors"),                  # copula
    ("chat", "im 17", "minors"),
    ("chat", "i'm 16 years old", "minors"),
    ("prompt", "girl, 17 years old, portrait", "minors"),
    # minors — contextual terms sexualized or in image prompts
    ("freetext", "naked teen on the bed", "minors"),
    ("freetext", "a schoolgirl undressing slowly", "minors"),
    ("chat", "naked school-girl", "minors"),
    ("prompt", "schoolgirl, blazer, classroom", "minors"),
    ("prompt", "high-school hallway", "minors"),
    ("prompt", "highschool classroom", "minors"),
    ("prompt", "teenaged girl at the beach", "minors"),
    ("prompt", "t.e.e.n at the beach", "minors"),     # spread contextual
    ("prompt", "cute child in a meadow", "minors"),
    ("prompt", "high schoolers", "minors"),           # plural of contextual term
    ("prompt", "school uniforms", "minors"),
    ("prompt", "baby girls", "minors"),
    ("freetext", "naked high schoolers", "minors"),
    # bestiality
    ("freetext", "bestiality", "bestiality"),
    ("freetext", "beastiality", "bestiality"),
    ("freetext", "zoophilia", "bestiality"),
    ("freetext", "zoosexuals forum", "bestiality"),
    ("freetext", "sex with a horse", "bestiality"),
    ("freetext", "sex with a feral wolf", "bestiality"),   # adjective insertion
    ("chat", "fucks the dog", "bestiality"),               # 3rd-person verb
    ("chat", "sex with a stallion", "bestiality"),
    # non-consent
    ("freetext", "he raped her", "noncon"),
    ("freetext", "r@pe", "noncon"),
    ("freetext", "r.a.p.e", "noncon"),
    ("freetext", "r*pe fantasy", "noncon"),               # masked vowel
    ("freetext", "molested", "noncon"),
    ("freetext", "molestations occurred", "noncon"),
    ("freetext", "nonconsensual scene", "noncon"),
    ("freetext", "took her nonconsensually", "noncon"),
    ("freetext", "a dubcon scenario", "noncon"),
    ("freetext", "dubious-consent scenario", "noncon"),   # hyphen multiword
    ("freetext", "somnophilia", "noncon"),
    ("freetext", "took her against her will", "noncon"),
    ("chat", "whether she wants it or not", "noncon"),
    ("freetext", "she was drugged and fucked", "noncon"),
    ("freetext", "forced her onto the bed and fucked her", "noncon"),
    ("chat", "coerces her into sex", "noncon"),           # inflection
    ("chat", "drugging her drink before sex", "noncon"),
    ("prompt", "unconscious woman on bed", "noncon"),
    ("prompt", "sexual-assault scene", "noncon"),         # hyphen multiword
    # self-harm
    ("chat", "how do i kill myself", "selfharm"),
    ("chat", "i want to die", "selfharm"),
    ("chat", "kys", "selfharm"),
    ("freetext", "she is suicidal", "selfharm"),
    ("freetext", "self-harm", "selfharm"),
    ("freetext", "self-harming", "selfharm"),             # inflection
    ("chat", "cut herself", "selfharm"),                  # 3rd-person reflexive
    ("chat", "starving herself", "selfharm"),
    ("chat", "takes her own life", "selfharm"),
    ("freetext", "cutting myself again", "selfharm"),
    ("freetext", "pro-ana blog", "selfharm"),
    ("chat", "tell me a good suicide method", "selfharm"),
    # slurs (including the name field — Stage 0 DoD)
    ("name", "nigger", "slurs"),
    ("name", "N1gger", "slurs"),
    ("name", "N*gger", "slurs"),                          # masked vowel
    ("name", "niɡger", "slurs"),                          # confusable 'g'
    ("name", "nɪgger", "slurs"),                          # small-capital I
    ("name", "ꜰaggot", "slurs"),                          # small-capital F
    ("name", "sandnigger", "slurs"),                      # concatenation
    ("name", "Faggot the Bold", "slurs"),
    ("name", "porch monkeys", "slurs"),                   # plural multiword
    ("name", "halfbreeds", "slurs"),
    ("freetext", "that tranny", "slurs"),
    ("chat", "you retard", "slurs"),
    # drugs
    ("freetext", "she deals cocaine", "drugs"),
    ("freetext", "crystal meth", "drugs"),
    ("freetext", "crystalmeth", "drugs"),                 # concatenation
    ("freetext", "angel-dust supply", "drugs"),           # hyphen multiword
    ("freetext", "m3th lab", "drugs"),
    ("chat", "how to cook meth", "drugs"),
    ("freetext", "fentanyl", "drugs"),
    ("freetext", "popping percocets", "drugs"),           # plural
    ("freetext", "smokes marijuana daily", "drugs"),
    # medical/legal advice extraction
    ("chat", "what dosage should I take", "advice"),
    ("chat", "is it legal to carry a knife in Ohio", "advice"),
    ("chat", "give me medical advice", "advice"),
    ("chat", "can i sue my landlord", "advice"),
    ("chat", "diagnose me, doc", "advice"),
    # misc prohibited
    ("freetext", "an incest storyline", "misc"),
    ("freetext", "necrophilia", "misc"),
    ("freetext", "he is a necrophiliac", "misc"),         # inflection
    ("freetext", "snuff-film idea", "misc"),              # hyphen multiword
    ("chat", "how to make a pipe bomb", "misc"),
]

# --- clean inputs that MUST pass (permitted adult content + ordinary text) ---

ALLOWED = [
    # explicit adult content between adults is permitted (DECISIONS.md §11)
    ("freetext", "Two adults spend an explicit, passionate night together."),
    ("freetext", "She pushes him onto the bed and they fuck, slow and deep."),
    ("chat", "I want you naked, right now."),
    ("prompt", "woman, 25 years old, silver hair, nude, reclining on silk sheets"),
    ("prompt", "muscular man, shirtless, forge in background, dramatic lighting"),
    ("freetext", "A dominant woman who enjoys consensual rope play and teasing."),
    # ordinary character-writing that keyword-blunt filters often break on
    ("freetext", "She is 25 years old, a mercenary from the northern wastes."),
    ("freetext", "A 118-year-old elf who looks ageless."),
    ("freetext", "A 111-year-old vampire countess."),
    ("freetext", "twenty-two years old"),                  # compound adult age
    ("freetext", "a thirty-five year old knight"),
    ("freetext", "a twenty-one-year-old ranger"),
    ("freetext", "twenty years old"),
    ("freetext", "Her childhood friend, now grown, runs the tavern."),
    ("freetext", "She grew up poor and left home at twenty."),
    ("freetext", "He has two kids from a previous marriage and sees them on weekends."),
    ("freetext", "As a child she watched the fireworks over the harbor every year."),
    ("freetext", "Forced to flee her homeland, she swore revenge."),
    ("freetext", "The assassin drugged the guard and slipped past the gate."),
    ("freetext", "A suicide mission to save the kingdom."),
    ("freetext", "He drinks whiskey at the tavern and smokes a pipe."),
    ("freetext", "A stern teacher at the royal academy for young nobles."),
    ("freetext", "She mounted her horse and rode north."),   # equestrian, not bestiality
    ("freetext", "He mounts his horse at dawn."),
    ("freetext", "a virgin forest where the children of the village play"),
    ("freetext", "an intimate dinner while the kids slept upstairs"),
    ("freetext", "Seduced by power, he abandoned his old friends."),
    ("freetext", "There are 5 guards at the gate."),        # copula counting FP
    ("freetext", "This is 1 of many secrets she keeps."),
    ("freetext", "The tower is 15 meters tall."),
    ("freetext", "She is 5 foot 9 and built like a soldier."),
    ("chat", "lol i think you're really cute, wanna chat more?"),  # 'lol i' != loli
    ("freetext", "The hunter shot a deer at dawn."),        # 'shot a' != shota
    ("chat", "My day was long; pour me a glass of wine."),
    ("prompt", "adult woman warrior, 30 years old, plate armor, snowfield"),
    ("prompt", "a 22-year-old university student in a lecture hall"),
    ("name", "Alexandra Vex"),
    ("name", "Kid"),                                       # contextual terms don't gate names
    ("name", "Seraphina"),
    ("name", "Cummings"),
    ("name", "Dickens"),
    ("freetext", "under 20 minutes to reach the safehouse"),
    ("freetext", "She waited 15 years for his return."),   # duration, not age
    ("freetext", "The grapes ripen in the vineyard."),     # no 'rape' hit
    ("freetext", "The torpedo struck the hull."),          # no 'pedo' hit
    ("freetext", "A therapist who lost a brother years ago and carries it quietly."),
    ("freetext", "The committee met at the university."),  # doubles not over-folded
    ("freetext", "She schools him in swordplay."),         # 'schools' verb, no minor context
]


@pytest.mark.parametrize("context,text,category", BLOCKED)
def test_blocks_known_bad(content_filter, context, text, category):
    result = content_filter.check(text, context)
    assert not result.allowed, f"should block ({category}): {text!r}"
    assert result.category == category
    assert result.matched


@pytest.mark.parametrize("context,text", ALLOWED)
def test_passes_clean(content_filter, context, text):
    result = content_filter.check(text, context)
    assert result.allowed, (
        f"false positive ({result.category}: {result.matched!r}): {text!r}"
    )


def test_empty_and_none_allowed(content_filter):
    assert content_filter.check("", "freetext").allowed
    assert content_filter.check(None, "chat").allowed
    assert content_filter.check("   ", "name").allowed


def test_unknown_context_rejected(content_filter):
    with pytest.raises(ValueError):
        content_filter.check("hello", "bogus")


def test_contexts_constant():
    assert set(CONTEXTS) == {"freetext", "chat", "prompt", "name"}


def test_check_name_helper(content_filter):
    assert content_filter.check_name("Kaelith").allowed
    assert not content_filter.check_name("sp1c").allowed


def test_result_shape(content_filter):
    result = content_filter.check("loli", "freetext")
    assert isinstance(result, FilterResult)
    data = result.to_dict()
    assert data["allowed"] is False
    assert data["category"] == "minors"
    assert "content policy" in data["message"]


def test_glued_nonascii_letter_does_not_break_boundary(content_filter):
    # A pasted Cyrillic letter glued to a slur must not defeat the name block.
    assert not content_filter.check_name("жfag").allowed
    assert not content_filter.check("cncж", "freetext").allowed


def test_proximity_window_is_directional_both_ways(content_filter):
    # sexual term before the contextual term
    assert not content_filter.check("nude photos of a teen", "freetext").allowed
    # sexual term after the contextual term
    assert not content_filter.check("a teen posing nude", "freetext").allowed
    # far apart (> window) — passes as freetext even with sexual vocab present
    filler = "the caravan crossed the dunes for many days and nights " * 4
    text = "the kid waved goodbye. " + filler + " later, two adults undress."
    assert content_filter.check(text, "freetext").allowed
