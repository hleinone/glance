"""sunrise_sunset: today's sunrise and sunset at [location], one above the
other, each beside FMI's sun on the horizon (light grey rising, black setting),
and the day's length with its change since yesterday below them. A time
already past, and its sun, are light grey, so the dark one is the one still to
come; a missing sunrise ("–") is grey too. On a polar night or day, the word
for it and when the sun next rises or sets. Calculated, not fetched
(glance/sun.py)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from PIL import Image, ImageDraw, ImageFont

from .. import i18n
from ..draw import BLACK, DARK, LIGHT, fit_text, font, paste_icon, text_vcenter
from ..layout import Rect
from ..scene import Scene
from ..sun import Day

TYPE = "sunrise_sunset"
WORDS = {
    "en": {
        "length": "Day length {length}, {change}", "length_only": "Day length {length}", "per_day": "{change} a day",
        "polar_night": "Polar night", "midnight_sun": "Midnight sun", "sunrise": "Sunrise", "sunset": "Sunset",
    },
    "fi": {
        "length": "Päivän pituus {length}, {change}", "length_only": "Päivän pituus {length}",
        "per_day": "{change} päivässä",
        "polar_night": "Kaamos", "midnight_sun": "Yötön yö", "sunrise": "Aurinko nousee", "sunset": "Aurinko laskee",
    },
}
SETTINGS: set[str] = set()  # besides `type`

# FMI's sun on the horizon, made by tools/build_fmi_icons.py from FMI's symbol 1.
ICONS = Path(__file__).resolve().parent.parent.parent / "icons" / "fmi" / "horizon"
PAD = 8
ROW = 40  # a time and its sun
GAP = 6  # between the rows and the lines below
ICON_GAP = 6
LINE = 18  # the day's length, a line of it
LINE_SIZES = range(16, 12, -1)  # a wording that needs smaller text than these gives way to a shorter one


@dataclass(frozen=True)
class SunriseSunset:
    TYPE: ClassVar[str] = TYPE
    wants: ClassVar[None] = None  # calculated from [location]: no source is asked
    # One column of a six-column screen; "23.59" and the longest polar word fit.
    MIN_SIZE: ClassVar[tuple[int, int]] = (125, 130)


def parse(raw: dict) -> SunriseSunset:
    return SunriseSunset()


def to_minute(t: datetime) -> datetime:
    """The minute a time is shown as, rounded to the nearest."""
    return (t + timedelta(seconds=30)).replace(second=0, microsecond=0)


@lru_cache(maxsize=None)
def horizon(rising: bool, past: bool) -> Image.Image:
    with Image.open(ICONS / f"{'sunrise' if rising else 'sunset'}{'-past' if past else ''}.png") as im:
        return im.convert("L")


def draw(img: Image.Image, d: ImageDraw.ImageDraw, sc: Scene, rect: Rect, name: str, w: SunriseSunset) -> None:
    day = sc.sun
    if day is None:
        raise ValueError("the scene has no sun: scene() fills it from [location]")
    x0, y0, x1, y1 = rect
    width = x1 - x0 - 2 * PAD
    if day.polar:
        word = i18n.t(WORDS, sc.language, "polar_night" if day.polar == "night" else "midnight_sun")
        rows = [(day.polar == "night", False, word)]
    else:
        rows = []
        for when, rising in ((day.sunrise, True), (day.sunset, False)):
            # No sunrise (the sun was up all night) is nothing still to come either.
            past = when is None or sc.now >= to_minute(when)
            rows.append((rising, past, i18n.clock(sc.language, to_minute(when)) if when else "–"))
    f, lines = fitting(line_choices(day, sc.language), width)
    top = round((y0 + y1) / 2 - (len(rows) * ROW + GAP + len(lines) * LINE) / 2)
    for i, (rising, past, text) in enumerate(rows):
        draw_row(img, d, x0 + PAD, top + i * ROW, width, rising, past, text)
    for i, line in enumerate(lines):
        text_vcenter(d, x0 + PAD, top + len(rows) * ROW + GAP + (i + 0.5) * LINE, line, f, DARK)


def draw_row(img: Image.Image, d: ImageDraw.ImageDraw, x: int, top: int, width: int, rising: bool, past: bool,
             text: str) -> None:
    """The sun on the horizon, then the time (or a polar spell's word), big. A
    word too long to fit beside the sun is drawn without it."""
    icon = horizon(rising, past)
    fill = LIGHT if past else BLACK
    beside = width - icon.width - ICON_GAP
    if font("display", 20).getlength(text) > beside:
        f, text = fit_text(text, "display", 36, 16, width)
        text_vcenter(d, x, top + ROW / 2, text, f, fill)
        return
    paste_icon(img, icon, x, round(top + (ROW - icon.height) / 2))
    f, text = fit_text(text, "display", 36, 20, beside)
    text_vcenter(d, x + icon.width + ICON_GAP, top + ROW / 2, text, f, fill)


def line_choices(day: Day, language: str) -> list[list[str]]:
    """What goes below the times, longest wording first, each as its lines."""
    if day.polar and day.polar_ends:
        event = i18n.t(WORDS, language, "sunrise" if day.polar == "night" else "sunset")
        when = i18n.day_month(language, day.polar_ends)
        return [[f"{event} {when}"], [event, when]]
    length = i18n.duration(minutes(day.length))
    if day.polar:
        return [[i18n.t(WORDS, language, "length_only", length=length)], [length]]
    change = i18n.duration(minutes(day.change), signed=True)
    return [
        [i18n.t(WORDS, language, "length", length=length, change=change)],
        [length, i18n.t(WORDS, language, "per_day", change=change)],
        [length, change],
    ]


def fitting(choices: list[list[str]], width: int) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """The first wording whose lines all fit at a readable size; else the last, cut to fit."""
    for lines in choices:
        for size in LINE_SIZES:
            f = font("medium", size)
            if all(f.getlength(line) <= width for line in lines):
                return f, lines
    last = choices[-1]
    size = LINE_SIZES[-1]
    return font("medium", size), [fit_text(line, "medium", size, size, width)[1] for line in last]


def minutes(t: timedelta) -> int:
    return round(t.total_seconds() / 60)
