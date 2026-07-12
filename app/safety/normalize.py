"""Deterministic text normalization for Layer-1 filtering.

Produces scan variants that defeat common obfuscation — case tricks, Unicode
homoglyphs, leetspeak, separator padding ("l.o.l.i"), character stretching
("loooli") and doubled-letter noise ("raape") — using pure string
transformation. No model involvement anywhere in this module; that is the
point of Layer 1.

Matching (in layer1.py) composes these primitives into two pattern families
per listed term:
  - a *joiner* pattern that tolerates 0-2 separators at word joins, matching
    a term against its spaced / hyphenated / underscored / concatenated forms
    ("angel dust" == "angel-dust" == "angeldust");
  - a *spread* pattern that requires a separator between every letter,
    matching fully-spread obfuscation ("l.o.l.i", "l o l i") without firing on
    ordinary adjacent words ("shot a", "lol i") — those are caught, when they
    are the literal term, by the joiner family instead.
Both use ASCII edge guards rather than ``\\b`` so a glued non-ASCII letter
cannot destroy a word boundary.
"""

from __future__ import annotations

import re
import unicodedata

# Zero-width / invisible formatting characters removed before matching.
_ZERO_WIDTH = dict.fromkeys(
    map(
        ord,
        "​‌‍‎‏"  # zero-width space, ZWNJ, ZWJ, LRM, RLM
        "⁠⁡⁢⁣"        # word joiner, invisible times/separator/plus
        "﻿­",                   # BOM, soft hyphen
    ),
    None,
)

# Cross-script and Latin-extended lookalikes that NFKC/NFKD do not fold.
# Keys are post-casefold confusables; values are the ASCII letter they imitate.
# Coverage spans every ASCII letter so no single-substitution bypass survives
# (the earlier partial table left d/f/g/l/q/r/z unmapped — a structural hole).
_HOMOGLYPHS = str.maketrans({
    # a
    "а": "a", "α": "a", "ɑ": "a", "ａ": "a", "ⱥ": "a", "ɐ": "a",
    # b
    "ь": "b", "ъ": "b", "ƀ": "b", "β": "b", "б": "b", "ɓ": "b",
    # c
    "с": "c", "ϲ": "c", "ⅽ": "c", "ｃ": "c", "ċ": "c", "ç": "c", "ć": "c", "ϛ": "c",
    # d
    "ԁ": "d", "ɗ": "d", "ď": "d", "ḍ": "d", "Ꮷ": "d", "ꮷ": "d", "ⅾ": "d", "đ": "d",
    # e
    "е": "e", "ε": "e", "ё": "e", "ℯ": "e", "ｅ": "e", "є": "e", "ҽ": "e", "ɇ": "e",
    # f
    "ƒ": "f", "ſ": "f", "ꞙ": "f", "ϝ": "f", "ք": "f",
    # g
    "ɡ": "g", "ԍ": "g", "ɢ": "g", "ǥ": "g", "ģ": "g", "９": "g",
    # h
    "һ": "h", "ｈ": "h", "հ": "h", "ħ": "h", "ⱨ": "h", "н": "h",
    # i
    "і": "i", "ı": "i", "ɩ": "i", "ι": "i", "ｉ": "i", "í": "i", "ї": "i", "ⅰ": "i", "ｌ": "l",
    # j
    "ј": "j", "ϳ": "j", "ｊ": "j", "ĵ": "j",
    # k
    "к": "k", "κ": "k", "ｋ": "k", "ķ": "k", "ⱪ": "k",
    # l
    "ӏ": "l", "ł": "l", "ɭ": "l", "ǀ": "l", "ł": "l", "ḷ": "l", "ⅼ": "l", "ℓ": "l", "ļ": "l",
    # m
    "м": "m", "ｍ": "m", "ⅿ": "m", "ᴍ": "m", "ɱ": "m",
    # n
    "ｎ": "n", "ɴ": "n", "ŋ": "n", "ⁿ": "n",
    # o
    "о": "o", "ο": "o", "ơ": "o", "ө": "o", "ｏ": "o", "ơ": "o", "σ": "o", "ø": "o", "ð": "o",
    # p
    "р": "p", "ρ": "p", "ｐ": "p", "ṗ": "p", "ƥ": "p",
    # q
    "ԛ": "q", "ɋ": "q", "ｑ": "q", "գ": "q", "ԛ": "q",
    # r
    "г": "r", "ɾ": "r", "ꭇ": "r", "ᴦ": "r", "ｒ": "r", "ř": "r", "ṛ": "r", "ɼ": "r",
    # s
    "ѕ": "s", "ꜱ": "s", "ｓ": "s", "ś": "s", "ş": "s", "ș": "s",
    # t
    "т": "t", "τ": "t", "ｔ": "t", "ţ": "t", "ṭ": "t", "ⱦ": "t",
    # u
    "υ": "u", "ս": "u", "ʋ": "u", "ｕ": "u", "ú": "u", "ц": "u", "ᴜ": "u",
    # v
    "ν": "v", "ѵ": "v", "ｖ": "v", "ⅴ": "v", "ᴠ": "v", "ѷ": "v",
    # w
    "ѡ": "w", "ԝ": "w", "ｗ": "w", "ա": "w", "ⱳ": "w", "ᴡ": "w",
    # x
    "х": "x", "χ": "x", "ｘ": "x", "ⅹ": "x", "ẋ": "x",
    # y
    "у": "y", "γ": "y", "ү": "y", "ｙ": "y", "ý": "y", "ỿ": "y", "ʏ": "y",
    # z
    "ᴢ": "z", "ʐ": "z", "ｚ": "z", "ż": "z", "ź": "z", "ⱬ": "z", "ẑ": "z",
})

