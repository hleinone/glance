"""Drawing pieces shared by the widgets and the status bar: the panel's four
greys, fonts, fitting text, icons."""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

from . import icons

FONTS = Path(__file__).resolve().parent.parent / "fonts"

BLACK, DARK, LIGHT, WHITE = 0, 85, 170, 255
LEVELS = (BLACK, DARK, LIGHT, WHITE)

FONT_FILES = {
    "regular": "Inter-Regular.ttf",
    "medium": "Inter-Medium.ttf",
    "semibold": "Inter-SemiBold.ttf",
    "bold": "Inter-Bold.ttf",
    "display": "InterDisplay-SemiBold.ttf",
}


@lru_cache(maxsize=None)
def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / FONT_FILES[weight]), size)


def fit_text(
    text: str, weight: str, size: int, min_size: int, max_width: float
) -> tuple[ImageFont.FreeTypeFont, str]:
    """Shrink down to min_size, then truncate with an ellipsis."""
    for s in range(size, min_size - 1, -1):
        f = font(weight, s)
        if f.getlength(text) <= max_width:
            return f, text
    f = font(weight, min_size)
    while text and f.getlength(text.rstrip() + "…") > max_width:
        text = text[:-1]
    return f, text.rstrip() + "…"


def text_vcenter(
    d: ImageDraw.ImageDraw, x: float, cy: float, text: str, f: ImageFont.FreeTypeFont,
    fill: int, align: str = "l",
) -> None:
    """Draw text with its cap height centred on cy.

    Centring on cap height (not each string's own ink) keeps baselines aligned
    across widgets whatever accents or descenders the text has, and digits
    still look centred.
    """
    anchor = f"{align}s"
    cap = -f.getbbox("H", anchor="ls")[1]
    d.text((x, cy + cap / 2), text, font=f, fill=fill, anchor=anchor)


def fmt_temp(t: float | None) -> str:
    if t is None:
        return "–"
    r = math.floor(t + 0.5)
    return f"−{-r}°" if r < 0 else f"{r}°"


def paste_icon(img: Image.Image, icon: Image.Image, x: int, y: int) -> None:
    box = (x, y, x + icon.width, y + icon.height)
    img.paste(ImageChops.darker(img.crop(box), icon), box)


def weather_icon(code: int | None, kind: str, intensity: int, size: int) -> Image.Image:
    """FMI's symbol when one is pre-rendered for this code and size, else our own drawing."""
    return icons.fmi_symbol(code, size) or icons.draw(kind, intensity, size)


def quantise(img: Image.Image) -> Image.Image:
    lut = [min(LEVELS, key=lambda lv: abs(lv - v)) for v in range(256)]
    return img.point(lut)
