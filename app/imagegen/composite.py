"""Character-over-background compositing (Stage 5 — DECISIONS.md §13).

Places a **matted** character frame (a keyable RGBA cutout from Stage 3f —
straight alpha, ORIGINAL RGB preserved) over a separately generated scene
background. This is the frozen §13 architecture: composite an
already-consistent character over a separately-generated background rather than
generating the character *inside* the scene in one pass (which fights identity
consistency).

Fully **[HERE]**: unlike matting (whose ONNX call is [HARDWARE]), compositing
is pure Pillow + arithmetic — it runs and is unit-tested in the GPU-less
sandbox. The pure geometry (``composite_geometry``) and config coercion import
and run with no Pillow installed; only the pixel ops import PIL lazily (house
style, mirrors ``matte.py``).

**The 3f edge residual, retired here.** Stage 3f left a named residual: matte
halos over BRIGHT and DARK composite backgrounds. Rather than re-matte (which
regenerates the alpha), this module chokes/feathers the alpha *at composite
time* — tunable per composite, no regeneration:
  - ``edge_choke`` erodes the alpha N px (``MinFilter(3)``, the same 1px choke
    ``matte._OnnxMatter`` uses),
  - ``feather_px`` softens the choked edge,
  - ``alpha_floor`` clamps near-zero alpha to 0 (kills faint halo fringe).
The RGB is never touched — straight alpha stays straight (no premultiply).

The background on/off toggle is a *service* concern: OFF = serve the matted
cutout unchanged (transparent passthrough); ON = ``composite_over`` here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings

# Supported placement anchors. bottom_center suits a standing character over a
# scene (feet toward the lower edge); the rest cover common framings.
ANCHORS = ("bottom_center", "center", "bottom_left", "bottom_right", "top_center")
DEFAULT_ANCHOR = "bottom_center"


class NotMatted(ValueError):
    """A frame offered for compositing is not a keyable RGBA cutout (no alpha
    channel) — only matted frames may be composited (ties to the §13 DoD)."""


@dataclass(frozen=True)
class CompositeConfig:
    anchor: str = DEFAULT_ANCHOR
    scale: float = 0.85     # foreground height as a fraction of background height
    margin: float = 0.0     # gap from the anchored edge, fraction of bg height
    edge_choke: int = 0     # alpha erosion passes (halo choke); 0 = off
    feather_px: int = 0     # Gaussian soften after the choke; 0 = off
    alpha_floor: int = 0    # clamp alpha < this (0..254) to 0; 0 = off


# -- pure geometry (sandbox-verifiable; no Pillow) ----------------------------


def composite_geometry(
    bg_size: tuple[int, int], fg_size: tuple[int, int], config: CompositeConfig
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return ``((fg_w, fg_h), (left, top))`` — the resized foreground box and
    where to paste it over the background. Scales the foreground to
    ``config.scale`` of the background height (aspect preserved), shrinks it
    further if that would exceed the background width, then positions it by the
    anchor with a ``config.margin`` gap. The box is clamped inside the
    background. Raises ``ValueError`` on a non-positive dimension."""
    bw, bh = int(bg_size[0]), int(bg_size[1])
    fw, fh = int(fg_size[0]), int(fg_size[1])
    if bw <= 0 or bh <= 0 or fw <= 0 or fh <= 0:
        raise ValueError(f"non-positive dimension: bg={bg_size} fg={fg_size}")

    target_h = max(1, min(bh, int(round(config.scale * bh))))
    ratio = target_h / fh
    new_w = max(1, int(round(fw * ratio)))
    new_h = target_h
    if new_w > bw:  # too wide after height-fit — refit to width
        new_h = max(1, int(round(new_h * (bw / new_w))))
        new_w = bw

    margin_px = max(0, int(round(config.margin * bh)))
    anchor = config.anchor if config.anchor in ANCHORS else DEFAULT_ANCHOR

    # horizontal
    if anchor == "bottom_left":
        left = margin_px
    elif anchor == "bottom_right":
        left = bw - new_w - margin_px
    else:  # bottom_center / center / top_center
        left = (bw - new_w) // 2
    # vertical
    if anchor in ("bottom_center", "bottom_left", "bottom_right"):
        top = bh - new_h - margin_px
    elif anchor == "top_center":
        top = margin_px
    else:  # center
        top = (bh - new_h) // 2

    left = max(0, min(left, bw - new_w))
    top = max(0, min(top, bh - new_h))
    return (new_w, new_h), (left, top)


