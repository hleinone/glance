"""The weather for a place other than [location]: a town by its name, or a
point by its coordinates, with the widget's heading."""

import io
import tempfile
import unittest
import urllib.error
import urllib.parse
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from glance import config, layout, render, widgets
from glance.draw import HEADING_H
from glance.sources import Sources, fmi
from glance.widgets import fmi_weather_hourly, fmi_weather_now
from test_glance import DATA, HELSINKI, isolated_config

SAMPLE = (DATA / "fmi_simple.xml").read_bytes()
# FMI's real answer for a place it doesn't know, saved 2026-11-19.
NOT_FOUND = (DATA / "fmi_place_not_found.xml").read_bytes()
MIKKELI = fmi.ForecastRequest(town="Mikkeli")


def weather_config(test: unittest.TestCase, **named: widgets.Widget) -> config.Config:
    """One screen of these widgets, side by side, and nothing else."""
    cfg = isolated_config(test, stops=[])
    return replace(cfg, widgets=named, screens={"main": layout.parse(" ".join(named))})


def forecast(fetched_at: datetime, temperature: float) -> fmi.Forecast:
    hours = [fmi.Hour(fetched_at + timedelta(hours=i), temperature, 3.0, 90.0, 10.0, 1, temperature)
             for i in range(-2, 12)]
    return fmi.Forecast(fetched_at, hours)


def requested(opened: mock.Mock) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(opened.call_args.args[0].full_url).query)


def drawn_text(module, widget: widgets.Widget, sc: render.Scene, rect=(0, 0, 400, 150)) -> list[str]:
    """Every piece of text the widget draws."""
    img = Image.new("L", (rect[2], rect[3]), 255)
    with mock.patch.object(module, "text_vcenter") as text:
        module.draw(img, ImageDraw.Draw(img), sc, rect, "w", widget)
    return [call.args[3] for call in text.call_args_list]


