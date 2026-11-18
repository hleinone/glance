"""sunrise_sunset: today's sunrise and sunset at [location], and the day's
length with its change since yesterday. A time already past is light grey, so
the dark one is the one still to come; a missing sunrise ("–") is grey too. On a polar night or day, the word for
it and when the sun next rises or sets. Calculated, not fetched (glance/sun.py)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import ClassVar

from PIL import Image, ImageDraw

from .. import i18n
from ..draw import BLACK, DARK, LIGHT, Pen, fit_text, paste_icon, text_vcenter
from ..layout import Rect
from ..scene import Scene
from ..sun import Day

TYPE = "sunrise_sunset"
WORDS = {
    "en": {
        "length": "Day length {length}, {change}", "length_only": "Day length {length}",
        "polar_night": "Polar night", "midnight_sun": "Midnight sun",
        "next_sunrise": "Sunrise {date}", "next_sunset": "Sunset {date}",
    },
    "fi": {
        "length": "Päivän pituus {length}, {change}", "length_only": "Päivän pituus {length}",
        "polar_night": "Kaamos", "midnight_sun": "Yötön yö",
        "next_sunrise": "Aurinko nousee {date}", "next_sunset": "Aurinko laskee {date}",
    },
}
SETTINGS: set[str] = set()  # besides `type`

PAD = 12
ICON = 30
TIME_ROW = 44  # the times' row, icons included
LINE_GAP = 12  # between the times and the day's length
LINE = 16


@dataclass(frozen=True)
class SunriseSunset:
    TYPE: ClassVar[str] = TYPE
    wants: ClassVar[None] = None  # calculated from [location]: no source is asked
    MIN_SIZE: ClassVar[tuple[int, int]] = (240, 80)  # "23.59" twice, and the longest day-length line


def parse(raw: dict) -> SunriseSunset:
    return SunriseSunset()


def to_minute(t: datetime) -> datetime:
    """The minute a time is shown as, rounded to the nearest."""
    return (t + timedelta(seconds=30)).replace(second=0, microsecond=0)


def draw(img: Image.Image, d: ImageDraw.ImageDraw, sc: Scene, rect: Rect, name: str, w: SunriseSunset) -> None:
    day = sc.sun
    if day is None:
        raise ValueError("the scene has no sun: scene() fills it from [location]")
    x0, y0, x1, y1 = rect
    top = round((y0 + y1) / 2 - (TIME_ROW + LINE_GAP + LINE) / 2)
    width = x1 - x0 - 2 * PAD
    if day.polar:
        draw_polar(img, d, sc, day, x0 + PAD, top, width)
        return
    half = width // 2
    for i, (when, rising) in enumerate(((day.sunrise, True), (day.sunset, False))):
        # No sunrise (the sun was up all night) is nothing still to come either.
        past = when is None or sc.now >= to_minute(when)
        text = i18n.clock(sc.language, to_minute(when)) if when else "–"
        draw_time(img, d, x0 + PAD + i * half, top, half - 8, rising, text, LIGHT if past else BLACK)
    length = i18n.duration(minutes(day.length))
    line = i18n.t(WORDS, sc.language, "length", length=length, change=i18n.duration(minutes(day.change), signed=True))
    draw_line(d, x0 + PAD, top + TIME_ROW + LINE_GAP + LINE / 2, line, width)


def draw_polar(img: Image.Image, d: ImageDraw.ImageDraw, sc: Scene, day: Day, x: int, top: int, width: int) -> None:
    night = day.polar == "night"
    draw_time(img, d, x, top, width, night, i18n.t(WORDS, sc.language, "polar_night" if night else "midnight_sun"), BLACK)
    if day.polar_ends:
        key = "next_sunrise" if night else "next_sunset"
        line = i18n.t(WORDS, sc.language, key, date=i18n.day_month(sc.language, day.polar_ends))
    else:
        line = i18n.t(WORDS, sc.language, "length_only", length=i18n.duration(minutes(day.length)))
    draw_line(d, x, top + TIME_ROW + LINE_GAP + LINE / 2, line, width)


def draw_time(img: Image.Image, d: ImageDraw.ImageDraw, x: int, top: int, width: int, rising: bool, text: str,
              fill: int) -> None:
    """The sun on the horizon, then the time (or a polar spell's word), big."""
    paste_icon(img, horizon_sun(rising, ICON, fill), x, round(top + (TIME_ROW - ICON) / 2))
    f, text = fit_text(text, "display", 38, 20, width - ICON - 8)
    text_vcenter(d, x + ICON + 8, top + TIME_ROW / 2, text, f, fill)


def draw_line(d: ImageDraw.ImageDraw, x: int, cy: float, text: str, width: int) -> None:
    f, text = fit_text(text, "medium", LINE, 12, width)
    text_vcenter(d, x, cy, text, f, DARK)


def minutes(t: timedelta) -> int:
    return round(t.total_seconds() / 60)


def horizon_sun(rising: bool, size: int, fill: int) -> Image.Image:
    """Half a sun on the horizon, with an arrow above it: up for sunrise, down for sunset."""
    pen = Pen(size)
    horizon, r = 0.86, 0.3
    s = pen.p
    pen.d.pieslice((s(0.5 - r), s(horizon - r), s(0.5 + r), s(horizon + r)), 180, 360, fill=BLACK)
    pen.line(0.06, horizon, 0.94, horizon, width=round(0.1 * pen.s))
    tip, tail = (0.06, 0.4) if rising else (0.4, 0.06)
    pen.line(0.5, tail, 0.5, tip, width=round(0.1 * pen.s))
    head = 0.14 if rising else -0.14
    pen.d.polygon([(s(0.5), s(tip - 0.04 * (1 if rising else -1))), (s(0.5 - 0.16), s(tip + head)),
                   (s(0.5 + 0.16), s(tip + head))], fill=BLACK)
    icon = pen.result(size)
    return icon if fill == BLACK else icon.point(lambda v: max(v, fill))