# -- settings resolution ------------------------------------------------------


def coerce_composite_config(settings: Settings) -> CompositeConfig:
    """Build a CompositeConfig from ``image_gen.compositing.*``, coerced
    defensively (mirrors ``coerce_matte_config``): a hand-edited Infinity / NaN
    / string / out-of-range value degrades to the code default and clamps,
    never reaching the bridge as a traceback."""
    d = CompositeConfig()

    def _int(key: str, default: int, *, lo: int, hi: int) -> int:
        try:
            v = float(settings.get(f"image_gen.compositing.{key}", default))
        except (TypeError, ValueError, OverflowError):
            return default
        if not math.isfinite(v):
            return default
        return int(min(hi, max(lo, v)))

    def _float(key: str, default: float, *, lo: float, hi: float) -> float:
        try:
            v = float(settings.get(f"image_gen.compositing.{key}", default))
        except (TypeError, ValueError, OverflowError):
            return default
        if not math.isfinite(v):
            return default
        return min(hi, max(lo, v))

    raw_anchor = settings.get("image_gen.compositing.anchor", d.anchor)
    anchor = raw_anchor if (isinstance(raw_anchor, str)
                            and raw_anchor in ANCHORS) else d.anchor
    return CompositeConfig(
        anchor=anchor,
        scale=_float("scale", d.scale, lo=0.05, hi=1.0),
        margin=_float("margin", d.margin, lo=0.0, hi=0.5),
        edge_choke=_int("edge_choke", d.edge_choke, lo=0, hi=8),
        feather_px=_int("feather_px", d.feather_px, lo=0, hi=8),
        alpha_floor=_int("alpha_floor", d.alpha_floor, lo=0, hi=254),
    )


# -- pixel ops (Pillow, lazy-imported) ----------------------------------------


def load_rgba_matted(path: Path) -> Any:
    """Open a matted frame as an RGBA ``Image`` copy (handle closed — Windows
    AV hygiene). Raises ``NotMatted`` if the frame carries no alpha channel
    (an unmatted RGB catalog frame), so the §13 "matted frame" guard holds at
    the pixel boundary, not just by directory convention."""
    from PIL import Image

    with Image.open(path) as opened:
        if "A" not in opened.getbands():
            raise NotMatted(
                f"{Path(path).name} has no alpha channel — only matted "
                f"(keyable RGBA) frames can be composited")
        return opened.convert("RGBA")  # convert returns a detached copy


def prepare_alpha(fg_rgba: Any, config: CompositeConfig) -> Any:
    """Apply the edge treatment to a straight-alpha RGBA image: choke, feather,
    alpha-floor — on the ALPHA band only (original RGB preserved). Returns a
    new RGBA image; the input is untouched."""
    from PIL import Image, ImageFilter

    if fg_rgba.mode != "RGBA":
        fg_rgba = fg_rgba.convert("RGBA")
    r, g, b, a = fg_rgba.split()
    for _ in range(max(0, config.edge_choke)):
        a = a.filter(ImageFilter.MinFilter(3))  # 1px grayscale erosion
    if config.feather_px:
        a = a.filter(ImageFilter.GaussianBlur(config.feather_px))
    if config.alpha_floor:
        floor = config.alpha_floor
        a = a.point(lambda v, f=floor: 0 if v < f else v)
    return Image.merge("RGBA", (r, g, b, a))


def composite_over(bg_rgb: Any, fg_rgba: Any, config: CompositeConfig) -> Any:
    """Composite a matted RGBA foreground over an RGB background per ``config``.
    Returns a flattened RGB ``Image``. Straight alpha (``alpha_composite``),
    never premultiplied — matches how ``matte._OnnxMatter`` writes the cutout."""
    from PIL import Image

    fg = prepare_alpha(fg_rgba, config)
    (nw, nh), box = composite_geometry(bg_rgb.size, fg.size, config)
    if (nw, nh) != fg.size:
        fg = fg.resize((nw, nh), Image.Resampling.LANCZOS)
    base = bg_rgb.convert("RGBA")
    base.alpha_composite(fg, box)
    return base.convert("RGB")


def encode_png_data_uri(image: Any) -> str:
    """A PIL image as a ``data:image/png;base64,...`` URI (the CSP allows
    ``img-src data:`` only, like ``library.thumbnail`` — the page never reads a
    disk path). PNG so a transparent-passthrough preview keeps its alpha."""
    import base64
    import io

    buf = io.BytesIO()
    image.save(buf, "PNG")
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    return "data:image/png;base64," + data
