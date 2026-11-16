"""hsl_stops: departures from HSL stops, in HSL's look (mode pictograms, tram
and trunk-route badges), as many as fit. Data from Digitransit (HSL region),
fetched for all stop widgets at once by the server's Transit."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from PIL import Image, ImageDraw

from .. import i18n
from ..draw import BLACK, DARK, LIGHT, WHITE, font, fit_text, paste_icon, text_vcenter
from ..layout import Rect
from ..scene import Scene
from ..sources.hsl import MODE_ORDER, Departure

TYPE = "hsl_stops"
SETTINGS = {"name", "codes", "code", "ids", "id", "routes"}  # besides `type`

# Rows at least this tall: 8 departures in 2 of 3 grid rows, as before layouts
# were configurable.
DEPARTURE_ROW_MIN_H = 32


@dataclass(frozen=True)
class HslStops:
    """One stop or several merged (e.g. both platforms)."""
    TYPE: ClassVar[str] = TYPE
    MIN_SIZE: ClassVar[tuple[int, int]] = (200, 72)  # header + one departure; destinations still readable
    name: str = ""  # "" = use HSL's name for the stop
    ids: tuple[str, ...] = ()  # stop codes ("H0665") and/or gtfsIds ("HSL:1174411")
    routes: frozenset[str] = frozenset()  # empty = all routes


def parse(raw: dict) -> HslStops:
    # `codes` (as on the stop sign, e.g. "H0665") and `ids` (e.g. "HSL:1174411")
    # both end up in ids; codes are resolved to ids at runtime.
    ids = []
    for key in ("codes", "code", "ids", "id"):
        value = raw.get(key, [])
        # str(): an unquoted code (codes = [1234]) is a number in TOML.
        ids.extend(str(v) for v in ([value] if isinstance(value, (str, int)) else value))
    # One route given as text or a number (routes = "550") is that route, not
    # its characters.
    routes = raw.get("routes", [])
    return HslStops(
        name=str(raw.get("name", "")),
        ids=tuple(ids),
        routes=frozenset(str(r) for r in ([routes] if isinstance(routes, (str, int)) else routes)),
    )


def departures_fitting(height: int) -> int:
    """Departure rows in a widget this tall (34 px header, 6 px bottom margin)."""
    return max(1, (height - 40) // DEPARTURE_ROW_MIN_H)


def minutes_text(dep: Departure, sc: Scene) -> tuple[str, str]:
    """(number or label, unit)."""
    if sc.departure_clock:
        return i18n.clock(sc.language, dep.time.astimezone(sc.now.tzinfo)), ""
    minutes = dep.minutes(sc.now)
    if minutes <= 0:
        return i18n.t(sc.language, "now"), ""
    if minutes >= 60:
        return i18n.clock(sc.language, dep.time.astimezone(sc.now.tzinfo)), ""
    return str(minutes), i18n.t(sc.language, "min")


def draw(img: Image.Image, d: ImageDraw.ImageDraw, sc: Scene, rect: Rect, name: str, w: HslStops) -> None:
    stop = sc.stops.get(name)
    if stop is None:
        return
    x0, y0, x1, y1 = rect
    pad = 10
    # Header: HSL's pictogram for each vehicle mode served, then the name.
    x, cy = x0 + pad, y0 + 16
    for mode in stop.modes:
        icon = mode_icon(mode)
        if icon:
            paste_icon(img, icon, x, round(cy - icon.height / 2))
            x += icon.width + 4
    if x > x0 + pad:
        x += 3
    f, name = fit_text(stop.name, "bold", 19, 14, x1 - pad - x)
    text_vcenter(d, x, cy, name, f, BLACK)

    top = y0 + 34
    count = departures_fitting(y1 - y0)
    deps = stop.upcoming(sc.now, count)
    # Old realtime predictions are no better than the timetable: draw them grey.
    realtime_ok = not sc.departures_stale
    if not deps:
        if sc.departures_live and sc.departures_fetched_at is None:
            return  # never fetched: the status bar says why; don't claim "no departures"
        text_vcenter(d, x0 + pad, y0 + 56, i18n.t(sc.language, "no_departures"), font("medium", 16), DARK)
        return

    row_h = (y1 - 6 - top) / count
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
        value, unit = minutes_text(dep, sc)
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


# --- HSL's mode pictograms ---------------------------------------------------

HSL_DIR = Path(__file__).resolve().parent.parent.parent / "icons" / "hsl"
MODE_ICON_SIZE = 28


@lru_cache(maxsize=None)
def mode_icon(mode: str) -> Image.Image | None:
    """HSL's pictogram for a vehicle mode ("tram", "bus", "subway", "rail", "ferry"), if any.

    Pre-rendered by tools/build_hsl_icons.py.
    """
    if mode not in MODE_ORDER:  # the name comes from the API: only ever open our own icons
        return None
    path = HSL_DIR / str(MODE_ICON_SIZE) / f"{mode}.png"
    if not path.exists():
        return None
    with Image.open(path) as im:
        return im.convert("L")