class TownByNameTest(unittest.TestCase):
    def test_home_is_asked_for_by_its_coordinates_as_before(self):
        cfg = weather_config(self, w=widgets.FmiWeatherNow())
        with mock.patch("glance.upstream._open", return_value=io.BytesIO(SAMPLE)) as opened:
            Sources.make(cfg).weather.places[fmi.HOME].refresh(force=True)
        query = requested(opened)
        self.assertEqual(query["latlon"], ["60.1987,24.9327"])  # config.toml's [location], as always
        self.assertNotIn("place", query)
        self.assertTrue((cfg.cache_dir / "forecast.json").exists())  # the file a running server already has

    def test_a_town_is_asked_for_by_its_name(self):
        cfg = weather_config(self, w=widgets.FmiWeatherNow(fmi.ForecastRequest(town="Pieksämäki")))
        with mock.patch("glance.upstream._open", return_value=io.BytesIO(SAMPLE)) as opened:
            for source in Sources.make(cfg).periodic:
                source.refresh(force=True)
        self.assertEqual(opened.call_count, 1)
        self.assertIn("place=Pieks%C3%A4m%C3%A4ki", opened.call_args.args[0].full_url)
        self.assertNotIn("latlon", requested(opened))

    def test_a_name_cannot_change_the_rest_of_the_request(self):
        town = "Mikkeli&latlon=1,1&storedquery_id=x"
        cfg = weather_config(self, w=widgets.FmiWeatherNow(fmi.ForecastRequest(town=town)))
        with mock.patch("glance.upstream._open", return_value=io.BytesIO(SAMPLE)) as opened:
            Sources.make(cfg).weather.places[fmi.ForecastRequest(town=town)].refresh(force=True)
        query = requested(opened)
        self.assertEqual(query["place"], [town])
        self.assertNotIn("latlon", query)
        self.assertEqual(query["storedquery_id"], ["fmi::forecast::edited::weather::scandinavia::point::simple"])

    def test_each_place_is_fetched_and_cached_on_its_own(self):
        cfg = weather_config(self, home=widgets.FmiWeatherNow(), town=widgets.FmiWeatherHourly(MIKKELI))
        now = datetime.now(UTC)
        answers = {"latlon": forecast(now, -3.0), "place": forecast(now, 7.0)}
        with mock.patch.object(fmi, "fetch", side_effect=lambda where, tz, now: answers[next(iter(where))]):
            for source in Sources.make(cfg).periodic:
                source.refresh(force=True)
        restarted = Sources.make(cfg).weather.forecasts()  # each from its own cache file
        self.assertEqual(restarted[fmi.HOME].hours[0].temperature, -3.0)
        self.assertEqual(restarted[MIKKELI].hours[0].temperature, 7.0)

    def test_one_place_failing_keeps_the_others_forecast_and_is_named_in_the_status_bar(self):
        cfg = weather_config(self, home=widgets.FmiWeatherNow(), town=widgets.FmiWeatherNow(MIKKELI))
        sources = Sources.make(cfg)
        now = datetime.now(UTC)

        def fetch(where, tz, now):
            if "place" in where:
                raise fmi.FMIError("HTTP 502")
            return forecast(now, -3.0)

        with mock.patch.object(fmi, "fetch", side_effect=fetch), self.assertLogs("glance", "WARNING") as logs:
            for source in sources.periodic:
                source.refresh(force=True)
        self.assertIn("weather for Mikkeli fetch failed (HTTP 502)", logs.output[0])
        self.assertEqual(list(sources.weather.forecasts()), [fmi.HOME])
        self.assertEqual(sources.warnings(now, "en"), ("Weather for Mikkeli unavailable",))
        self.assertEqual(sources.warnings(now, "fi"), ("Säätietoja ei saatavilla (Mikkeli)",))
        sources.weather.places[MIKKELI].forecast = forecast(now - timedelta(hours=2), 7.0)
        self.assertEqual(sources.warnings(now, "en"), ("Weather for Mikkeli 2 h old",))
        self.assertEqual(sources.warnings(now, "fi"), ("Sää (Mikkeli) 2 h vanha",))

    def test_a_place_two_widgets_show_is_fetched_once(self):
        cfg = weather_config(self, a=widgets.FmiWeatherNow(MIKKELI), b=widgets.FmiWeatherHourly(MIKKELI))
        with mock.patch.object(fmi, "fetch", return_value=forecast(datetime.now(UTC), 1.0)) as fetch:
            for source in Sources.make(cfg).periodic:
                source.refresh(force=True)
        self.assertEqual(fetch.call_count, 1)

    def test_the_log_says_where_fmi_took_a_town_to_be(self):
        cfg = weather_config(self, w=widgets.FmiWeatherNow(fmi.ForecastRequest(town="Koski")))
        with mock.patch("glance.upstream._open", return_value=io.BytesIO(SAMPLE)), \
                self.assertLogs("glance", "INFO") as logs:
            for source in Sources.make(cfg).periodic:
                source.refresh(force=True)
        # The sample answer is for Pasila: FMI's own coordinates in it.
        self.assertIn("weather for Koski updated: ", logs.output[-1])
        self.assertTrue(logs.output[-1].endswith(" at 60.1987, 24.9327"), logs.output[-1])

    def test_a_town_fmi_does_not_know_is_said_on_the_widget(self):
        cfg = weather_config(self, now=widgets.FmiWeatherNow(fmi.ForecastRequest(town="Tampre")),
                             hours=widgets.FmiWeatherHourly(fmi.ForecastRequest(town="Tampre")))
        sources = Sources.make(cfg)
        refused = urllib.error.HTTPError("https://opendata.fmi.fi/wfs", 400, "Bad Request", {}, io.BytesIO(NOT_FOUND))
        with mock.patch("glance.upstream._open", side_effect=refused), self.assertLogs("glance", "WARNING") as logs:
            for source in sources.periodic:
                source.refresh(force=True)
        self.assertIn("No locations found for the place", logs.output[0])
        from glance.__main__ import scene
        for language, words in (("en", "Place “Tampre” not found"), ("fi", "Paikkaa ”Tampre” ei löytynyt")):
            sc = scene(replace(cfg, language=language), sources, datetime.now(HELSINKI))
            self.assertEqual(drawn_text(fmi_weather_now, cfg.widgets["now"], sc), [words])
            self.assertEqual(drawn_text(fmi_weather_hourly, cfg.widgets["hours"], sc), [words])
        # Found later (FMI's place names changed, say): drawn as any other.
        with mock.patch("glance.upstream._open", return_value=io.BytesIO(SAMPLE)):
            sources.weather.places[fmi.ForecastRequest(town="Tampre")].refresh(force=True)
        self.assertEqual(sources.weather.not_found(), frozenset())

    def test_an_unknown_town_is_quoted_as_written_under_a_heading_too(self):
        tampre = fmi.ForecastRequest(town="Tampre", name="Äiti")
        cfg = weather_config(self, now=widgets.FmiWeatherNow(tampre), hours=widgets.FmiWeatherHourly(tampre))
        sources = Sources.make(cfg)
        refused = urllib.error.HTTPError("https://opendata.fmi.fi/wfs", 400, "Bad Request", {}, io.BytesIO(NOT_FOUND))
        with mock.patch("glance.upstream._open", side_effect=refused), self.assertLogs("glance", "WARNING") as logs:
            sources.weather.places[tampre].refresh(force=True)
        # The log says what was typed; the heading is the widget's own word.
        self.assertIn("weather for Tampre fetch failed", logs.output[0])
        from glance.__main__ import scene
        for language, words in (("en", "Place “Tampre” not found"), ("fi", "Paikkaa ”Tampre” ei löytynyt")):
            sc = scene(replace(cfg, language=language), sources, datetime.now(HELSINKI))
            for module, name in ((fmi_weather_now, "now"), (fmi_weather_hourly, "hours")):
                with self.subTest(language=language, widget=name), mock.patch.object(module, "heading"):
                    self.assertEqual(drawn_text(module, cfg.widgets[name], sc), [words])

    def test_the_log_names_a_town_by_its_name_and_a_point_by_its_coordinates(self):
        for request, said in ((fmi.ForecastRequest(town="Koski", name="Mökki"), "weather for Koski updated: "),
                              (fmi.ForecastRequest(lat=60.5, lon=26.25, name="Mökki"),
                               "weather for 60.5000, 26.2500 updated: ")):
            cfg = weather_config(self, w=widgets.FmiWeatherNow(request))
            with mock.patch("glance.upstream._open", return_value=io.BytesIO(SAMPLE)), \
                    self.assertLogs("glance", "INFO") as logs:
                Sources.make(cfg).weather.places[request].refresh(force=True)
            with self.subTest(request=request):
                self.assertIn(said, logs.output[-1])

    def test_only_a_town_can_be_an_unknown_place(self):
        # FMI's words for an unknown name, were it ever to say them for coordinates.
        for request in (fmi.HOME, fmi.ForecastRequest(lat=60.5, lon=26.25)):
            cfg = weather_config(self, w=widgets.FmiWeatherNow(request))
            sources = Sources.make(cfg)
            refused = urllib.error.HTTPError("https://opendata.fmi.fi/wfs", 400, "Bad", {}, io.BytesIO(NOT_FOUND))
            with mock.patch("glance.upstream._open", side_effect=refused), self.assertLogs("glance", "WARNING"):
                sources.weather.places[request].refresh(force=True)
            with self.subTest(request=request):
                self.assertEqual(sources.weather.not_found(), frozenset())

    def test_other_places_keep_their_cache_files_names(self):
        # Changing how a name is made would lose every cached place at a deploy.
        self.assertEqual(fmi.cache_name(fmi.HOME), "forecast.json")
        self.assertEqual(fmi.cache_name(fmi.ForecastRequest(name="Koti")), "forecast.json")
        self.assertEqual(fmi.cache_name(MIKKELI), "forecast-2cc813418d4080b4.json")
        self.assertEqual(fmi.cache_name(fmi.ForecastRequest(lat=60.5, lon=26.25, name="Mökki")),
                         "forecast-c25179b92f3263eb.json")

    def test_other_failures_are_not_said_to_be_an_unknown_town(self):
        cfg = weather_config(self, now=widgets.FmiWeatherNow(MIKKELI), hours=widgets.FmiWeatherHourly(MIKKELI))
        sources = Sources.make(cfg)
        busy = urllib.error.HTTPError("https://opendata.fmi.fi/wfs", 503, "Busy", {}, io.BytesIO(b"<html>busy"))
        with mock.patch("glance.upstream._open", side_effect=busy), self.assertLogs("glance", "WARNING"):
            sources.weather.places[MIKKELI].refresh(force=True)
        from glance.__main__ import scene
        sc = scene(replace(cfg, language="en"), sources, datetime.now(HELSINKI))
        self.assertEqual(drawn_text(fmi_weather_now, cfg.widgets["now"], sc), ["Weather unavailable"])
        self.assertEqual(drawn_text(fmi_weather_hourly, cfg.widgets["hours"], sc), [])  # the status bar says it

    def test_each_widget_draws_its_own_places_forecast(self):
        now = datetime(2026, 11, 19, 12, tzinfo=HELSINKI)
        sc = render.Scene(now=now, language="en", stops={},
                          forecasts={fmi.HOME: forecast(now, -3.0), MIKKELI: forecast(now, 7.0)})
        self.assertIn("7°", drawn_text(fmi_weather_now, widgets.FmiWeatherNow(MIKKELI), sc))
        self.assertIn("−3°", drawn_text(fmi_weather_now, widgets.FmiWeatherNow(), sc))
        self.assertIn("7°", drawn_text(fmi_weather_hourly, widgets.FmiWeatherHourly(MIKKELI), sc))
        self.assertNotIn("−3°", drawn_text(fmi_weather_hourly, widgets.FmiWeatherHourly(MIKKELI), sc))