# Leetspeak digit/symbol substitutions. Applied as a separate variant because
# folding digits corrupts legitimate numbers (age patterns must see "15").
_LEET = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b",
    "9": "g", "@": "a", "$": "s", "!": "i", "|": "l", "€": "e", "£": "l",
    "¢": "c",
})

_WS_RE = re.compile(r"\s+")
# Letters only: digit runs must survive ("111-year-old" is a valid age).
_STRETCH_RE = re.compile(r"([a-z])\1{2,}")
_DOUBLE_RE = re.compile(r"([a-z])\1+")


# Cache for the name-based Latin-letter fold (see _fold_latin_letter).
_NAME_FOLD_CACHE: dict[str, str] = {}


def _fold_latin_letter(ch: str) -> str:
    """Map a single non-ASCII character to the ASCII letter it *is named for*,
    if any — e.g. ʀ (LATIN LETTER SMALL CAPITAL R) -> 'r', ɡ (LATIN SMALL
    LETTER SCRIPT G) -> 'g', ı (LATIN SMALL LETTER DOTLESS I) -> 'i'.

    This generalizes the hand-curated homoglyph table across the whole Latin
    Small-Capital / script / turned block so no single-substitution bypass
    survives merely because a codepoint was left off a list. Cross-script
    confusables (Cyrillic/Greek) do NOT carry Latin-letter names and stay with
    the explicit _HOMOGLYPHS table. Returns '' when there is no clean mapping."""
    cached = _NAME_FOLD_CACHE.get(ch)
    if cached is not None:
        return cached
    result = ""
    try:
        name = unicodedata.name(ch)
    except ValueError:
        name = ""
    if name.startswith("LATIN "):
        last = name.rsplit(" ", 1)[-1]
        if len(last) == 1 and "A" <= last <= "Z":
            result = last.lower()
    _NAME_FOLD_CACHE[ch] = result
    return result


def normalize(text: str) -> str:
    """Canonical base form: NFKC, invisibles stripped, casefolded,
    accent-stripped, homoglyph-folded (explicit table + name-based Latin fold),
    whitespace collapsed."""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_ZERO_WIDTH)
    text = text.casefold()
    # Strip combining marks so accent obfuscation folds ('lólí' -> 'loli').
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.translate(_HOMOGLYPHS)
    # Second pass: fold any remaining non-ASCII character that is *named* for a
    # single Latin letter (small caps, script, dotless, turned forms, ...).
    if any(ord(ch) > 0x7F for ch in text):
        text = "".join(
            (_fold_latin_letter(ch) or ch) if ord(ch) > 0x7F else ch for ch in text
        )
    return _WS_RE.sub(" ", text).strip()


def leet_fold(text: str) -> str:
    """Decode leetspeak substitutions ('l0l1' -> 'loli'). Digits are
    rewritten — never run digit-sensitive patterns on this variant."""
    return text.translate(_LEET)


def squeeze(text: str) -> str:
    """Collapse runs of 3+ identical letters ('loooli' -> 'loli').
    Doubles are kept so ordinary spelling ('committee') is untouched;
    digits are never squeezed (age patterns must see them intact)."""
    return _STRETCH_RE.sub(r"\1", text)


def collapse_doubles(text: str) -> str:
    """Collapse every run of 2+ identical letters to one ('raape' -> 'rape',
    'looooli' -> 'loli', 'niggger' -> 'niger'). Aggressive: applied to BOTH
    the scanned text and the listed term so doubled-letter noise folds out
    symmetrically ('preteeen' and 'preteen' both -> 'preten'). Digits are
    left intact."""
    return _DOUBLE_RE.sub(r"\1", text)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def scan_variants(text: str) -> list[str]:
    """All folded variants for literal-term joiner matching."""
    base = normalize(text)
    return _dedupe([base, leet_fold(base)])


def double_variants(text: str) -> list[str]:
    """Variants with doubled letters collapsed, for the doubles family.
    Includes collapse-after-leet so digit-stretched leetspeak folds
    ('l0000li' -> leet 'loooli' -> 'loli')."""
    base = normalize(text)
    cd = collapse_doubles(base)
    cd_after_leet = collapse_doubles(leet_fold(base))
    return _dedupe([cd, leet_fold(cd), cd_after_leet])


def digit_safe_variants(text: str) -> list[str]:
    """Variants that preserve digits, for regex rules like age patterns."""
    base = normalize(text)
    return _dedupe([base, squeeze(base)])


def leet_variants(text: str) -> list[str]:
    """Leet-folded variants, for non-digit regex rules."""
    base = normalize(text)
    return _dedupe([leet_fold(base), leet_fold(squeeze(base))])
