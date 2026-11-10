"""Render the 800×480 dashboard, snapped to the panel's four grey levels."""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFont

from . import battery, fmi, i18n, icons
from .config import ROOT, Config
from .departures import Departure, StopDepartures

W, H = 800, 480
BAR = 30
COLS, ROWS = 6, 3
ROW_H = (H - BAR) // ROWS  # 150
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
    return ImageFont.truetype(str(ROOT / "fonts" / FONT_FILES[weight]), size)


def col_x(i: int) -> int:
    return round(i * W / COLS)


def row_y(i: int) -> int:
    return BAR + i * ROW_H


def cell(col: int, row: int, cols: int, rows: int) -> tuple[int, int, int, int]:
    return col_x(col), row_y(row), col_x(col + cols), row_y(row + rows)


# --- text helpers ----------------------------------------------------------

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


# --- widgets ---------------------------------------------------------------

@dataclass(frozen=True)
class Scene:
    now: datetime  # local, aware
    language: str
    forecast: fmi.Forecast | None
    weather_stale_after: timedelta
    stops: list[StopDepartures]
    # Live departures: False while all stops show placeholders.
    departures_live: bool = False
    departures_fetched_at: datetime | None = None
    departures_stale_after: timedelta = timedelta(minutes=5)
    battery_percent: int | None = None  # from the device's last report, if recent
    # Departures as clock times (14.32) instead of minutes: when the device
    # updates only every few minutes, countdowns would go stale.
    departure_clock: bool = False

    @property
    def departures_stale(self) -> bool:
        return self.departures_live and (
            self.departures_fetched_at is None
            or self.now - self.departures_fetched_at > self.departures_stale_after
        )


def _status_warning(sc: Scene) -> str | None:
    """Right-hand status bar warning, or None while all data is fresh.

    No "Updated HH:MM" when all is well: data is fetched just before the
    minute, so it would always read one minute behind the clock.
    """
    lang = sc.language
    warnings = []
    fc = sc.forecast
    if fc is None:
        warnings.append(i18n.t(lang, "weather_unavailable"))
    elif sc.now - fc.fetched_at > sc.weather_stale_after:
        minutes = (sc.now - fc.fetched_at).total_seconds() / 60
        warnings.append(i18n.t(lang, "weather_old", age=i18n.age(minutes)))
    if sc.departures_stale:
        if sc.departures_fetched_at is None:
            warnings.append(i18n.t(lang, "departures_unavailable"))
        else:
            minutes = (sc.now - sc.departures_fetched_at).total_seconds() / 60
            warnings.append(i18n.t(lang, "departures_old", age=i18n.age(minutes)))
    if sc.battery_percent is not None and sc.battery_percent <= battery.LOW_PERCENT:
        # The level itself is shown next to the badge; the badge says what to do.
        warnings.append(i18n.t(lang, "battery_low"))
    return " · ".join(warnings) or None


def draw_status_bar(img: Image.Image, d: ImageDraw.ImageDraw, sc: Scene) -> None:
    d.rectangle((0, 0, W, BAR - 1), fill=BLACK)
    lang = sc.language
    left = f"{i18n.date(lang, sc.now)}   {i18n.clock(lang, sc.now)}"
    text_vcenter(d, 10, BAR / 2, left, font("semibold", 17), WHITE)
    right = W - 10
    if sc.battery_percent is not None:
        right = draw_battery(d, right, sc)
    warning = _status_warning(sc)
    if warning:  # inverted so a problem stands out from the black bar
        f = font("semibold", 15)
        tw = f.getlength(warning)
        d.rounded_rectangle((right - tw - 16, 4, right + 4, BAR - 5), radius=4, fill=WHITE)
        text_vcenter(d, right - 4, BAR / 2, warning, f, BLACK, align="r")


def draw_battery(d: ImageDraw.ImageDraw, right: float, sc: Scene) -> float:
    """Battery icon + percentage, right-aligned at `right`; returns the new right edge."""
    f = font("medium", 15)
    text = i18n.percent(sc.language, sc.battery_percent)
    text_vcenter(d, right, BAR / 2, text, f, WHITE, align="r")
    x1 = right - f.getlength(text) - 6
    # Body 22x11 with a nub on the right, filled in proportion to the charge.
    bw, bh = 22, 11
    top = BAR / 2 - bh / 2
    x0 = x1 - 3 - bw
    d.rectangle((x0 + bw + 1, top + 3, x0 + bw + 2, top + bh - 3), fill=WHITE)  # nub
    d.rectangle((x0, top, x0 + bw, top + bh), outline=WHITE, width=1)
    fill_w = round((bw - 4) * max(0, min(100, sc.battery_percent)) / 100)
    if fill_w > 0:
        d.rectangle((x0 + 2, top + 2, x0 + 2 + fill_w - 1, top + bh - 2), fill=WHITE)
    return x0 - 14


def draw_current_weather(img: Image.Image, d: ImageDraw.ImageDraw, sc: Scene) -> None:
    x0, y0, x1, y1 = cell(0, 0, 2, 1)
    pad = 12
    fc = sc.forecast
    hour = fc.current(sc.now) if fc else None
    if hour is None:
        f, text = fit_text(i18n.t(sc.language, "weather_unavailable"), "medium", 16, 12, x1 - x0 - 2 * pad)
        text_vcenter(d, x0 + pad, (y0 + y1) / 2, text, f, DARK)
        return

    kind, intensity, desc = fmi.symbol_info(hour.symbol, sc.language)
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
        text = i18n.t(sc.language, "feels_like", temp=fmt_temp(hour.feels_like))
        text_vcenter(d, x, cy, text, f, BLACK)
        x += f.getlength(text) + 16
    if hour.wind_speed is not None:
        if hour.wind_direction is not None:
            paste_icon(img, icons.wind_arrow(hour.wind_direction, 16), round(x), round(cy - 8))
            x += 20
        text_vcenter(d, x, cy, f"{round(hour.wind_speed)} m/s", f, BLACK)