class PointAndHeadingTest(unittest.TestCase):
    POINT = fmi.ForecastRequest(lat=60.5, lon=26.25)

    def test_a_point_is_asked_for_by_its_coordinates(self):
        cfg = weather_config(self, w=widgets.FmiWeatherHourly(self.POINT))
        with mock.patch("glance.upstream._open", return_value=io.BytesIO(SAMPLE)) as opened:
            for source in Sources.make(cfg).periodic:
                source.refresh(force=True)
        self.assertEqual(opened.call_count, 1)
        query = requested(opened)
        self.assertEqual(query["latlon"], ["60.5000,26.2500"])
        self.assertNotIn("place", query)

    def test_one_place_under_two_headings_is_fetched_once(self):
        cfg = weather_config(self, a=widgets.FmiWeatherNow(replace(self.POINT, name="Mökki")),
                             b=widgets.FmiWeatherHourly(replace(self.POINT, name="Kesäpaikka")))
        with mock.patch.object(fmi, "fetch", return_value=forecast(datetime.now(UTC), 1.0)) as fetch:
            for source in Sources.make(cfg).periodic:
                source.refresh(force=True)
        self.assertEqual(fetch.call_count, 1)

    def test_the_status_bar_names_a_point_by_its_heading_or_its_coordinates(self):
        now = datetime.now(UTC)
        for request, words in ((replace(self.POINT, name="Mökki"), "Weather for Mökki unavailable"),
                               (self.POINT, "Weather for 60.50, 26.25 unavailable"),
                               (fmi.ForecastRequest(town="Mikkeli", name="Äiti"), "Weather for Äiti unavailable"),
                               (fmi.ForecastRequest(name="Koti"), "Weather unavailable")):
            sources = Sources.make(weather_config(self, w=widgets.FmiWeatherNow(request)))
            with self.subTest(request=request):
                self.assertEqual(sources.warnings(now, "en"), (words,))

    def test_the_heading_is_drawn_above_the_weather(self):
        now = datetime(2026, 11, 19, 12, tzinfo=HELSINKI)
        point = replace(self.POINT, name="Mökki")
        sc = render.Scene(now=now, language="en", stops={}, forecasts={point: forecast(now, 7.0)})
        for module, widget in ((fmi_weather_now, widgets.FmiWeatherNow), (fmi_weather_hourly, widgets.FmiWeatherHourly)):
            with self.subTest(widget=widget.TYPE):
                with mock.patch.object(module, "heading") as heading:
                    texts = drawn_text(module, widget(point), sc)
                self.assertEqual(heading.call_args.args[3], "Mökki")
                self.assertIn("7°", texts)
                with mock.patch.object(module, "heading") as heading:
                    drawn_text(module, widget(self.POINT), replace(sc, forecasts={self.POINT: forecast(now, 7.0)}))
                heading.assert_not_called()  # coordinates without a name: no heading

    def test_nothing_else_is_drawn_in_the_headings_line(self):
        now = datetime(2026, 11, 19, 12, tzinfo=HELSINKI)
        point = replace(self.POINT, name="Mökki")
        for module, widget in ((fmi_weather_now, widgets.FmiWeatherNow), (fmi_weather_hourly, widgets.FmiWeatherHourly)):
            for sc in (render.Scene(now=now, language="fi", stops={}, forecasts={point: forecast(now, -29.0)}),
                       render.Scene(now=now, language="fi", stops={}, places_not_found=frozenset({point}))):
                img = Image.new("L", (400, 200), 255)
                with mock.patch.object(module, "heading"):  # the heading itself left out
                    module.draw(img, ImageDraw.Draw(img), sc, (0, 0, 400, 200), "w", widget(point))
                top = Image.eval(img, lambda v: 255 - v).getbbox()[1]
                with self.subTest(widget=widget.TYPE, not_found=bool(sc.places_not_found)):
                    self.assertGreaterEqual(top, HEADING_H)

    def test_a_heading_needs_a_line_more(self):
        def load(name: str) -> config.Config:
            path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "config.toml"
            path.write_text("[location]\nlat = 60.2\nlon = 24.9\n"
                            f"[widgets.w]\ntype = \"fmi_weather_now\"\nlat = 60.5\nlon = 26.25\n{name}"
                            "[screens.main]\ngrid = \"\"\"\nw\n.\n.\n\"\"\"\n")
            return config.load(path, local_overrides=False)

        load("")  # 150 px tall: enough without a heading
        with self.assertRaises(ValueError) as e:
            load('name = "Mökki"\n')
        self.assertIn("'w' (fmi_weather_now) needs at least 245×168 px", str(e.exception))
        self.assertIn("give it 2 rows", str(e.exception))


