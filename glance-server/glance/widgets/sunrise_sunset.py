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
ICON_W = 42  # the horizon icons' width, as tools/build_fmi_icons.py makes them
ICON_GAP = 6
LINE = 18  # the day's length, a line of it
LINE_SIZES = range(16, 12, -1)  # a wording that needs smaller text than these gives way to a shorter one


@dataclass(frozen=True)
class SunriseSunset:
    TYPE: ClassVar[str] = TYPE
    wants: ClassVar[None] = None  # calculated from [location]: no source is asked
    # One column of a six-column screen: the widest time ("04.44") fits beside its sun at 20 px.
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
    x, width = x0 + PAD, x1 - x0 - 2 * PAD
    choices, fallback = line_choices(day, sc.language)
    f, lines = fitting(choices, fallback, width)
    rows = 1 if day.polar else 2
    top = round((y0 + y1) / 2 - (rows * ROW + GAP + len(lines) * LINE) / 2)
    if day.polar:
        word = i18n.t(WORDS, sc.language, "polar_night" if day.polar == "night" else "midnight_sun")
        draw_polar_word(img, d, x, top, width, day.polar == "night", word)
    else:
        times = []
        for when, rising in ((day.sunrise, True), (day.sunset, False)):
            # No sunrise (the sun was up all night) is nothing still to come either.
            past = when is None or sc.now >= to_minute(when)
            times.append((rising, past, i18n.clock(sc.language, to_minute(when)) if when else "–"))
        # Both times in one size, the largest the wider of them fits in.
        time_font = largest("display", [text for _, _, text in times], range(36, 19, -1), width - ICON_W - ICON_GAP)
        for i, (rising, past, text) in enumerate(times):
            draw_time(img, d, x, top + i * ROW, rising, past, text, time_font)
    for i, line in enumerate(lines):
        text_vcenter(d, x, top + rows * ROW + GAP + (i + 0.5) * LINE, line, f, DARK)


def draw_time(img: Image.Image, d: ImageDraw.ImageDraw, x: int, top: int, rising: bool, past: bool, text: str,
              f: ImageFont.FreeTypeFont) -> None:
    """The sun on the horizon, then the time, big."""
    icon = horizon(rising, past)
    paste_icon(img, icon, x, round(top + (ROW - icon.height) / 2))
    text_vcenter(d, x + ICON_W + ICON_GAP, top + ROW / 2, text, f, LIGHT if past else BLACK)


def draw_polar_word(img: Image.Image, d: ImageDraw.ImageDraw, x: int, top: int, width: int, night: bool,
                    word: str) -> None:
    """The polar spell's word, big, beside the sun it waits for when it fits there."""
    beside = width - ICON_W - ICON_GAP
    if font("display", 20).getlength(word) > beside:
        f, word = fit_text(word, "display", 36, 16, width)
        text_vcenter(d, x, top + ROW / 2, word, f, BLACK)
        return
    draw_time(img, d, x, top, night, False, word, fit_text(word, "display", 36, 20, beside)[0])


def line_choices(day: Day, language: str) -> tuple[list[list[str]], list[str]]:
    """What goes below the times: the wordings that say it all, longest first,
    each as its lines; and a shorter one, for when none of those fits."""
    if day.polar and day.polar_ends:
        event = i18n.t(WORDS, language, "sunrise" if day.polar == "night" else "sunset")
        when = i18n.day_month(language, day.polar_ends)
        return [[f"{event} {when}"], [event, when]], [event, when]
    length = i18n.duration(minutes(day.length))
    if day.polar:
        return [[i18n.t(WORDS, language, "length_only", length=length)]], [length]
    change = i18n.duration(minutes(day.change), signed=True)
    one_line = [i18n.t(WORDS, language, "length", length=length, change=change)]
    return [one_line, [length, i18n.t(WORDS, language, "per_day", change=change)]], [length, change]


def fitting(choices: list[list[str]], fallback: list[str], width: int) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Of the wordings, the one that fits at the largest readable size, the
    longer one at the same size; the fallback only when none fits; else the
    fallback cut to fit."""
    for size in LINE_SIZES:
        f = font("medium", size)
        for lines in choices:
            if all(f.getlength(line) <= width for line in lines):
                return f, lines
    for size in LINE_SIZES:
        f = font("medium", size)
        if all(f.getlength(line) <= width for line in fallback):
            return f, fallback
    size = LINE_SIZES[-1]
    return font("medium", size), [fit_text(line, "medium", size, size, width)[1] for line in fallback]


def largest(weight: str, texts: list[str], sizes: range, width: float) -> ImageFont.FreeTypeFont:
    """The largest of the sizes all the texts fit in, else the smallest."""
    for size in sizes:
        f = font(weight, size)
        if all(f.getlength(text) <= width for text in texts):
            return f
    return font(weight, sizes[-1])


def minutes(t: timedelta) -> int:
    return round(t.total_seconds() / 60)
