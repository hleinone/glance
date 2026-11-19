"""fmi_weather_now: current weather from FMI: icon, temperature, today's high
and low, condition, feels like and wind, at [location] or at the town in
`place` (or at `lat`, `lon`), under `name` if it has one. One design; in a
bigger area it sits at the top left."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar

from PIL import Image, ImageDraw

from .. import i18n
from ..draw import BLACK, DARK, HEADING_H, Pen, font, fit_text, fmt_temp, heading, paste_icon, text_vcenter
from ..layout import Rect
from ..scene import Scene
from ..sources.fmi import HOME, ForecastRequest, parse_request
from .fmi_symbols import symbol_info, weather_icon

TYPE = "fmi_weather_now"
WORDS = {
    "en": {"weather_unavailable": "Weather unavailable", "feels_like": "Feels like {temp}",
           "not_found": "Place “{place}” not found"},
    "fi": {"weather_unavailable": "Säätietoja ei saatavilla", "feels_like": "Tuntuu kuin {temp}",
           "not_found": "Paikkaa ”{place}” ei löytynyt"},
}
SETTINGS = {"place", "lat", "lon", "name"}  # besides `type`


@dataclass(frozen=True)
class FmiWeatherNow:
    TYPE: ClassVar[str] = TYPE
    wants: ForecastRequest = HOME

    @property
    def MIN_SIZE(self) -> tuple[int, int]:
        # "−29°" untruncated next to the high/low, under the heading if any.
        return 245, 140 + (HEADING_H if self.wants.name else 0)


def parse(raw: dict) -> FmiWeatherNow:
    return FmiWeatherNow(parse_request(raw))


def draw(img: Image.Image, d: ImageDraw.ImageDraw, sc: Scene, rect: Rect, name: str, w: FmiWeatherNow) -> None:
    x0, y0, x1, y1 = rect
    pad = 12
    if w.wants.name:
        heading(d, x0 + pad, y0 + HEADING_H / 2 + 1, w.wants.name, x1 - x0 - 2 * pad)
        y0 += HEADING_H
    fc = sc.forecasts.get(w.wants)
    hour = fc.current(sc.now) if fc else None
    if hour is None:
        key = "not_found" if w.wants in sc.places_not_found else "weather_unavailable"
        f, text = fit_text(i18n.t(WORDS, sc.language, key, place=w.wants.town), "medium", 16, 12, x1 - x0 - 2 * pad)
        text_vcenter(d, x0 + pad, (y0 + y1) / 2, text, f, DARK)
        return

    kind, intensity, desc = symbol_info(hour.symbol, sc.language)
    icon_size = 76
    paste_icon(img, weather_icon(hour.symbol, kind, intensity, icon_size), x0 + pad - 4, y0 + 8)

    # Temperature right of the icon, with today's high/low stacked right after
    # it. The temperature shrinks rather than overlap the icon or the high/low.
    tx = x0 + pad - 4 + icon_size + 8
    cy_top = y0 + 8 + icon_size / 2
    day_start = sc.now.replace(hour=0, minute=0, second=0, microsecond=0)
    rng = fc.day_range(day_start, day_start + timedelta(days=1))
    range_font = font("semibold", 17)
    # High above low; the low in dark grey tells them apart without arrows.
    range_lines = [fmt_temp(rng[1]), fmt_temp(rng[0])] if rng else []
    range_w = max((range_font.getlength(s) for s in range_lines), default=0)
    gap = 10 if range_lines else 0
    f, temp = fit_text(fmt_temp(hour.temperature), "display", 66, 40, x1 - pad - tx - gap - range_w)
    text_vcenter(d, tx, cy_top, temp, f, BLACK)
    if range_lines:
        rx = tx + f.getlength(temp) + gap
        text_vcenter(d, rx, cy_top - 12, range_lines[0], range_font, BLACK)
        text_vcenter(d, rx, cy_top + 12, range_lines[1], range_font, DARK)

    f, desc = fit_text(desc, "semibold", 19, 14, x1 - x0 - 2 * pad)
    text_vcenter(d, x0 + pad, y0 + 104, desc, f, BLACK)

    # "Feels like" and wind.
    f = font("medium", 16)
    x, cy = x0 + pad, y0 + 131
    if hour.feels_like is not None:
        text = i18n.t(WORDS, sc.language, "feels_like", temp=fmt_temp(hour.feels_like))
        text_vcenter(d, x, cy, text, f, BLACK)
        x += f.getlength(text) + 16
    if hour.wind_speed is not None:
        if hour.wind_direction is not None:
            paste_icon(img, wind_arrow(hour.wind_direction, 16), round(x), round(cy - 8))
            x += 20
        text_vcenter(d, x, cy, f"{round(hour.wind_speed)} m/s", f, BLACK)


def wind_arrow(direction_from: float, size: int) -> Image.Image:
    """Arrow pointing where the wind blows to (meteorological direction + 180°)."""
    pen = Pen(size)
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
