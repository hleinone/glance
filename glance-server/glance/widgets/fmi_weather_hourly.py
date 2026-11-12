"""fmi_weather_hourly: hour-by-hour forecast from FMI, as many hours as fit:
temperature, icon, chance of precipitation, hour."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from PIL import Image, ImageDraw

from .. import fmi, i18n
from ..draw import BLACK, DARK, font, fit_text, fmt_temp, paste_icon, text_vcenter, weather_icon
from ..layout import Rect
from ..scene import Scene

TYPE = "fmi_weather_hourly"
SETTINGS: set[str] = set()  # besides `type`

# Columns at least this wide: 12 hours in 4 of 6 grid columns, as before
# layouts were configurable.
HOUR_COLUMN_MIN_W = 41


@dataclass(frozen=True)
class FmiWeatherHourly:
    TYPE: ClassVar[str] = TYPE
    MIN_SIZE: ClassVar[tuple[int, int]] = (139, 130)  # 3 hours


def parse(raw: dict) -> FmiWeatherHourly:
    return FmiWeatherHourly()


def hours_fitting(width: int) -> int:
    """Hourly columns in a widget this wide (8 px padding each side)."""
    return max(1, (width - 16) // HOUR_COLUMN_MIN_W)


def draw(img: Image.Image, d: ImageDraw.ImageDraw, sc: Scene, rect: Rect, name: str, w: FmiWeatherHourly) -> None:
    x0, y0, x1, y1 = rect
    pad = 8
    count = hours_fitting(x1 - x0)
    hours = sc.forecast.upcoming(sc.now, count) if sc.forecast else []
    if not hours:
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
        kind, intensity, _ = fmi.symbol_info(h.symbol, sc.language)
        paste_icon(img, weather_icon(h.symbol, kind, intensity, icon_size), round(cx - icon_size / 2), y0 + 42)
        # Every hour, 0 % included: a gap would look like missing data.
        if h.pop is not None:
            pop = i18n.percent(sc.language, round(h.pop / 10) * 10)
            text_vcenter(d, cx, y0 + 96, pop, font("medium", 13), DARK, "m")
        local = h.time.astimezone(sc.now.tzinfo)
        text_vcenter(d, cx, y0 + 124, f"{local.hour:02d}", font("medium", 15), DARK, "m")
