"""Composite call-to-action text onto a cover image.

Decision 11: the model draws the picture, Pillow draws the words. Diffusion
models render text unreliably — misspellings and mangled phone numbers are
common — and the phone number is the one thing on the ad that must be exactly
right. Compositing also means changing the wording or the number is a free
re-render rather than a paid regeneration.

Templates are stored as JSON so the layout can be edited without a deploy.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

from loguru import logger
from PIL import Image, ImageDraw, ImageFont

# DejaVu ships with the Pillow wheels' Debian base; fall back to the bitmap
# default rather than failing to render at all.
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


@dataclass
class OverlayTemplate:
    """A stack of text lines drawn over a translucent band.

    Positions are fractions of image height so a template works at any size.
    """
    lines: list[str] = field(default_factory=list)
    position: str = "bottom"          # top | bottom
    band_opacity: int = 165           # 0-255; 0 disables the band
    band_colour: tuple[int, int, int] = (10, 20, 35)
    text_colour: tuple[int, int, int] = (255, 255, 255)
    accent_colour: tuple[int, int, int] = (255, 196, 60)
    # Height of the first line as a fraction of image height. Later lines are
    # drawn at 70% of it.
    title_scale: float = 0.085
    padding_scale: float = 0.035

    @classmethod
    def from_dict(cls, d: dict) -> "OverlayTemplate":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    logger.warning("no TrueType font found; falling back to the bitmap default")
    return ImageFont.load_default()


def _fit(draw: ImageDraw.ImageDraw, text: str, target_px: int, max_width: int):
    """Largest font that keeps `text` inside `max_width`, capped at target_px."""
    size = target_px
    while size > 8:
        f = _font(size)
        if draw.textlength(text, font=f) <= max_width:
            return f
        size = int(size * 0.92)
    return _font(8)


def render(image_bytes: bytes, template: OverlayTemplate, tokens: dict[str, str]) -> bytes:
    """Draw the template over the image. Returns JPEG bytes.

    Token substitution happens here, so `{phone}` in a template becomes the
    draft's actual number and stays exact.
    """
    lines = [
        line.format(**{k: v or "" for k, v in tokens.items()})
        for line in template.lines
        if line.strip()
    ]
    src = Image.open(io.BytesIO(image_bytes))
    img = src.convert("RGB")
    if not lines:
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=88)
        return out.getvalue()

    W, H = img.size
    pad = int(H * template.padding_scale)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    fonts, heights = [], []
    for i, line in enumerate(lines):
        target = int(H * template.title_scale * (1.0 if i == 0 else 0.7))
        f = _fit(draw, line, target, W - 2 * pad)
        fonts.append(f)
        box = draw.textbbox((0, 0), line, font=f)
        heights.append(box[3] - box[1])

    gap = int(pad * 0.35)
    block = sum(heights) + gap * (len(lines) - 1)
    band_h = block + pad * 2

    if template.position == "top":
        band_y0, text_y = 0, pad
    else:
        band_y0, text_y = H - band_h, H - band_h + pad

    if template.band_opacity > 0:
        draw.rectangle(
            [0, band_y0, W, band_y0 + band_h],
            fill=(*template.band_colour, template.band_opacity),
        )

    for i, (line, f, h) in enumerate(zip(lines, fonts, heights)):
        colour = template.text_colour if i == 0 else template.accent_colour
        w = draw.textlength(line, font=f)
        draw.text(((W - w) / 2, text_y), line, font=f, fill=(*colour, 255))
        text_y += h + gap

    composed = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    out = io.BytesIO()
    composed.save(out, format="JPEG", quality=88)
    return out.getvalue()


DEFAULT_TEMPLATE = OverlayTemplate(
    lines=["FREE ROOF INSPECTION", "{phone}", "Licensed & Insured  Lic #{license}"],
)
