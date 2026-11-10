"""Weather icons.

Weather conditions use FMI's own symbols (`fmi_symbol`), adapted for 4-grey
e-paper and pre-rendered to PNG by tools/build_fmi_icons.py; stop headers use
HSL's mode pictograms (`mode_icon`), pre-rendered by tools/build_hsl_icons.py. The hand-drawn
icons below (`draw`) are the fallback for a code or size without a PNG, and
`wind_arrow` is always drawn here.

Drawn icons are supersampled on a white square, downscaled for smooth edges,
and later snapped to the panel's four grey levels together with the rest of
the screen. Geometry is in unit coordinates (0..1 of the icon size).
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw

FMI_DIR = Path(__file__).resolve().parent.parent / "icons" / "fmi"
HSL_DIR = Path(__file__).resolve().parent.parent / "icons" / "hsl"
MODE_ICON_SIZE = 28
MODES = ("subway", "rail", "tram", "bus", "ferry")


@lru_cache(maxsize=None)
def mode_icon(mode: str) -> Image.Image | None:
    """HSL's pictogram for a vehicle mode ("tram", "bus", "subway", "rail", "ferry"), if any.

    Pre-rendered by tools/build_hsl_icons.py.
    """
    if mode not in MODES:  # the name comes from the API: only ever open our own icons
        return None
    path = HSL_DIR / str(MODE_ICON_SIZE) / f"{mode}.png"
    if not path.exists():
        return None
    with Image.open(path) as im:
        return im.convert("L")


@lru_cache(maxsize=None)
def fmi_symbol(code: int | None, size: int) -> Image.Image | None:
    """FMI's symbol for a WeatherSymbol3 code at a pre-rendered size (76 or 36), if any."""
    if code is None:
        return None
    path = FMI_DIR / str(size) / f"{code}.png"
    if not path.exists():
        return None
    with Image.open(path) as im:
        return im.convert("L")

BLACK, DARK, LIGHT, WHITE = 0, 85, 170, 255
SS = 4  # supersampling factor
STROKE = 0.055  # outline width, fraction of icon size


class _Pen:
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


def _sun(pen: _Pen, cx: float, cy: float, r: float) -> None:
    for i in range(8):
        a = i * math.pi / 4
        pen.line(
            cx + math.cos(a) * r * 1.38, cy + math.sin(a) * r * 1.38,
            cx + math.cos(a) * r * 1.75, cy + math.sin(a) * r * 1.75,
        )
    pen.circle(cx, cy, r, BLACK)
    pen.circle(cx, cy, r - pen.w / pen.s, LIGHT)


def _cloud(pen: _Pen, x: float, y: float, w: float, fill: int) -> None:
    """Cloud with its top-left at (x, y), width w and height 0.62 * w."""
    # Parts in cloud-local units: rounded base + two bumps.
    parts = [("rect", 0.0, 0.32, 1.0, 0.62, 0.15), ("circle", 0.30, 0.36, 0.20), ("circle", 0.62, 0.28, 0.27)]
    for inset, colour in ((0.0, BLACK), (pen.w / pen.s, fill)):
        for part in parts:
            if part[0] == "rect":
                _, x0, y0, x1, y1, r = part
                pen.d.rounded_rectangle(
                    (
                        pen.p(x + x0 * w + inset), pen.p(y + y0 * w + inset),
                        pen.p(x + x1 * w - inset), pen.p(y + y1 * w - inset),
                    ),
                    radius=pen.p(r * w - inset),
                    fill=colour,
                )
            else:
                _, cx, cy, r = part
                pen.circle(x + cx * w, y + cy * w, r * w - inset, colour)


def _drops(pen: _Pen, xs: list[float], y: float) -> None:
    for x in xs:
        pen.line(x, y, x - 0.06, y + 0.19, width=round(pen.w * 1.25))


def _flakes(pen: _Pen, xs: list[float], y: float) -> None:
    for i, x in enumerate(xs):
        pen.circle(x - 0.03, y + 0.07 + (0.07 if i % 2 else 0), 0.062, BLACK)


def _precip_xs(intensity: int) -> list[float]:
    n = {1: 2, 2: 3, 3: 4}.get(intensity, 3)
    span = 0.16 * (n - 1)
    return [0.5 - span / 2 + i * 0.16 for i in range(n)]


def _bolt(pen: _Pen, x: float, y: float, h: float) -> None:
    pts = [(0.55, 0.0), (0.15, 0.55), (0.45, 0.55), (0.30, 1.0), (0.85, 0.38), (0.55, 0.38), (0.75, 0.0)]
    pen.d.polygon([(pen.p(x + px * h * 0.6), pen.p(y + py * h)) for px, py in pts], fill=BLACK)


def draw(kind: str, intensity: int, size: int) -> Image.Image:
    pen = _Pen(size)
    if kind == "clear":
        _sun(pen, 0.5, 0.5, 0.24)
    elif kind == "partly_cloudy":
        _sun(pen, 0.62, 0.36, 0.18)
        _cloud(pen, 0.04, 0.38, 0.78, WHITE)
    elif kind == "cloudy":
        _cloud(pen, 0.30, 0.16, 0.66, LIGHT)
        _cloud(pen, 0.04, 0.36, 0.80, WHITE)
    elif kind in ("rain", "snow", "sleet", "thunder"):
        _cloud(pen, 0.06, 0.10, 0.88, LIGHT)
    elif kind.endswith("_showers"):
        _sun(pen, 0.68, 0.26, 0.15)
        _cloud(pen, 0.04, 0.16, 0.80, LIGHT)
    elif kind == "fog":
        for i, (x0, x1) in enumerate([(0.18, 0.82), (0.10, 0.70), (0.26, 0.90), (0.14, 0.78)]):
            pen.line(x0, 0.26 + i * 0.16, x1, 0.26 + i * 0.16)
    else:  # unknown
        _cloud(pen, 0.06, 0.20, 0.88, WHITE)

    top = 0.70 if not kind.endswith("_showers") else 0.68
    xs = _precip_xs(intensity)
    if kind in ("rain", "rain_showers"):
        _drops(pen, xs, top)
    elif kind in ("snow", "snow_showers"):
        _flakes(pen, xs, top)
    elif kind in ("sleet", "sleet_showers"):
        for i, x in enumerate(xs):
            (_drops if i % 2 == 0 else _flakes)(pen, [x], top)
    elif kind == "thunder":
        _bolt(pen, 0.34, 0.50, 0.48)
    return pen.result(size)


def wind_arrow(direction_from: float, size: int) -> Image.Image:
    """Arrow pointing where the wind blows to (meteorological direction + 180°)."""
    pen = _Pen(size)
    a = math.radians(direction_from + 180)
    # Screen coordinates: 0° = up (north), clockwise.
    dx, dy = math.sin(a), -math.cos(a)
    px, py = -dy, dx
    tip = (0.5 + dx * 0.42, 0.5 + dy * 0.42)
    tail = (0.5 - dx * 0.38, 0.5 - dy * 0.38)
    base = (0.5 + dx * 0.02, 0.5 + dy * 0.02)
    pen.line(tail[0], tail[1], base[0], base[1], width=round(0.14 * pen.s))
    head = [
        tip,
        (base[0] + px * 0.30 - dx * 0.08, base[1] + py * 0.30 - dy * 0.08),
        (base[0] - px * 0.30 - dx * 0.08, base[1] - py * 0.30 - dy * 0.08),
    ]
    pen.d.polygon([(pen.p(x), pen.p(y)) for x, y in head], fill=BLACK)
    return pen.result(size)
