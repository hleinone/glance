"""Widget types, one module each. A module has everything about its type:

    TYPE       its name in config.toml ("hsl_stops")
    SETTINGS   the settings it takes, besides `type`
    parse()    its [widgets.<name>] section -> a frozen settings object, whose
               class carries TYPE and MIN_SIZE
    draw()     draw(img, draw, scene, rect, widget name, settings)

MIN_SIZE is the smallest rectangle (px) the type's design reads well in, chosen
by drawing worst-case data (long names, −29°, clock times) at candidate sizes.
Widgets fill bigger rectangles; a layout that gives one less fails when the
config loads, and a test checks nothing is drawn outside the rectangle.

A type is named for its look ("hsl_stops": HSL's pictograms and badge styles)
and knows its data provider in code, so its config never names one. Shared
drawing pieces go in glance/draw.py, not in another widget's module.
"""

from __future__ import annotations

from types import ModuleType

from . import fmi_weather_hourly, fmi_weather_now, hsl_stops
from .fmi_weather_hourly import FmiWeatherHourly
from .fmi_weather_now import FmiWeatherNow
from .hsl_stops import HslStops

TYPES: dict[str, ModuleType] = {m.TYPE: m for m in (fmi_weather_now, fmi_weather_hourly, hsl_stops)}

Widget = FmiWeatherNow | FmiWeatherHourly | HslStops


def module(widget: Widget) -> ModuleType:
    return TYPES[widget.TYPE]