class PlaceConfigTest(unittest.TestCase):
    def load(self, place: str) -> config.Config:
        text = ("[location]\nlat = 60.2\nlon = 24.9\n"
                f"[widgets.w]\ntype = \"fmi_weather_hourly\"\nplace = {place}\n"
                "[screens.main]\ngrid = \"w\"\n")
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "config.toml"
        path.write_text(text)
        return config.load(path, local_overrides=False)

    def test_a_town_is_read_and_trimmed(self):
        self.assertEqual(self.load('" Mikkeli "').widgets["w"].wants, MIKKELI)

    def test_a_place_that_cannot_be_right_is_refused_at_load(self):
        for value, message in (
            ('5', "widget 'w' (fmi_weather_hourly): place must be text, like \"Mikkeli\", not 5"),
            ('""', "widget 'w' (fmi_weather_hourly): place must be 1 to 60 characters, not 0"),
            ('"   "', "place must be 1 to 60 characters, not 0"),
            (f'"{"x" * 61}"', "place must be 1 to 60 characters, not 61"),
            ('"Mikkeli\\nINFO forged"', "has a control character"),
            ('"Mikke\\u200bli"', "has a control character"),  # invisible, and not what FMI knows
        ):
            with self.subTest(value=value), self.assertRaises(ValueError) as e:
                self.load(value)
            self.assertIn(message, str(e.exception))

    def test_a_point_and_a_heading_are_read(self):
        w = self.load('"x"\nname = " Mökki "').widgets["w"]  # a town named x, under a heading
        self.assertEqual(w.wants.name, "Mökki")
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "config.toml"
        path.write_text('[location]\nlat = 60.2\nlon = 24.9\n[widgets.w]\ntype = "fmi_weather_hourly"\n'
                        'lat = 61\nlon = 26.5\n[screens.main]\ngrid = "w"\n')
        self.assertEqual(config.load(path, local_overrides=False).widgets["w"].wants,
                         fmi.ForecastRequest(lat=61.0, lon=26.5))

    def test_a_point_or_a_heading_that_cannot_be_right_is_refused_at_load(self):
        for value, message in (
            ('"Mikkeli"\nlat = 61\nlon = 27', "give the place by its name (place) or by its coordinates (lat and lon), not both"),
            ('"Mikkeli"\nname = ""', "name must be 1 to 40 characters, not 0"),
            (f'"Mikkeli"\nname = "{"x" * 41}"', "name must be 1 to 40 characters, not 41"),
            ('"Mikkeli"\nname = "M\\u0000"', "has a control character"),
            ('"Mikkeli"\nname = 7', "name must be text"),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError) as e:
                self.load(value)
            self.assertIn(message, str(e.exception))
        for point, message in (("lat = 61", "lat needs lon too"), ("lon = 27", "lon needs lat too"),
                               ("lat = 91\nlon = 27", "lat must be -90..90, not 91.0"),
                               ("lat = 61\nlon = \"east\"", "lon must be a number, not 'east'")):
            path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "config.toml"
            path.write_text('[location]\nlat = 60.2\nlon = 24.9\n[widgets.w]\ntype = "fmi_weather_now"\n'
                            f'{point}\n[screens.main]\ngrid = "w"\n')
            with self.subTest(point=point), self.assertRaises(ValueError) as e:
                config.load(path, local_overrides=False)
            self.assertIn(f"widget 'w' (fmi_weather_now): {message}", str(e.exception))

    def test_at_most_eight_places(self):
        towns = {f"w{i}": widgets.FmiWeatherNow(fmi.ForecastRequest(town=f"Town {i}")) for i in range(8)}
        Sources.make(weather_config(self, **towns))
        towns["home"] = widgets.FmiWeatherNow()
        with self.assertRaises(ValueError) as e:
            Sources.make(weather_config(self, **towns))
        self.assertEqual(str(e.exception), "the weather widgets on the screens ask for 9 places; at most 8 can be shown")


