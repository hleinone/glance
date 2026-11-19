"""Drawing pieces shared by the widgets and the status bar: the panel's four
greys, fonts, fitting text, a widget's heading, pasting icons, and a pen for
drawing small icons.
It imports nothing of ours, so every widget can use it."""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont


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


# The line a heading takes across a widget's top, when the widget has one
# besides its own (a weather widget's place).
HEADING_H = 28


def heading(d: ImageDraw.ImageDraw, x: float, cy: float, text: str, max_width: float) -> None:
    """A widget's name at its top (a stop's, a place's): bold, shrinking, then cut."""
    f, text = fit_text(text, "bold", 19, 14, max_width)
    text_vcenter(d, x, cy, text, f, BLACK)


def fmt_temp(t: float | None) -> str:
    if t is None:
        return "–"
    r = math.floor(t + 0.5)
    return f"−{-r}°" if r < 0 else f"{r}°"


def paste_icon(img: Image.Image, icon: Image.Image, x: int, y: int) -> None:
    box = (x, y, x + icon.width, y + icon.height)
    img.paste(ImageChops.darker(img.crop(box), icon), box)


def quantise(img: Image.Image) -> Image.Image:
    lut = [min(LEVELS, key=lambda lv: abs(lv - v)) for v in range(256)]
    return img.point(lut)


SS = 4  # supersampling factor
STROKE = 0.055  # outline width, fraction of icon size


class Pen:
    """Draws an icon supersampled on a white square, then scales it down for
    smooth edges; it is snapped to the four greys with the rest of the screen.
    Geometry is in unit coordinates (0..1 of the icon size)."""

    def __init__(self, size: int):
        self.s = size * SS
        self.img = Image.new("L", (self.s, self.s), WHITE)
        self.d = ImageDraw.Draw(self.img)
        self.w = max(SS, round(STROKE * self.s))

    def p(self, v: float) -> float:
        return v * self.s

    def circle(self, cx, cy, r, fill):
        s = self.p
        self.d.ellipse((s(cx) - s(r), s(cy) - s(r), s(cx) + s(r), s(cy) + s(r)), fill=fill)

    def line(self, x0, y0, x1, y1, width=None, fill=BLACK):
        w = width or self.w
        s = self.p
        self.d.line((s(x0), s(y0), s(x1), s(y1)), fill=fill, width=w)
        for x, y in ((x0, y0), (x1, y1)):  # round caps
            self.d.ellipse((s(x) - w / 2, s(y) - w / 2, s(x) + w / 2, s(y) + w / 2), fill=fill)

    def result(self, size: int) -> Image.Image:
        return self.img.resize((size, size), Image.Resampling.LANCZOS)
