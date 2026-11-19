"""fmi_weather_hourly: hour-by-hour forecast from FMI, as many hours as fit:
temperature, icon, chance of precipitation, hour; at [location] or at the town
in `place` (or at `lat`, `lon`), under `name` if it has one."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from PIL import Image, ImageDraw

from .. import i18n
from ..draw import BLACK, DARK, HEADING_H, font, fit_text, fmt_temp, heading, paste_icon, text_vcenter
from ..layout import Rect
from ..scene import Scene
from ..sources.fmi import HOME, ForecastRequest, parse_request
from .fmi_symbols import symbol_info, weather_icon

TYPE = "fmi_weather_hourly"
WORDS = {
    "en": {"not_found": "Place “{place}” not found"},
    "fi": {"not_found": "Paikkaa ”{place}” ei löytynyt"},
}
SETTINGS = {"place", "lat", "lon", "name"}  # besides `type`

# Columns at least this wide: 12 hours in 4 of 6 grid columns, as before
# layouts were configurable.
HOUR_COLUMN_MIN_W = 41


@dataclass(frozen=True)
class FmiWeatherHourly:
    TYPE: ClassVar[str] = TYPE
    wants: ForecastRequest = HOME

    @property
    def MIN_SIZE(self) -> tuple[int, int]:
        # 3 hours, under the heading if any.
        return 139, 130 + (HEADING_H if self.wants.name else 0)


def parse(raw: dict) -> FmiWeatherHourly:
    return FmiWeatherHourly(parse_request(raw))


def hours_fitting(width: int) -> int:
    """Hourly columns in a widget this wide (8 px padding each side)."""
    return max(1, (width - 16) // HOUR_COLUMN_MIN_W)


def draw(img: Image.Image, d: ImageDraw.ImageDraw, sc: Scene, rect: Rect, name: str, w: FmiWeatherHourly) -> None:
    x0, y0, x1, y1 = rect
    pad = 8
    if w.wants.name:
        heading(d, x0 + pad, y0 + HEADING_H / 2 + 1, w.wants.name, x1 - x0 - 2 * pad)
        y0 += HEADING_H
    count = hours_fitting(x1 - x0)
    fc = sc.forecasts.get(w.wants)
    hours = fc.upcoming(sc.now, count) if fc else []
    if not hours:
        # Otherwise empty: the status bar says the weather is missing.
        if w.wants in sc.places_not_found:
            text = i18n.t(WORDS, sc.language, "not_found", place=w.wants.town)
            f, text = fit_text(text, "medium", 16, 12, x1 - x0 - 2 * pad)
            text_vcenter(d, x0 + pad, (y0 + y1) / 2, text, f, DARK)
        return
    col_w = (x1 - x0 - 2 * pad) / count
    icon_size = 36
    # One size for every column, so "−20°" columns don't touch their neighbours.
    temps = [fmt_temp(h.temperature) for h in hours]
    widest = max(temps, key=lambda s: font("semibold", 18).getlength(s))
    temp_font, _ = fit_text(widest, "semibold", 18, 12, col_w - 5)
    for i, h in enumerate(hours):
        cx = x0 + pad + col_w * (i + 0.5)
        text_vcenter(d, cx, y0 + 24, temps[i], temp_font, BLACK, "m")
        kind, intensity, _ = symbol_info(h.symbol, sc.language)
        paste_icon(img, weather_icon(h.symbol, kind, intensity, icon_size), round(cx - icon_size / 2), y0 + 42)
        # Every hour, 0 % included: a gap would look like missing data.
        if h.pop is not None:
            pop = i18n.percent(sc.language, round(h.pop / 10) * 10)
            text_vcenter(d, cx, y0 + 96, pop, font("medium", 13), DARK, "m")
        local = h.time.astimezone(sc.now.tzinfo)
        text_vcenter(d, cx, y0 + 124, f"{local.hour:02d}", font("medium", 15), DARK, "m")
