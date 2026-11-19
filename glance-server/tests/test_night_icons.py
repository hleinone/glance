"""Night icons in the two weather widgets: a moon in place of the sun in the
hours FMI marks as dark at the forecast's place."""

import io
import json
import tempfile
import unittest
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from glance import render, widgets
from glance.sources import fmi
from glance.widgets import fmi_symbols, fmi_weather_hourly, fmi_weather_now
from test_glance import DATA, HELSINKI

# FMI's real answer for the example place (Pasila) with SmartSymbol, saved
# 2026-11-19 in the evening: from that day's midnight to 36 hours ahead.
NIGHT_SAMPLE = (DATA / "fmi_night.xml").read_bytes()
# The WeatherSymbol3 codes whose picture shows a sun, read off FMI's symbols:
# clear, partly cloudy, and the showers of rain, snow, thunder and sleet.
WITH_SUN = {1, 2, 21, 22, 23, 41, 42, 43, 61, 62, 71, 72, 73}
MIKKELI = fmi.ForecastRequest(town="Mikkeli")


def answer(*elements: tuple[str, str, str]) -> bytes:
    """An FMI answer of (time, parameter, value) elements."""
    members = "".join(
        f"<wfs:member><BsWfs:BsWfsElement><BsWfs:Time>{time}</BsWfs:Time>"
        f"<BsWfs:ParameterName>{name}</BsWfs:ParameterName>"
        f"<BsWfs:ParameterValue>{value}</BsWfs:ParameterValue></BsWfs:BsWfsElement></wfs:member>"
        for time, name, value in elements)
    return (f'<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" '
            f'xmlns:BsWfs="http://xml.fmi.fi/schema/wfs/2.0">{members}</wfs:FeatureCollection>').encode()


class NightFromFMITest(unittest.TestCase):
    def test_the_hours_fmi_marks_dark_are_night(self):
        # In Pasila on 2026-11-19 the sun rose at 8.29 and set at 15.41, so the
        # hours from 9 to 15 o'clock are day, and the rest of the day night.
        hours = [h for h in fmi.parse(NIGHT_SAMPLE) if h.time.astimezone(HELSINKI).date() == date(2026, 11, 19)]
        self.assertEqual(len(hours), 24)
        self.assertEqual([h.time.astimezone(HELSINKI).hour for h in hours if not h.night], list(range(9, 16)))

    def test_only_a_night_code_makes_an_hour_night(self):
        values = ["7.0", "107.0", "100", "99", "NaN", "1e300", "-107"]
        hours = fmi.parse(answer(
            *((f"2026-11-19T{i:02}:00:00Z", "SmartSymbol", v) for i, v in enumerate(values)),
            ("2026-11-19T20:00:00Z", "WeatherSymbol3", "1.0"),  # an hour without one
        ))
        self.assertEqual([h.night for h in hours], [False, True, True, False, False, False, False, False])

    def test_fmi_is_asked_for_the_night_code(self):
        with mock.patch("glance.upstream._open", return_value=io.BytesIO(NIGHT_SAMPLE)) as opened:
            forecast = fmi.fetch(fmi.latlon(60.1987, 24.9327), HELSINKI)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(opened.call_args.args[0].full_url).query)
        self.assertIn("SmartSymbol", query["parameters"][0].split(","))
        self.assertTrue(any(h.night for h in forecast.hours))


