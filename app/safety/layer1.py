"""Layer 1 — deterministic input/output content filter.

The safety floor (DECISIONS.md §11, layer 1): pure blocklist/regex gating on
normalized text. No model, no network, no judgment calls. Every user-entered
string and every model output in the app routes through this module; later
stages import it rather than reimplementing checks.

Term data lives in editable files under ``app/safety/data/``. File format:
  - one entry per line; blank lines and ``#`` comments ignored
  - plain lines are literal terms. Each is matched three ways on normalized,
    obfuscation-folded text: (1) a *joiner* form tolerating 0-2 separators at
    word joins, so "angel dust" also catches "angel-dust"/"angeldust";
    (2) a *spread* form (single alpha words, 3+ chars) requiring a separator
    between every letter, catching "l.o.l.i"/"l o l i"; (3) a doubled-letter
    fold catching "raape"/"looli". All three use ASCII edge guards so a glued
    non-ASCII letter cannot break the boundary, and all run over leetspeak
    variants too.
  - ``re:`` lines are raw regexes. Digit-bearing regexes (age patterns) run
    only on digit-preserving variants; digit-free regexes additionally run on
    leetspeak variants (so "against her w1ll" folds).

Category semantics:
  - **always** lists block in every context.
  - **contextual** lists block outright in image-``prompt`` context, and in
    ``freetext``/``chat`` contexts only when sexual vocabulary occurs within
    ``PROXIMITY_WINDOW`` characters. They are not applied to ``name`` context.

Layer 1 errs toward blocking: false positives are accepted by design and the
data files are the tuning surface (see docs/CONTENT_POLICY.md). Layer 2
(semantic gating) catches intent that keywords cannot.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from . import normalize as norm

DATA_DIR = Path(__file__).resolve().parent / "data"

# Max characters between a contextual term and sexual vocabulary for a
# proximity block. Roughly one sentence.
PROXIMITY_WINDOW = 120

CONTEXTS = ("freetext", "chat", "prompt", "name")

# category -> (always file, contextual file). Dict order is scan order,
# most severe first, so the reported category on multi-hit text is stable.
_CATEGORY_FILES: dict[str, tuple[str, str | None]] = {
    "minors": ("minors_always.txt", "minors_contextual.txt"),
    "bestiality": ("bestiality_always.txt", None),
    "noncon": ("noncon_always.txt", "noncon_contextual.txt"),
    "selfharm": ("selfharm_always.txt", None),
    "slurs": ("slurs.txt", None),
    "drugs": ("drugs_always.txt", None),
    "advice": ("advice_always.txt", None),
    "misc": ("misc_always.txt", None),
}
_SEXUAL_CONTEXT_FILE = "sexual_context.txt"

# Spread (every-letter-separated) matching only for single words this long or
# longer. 3 catches short listed terms ("cnc" -> "c.n.c") while staying rare
# enough on ordinary text to be an accepted err-toward-blocking tradeoff.
_SPREAD_MIN_LEN = 3

_ASCII_WORD = "[a-z0-9]"
_EDGE_L = r"(?<!" + _ASCII_WORD + r")"
_EDGE_R = r"(?!" + _ASCII_WORD + r")"
_SEP0 = r"[^a-z0-9]{0,2}"        # word-join separators (0 = concatenation)
_SEP_PUNCT = r"[^a-z0-9\s]{0,2}"  # intra-word punctuation (NOT whitespace)
_SEP1 = r"[^a-z0-9]{1,2}"        # full spread (>=1 sep, whitespace allowed)
_PLURAL = r"(?:e?s)?"            # tolerate a trailing plural on the whole term
_DIGIT_RE = re.compile(r"\\d|[0-9]")


@dataclass(frozen=True)
class FilterResult:
    allowed: bool
    category: str | None = None
    matched: str | None = None
    context: str = "freetext"
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_list_file(path: Path) -> tuple[list[str], list[str]]:
    """Returns (literal terms, raw regex strings) from one data file."""
    terms: list[str] = []
    regexes: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            regexes.append(line[3:].strip())
        else:
            terms.append(norm.normalize(line))
    return terms, regexes


def _chunks(term: str) -> list[str]:
    """Alphanumeric chunks of a normalized term ('self-harm' -> [self, harm])."""
    return [c for c in re.split(r"[^a-z0-9]+", term) if c]


def _joiner_core(term: str) -> str | None:
    """Regex core matching a term across separator/concatenation variants,
    with a trailing plural tolerated ('high school' -> 'high schools')."""
    parts = _chunks(term)
    if not parts:
        return None
    return _SEP0.join(re.escape(p) for p in parts) + _PLURAL


def _single_word(term: str) -> str | None:
    """The lone alphabetic chunk of a single-word term long enough to obfuscate,
    else None."""
    parts = _chunks(term)
    if len(parts) != 1:
        return None
    word = parts[0]
    if len(word) < _SPREAD_MIN_LEN or not word.isalpha():
        return None
    return word


def _punct_core(term: str) -> str | None:
    """Core tolerating 0-2 NON-whitespace separators between every letter of a
    single word. Catches punctuation obfuscation and a lone separator dropped
    inside a word ('under-age', 'l.o.l.i', 'school-girl') without folding two
    space-separated words into a term ('shot a' never becomes 'shota')."""
    word = _single_word(term)
    if word is None:
        return None
    return _SEP_PUNCT.join(re.escape(ch) for ch in word) + _PLURAL


def _spread_core(term: str) -> str | None:
    """Core requiring at least one separator (whitespace allowed) between every
    letter, catching fully space-spread obfuscation ('l o l i') that _punct_core
    deliberately rejects. Adjacent-word text cannot match: every pair must be
    separated, which 'shot a'/'lol i' are not."""
    word = _single_word(term)
    if word is None:
        return None
    return _SEP1.join(re.escape(ch) for ch in word)


def _alt(cores: list[str]) -> re.Pattern[str] | None:
    """Edge-guarded alternation of cores, longest-first for specific matches."""
    cores = [c for c in cores if c]
    if not cores:
        return None
    cores = sorted(set(cores), key=len, reverse=True)
    return re.compile(_EDGE_L + r"(?:" + "|".join(cores) + r")" + _EDGE_R)


class _TermSet:
    """Compiled matcher for one term list: joiner + spread families over both
    the plain and doubled-collapsed forms, plus raw regexes."""

    def __init__(self, terms: list[str], regexes: list[str]):
        self._plain = self._build(terms)
        self._doubled = self._build([norm.collapse_doubles(t) for t in terms])
        self.digit_res = [re.compile(rx) for rx in regexes if _DIGIT_RE.search(rx)]
        self.free_res = [re.compile(rx) for rx in regexes if not _DIGIT_RE.search(rx)]

    @staticmethod
    def _build(terms: list[str]) -> tuple[re.Pattern[str] | None, ...]:
        return (
            _alt([_joiner_core(t) for t in terms]),
            _alt([_punct_core(t) for t in terms]),
            _alt([_spread_core(t) for t in terms]),
        )

    def find(self, text: str) -> str | None:
        """First match across every family/variant, or None."""
        for variant in norm.scan_variants(text):      # base + leet
            for pat in self._plain:
                if pat is not None:
                    m = pat.search(variant)
                    if m:
                        return m.group(0)
        for variant in norm.double_variants(text):    # doubles collapsed
            for pat in self._doubled:
                if pat is not None:
                    m = pat.search(variant)
                    if m:
                        return m.group(0)
        # Raw regexes: digit-bearing on digit-safe variants only; digit-free
        # additionally on leet variants.
        if self.digit_res:
            for variant in norm.digit_safe_variants(text):
                for pat in self.digit_res:
                    m = pat.search(variant)
                    if m:
                        return m.group(0)
        if self.free_res:
            variants = norm.digit_safe_variants(text) + norm.leet_variants(text)
            for variant in variants:
                for pat in self.free_res:
                    m = pat.search(variant)
                    if m:
                        return m.group(0)
        return None

    def iter_spans(self, variant: str):
        """(start, end, text) matches of the literal families on one variant,
        for proximity checks on contextual lists."""
        for pat in self._plain:
            if pat is not None:
                for m in pat.finditer(variant):
                    yield m.start(), m.end(), m.group(0)


class Layer1Filter:
    """Deterministic content filter over the editable blocklist data files."""

    def __init__(self, data_dir: Path | str = DATA_DIR):
        data_dir = Path(data_dir)
        self._categories: dict[str, tuple[_TermSet, _TermSet | None]] = {}
        for category, (always_file, ctx_file) in _CATEGORY_FILES.items():
            terms, regexes = _parse_list_file(data_dir / always_file)
            always = _TermSet(terms, regexes)
            contextual: _TermSet | None = None
            if ctx_file is not None:
                ctx_terms, ctx_regexes = _parse_list_file(data_dir / ctx_file)
                if ctx_regexes:
                    raise ValueError(
                        f"{ctx_file}: contextual lists take literal terms only"
                    )
                contextual = _TermSet(ctx_terms, [])
            self._categories[category] = (always, contextual)
        sex_terms, sex_regexes = _parse_list_file(data_dir / _SEXUAL_CONTEXT_FILE)
        if sex_regexes:
            raise ValueError("sexual_context.txt takes literal terms only")
        self._sexual = _TermSet(sex_terms, [])

    # -- public API ---------------------------------------------------------

    def check(self, text: str | None, context: str = "freetext") -> FilterResult:
        """Gate one string. Same call wraps inputs and model outputs."""
        if context not in CONTEXTS:
            raise ValueError(f"unknown filter context: {context!r}")
        if not text or not text.strip():
            return FilterResult(True, context=context, message="ok")

        for category, (always, contextual) in self._categories.items():
            matched = always.find(text)
            if matched:
                return self._blocked(category, matched, context)
            if contextual is None or context == "name":
                continue
            hit = self._contextual_hit(contextual, text, context)
            if hit:
                return self._blocked(category, hit, context)
        return FilterResult(True, context=context, message="ok")

    def check_name(self, name: str | None) -> FilterResult:
        return self.check(name, context="name")

    # -- internals ----------------------------------------------------------

    def _contextual_hit(
        self, contextual: _TermSet, text: str, context: str
    ) -> str | None:
        if context == "prompt":
            # Strictest: any contextual match blocks outright.
            return contextual.find(text)
        # freetext / chat: block only near sexual vocabulary, checked in the
        # same variant so spans line up.
        for variant in norm.scan_variants(text):
            for start, end, hit in contextual.iter_spans(variant):
                if self._sexual_near(variant, start, end):
                    return hit
        return None

    def _sexual_near(self, variant: str, start: int, end: int) -> bool:
        lo = max(0, start - PROXIMITY_WINDOW)
        hi = end + PROXIMITY_WINDOW
        for s, e, _ in self._sexual.iter_spans(variant):
            if e >= lo and s <= hi:
                return True
        return False

    @staticmethod
    def _blocked(category: str, matched: str, context: str) -> FilterResult:
        return FilterResult(
            False,
            category=category,
            matched=matched,
            context=context,
            message=f"Blocked by content policy ({category}).",
        )


# -- module-level convenience ------------------------------------------------

_default: Layer1Filter | None = None


def get_filter() -> Layer1Filter:
    global _default
    if _default is None:
        _default = Layer1Filter()
    return _default


def filter_text(text: str | None, context: str = "freetext") -> FilterResult:
    return get_filter().check(text, context)


def filter_name(name: str | None) -> FilterResult:
    return get_filter().check_name(name)