class RefreshTest(unittest.TestCase):
    def test_places_are_refreshed_at_once_so_slow_answers_do_not_add_up(self):
        import threading
        import time
        from types import SimpleNamespace
        from glance.__main__ import refresh_periodic
        started, done = [], []
        both = threading.Barrier(2, timeout=5)

        class Slow:
            WHAT = "slow"

            def refresh(self, force=False):
                started.append(time.monotonic())
                both.wait()  # passes only once the other has started too
                time.sleep(0.05)
                done.append(force)

        class Broken:
            WHAT = "broken"

            def refresh(self, force=False):
                raise RuntimeError("boom")

        with self.assertLogs("glance", "ERROR") as logs:
            refresh_periodic(SimpleNamespace(periodic=(Slow(), Broken(), Slow())))
        self.assertEqual(len(started), 2)
        self.assertEqual(done, [False, False])  # the round ends when every refresh has
        # One after the other, the first would have waited for the second in vain.
        self.assertEqual(len(logs.output), 1, logs.output)
        self.assertIn("broken refresh failed", logs.output[0])

    def test_a_thread_that_cannot_start_does_not_end_the_round(self):
        # The service's task limit reached: the refresher thread must live on.
        import threading
        from types import SimpleNamespace
        from glance.__main__ import refresh_periodic
        refreshed = []

        class Source:
            WHAT = "source"

            def refresh(self, force=False):
                refreshed.append(force)

        real_start, calls = threading.Thread.start, []

        def start(thread):
            calls.append(thread)
            if len(calls) == 2:
                raise RuntimeError("can't start new thread")
            real_start(thread)

        with mock.patch.object(threading.Thread, "start", start), self.assertLogs("glance", "WARNING") as logs:
            refresh_periodic(SimpleNamespace(periodic=(Source(), Source(), Source())), force=True)
        self.assertEqual(refreshed, [True] * 3)  # the one without a thread, in this one
        self.assertIn("can't start new thread", logs.output[0])


if __name__ == "__main__":
    unittest.main()
