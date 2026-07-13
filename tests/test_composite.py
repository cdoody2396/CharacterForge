"""Stage-5 compositing (app/imagegen/composite.py) — pure placement geometry,
config coercion, and the real Pillow pixel ops (PIL is in the sandbox)."""

import numpy as np
import pytest
from PIL import Image

from app.config import Settings
from app.imagegen.composite import (
    ANCHORS,
    CompositeConfig,
    NotMatted,
    coerce_composite_config,
    composite_geometry,
    composite_over,
    encode_png_data_uri,
    load_rgba_matted,
    prepare_alpha,
)


# -- pure geometry -----------------------------------------------------------

def test_geometry_bottom_center_scales_to_height():
    (w, h), (left, top) = composite_geometry((400, 400), (100, 200),
                                             CompositeConfig(scale=0.85))
    assert h == 340                      # 0.85 * 400
    assert w == 170                      # aspect preserved
    assert left == (400 - w) // 2        # centered horizontally
    assert top == 400 - h                # feet at the bottom edge


def test_geometry_refits_when_too_wide():
    (w, h), _ = composite_geometry((400, 400), (900, 200),
                                   CompositeConfig(scale=1.0))
    assert w <= 400 and h <= 400


@pytest.mark.parametrize("anchor", ANCHORS)
def test_geometry_box_stays_inside_background(anchor):
    (w, h), (left, top) = composite_geometry(
        (500, 500), (300, 300), CompositeConfig(anchor=anchor, scale=0.9))
    assert 0 <= left and left + w <= 500
    assert 0 <= top and top + h <= 500


def test_geometry_rejects_nonpositive():
    with pytest.raises(ValueError):
        composite_geometry((0, 100), (10, 10), CompositeConfig())


# -- config coercion ---------------------------------------------------------

def test_coerce_clamps_and_defaults(tmp_path):
    s = Settings(tmp_path / "s.json")
    s.set("image_gen.compositing.scale", float("nan"))
    s.set("image_gen.compositing.anchor", "center")
    s.set("image_gen.compositing.edge_choke", 99)
    s.set("image_gen.compositing.alpha_floor", 30)
    cfg = coerce_composite_config(s)
    assert cfg.scale == 0.85          # NaN -> default
    assert cfg.anchor == "center"
    assert cfg.edge_choke == 8        # clamped to [0, 8]
    assert cfg.alpha_floor == 30


def test_coerce_bad_anchor_falls_back(tmp_path):
    s = Settings(tmp_path / "s.json")
    s.set("image_gen.compositing.anchor", ["not", "a", "string"])
    assert coerce_composite_config(s).anchor == "bottom_center"


# -- pixel ops ---------------------------------------------------------------

def _matted(tmp_path, name="fg.png", size=(100, 200)):
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    for y in range(size[1]):
        for x in range(size[0]):
            if abs(x - size[0] // 2) < size[0] // 3 and 10 < y < size[1] - 10:
                im.putpixel((x, y), (200, 40, 40, 255))
    p = tmp_path / name
    im.save(p)
    return p


def test_load_rgba_matted_rejects_rgb(tmp_path):
    p = tmp_path / "plain.png"
    Image.new("RGB", (30, 30), (1, 2, 3)).save(p)
    with pytest.raises(NotMatted):
        load_rgba_matted(p)


def test_load_rgba_matted_accepts_rgba(tmp_path):
    im = load_rgba_matted(_matted(tmp_path))
    assert im.mode == "RGBA"


def test_composite_over_produces_rgb_of_bg_size(tmp_path):
    fg = load_rgba_matted(_matted(tmp_path))
    bg = Image.new("RGB", (400, 500), (240, 240, 240))
    out = composite_over(bg, fg, CompositeConfig(scale=0.8))
    assert out.mode == "RGB" and out.size == (400, 500)


def test_edge_choke_shrinks_alpha_coverage(tmp_path):
    fg = load_rgba_matted(_matted(tmp_path))
    a0 = np.asarray(prepare_alpha(fg, CompositeConfig()).split()[3])
    a1 = np.asarray(prepare_alpha(fg, CompositeConfig(edge_choke=3)).split()[3])
    assert (a1 >= 128).mean() < (a0 >= 128).mean()


def test_alpha_floor_zeroes_low_alpha(tmp_path):
    im = Image.new("RGBA", (10, 10), (10, 20, 30, 40))   # uniform low alpha
    out = prepare_alpha(im, CompositeConfig(alpha_floor=50))
    assert np.asarray(out.split()[3]).max() == 0
    # RGB preserved (straight alpha, not premultiplied)
    assert np.asarray(out)[0, 0, 0] == 10


def test_encode_png_data_uri(tmp_path):
    uri = encode_png_data_uri(Image.new("RGBA", (4, 4), (0, 0, 0, 0)))
    assert uri.startswith("data:image/png;base64,")