class NightCacheTest(unittest.TestCase):
    def test_a_forecast_keeps_its_night_through_the_cache(self):
        forecast = fmi.Forecast(datetime(2026, 11, 19, 17, tzinfo=HELSINKI), fmi.parse(NIGHT_SAMPLE))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "forecast.json"
            fmi.save_cache(forecast, path)
            self.assertEqual(fmi.load_cache(path), forecast)

    def test_a_cache_from_before_loads_as_day_and_is_refetched(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "forecast.json"
            path.write_text(json.dumps({
                "fetched_at": "2026-11-19T15:00:00+00:00",
                "parameters": ["Temperature", "WindSpeedMS", "WindDirection", "PoP", "WeatherSymbol3", "FeelsLike"],
                "hours": [{"time": "2026-11-19T20:00:00+00:00", "temperature": 1.0, "wind_speed": 3.0,
                           "wind_direction": 180.0, "pop": 10.0, "symbol": 1, "feels_like": -2.0}],
            }))
            forecast = fmi.load_cache(path)
        self.assertFalse(forecast.hours[0].night)
        self.assertNotEqual(forecast.parameters, fmi.PARAMETERS)  # so the server refetches at once


class NightIconTest(unittest.TestCase):
    def icon(self, code: int, size: int, night: bool) -> Image.Image:
        kind, intensity, _ = fmi_symbols.symbol_info(code, "en")
        return fmi_symbols.weather_icon(code, kind, intensity, size, night)

    def test_a_night_hour_shows_the_moon_where_the_day_shows_a_sun(self):
        for code in WITH_SUN:
            for size in (76, 36):
                with self.subTest(code=code, size=size):
                    night = self.icon(code, size, night=True)
                    self.assertNotEqual(night.tobytes(), self.icon(code, size, night=False).tobytes())
                    with Image.open(fmi_symbols.FMI_DIR / str(size) / f"{code}-night.png") as moon:
                        self.assertEqual(night.tobytes(), moon.convert("L").tobytes())

    def test_weather_without_a_sun_looks_the_same_at_night(self):
        for code in set(fmi_symbols.SYMBOLS) - WITH_SUN:
            for size in (76, 36):
                with self.subTest(code=code, size=size):
                    self.assertEqual(self.icon(code, size, night=True).tobytes(),
                                     self.icon(code, size, night=False).tobytes())

    def test_only_the_symbols_with_a_sun_have_a_night_icon(self):
        for size in (76, 36):
            made = {int(p.name.removesuffix("-night.png")) for p in (fmi_symbols.FMI_DIR / str(size)).glob("*-night.png")}
            self.assertEqual(made, WITH_SUN, size)

    def test_an_unknown_code_at_night_is_still_drawn(self):
        self.assertEqual(fmi_symbols.weather_icon(999, "unknown", 1, 50, night=True).size, (50, 50))


class NightWidgetsTest(unittest.TestCase):
    NOW = datetime(2026, 11, 19, 18, 10, tzinfo=HELSINKI)

    def forecast(self, symbol: int, night: bool) -> fmi.Forecast:
        hours = [fmi.Hour(self.NOW.replace(minute=0) + timedelta(hours=i), -2.0, 3.0, 90.0, 10.0, symbol, -5.0, night)
                 for i in range(-2, 14)]
        return fmi.Forecast(self.NOW, hours)

    def drawn(self, module, widget: widgets.Widget, forecasts: dict) -> bytes:
        sc = render.Scene(now=self.NOW, language="en", stops={}, forecasts=forecasts)
        img = Image.new("L", (560, 160), 255)
        module.draw(img, ImageDraw.Draw(img), sc, (0, 0, 560, 160), "w", widget)
        return img.tobytes()

    def test_both_widgets_draw_the_moon_in_a_dark_hour(self):
        for module, widget in ((fmi_weather_now, widgets.FmiWeatherNow()), (fmi_weather_hourly, widgets.FmiWeatherHourly())):
            with self.subTest(module.__name__):
                day = self.drawn(module, widget, {fmi.HOME: self.forecast(1, night=False)})
                night = self.drawn(module, widget, {fmi.HOME: self.forecast(1, night=True)})
                self.assertNotEqual(night, day)
                # Cloudy has no sun, so nothing changes at night.
                self.assertEqual(self.drawn(module, widget, {fmi.HOME: self.forecast(3, night=True)}),
                                 self.drawn(module, widget, {fmi.HOME: self.forecast(3, night=False)}))

    def test_another_place_follows_its_own_night(self):
        dark_there = {fmi.HOME: self.forecast(1, night=False), MIKKELI: self.forecast(1, night=True)}
        for module, kind in ((fmi_weather_now, widgets.FmiWeatherNow), (fmi_weather_hourly, widgets.FmiWeatherHourly)):
            with self.subTest(module.__name__):
                self.assertEqual(self.drawn(module, kind(MIKKELI), dark_there),
                                 self.drawn(module, kind(), {fmi.HOME: self.forecast(1, night=True)}))
                self.assertEqual(self.drawn(module, kind(), dark_there),
                                 self.drawn(module, kind(), {fmi.HOME: self.forecast(1, night=False)}))


if __name__ == "__main__":
    unittest.main()