def draw_hourly(img: Image.Image, d: ImageDraw.ImageDraw, sc: Scene) -> None:
    x0, y0, x1, y1 = cell(2, 0, 4, 1)
    pad = 8
    hours = sc.forecast.upcoming(sc.now, 12) if sc.forecast else []
    if not hours:
        return
    col_w = (x1 - x0 - 2 * pad) / 12
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


def _minutes_text(dep: Departure, sc: Scene) -> tuple[str, str]:
    """(number or label, unit)."""
    if sc.departure_clock:
        return i18n.clock(sc.language, dep.time.astimezone(sc.now.tzinfo)), ""
    minutes = dep.minutes(sc.now)
    if minutes <= 0:
        return i18n.t(sc.language, "now"), ""
    if minutes >= 60:
        return i18n.clock(sc.language, dep.time.astimezone(sc.now.tzinfo)), ""
    return str(minutes), i18n.t(sc.language, "min")


def draw_stop(img: Image.Image, d: ImageDraw.ImageDraw, sc: Scene, col: int, stop: StopDepartures) -> None:
    x0, y0, x1, y1 = cell(col, 1, 2, 2)
    pad = 10
    # Header: HSL's pictogram for each vehicle mode served, then the name.
    x, cy = x0 + pad, y0 + 16
    for mode in stop.modes:
        icon = icons.mode_icon(mode)
        if icon:
            paste_icon(img, icon, x, round(cy - icon.height / 2))
            x += icon.width + 4
    if x > x0 + pad:
        x += 3
    f, name = fit_text(stop.name, "bold", 19, 14, x1 - pad - x)
    text_vcenter(d, x, cy, name, f, BLACK)

    deps = stop.upcoming(sc.now, 8)
    # Old realtime predictions are no better than the timetable: draw them grey.
    realtime_ok = not sc.departures_stale
    if not deps:
        if sc.departures_live and sc.departures_fetched_at is None:
            return  # never fetched: the status bar says why; don't claim "no departures"
        text_vcenter(d, x0 + pad, y0 + 56, i18n.t(sc.language, "no_departures"), font("medium", 16), DARK)
        return

    top = y0 + 34
    row_h = (y1 - 6 - top) / 8
    badge_w, badge_h = 46, 24
    num_font, unit_font = font("bold", 20), font("medium", 13)
    unit_w = max(unit_font.getlength(i18n.t(sc.language, "min")), 0)
    for i, dep in enumerate(deps):
        cy = top + row_h * (i + 0.5)
        bx = x0 + pad
        by = round(cy - badge_h / 2)
        if dep.mode == "tram":
            d.rounded_rectangle((bx, by, bx + badge_w, by + badge_h), radius=5, fill=BLACK)
            route_fill = WHITE
        elif dep.trunk:  # HSL's orange trunk buses: light grey between bus white and tram black
            d.rounded_rectangle((bx, by, bx + badge_w, by + badge_h), radius=5, fill=LIGHT, outline=BLACK, width=2)
            route_fill = BLACK
        else:
            d.rounded_rectangle((bx, by, bx + badge_w, by + badge_h), radius=5, outline=BLACK, width=2)
            route_fill = BLACK
        rf, route = fit_text(dep.route, "bold", 17, 11, badge_w - 6)
        text_vcenter(d, bx + badge_w / 2, cy, route, rf, route_fill, "m")

        # Minutes, right-aligned: the number column lines up, the unit sits after it.
        colour = BLACK if dep.realtime and realtime_ok else DARK
        value, unit = _minutes_text(dep, sc)
        right = x1 - pad
        if unit:
            text_vcenter(d, right, cy + 2, unit, unit_font, colour, "r")
            num_right = right - unit_w - 3
        else:
            num_right = right
        text_vcenter(d, num_right, cy, value, num_font, colour, "r")
        minutes_left = num_right - max(num_font.getlength(value), num_font.getlength("00"))

        dx = bx + badge_w + 8
        f, dest = fit_text(dep.headsign, "medium", 16, 13, minutes_left - 8 - dx)
        text_vcenter(d, dx, cy, dest, f, BLACK)


def render(cfg: Config, sc: Scene) -> Image.Image:
    img = Image.new("L", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    draw_status_bar(img, d, sc)
    draw_current_weather(img, d, sc)
    draw_hourly(img, d, sc)
    for i, stop in enumerate(sc.stops[:3]):
        draw_stop(img, d, sc, i * 2, stop)
    return quantise(img)


def to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    # Level 6, not optimize=True: on a Pi 1 that is 0.16 s instead of 1.1 s,
    # for a file only ~6 % bigger.
    img.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


def to_g2(img: Image.Image) -> bytes:
    """The panel's own format: 2 bits per pixel, 4 pixels per byte, first pixel
    in the top bits, rows top to bottom; 0 = black .. 3 = white (= the
    firmware's TFT_GRAY_0..3). 96,000 bytes for 800x480; the device copies it
    straight into its frame buffer instead of decoding a PNG (2.2 s on the ESP32).
    """
    index = img.point([min(3, (v + 42) // 85) for v in range(256)])
    # Pillow 11 (the Pi's) has no 2-bit packer for mode L, but has one for P.
    return Image.frombytes("P", img.size, index.tobytes()).tobytes("raw", "P;2")


# Image formats served, by URL path: encoder and Content-Type.
FORMATS = {
    "/glance.png": (to_png, "image/png"),
    "/glance.g2": (to_g2, "application/octet-stream"),
}
