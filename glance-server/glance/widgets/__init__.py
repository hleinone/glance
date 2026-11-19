"""Widget types, one module each. A module has everything about its type:

    TYPE       its name in config.toml ("hsl_stops")
    SETTINGS   the settings it takes, besides `type`
    parse()    its [widgets.<name>] section -> a frozen settings object, whose
               class carries TYPE, which has a MIN_SIZE (larger for a weather
               widget with a heading), and whose `wants` is what it
               asks a source for, in that source's own type (a stop widget:
               an hsl.StopRequest); sources fetch only what is asked for
    draw()     draw(img, draw, scene, rect, widget name, settings)

MIN_SIZE is the smallest rectangle (px) the type's design reads well in, chosen
by drawing worst-case data (long names, −29°, clock times) at candidate sizes.
Widgets fill bigger rectangles; a layout that gives one less fails when the
config loads, and a test checks nothing is drawn outside the rectangle.

A type is named for its look ("hsl_stops": HSL's pictograms and badge styles)
and knows its data provider in code, so its config never names one. Drawing
every widget can use goes in glance/draw.py, never in another widget's module.
Pictures one widget draws stay in its module (hsl_stops: HSL's pictograms);
pictures a family of widgets shares get a module named for them (fmi_symbols).
"""

from __future__ import annotations

from types import ModuleType

from . import fmi_weather_hourly, fmi_weather_now, hsl_stops, spot_prices, sunrise_sunset
from .fmi_weather_hourly import FmiWeatherHourly
from .fmi_weather_now import FmiWeatherNow
from .hsl_stops import HslStops
from .spot_prices import SpotPrices
from .sunrise_sunset import SunriseSunset

TYPES: dict[str, ModuleType] = {m.TYPE: m for m in (fmi_weather_now, fmi_weather_hourly, hsl_stops, spot_prices, sunrise_sunset)}

Widget = FmiWeatherNow | FmiWeatherHourly | HslStops | SpotPrices | SunriseSunset


def module(widget: Widget) -> ModuleType:
    return TYPES[widget.TYPE]
