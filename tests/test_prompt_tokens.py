"""Stage 5.5b — chunked long-prompt encoding + CLIP token accounting.

The chunking/orchestration and the token report are pure [HERE] logic tested
with fakes; the real diffusers ``pipe.encode_prompt`` is [HARDWARE]. A real
CLIP-tokenizer test runs only where the tokenizer files are on disk."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.imagegen.engine import (
    CLIP_CONTENT_BUDGET,
    _comma_windows,
    clip_token_counter,
    encode_chunked,
)
from app.imagegen.prompt import AssembledPrompt, PromptPiece, token_report

APP_ROOT = Path(__file__).resolve().parents[1]
_REAL_TOKENIZER = (APP_ROOT / "models" / "sdxl_config" / "tokenizer" / "vocab.json").is_file()


class _WordTokenizer:
    """A deterministic stand-in: one token per whitespace word (add_special
    ignored). Lets the windowing/accounting logic be tested without the BPE."""

    def __call__(self, text, add_special_tokens=False):
        return SimpleNamespace(input_ids=text.split())


def _count_words(text):
    return len(text.split())


# -- _comma_windows ----------------------------------------------------------


def test_comma_windows_packs_to_budget():
    tok = _WordTokenizer()
    # 100 one-word fragments, budget 75 -> the joined ", "-separated windows
    # each stay <= 75 words (the separators count as words here too).
    text = ", ".join(f"w{i}" for i in range(100))
    windows = _comma_windows(tok, text, budget=10)
    assert len(windows) > 1
    for win in windows:
        assert _count_words(win) <= 10


def test_comma_windows_emits_an_oversize_fragment_alone():
    tok = _WordTokenizer()
    big = "a b c d e f g h i j k l"  # 12 words, budget 5
    text = f"short, {big}, tail"
    windows = _comma_windows(tok, text, budget=5)
    assert big in windows  # the oversize fragment is its own window


def test_comma_windows_empty_text_is_one_empty_window():
    assert _comma_windows(_WordTokenizer(), "") == [""]
    assert _comma_windows(_WordTokenizer(), "   ,  ,") == [""]


# -- encode_chunked (orchestration; fake pipe + real torch) ------------------


def _fake_pipe(torch, tokenizer):
    class _Pipe:
        def __init__(self):
            self.tokenizer = tokenizer
            self.calls = 0

        def encode_prompt(self, prompt, negative_prompt, num_images_per_prompt,
                          do_classifier_free_guidance):
            self.calls += 1
            # diffusers 0.39 SDXL shapes: embeds [1,77,2048], pooled [1,1280]
            tag = float(self.calls)
            return (torch.ones(1, 77, 2048) * tag,
                    torch.ones(1, 77, 2048) * -tag,
                    torch.ones(1, 1280) * tag,
                    torch.ones(1, 1280) * -tag)

    return _Pipe()


def test_encode_chunked_concatenates_windows_and_pools_from_first():
    torch = pytest.importorskip("torch")
    tok = _WordTokenizer()
    pipe = _fake_pipe(torch, tok)
    # 60 three-word fragments -> ~180 words >> the 75 budget -> several windows.
    text = ", ".join(f"detail {i} here" for i in range(60))
    windows = _comma_windows(tok, text, budget=CLIP_CONTENT_BUDGET)
    k = len(windows)
    assert k >= 2  # a genuinely chunked prompt
    emb = encode_chunked(pipe, torch, text, "lowres, bad hands")
    assert emb["prompt_embeds"].shape == (1, 77 * k, 2048)
    # neg is padded to the SAME sequence length (CFG requires equal lengths)
    assert emb["negative_prompt_embeds"].shape == (1, 77 * k, 2048)
    # pooled comes from the FIRST window (tag == 1.0)
    assert float(emb["pooled_prompt_embeds"][0, 0]) == 1.0
    assert float(emb["negative_pooled_prompt_embeds"][0, 0]) == -1.0


def test_encode_chunked_short_prompt_is_one_window():
    torch = pytest.importorskip("torch")
    pipe = _fake_pipe(torch, _WordTokenizer())
    emb = encode_chunked(pipe, torch, "masterpiece, 1girl, elf", "lowres")
    assert emb["prompt_embeds"].shape == (1, 77, 2048)
    assert pipe.calls == 1  # behaviourally identical to the old string path


def test_encode_chunked_disabled_is_single_encode(monkeypatch):
    # 5.5b A/B baseline: chunked=False encodes the RAW strings once (diffusers
    # truncates at 77), regardless of how many comma windows the prompt would
    # otherwise fill. One encode_prompt call, 77-length embeds.
    torch = pytest.importorskip("torch")
    tok = _WordTokenizer()
    pipe = _fake_pipe(torch, tok)
    text = ", ".join(f"detail {i} here" for i in range(60))  # would be >=2 windows
    assert len(_comma_windows(tok, text, budget=CLIP_CONTENT_BUDGET)) >= 2

    seen = {}

    real = pipe.encode_prompt

    def _spy(prompt, negative_prompt, num_images_per_prompt,
             do_classifier_free_guidance):
        seen["prompt"] = prompt
        return real(prompt, negative_prompt, num_images_per_prompt,
                    do_classifier_free_guidance)

    monkeypatch.setattr(pipe, "encode_prompt", _spy)
    emb = encode_chunked(pipe, torch, text, "lowres, bad hands", chunked=False)
    assert pipe.calls == 1                          # NOT windowed
    assert seen["prompt"] == text                   # the whole raw string
    assert emb["prompt_embeds"].shape == (1, 77, 2048)
    assert emb["negative_prompt_embeds"].shape == (1, 77, 2048)


# -- token_report (pure) -----------------------------------------------------


def _assembled(*fragments):
    pieces = tuple(PromptPiece(source=s, text=t) for s, t in fragments)
    positive = ", ".join(p.text for p in pieces)
    return AssembledPrompt(positive=positive, negative="lowres", pieces=pieces)


def test_token_report_within_budget():
    ap = _assembled(("quality", "masterpiece"), ("subject", "1girl"))
    rep = token_report(ap, _count_words)
    assert rep["available"] is True
    assert rep["within_budget"] is True
    assert rep["boundary_index"] == len(ap.pieces)  # nothing dropped
    assert [p["text"] for p in rep["per_piece"]] == ["masterpiece", "1girl"]


def test_token_report_marks_the_overflow_boundary():
    # Build a prompt whose cumulative word-count crosses the budget; the
    # boundary_index is the first piece past it.
    frags = [("g%d" % i, "word " * 20) for i in range(10)]  # 20 words each
    ap = _assembled(*frags)
    rep = token_report(ap, _count_words)
    assert rep["within_budget"] is False
    assert rep["total"] > CLIP_CONTENT_BUDGET
    assert 0 <= rep["boundary_index"] < len(ap.pieces)
    # per-piece cumulative is monotonic
    cums = [p["cumulative"] for p in rep["per_piece"]]
    assert cums == sorted(cums)


# -- clip_token_counter ------------------------------------------------------


def test_clip_token_counter_unavailable_without_config(settings):
    # No pipeline_config_dir -> honestly unavailable (the sandbox posture).
    assert clip_token_counter(settings) is None


def test_clip_token_counter_unavailable_for_a_bad_dir(settings, tmp_path):
    settings.set("models.image.pipeline_config_dir", str(tmp_path / "nope"))
    assert clip_token_counter(settings) is None


@pytest.mark.skipif(not _REAL_TOKENIZER,
                    reason="the model's CLIP tokenizer is not on disk here")
def test_clip_token_counter_returns_real_counts(settings):
    settings.set("models.image.pipeline_config_dir", "models/sdxl_config")
    count = clip_token_counter(settings)
    assert count is not None
    n = count("masterpiece, 1girl, elf, silver hair, blue eyes")
    assert isinstance(n, int) and n > 0
    # a long prompt exceeds the single-window budget (the whole point of 5.5b)
    long = ", ".join(f"detail number {i}" for i in range(60))
    assert count(long) > CLIP_CONTENT_BUDGET


def test_preview_prompt_reports_tokens_unavailable_on_sandbox(images, creator):
    rec = creator.create_character({
        "mode": "quick", "name": "Tok One", "age": 24,
        "selections": {"race": "human", "gender_presentation": "feminine",
                       "skin_tone": "fair", "hair_color": "black",
                       "hair_style": "bob", "eye_color": "brown",
                       "body_type": "average"}})
    pv = images.preview_prompt(rec["id"])
    assert pv["ok"] is True
    assert pv["tokens"]["available"] is False  # no config -> structured
    assert "reason" in pv["tokens"]
