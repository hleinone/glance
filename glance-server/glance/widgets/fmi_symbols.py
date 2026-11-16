"""FMI's weather symbols, shared by the two FMI weather widgets.

FMI's own WeatherSymbol3 symbols are adapted for 4-grey e-paper and
pre-rendered to PNG by tools/build_fmi_icons.py (`fmi_symbol`). The drawings
below (`drawn`) are the fallback for a code or size without a PNG.
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from PIL import Image

from ..draw import BLACK, LIGHT, WHITE, Pen

FMI_DIR = Path(__file__).resolve().parent.parent.parent / "icons" / "fmi"


def weather_icon(code: int | None, kind: str, intensity: int, size: int) -> Image.Image:
    """FMI's symbol when one is pre-rendered for this code and size, else our own drawing."""
    return fmi_symbol(code, size) or drawn(kind, intensity, size)


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


def _sun(pen: Pen, cx: float, cy: float, r: float) -> None:
    for i in range(8):
        a = i * math.pi / 4
        pen.line(
            cx + math.cos(a) * r * 1.38, cy + math.sin(a) * r * 1.38,
            cx + math.cos(a) * r * 1.75, cy + math.sin(a) * r * 1.75,
        )
    pen.circle(cx, cy, r, BLACK)
    pen.circle(cx, cy, r - pen.w / pen.s, LIGHT)


def _cloud(pen: Pen, x: float, y: float, w: float, fill: int) -> None:
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


def _drops(pen: Pen, xs: list[float], y: float) -> None:
    for x in xs:
        pen.line(x, y, x - 0.06, y + 0.19, width=round(pen.w * 1.25))


def _flakes(pen: Pen, xs: list[float], y: float) -> None:
    for i, x in enumerate(xs):
        pen.circle(x - 0.03, y + 0.07 + (0.07 if i % 2 else 0), 0.062, BLACK)


def _precip_xs(intensity: int) -> list[float]:
    n = {1: 2, 2: 3, 3: 4}.get(intensity, 3)
    span = 0.16 * (n - 1)
    return [0.5 - span / 2 + i * 0.16 for i in range(n)]


def _bolt(pen: Pen, x: float, y: float, h: float) -> None:
    pts = [(0.55, 0.0), (0.15, 0.55), (0.45, 0.55), (0.30, 1.0), (0.85, 0.38), (0.55, 0.38), (0.75, 0.0)]
    pen.d.polygon([(pen.p(x + px * h * 0.6), pen.p(y + py * h)) for px, py in pts], fill=BLACK)


def drawn(kind: str, intensity: int, size: int) -> Image.Image:
    """Our own drawing of a kind of weather, for a code or size without FMI's symbol."""
    pen = Pen(size)
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
