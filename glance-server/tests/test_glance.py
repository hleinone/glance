import http.client
import io
import json
import os
import tempfile
import unittest
import urllib.parse
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from glance import battery, config, decks, draw, i18n, layout, render, schedule, sun, widgets
from glance.sources import elering, fmi, hsl
from glance.widgets import fmi_symbols, fmi_weather_hourly, hsl_stops, spot_prices, sunrise_sunset

DATA = Path(__file__).parent / "data"
HELSINKI = ZoneInfo("Europe/Helsinki")


def isolated_config(test: unittest.TestCase, stops: list[widgets.HslStops] | None = None, weather: bool = False,
                    spot: bool = False, **changes) -> config.Config:
    """config.toml alone (no config.local.toml, no environment), no key, and a
    cache folder of the test's own: tests must not depend on this machine's
    settings, reach the network or touch the real cache.

    stops: one screen of just these stop widgets, side by side (s0, s1, ...);
    with weather, a current weather widget first; with spot, a spot price widget
    last."""
    cache = Path(test.enterContext(tempfile.TemporaryDirectory()))
    cfg = config.load(local_overrides=False)
    if stops is not None:
        names = [f"s{i}" for i in range(len(stops))]
        changes["widgets"] = dict(zip(names, stops))
        if weather:
            names.insert(0, "weather")
            changes["widgets"]["weather"] = widgets.FmiWeatherNow()
        if spot:
            names.append("spot")
            changes["widgets"]["spot"] = widgets.SpotPrices()
        changes["screens"] = {"main": layout.parse(" ".join(names) or ".")}
        changes["decks"] = {"all": decks.Deck("all", ("main",), "main")}
        # The example's wake times, without its decks.
        changes["wake_schedule"] = schedule.WakeSchedule(
            tuple(replace(r, deck=None) for r in cfg.wake_schedule.rules), cfg.wake_schedule.default_every, "all")
    # The key is a setting of the departures source's own; None unless a test gives one.
    transit = replace(cfg.source_settings.transit, key=changes.pop("digitransit_key", None))
    changes.setdefault("source_settings", replace(cfg.source_settings, transit=transit))
    return replace(cfg, **{"cache_dir": cache, **changes})


# The least a config file needs: a location and a screen.
MINIMAL = '[location]\nlat = 60.2\nlon = 24.9\n[screens.main]\ngrid = "."\n'


class FMITest(unittest.TestCase):
    def test_parse_live_sample(self):
        # tests/data/fmi_simple.xml is a real response saved on 2026-11-09.
        hours = fmi.parse((DATA / "fmi_simple.xml").read_bytes())
        self.assertEqual(len(hours), 72)
        first = hours[0]
        self.assertEqual(first.time, datetime(2026, 11, 9, 21, tzinfo=UTC))
        self.assertEqual(first.temperature, 4.43)
        self.assertEqual(first.wind_speed, 3.5)
        self.assertEqual(first.wind_direction, 247.0)
        self.assertEqual(first.pop, 0.0)
        self.assertEqual(first.symbol, 3)
        self.assertEqual(hours, sorted(hours, key=lambda h: h.time))

    def test_nan_becomes_none(self):
        xml = b"""<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0"
            xmlns:BsWfs="http://xml.fmi.fi/schema/wfs/2.0"><wfs:member><BsWfs:BsWfsElement>
            <BsWfs:Time>2026-11-09T21:00:00Z</BsWfs:Time>
            <BsWfs:ParameterName>Temperature</BsWfs:ParameterName>
            <BsWfs:ParameterValue>NaN</BsWfs:ParameterValue>
            </BsWfs:BsWfsElement></wfs:member></wfs:FeatureCollection>"""
        (hour,) = fmi.parse(xml)
        self.assertIsNone(hour.temperature)
        self.assertIsNone(hour.symbol)

    def test_infinite_values_are_dropped(self):
        # Only NaN used to be: infinity then broke every drawing, also from the cache.
        self.assertEqual([fmi._value(v) for v in ("inf", "-Infinity", "1e999", "NaN", "3.5", None, "x")],
                         [None, None, None, None, 3.5, None, None])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "forecast.json"
            path.write_text(json.dumps({
                "fetched_at": "2026-11-11T10:00:00+00:00", "parameters": list(fmi.PARAMETERS),
                "hours": [{"time": "2026-11-11T10:00:00+00:00", "temperature": float("inf"), "wind_speed": float("nan"),
                           "wind_direction": 180.0, "pop": 10.0, "symbol": 3, "feels_like": -2.0}],
            }))
            hour = fmi.load_cache(path).hours[0]
        self.assertEqual((hour.temperature, hour.wind_speed, hour.wind_direction, hour.symbol), (None, None, 180.0, 3))

    def test_values_that_could_not_be_drawn(self):
        def element(time, name, value):
            return (f"<wfs:member><BsWfs:BsWfsElement><BsWfs:Time>{time}</BsWfs:Time>"
                    f"<BsWfs:ParameterName>{name}</BsWfs:ParameterName>"
                    f"<BsWfs:ParameterValue>{value}</BsWfs:ParameterValue></BsWfs:BsWfsElement></wfs:member>")
        hours = fmi.parse((
            '<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:BsWfs="http://xml.fmi.fi/schema/wfs/2.0">'
            + element("2026-11-09T12:00:00", "WeatherSymbol3", "1e300")   # no zone; an absurd code
            + element("2026-11-09T13:00:00+02:00", "WeatherSymbol3", "2.0")
            + element("9999-12-31T23:00:00Z", "Temperature", "1")          # can't be shown in local time
            + "</wfs:FeatureCollection>").encode())
        self.assertEqual([h.time for h in hours], [datetime(2026, 11, 9, 11, tzinfo=UTC), datetime(2026, 11, 9, 12, tzinfo=UTC)])
        self.assertEqual([h.symbol for h in hours], [2, None])

    def test_feels_like(self):
        xml = b"""<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0"
            xmlns:BsWfs="http://xml.fmi.fi/schema/wfs/2.0"><wfs:member><BsWfs:BsWfsElement>
            <BsWfs:Time>2026-11-10T16:00:00Z</BsWfs:Time>
            <BsWfs:ParameterName>FeelsLike</BsWfs:ParameterName>
            <BsWfs:ParameterValue>2.41793</BsWfs:ParameterValue>
            </BsWfs:BsWfsElement></wfs:member></wfs:FeatureCollection>"""
        (hour,) = fmi.parse(xml)
        self.assertEqual(hour.feels_like, 2.41793)
        self.assertIn("FeelsLike", fmi.PARAMETERS)

    def test_cache_from_before_feels_like_loads(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "forecast.json"
            path.write_text(json.dumps({"fetched_at": "2026-11-09T20:00:00+00:00", "hours": [
                {"time": "2026-11-09T21:00:00+00:00", "temperature": 4.4, "wind_speed": 3.5,
                 "wind_direction": 247.0, "pop": 0.0, "symbol": 3}]}))
            fc = fmi.load_cache(path)
        self.assertIsNotNone(fc)
        self.assertIsNone(fc.hours[0].feels_like)
        self.assertNotEqual(fc.parameters, fmi.PARAMETERS)  # so the server refetches at once

    def test_exception_report(self):
        xml = b"""<ExceptionReport xmlns="http://www.opengis.net/ows/1.1"><Exception>
            <ExceptionText>Unknown parameter 'Bogus'</ExceptionText></Exception></ExceptionReport>"""
        with self.assertRaisesRegex(fmi.FMIError, "Bogus"):
            fmi.parse(xml)

    def test_any_bad_response_is_an_fmi_error(self):
        # An error page from a proxy, an empty or cut-off body: all one kind of
        # failure for the caller (another kind once killed the refresher thread).
        import http.client, io, urllib.error
        from unittest import mock
        for body in (b"<html><body>502 Bad Gateway<br></body></html>", b"", b"<wfs:FeatureCollection"):
            with self.assertRaises(fmi.FMIError):
                fmi.parse(body)
        for failure in (
            urllib.error.HTTPError("http://x", 502, "Bad Gateway", {}, io.BytesIO(b"<html>oops")),
            urllib.error.HTTPError("http://x", 429, "Too Many", {}, io.BytesIO(b"")),
            http.client.IncompleteRead(b"<wfs"),
            TimeoutError("timed out"),
        ):
            with mock.patch("glance.upstream._open", side_effect=failure):
                with self.assertRaises(fmi.FMIError):
                    fmi.fetch(60.2, 24.9, HELSINKI)

    def test_cache_round_trip(self):
        fc = fmi.Forecast(datetime(2026, 11, 9, 20, tzinfo=UTC), fmi.parse((DATA / "fmi_simple.xml").read_bytes()))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "forecast.json"
            fmi.save_cache(fc, path)
            self.assertEqual(fmi.load_cache(path), fc)
            path.write_text("{not json")
            self.assertIsNone(fmi.load_cache(path))
            self.assertIsNone(fmi.load_cache(Path(d) / "missing.json"))

    def test_fmi_icons_cover_every_code(self):
        for code in fmi_symbols.SYMBOLS:
            for size in (76, 36):
                icon = fmi_symbols.fmi_symbol(code, size)
                self.assertIsNotNone(icon, (code, size))
                self.assertEqual((icon.mode, icon.size), ("L", (size, size)))
        self.assertIsNone(fmi_symbols.fmi_symbol(None, 76))
        self.assertIsNone(fmi_symbols.fmi_symbol(999, 76))
        # Without a pre-rendered PNG, the hand-drawn icon is used.
        self.assertEqual(fmi_symbols.weather_icon(999, "rain", 2, 50).size, (50, 50))
        self.assertTrue((fmi_symbols.FMI_DIR / "LICENSE").exists())

    def test_symbols(self):
        self.assertEqual(fmi_symbols.symbol_info(31, "fi"), ("rain", 1, "Heikkoa vesisadetta"))
        self.assertEqual(fmi_symbols.symbol_info(2, "en"), ("partly_cloudy", 1, "Partly cloudy"))
        self.assertEqual(fmi_symbols.symbol_info(999, "en")[0], "unknown")

    def test_forecast_helpers(self):
        fc = fmi.Forecast(datetime(2026, 11, 9, 20, tzinfo=UTC), fmi.parse((DATA / "fmi_simple.xml").read_bytes()))
        now = datetime(2026, 11, 9, 21, 40, tzinfo=UTC)
        self.assertEqual(fc.current(now).time, datetime(2026, 11, 9, 22, tzinfo=UTC))
        upcoming = fc.upcoming(now, 12)
        self.assertEqual(len(upcoming), 12)
        self.assertEqual(upcoming[0].time, datetime(2026, 11, 9, 22, tzinfo=UTC))


class FormatTest(unittest.TestCase):
    def test_temperature_uses_real_minus(self):
        self.assertEqual(draw.fmt_temp(-3.2), "−3°")
        self.assertEqual(draw.fmt_temp(-0.4), "0°")
        self.assertEqual(draw.fmt_temp(4.5), "5°")
        self.assertEqual(draw.fmt_temp(None), "–")

    def test_fit_text_shrinks_then_truncates(self):
        f, text = draw.fit_text("Short", "medium", 16, 12, 200)
        self.assertEqual((f.size, text), (16, "Short"))
        f, text = draw.fit_text("Westendinasema via Otaniemi ja Keilaniemi", "medium", 16, 12, 120)
        self.assertEqual(f.size, 12)
        self.assertTrue(text.endswith("…"))
        self.assertLessEqual(f.getlength(text), 120)

    def test_i18n(self):
        t = datetime(2026, 11, 9, 7, 5, tzinfo=HELSINKI)
        self.assertEqual(i18n.date("en", t), "Monday 9 November")
        self.assertEqual(i18n.date("fi", t), "Maanantai 9.11.")
        self.assertEqual(i18n.clock("en", t), "07:05")
        self.assertEqual(i18n.clock("fi", t), "07.05")
        self.assertEqual(i18n.age(130), "2 h 10 min")

    def test_durations_and_short_dates(self):
        self.assertEqual(i18n.duration(437), "7 h 17 min")
        self.assertEqual(i18n.duration(45), "45 min")
        self.assertEqual(i18n.duration(1440), "24 h")
        self.assertEqual(i18n.duration(0), "0 min")
        self.assertEqual(i18n.duration(4, signed=True), "+4 min")
        self.assertEqual(i18n.duration(-65, signed=True), "−1 h 5 min")  # a real minus sign
        self.assertEqual(i18n.duration(0, signed=True), "±0 min")
        self.assertEqual(i18n.day_month("fi", datetime(2027, 1, 17)), "17.1.")
        self.assertEqual(i18n.day_month("en", datetime(2027, 1, 17)), "17 Jan")


def _stoptime(service_day, seconds, route, headsign, mode="BUS", realtime=True, delay=0):
    return {
        "serviceDay": service_day,
        "scheduledDeparture": seconds,
        "realtimeDeparture": seconds + delay,
        "realtime": realtime,
        "headsign": headsign,
        "trip": {"tripHeadsign": headsign + " (trip)", "route": {"shortName": route, "mode": mode}},
    }


class HSLTest(unittest.TestCase):
    # serviceDay is local midnight as a Unix timestamp; times are seconds after it.
    DAY = int(datetime(2026, 11, 9, tzinfo=HELSINKI).timestamp())
    DATA = {
        "stops": [
            {"gtfsId": "HSL:1", "stoptimesWithoutPatterns": [
                _stoptime(DAY, 23 * 3600 + 35 * 60, "7", "Senaatintori", "TRAM", delay=60),
                _stoptime(DAY, 23 * 3600 + 50 * 60, "59", "Itäkeskus (M)", realtime=False),
            ]},
            {"gtfsId": "HSL:2", "stoptimesWithoutPatterns": [
                _stoptime(DAY, 23 * 3600 + 40 * 60, "7", "Pasila", "TRAM"),
                {**_stoptime(DAY, 24 * 3600 + 5 * 60, "9", "Kirurgi", "TRAM"), "headsign": None},
            ]},
            None,
        ]
    }

    def test_parse(self):
        by_stop = hsl.parse_departures(self.DATA)
        self.assertEqual(set(by_stop), {"HSL:1", "HSL:2"})
        tram, bus = by_stop["HSL:1"]
        self.assertEqual((tram.route, tram.headsign, tram.mode, tram.realtime), ("7", "Senaatintori", "tram", True))
        # Realtime departure = serviceDay + realtimeDeparture (scheduled + 60 s delay).
        self.assertEqual(tram.time, datetime(2026, 11, 9, 23, 36, tzinfo=HELSINKI))
        self.assertEqual((bus.mode, bus.realtime), ("bus", False))
        # Past midnight on the same service day, and the trip headsign as fallback.
        after_midnight = by_stop["HSL:2"][1]
        self.assertEqual(after_midnight.time, datetime(2026, 11, 10, 0, 5, tzinfo=HELSINKI))
        self.assertEqual(after_midnight.headsign, "Kirurgi (trip)")

    def test_parse_live_sample(self):
        # tests/data/hsl_departures.json is a real response saved on 2026-11-09 for
        # Pasilan asema: tram platforms 32 + 34, bus 31 + 36, train platform 2.
        payload = json.loads((DATA / "hsl_departures.json").read_text())
        by_stop = hsl.parse_departures(payload["data"])
        self.assertEqual(len(by_stop), 5)
        self.assertTrue(all(len(deps) == 12 for deps in by_stop.values()))
        trams = hsl.select(by_stop, hsl.StopQuery(("HSL:1174411", "HSL:1174410")))
        self.assertEqual({d.mode for d in trams}, {"tram"})
        self.assertEqual(trams, sorted(trams, key=lambda d: d.time))
        self.assertTrue(any(d.realtime for d in trams) and not all(d.realtime for d in trams))
        self.assertEqual({d.mode for d in by_stop["HSL:1174501"]}, {"rail"})
        only_7 = hsl.select(by_stop, hsl.StopQuery(("HSL:1174411", "HSL:1174410"), frozenset({"7"})))
        self.assertTrue(only_7 and all(d.route == "7" for d in only_7))

    def test_any_bad_response_is_an_hsl_error(self):
        import http.client, io
        from unittest import mock
        query = [hsl.StopQuery(("HSL:1",))]

        def answer(payload):
            return mock.patch("glance.upstream._open", return_value=io.BytesIO(json.dumps(payload).encode()))

        for payload in (
            [],                                                     # not an object
            {"data": {"stops": [{"name": "no id"}]}},
            {"data": {"stops": [{"gtfsId": "HSL:1", "stoptimesWithoutPatterns": [
                {"serviceDay": "today", "realtimeDeparture": 60}]}]}},
            {"errors": [{"message": None}]},
            {"errors": ["text"]},
        ):
            with answer(payload), self.assertRaises(hsl.HSLError, msg=payload):
                hsl.fetch(query, "key", "fi")
        with mock.patch("glance.upstream._open", side_effect=http.client.IncompleteRead(b"{")):
            with self.assertRaises(hsl.HSLError):
                hsl.fetch(query, "key", "fi")

    def test_mode_icon_names_are_limited(self):
        self.assertIsNone(hsl_stops.mode_icon("../fmi/36/1"))
        self.assertIsNone(hsl_stops.mode_icon("airplane"))

    def test_one_route_is_not_its_characters(self):
        self.assertEqual(hsl_stops.parse({"routes": "550"}).routes, frozenset({"550"}))
        self.assertEqual(hsl_stops.parse({"routes": 550}).routes, frozenset({"550"}))
        self.assertEqual(hsl_stops.parse({"routes": ["7", 9]}).routes, frozenset({"7", "9"}))

    def test_absurdly_long_text_is_cut(self):
        long = "x" * 5000
        data = {"stops": [{"gtfsId": "HSL:1", "name": long, "stoptimesWithoutPatterns": [
            _stoptime(self.DAY, 3600, long, long)]}]}
        dep = hsl.parse_departures(data)["HSL:1"][0]
        self.assertEqual((len(dep.route), len(dep.headsign)), (hsl.MAX_TEXT, hsl.MAX_TEXT))
        self.assertEqual(len(hsl.parse_stop_names(data)["HSL:1"]), hsl.MAX_TEXT)

    def test_values_that_could_not_be_drawn(self):
        far = _stoptime(253402214400, 86399, "7", "Pasila")  # 9999-12-31 23:59:59 UTC
        data = {"stops": [{"gtfsId": "HSL:1", "name": "Stop", "stoptimesWithoutPatterns": [
            far, _stoptime(self.DAY, 3600, "7", "Pasila")]}]}
        self.assertEqual(len(hsl.parse_departures(data)["HSL:1"]), 1)  # the one a clock can show
        for bad in ({"name": ["a", "b"]}, {"vehicleMode": 7}):
            with self.assertRaises(TypeError, msg=bad):
                (hsl.parse_stop_names if "name" in bad else hsl.parse_stop_modes)({"stops": [{"gtfsId": "HSL:1", **bad}]})
        number = _stoptime(self.DAY, 3600, "7", "Pasila")
        number["trip"]["route"]["shortName"] = 7
        with self.assertRaises(TypeError):
            hsl.parse_departures({"stops": [{"gtfsId": "HSL:1", "stoptimesWithoutPatterns": [number]}]})

    def test_short_headsign(self):
        self.assertEqual(hsl.short_headsign("Herttoniemi(M) via Kalasatama(M)"), "Herttoniemi(M)")
        self.assertEqual(hsl.short_headsign("Länsisatama VIA Kallio"), "Länsisatama")
        self.assertEqual(hsl.short_headsign("Viikki"), "Viikki")  # "vi..." is not "via"
        self.assertEqual(hsl.short_headsign("Viaporintie"), "Viaporintie")
        payload = json.loads((DATA / "hsl_departures.json").read_text())
        deps = [d for ds in hsl.parse_departures(payload["data"]).values() for d in ds]
        self.assertFalse(any(" via " in d.headsign.lower() for d in deps))

    def test_stop_names(self):
        data = {"stops": [{"gtfsId": "HSL:1", "name": "Pasilan asema", "stoptimesWithoutPatterns": []},
                          {"gtfsId": "HSL:2", "stoptimesWithoutPatterns": []}, None]}
        self.assertEqual(hsl.parse_stop_names(data), {"HSL:1": "Pasilan asema"})

    def test_trunk_routes(self):
        day = int(datetime(2026, 11, 10, tzinfo=HELSINKI).timestamp())
        def st(route, rtype):
            return {**_stoptime(day, 8 * 3600, route, "Kamppi"), "trip": {"route": {"shortName": route, "mode": "BUS", "type": rtype}}}
        data = {"stops": [{"gtfsId": "HSL:1", "stoptimesWithoutPatterns": [st("400", 702), st("25", 701), st("280", 700), st("x", None)]}]}
        self.assertEqual([d.trunk for d in hsl.parse_departures(data)["HSL:1"]], [True, False, False, False])
        # The saved live response predates fetching the type: nothing is trunk, nothing breaks.
        payload = json.loads((DATA / "hsl_departures.json").read_text())
        self.assertFalse(any(d.trunk for ds in hsl.parse_departures(payload["data"]).values() for d in ds))

    def test_stop_modes(self):
        data = {"stops": [{"gtfsId": "HSL:1", "vehicleMode": "TRAM"}, {"gtfsId": "HSL:2", "vehicleMode": None},
                          {"gtfsId": "HSL:3", "vehicleMode": "BUS"}, None]}
        self.assertEqual(hsl.parse_stop_modes(data), {"HSL:1": "tram", "HSL:3": "bus"})
        self.assertEqual(hsl.ordered_modes(["bus", "tram", "bus", "hovercraft"]), ("tram", "bus", "hovercraft"))

    def test_mode_icons(self):
        for mode in ("tram", "bus", "subway", "rail", "ferry"):
            icon = hsl_stops.mode_icon(mode)
            self.assertIsNotNone(icon, mode)
            self.assertEqual(icon.size, (hsl_stops.MODE_ICON_SIZE, hsl_stops.MODE_ICON_SIZE))
        self.assertIsNone(hsl_stops.mode_icon("hovercraft"))
        for name in ("COPYING", "LICENSE-EUPL.txt", "README"):
            self.assertTrue((hsl_stops.HSL_DIR / name).exists(), name)
        now = datetime(2026, 11, 10, 8, 0, tzinfo=HELSINKI)
        self.assertEqual(hsl.placeholder("X", 1, now).modes, ("bus",))

    def test_select_merges_and_filters(self):
        by_stop = hsl.parse_departures(self.DATA)
        merged = hsl.select(by_stop, hsl.StopQuery(("HSL:1", "HSL:2")))
        self.assertEqual([d.route for d in merged], ["7", "7", "59", "9"])
        trams = hsl.select(by_stop, hsl.StopQuery(("HSL:1", "HSL:2", "HSL:404"), frozenset({"7", "9"})))
        self.assertEqual([d.headsign for d in trams], ["Senaatintori", "Pasila", "Kirurgi (trip)"])

    def test_minutes_and_upcoming(self):
        now = datetime(2026, 11, 9, 23, 30, 50, tzinfo=HELSINKI)
        deps = hsl.select(hsl.parse_departures(self.DATA), hsl.StopQuery(("HSL:1", "HSL:2")))
        self.assertEqual([d.minutes(now) for d in deps], [5, 9, 19, 34])
        stop = hsl.StopDepartures("X", deps)
        self.assertEqual(len(stop.upcoming(now + timedelta(minutes=5, seconds=20), 8)), 4)  # 10 s past: kept
        self.assertEqual(len(stop.upcoming(now + timedelta(minutes=6), 8)), 3)

    def test_stop_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text('''
[location]
lat = 60.2
lon = 24.9
[screens.main]
grid = """
now  now   now   now
one  two   three four
"""
[widgets.now]
type = "fmi_weather_now"
[widgets.one]
type = "hsl_stops"
name = "One"
id = "HSL:1"
routes = [7, "9"]
[widgets.two]
type = "hsl_stops"
name = "Two"
ids = ["HSL:2", "HSL:3"]
[widgets.three]
type = "hsl_stops"
name = "Three"
codes = ["H0665", "H0664"]
[widgets.four]
type = "hsl_stops"
codes = "H0664"
[widgets.unused]
type = "hsl_stops"
codes = "H0001"
''')
            (Path(d) / "config.local.toml").write_text('[digitransit]\nkey = "secret"\n')
            from unittest import mock
            with mock.patch.dict("os.environ"):
                os.environ.pop("GLANCE_DIGITRANSIT_KEY", None)  # it would win over the file
                cfg = config.load(path)
                os.environ["GLANCE_DIGITRANSIT_KEY"] = "from-env"
                self.assertEqual(config.load(path).source_settings.transit.key, "from-env")
                self.assertEqual(config.load(path, local_overrides=False).source_settings.transit.key, None)
        self.assertEqual(cfg.source_settings.transit.key, "secret")
        stops = cfg.wanted(hsl.StopRequest)  # what the stop widgets ask the departures source for
        self.assertEqual(list(stops), ["one", "two", "three", "four"])  # layout order; unused left out
        self.assertEqual(stops["one"], hsl.StopRequest("One", ("HSL:1",), frozenset({"7", "9"})))
        self.assertEqual(stops["two"].ids, ("HSL:2", "HSL:3"))
        self.assertEqual(stops["three"].ids, ("H0665", "H0664"))
        self.assertEqual(stops["four"], hsl.StopRequest("", ("H0664",)))
        self.assertEqual(cfg.widgets["now"], widgets.FmiWeatherNow())

    def test_stop_code_from_geocoding(self):
        # Shape of a real geocoding response for a stop code: the HSL feed's stop
        # comes first, then the national (MATKA) feed's copies of it.
        def feature(source, ref, code):
            return {"properties": {"source": source, "id": f"GTFS:{ref}#{code}",
                                   "addendum": {"GTFS": {"code": code, "modes": ["BUS"]}}}}
        data = {"features": [
            feature("gtfsmatka", "MATKA:123456", "H0665"),
            feature("gtfshsl", "HSL:1174410", "H0664"),
            feature("gtfshsl", "HSL:1174411", "H0665"),
        ]}
        self.assertEqual(hsl.stop_id_from_geocoding(data, "h0665"), "HSL:1174411")
        self.assertIsNone(hsl.stop_id_from_geocoding(data, "H9999"))
        self.assertIsNone(hsl.stop_id_from_geocoding({}, "H0665"))
        self.assertTrue(hsl.is_gtfs_id("HSL:1174411"))
        self.assertFalse(hsl.is_gtfs_id("H0665"))


class EleringTest(unittest.TestCase):
    """Finland's spot prices from Elering, against answers written in its shape."""

    # 2027-01-19 00:00 and 00:15 Finnish time, in Unix seconds.
    T0, T1 = 1800309600, 1800310500

    def answer(self, payload) -> bytes:
        return json.dumps(payload).encode()

    def test_finlands_prices_are_taken_and_the_other_areas_left(self):
        body = self.answer({"success": True, "data": {
            "ee": [{"timestamp": self.T0, "price": 999.0}],
            "fi": [{"timestamp": self.T1, "price": 52.5}, {"timestamp": self.T0, "price": -3.25}],
            "lv": [], "lt": [],
        }})
        prices = elering.parse(body)
        self.assertEqual(prices, (
            elering.Price(datetime(2027, 1, 19, 0, 0, tzinfo=HELSINKI).astimezone(UTC), -3.25),
            elering.Price(datetime(2027, 1, 19, 0, 15, tzinfo=HELSINKI).astimezone(UTC), 52.5),
        ))

    def test_prices_that_cant_be_right_are_left_out(self):
        body = b'{"success": true, "data": {"fi": [' + b", ".join([
            b'{"timestamp": %d, "price": 40.0}' % self.T0,   # the one good price
            b'{"timestamp": %d, "price": NaN}' % self.T1,
            b'{"timestamp": %d, "price": Infinity}' % self.T1,
            b'{"timestamp": %d, "price": 20000.0}' % self.T1,  # beyond any market limit
            b'{"timestamp": %d, "price": "40"}' % self.T1,      # text, not a number
            b'{"timestamp": %d, "price": true}' % self.T1,
            b'{"timestamp": 0, "price": 40.0}',                 # 1970
            b'{"timestamp": 99999999999999999999, "price": 40.0}',
            b'{"timestamp": %d, "price": 1%s}' % (self.T1, b"0" * 400),  # too big for a float
            b'{"price": 40.0}', b'"a string"', b'null',
        ]) + b"]}}"
        self.assertEqual([p.eur_per_mwh for p in elering.parse(body)], [40.0])

    def test_any_bad_answer_is_an_elering_error(self):
        for body in (b"<html>busy</html>", b"[]", b"[" * 200_000, self.answer({"success": False, "data": {}}),
                     self.answer({"success": True, "data": {"ee": []}}),
                     self.answer({"success": True, "data": {"fi": "none"}}),
                     self.answer({"success": True, "data": []})):
            with self.subTest(body=body[:40]), self.assertRaises(elering.EleringError):
                elering.parse(body)
        empty = self.answer({"success": True, "data": {"fi": []}})
        with mock.patch("glance.upstream._open", return_value=io.BytesIO(empty)), \
                self.assertRaises(elering.EleringError):
            elering.fetch(HELSINKI, datetime.now(UTC))
        with mock.patch("glance.upstream._open", side_effect=http.client.IncompleteRead(b"{")), \
                self.assertRaises(elering.EleringError):
            elering.fetch(HELSINKI, datetime.now(UTC))

    def test_the_request_carries_only_dates_from_local_midnight(self):
        body = self.answer({"success": True, "data": {"fi": [
            {"timestamp": self.T0, "price": 1.0},
            {"timestamp": self.T0 + 3 * 24 * 3600, "price": 2.0},  # not asked for: left out
        ]}})
        with mock.patch("glance.upstream._open", return_value=io.BytesIO(body)) as opened:
            prices = elering.fetch(HELSINKI, datetime(2027, 1, 19, 16, 42, tzinfo=HELSINKI))
        self.assertEqual([p.eur_per_mwh for p in prices.prices], [1.0])
        url = urllib.parse.urlsplit(opened.call_args.args[0].full_url)
        self.assertEqual(url.netloc, "dashboard.elering.ee")
        self.assertEqual(urllib.parse.parse_qs(url.query), {
            "start": ["2027-01-18T22:00:00.000Z"],  # midnight in Helsinki
            "end": ["2027-01-20T21:59:59.999Z"],    # the end of tomorrow there
        })

    def test_the_source_keeps_its_last_good_prices(self):
        from glance.sources.elering import Electricity
        cfg = isolated_config(self, stops=[], spot=True)
        now = datetime.now(UTC)
        good = elering.Prices(now, (elering.Price(now - timedelta(minutes=5), 50.0),))
        source = Electricity(cfg)
        source.prices = good
        with mock.patch("glance.upstream._open", return_value=io.BytesIO(b"<html>busy</html>")), \
                self.assertLogs("glance", "WARNING"):
            source.refresh(force=True)
        self.assertIs(source.prices, good)
        self.assertEqual(Electricity(cfg).prices, None)  # nothing was cached by the failure
        with mock.patch.object(elering, "fetch", return_value=good):
            source.refresh(force=True)
        self.assertEqual(Electricity(cfg).prices, good)  # a restart starts from the cache

    def test_prices_that_dont_cover_now_are_fetched_again_at_once(self):
        from glance.sources.elering import Electricity
        cfg = isolated_config(self, stops=[], spot=True)
        now = datetime.now(UTC)
        source = Electricity(cfg)
        fresh = elering.Prices(now, (elering.Price(now - timedelta(minutes=5), 50.0),))
        with mock.patch.object(elering, "fetch", return_value=fresh) as fetch:
            source.refresh()
            source.refresh()  # covers now, fetched a moment ago: not again within the hour
        self.assertEqual(fetch.call_count, 1)
        ended = elering.Prices(now, (elering.Price(now - timedelta(hours=2), 50.0),))  # fetched now, but over
        source = Electricity(cfg)
        source.prices = ended
        with mock.patch.object(elering, "fetch", return_value=fresh) as fetch:
            source.refresh()
        self.assertEqual(fetch.call_count, 1)

    def test_prices_that_dont_cover_now_are_fetched_again_soon_after_a_fetch(self):
        # A fetch can succeed before tomorrow's prices are out; past midnight they don't cover now.
        from glance.sources.elering import Electricity
        source = Electricity(isolated_config(self, stops=[], spot=True))
        now = datetime.now(UTC)
        ended = elering.Prices(now, (elering.Price(now - timedelta(hours=2), 50.0),))
        ticks = [1000.0, 1000.0 + 60, 1000.0 + 121]  # time.monotonic() at each refresh
        with mock.patch.object(elering, "fetch", return_value=ended) as fetch, \
                mock.patch("glance.sources.periodic.time.monotonic", side_effect=ticks):
            source.refresh()  # succeeds, but nothing covers now
            source.refresh()  # a minute later: not yet
            source.refresh()  # after Periodic.RETRY_AFTER, not the hour
        self.assertEqual(fetch.call_count, 2)

    def test_the_cache_is_checked_as_the_answer_is(self):
        from glance.sources.elering import Electricity
        cfg = isolated_config(self, stops=[], spot=True)
        path = cfg.cache_dir / "spot_prices.json"
        good = '["2027-01-19T14:30:00+00:00", 50.0]'
        for fetched_at, rows in (
            ("2027-01-19T14:00:00", good),                                  # no time zone
            ("2027-01-19T14:00:00+00:00", '["2027-01-19T14:30:00+00:00", 1e308]'),  # beyond any market limit
            ("2027-01-19T14:00:00+00:00", '["2027-01-19T14:30:00", 50.0]'),          # a start with no zone
            ("2027-01-19T14:00:00+00:00", '["1970-01-01T00:00:00+00:00", 50.0]'),
            ("2027-01-19T14:00:00+00:00", '[50.0]'),
        ):
            with self.subTest(fetched_at=fetched_at, rows=rows):
                path.write_text(f'{{"fetched_at": "{fetched_at}", "prices": [{good}, {rows}]}}')
                with self.assertLogs("glance", "WARNING"):
                    self.assertIsNone(Electricity(cfg).prices)
        path.write_text('{"fetched_at": "2027-01-19T14:00:00+00:00", "prices": ['
                        '["2027-01-19T14:45:00+00:00", 60.0], ["2027-01-19T16:30:00+02:00", 50.0]]}')
        self.assertEqual([p.start.isoformat() for p in Electricity(cfg).prices.prices],
                         ["2027-01-19T14:30:00+00:00", "2027-01-19T14:45:00+00:00"])  # in order, in UTC

    def test_status_bar_says_when_there_is_no_price_for_now(self):
        from glance.sources import Sources
        cfg = isolated_config(self, stops=[], spot=True)
        sources = Sources.make(cfg)
        now = datetime(2027, 1, 19, 16, 42, tzinfo=HELSINKI)
        self.assertEqual(sources.warnings(now, "en"), ("Spot price unavailable",))
        self.assertEqual(sources.warnings(now, "fi"), ("Pörssisähkön hintaa ei saatavilla",))
        start = datetime(2027, 1, 19, 16, 30, tzinfo=HELSINKI).astimezone(UTC)
        sources.electricity.prices = elering.Prices(start - timedelta(days=1), (elering.Price(start, 50.0),))
        self.assertEqual(sources.warnings(now, "en"), ())  # a day-old fetch, but its price covers now
        self.assertEqual(sources.warnings(now + timedelta(minutes=15), "en"), ("Spot price unavailable",))

    def test_without_a_spot_price_widget_nothing_is_fetched_or_warned_about(self):
        from glance.sources import Sources
        cfg = isolated_config(self, stops=[], weather=True)
        sources = Sources.make(cfg)
        self.assertNotIn(sources.electricity, sources.periodic)  # the refresher and `render` leave it
        self.assertEqual(sources.warnings(datetime(2027, 1, 19, 16, 42, tzinfo=HELSINKI), "en"), ("Weather unavailable",))

    def test_the_scene_carries_the_prices_to_the_widget(self):
        from glance.__main__ import scene
        from glance.sources import Sources
        cfg = isolated_config(self, stops=[], spot=True)
        sources = Sources.make(cfg)
        prices = elering.Prices(datetime.now(UTC), (elering.Price(datetime.now(UTC), 50.0),))
        sources.electricity.prices = prices
        self.assertIs(scene(cfg, sources).spot_prices, prices)


class SourcesTest(unittest.TestCase):
    def test_sources_reach_the_network_only_through_upstream(self):
        # A source that opened its own connection would skip the deadline,
        # the size limit and the refused redirects that upstream.read keeps.
        import re
        sources = Path(__file__).parent.parent / "glance" / "sources"
        direct = re.compile(r"urlopen|build_opener|HTTPS?Connection\(|\bsocket\.|\bopen_url|requests\.")
        modules = sorted(sources.glob("*.py"))
        self.assertIn("hsl.py", [m.name for m in modules])
        for module in modules:
            with self.subTest(module.name):
                self.assertEqual(direct.findall(module.read_text()), [], "fetch through glance.upstream.read")


class UpstreamTest(unittest.TestCase):
    """The limits on reading a provider's answer, against a server of our own."""

    tls = False

    def setUp(self):
        import threading, time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        test = self
        test.seen = []

        class Provider(BaseHTTPRequestHandler):
            def do_GET(self):
                test.seen.append((self.path, self.headers.get("digitransit-subscription-key")))
                if self.path == "/dripping-headers":  # a byte at a time, each within any per-read timeout
                    try:
                        for byte in b"HTTP/1.1 200 OK\r\nX-Padding: " + b"x" * 250 + b"\r\nContent-Length: 2\r\n\r\nok":
                            self.wfile.write(bytes([byte]))
                            time.sleep(0.02)
                    except OSError:
                        pass  # the client gave up, as it should
                    return
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/ok")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", "3000")
                self.end_headers()
                try:
                    if self.path == "/ok":
                        self.wfile.write(b"x" * 3000)
                    elif self.path == "/cut-short":
                        self.wfile.write(b"x" * 10)
                    elif self.path == "/dripping":  # a byte at a time, each within any per-read timeout
                        for _ in range(3000):
                            self.wfile.write(b"x")
                            time.sleep(0.02)
                except OSError:
                    pass  # the client gave up, as it should

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        if self.tls:
            import ssl
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(DATA / "localhost.pem")
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def read(self, path, **limits):
        import ssl
        import urllib.request
        from unittest import mock
        from glance import upstream
        # As upstream's own opener, but never through a proxy from this
        # machine's environment (http_proxy), which can't reach this server,
        # and trusting the test certificate.
        handlers = upstream._handlers(ssl.create_default_context(cafile=DATA / "localhost.pem"))
        direct = urllib.request.build_opener(*handlers, urllib.request.ProxyHandler({}))
        self.enterContext(mock.patch.object(upstream, "_opener", direct))
        scheme = "https" if self.tls else "http"
        req = urllib.request.Request(f"{scheme}://127.0.0.1:{self.server.server_address[1]}{path}",
                                     headers={"digitransit-subscription-key": "secret"})
        return upstream.read(req, **{"timeout": 2, "deadline": 5, **limits})

    def test_reads_the_answer(self):
        self.assertEqual(self.read("/ok"), b"x" * 3000)

    def test_providers_are_read_with_these_limits(self):
        # The tests above use an opener of their own (no proxy, a trusted test
        # certificate) built from the same handlers as the real one.
        from glance import upstream
        kinds = {type(h) for h in upstream._opener.handlers}
        self.assertLessEqual({type(h) for h in upstream._handlers()}, kinds)

    def test_too_large(self):
        with self.assertRaisesRegex(OSError, "larger"):
            self.read("/ok", max_bytes=1000)

    def test_too_slow(self):
        import time
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.read("/dripping", deadline=0.3)
        self.assertLess(time.monotonic() - started, 2)  # not the minute the whole answer would take

    def test_headers_sent_too_slowly_time_out(self):
        # The status line and headers take about 6 s to arrive, though no
        # single read waits longer than the per-read timeout.
        import time
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.read("/dripping-headers", deadline=0.3)
        self.assertLess(time.monotonic() - started, 2)

    def test_cut_short(self):
        import http.client
        with self.assertRaises(http.client.IncompleteRead):
            self.read("/cut-short")

    def test_redirect_is_refused(self):
        # urllib would follow it and send the key along.
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as refused:
            self.read("/redirect")
        self.assertEqual(refused.exception.code, 302)
        self.assertEqual(self.seen, [("/redirect", "secret")])


class UpstreamTLSTest(UpstreamTest):
    """The same limits over HTTPS, as both providers are reached."""

    tls = True


class LayoutTest(unittest.TestCase):
    def test_grid(self):
        lay = layout.parse("""
            weather weather hours hours hours hours
            trams   trams   buses buses .     .
            trams   trams   buses buses small .
        """)
        self.assertEqual((lay.cols, lay.rows), (6, 3))
        self.assertEqual(lay.names, ("weather", "hours", "trams", "buses", "small"))  # reading order
        self.assertEqual(lay.areas[2], layout.Area("trams", 0, 1, 2, 2))
        self.assertEqual(lay.areas[4], layout.Area("small", 4, 2, 1, 1))

    def test_bad_grids(self):
        for grid, message in (
            ("", "empty"),
            ("a a\nb", "line 2 has 1 cells"),
            ("a a\na b", "'a' doesn't fill a rectangle"),  # L shape
            ("a b a", "'a' doesn't fill a rectangle"),  # in two places
        ):
            with self.assertRaisesRegex(ValueError, message):
                layout.parse(grid)

    def test_area_pixels(self):
        lay = layout.parse("a a b b b b\nc c d d e e\nc c d d e e")
        rects = [lay.rect(a) for a in lay.areas]
        # The fixed 6x3 grid's cells before layouts were configurable.
        self.assertEqual(rects[:3], [(0, 30, 267, 180), (267, 30, 800, 180), (0, 180, 267, 480)])
        halves = layout.parse("a\nb\nb\nb\nb\nc")  # 6 rows: half a 3-row grid's row each
        self.assertEqual([halves.rect(a)[1::2] for a in halves.areas], [(30, 105), (105, 405), (405, 480)])

    def test_bad_widget_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            base = "[location]\nlat = 60.2\nlon = 24.9\n"
            for bad, message in (
                ("", "no screens"),
                ('[screens.s]\n', "'s': .* needs a grid"),
                ('[screens.s]\ngrid = "a"\n', "screen 's': .*no \\[widgets.a\\]"),
                ('[screens.s]\ngrid = "a"\n[widgets.a]\ntype = "clock"\n', "type must be one of"),
                ('[screens.s]\ngrid = "a"\n[widgets.a]\ntype = "hsl_stops"\ncodez = ["H0665"]\n', "unknown setting.*codez"),
                ('[screens.s]\ngrid = "."\nwidth = 3\n', "screen 's': unknown setting.*width"),
                ('[screens.s]\ngrid = "."\n[[stops]]\ncodes = ["H0665"]\n', "stops.*no longer used"),
                ('[screens.s]\ngrid = "."\n[weather]\nrefresh_minutes = 15\n', "weather.*no longer used"),
                ('[layout]\ngrid = "."\n', "layout.*no longer used.*screens"),
                ('[screens.s]\ngrid = "."\n[[device.schedule]]\nfrom = "07:00"\nto = "09:00"\n', "device.schedule.*no longer used"),
                ('[screens.s]\ngrid = "."\n[device]\nwake_interval_minutes = 5\n', "wake_interval_minutes is no longer used"),
                # Decks and the schedule refer to what exists.
                ('[screens.s]\ngrid = "."\n[decks.d]\nscreens = ["s", "t"]\n', "deck 'd': there is no \\[screens.t\\]"),
                ('[screens.s]\ngrid = "."\n[decks.d]\nscreens = []\n', "deck 'd': screens = .* is missing"),
                ('[screens.s]\ngrid = "."\n[decks.d]\nscreens = ["s", "s"]\n', "listed twice"),
                ('[screens.s]\ngrid = "."\n[screens.t]\ngrid = "."\n[decks.d]\nscreens = ["s"]\nstart = "t"\n', "start = 't' is not one of"),
                ('[screens.s]\ngrid = "."\n[schedule]\ndefault_deck = "night"\n', "default_deck: there is no \\[decks.night\\]"),
                ('[screens.s]\ngrid = "."\n[[schedule.rules]]\nfrom = "07:00"\nto = "09:00"\ndeck = "x"\n', "rule 1: there is no \\[decks.x\\]"),
                ('[screens.s]\ngrid = "."\n[[schedule.rules]]\nfrom = "07:00"\n', "rule 1: to = .* is missing"),
                ('[screens.s]\ngrid = "."\n[[schedule.rules]]\nfrom = "07:00"\nto = "09:00"\ndeck_name = "x"\n', "rule 1: unknown setting.*deck_name"),
            ):
                path.write_text(base + bad)
                with self.assertRaisesRegex(ValueError, message):
                    config.load(path)

    def test_local_setup_replaces_the_examples(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text('[location]\nlat = 60.2\nlon = 24.9\n[screens.home]\ngrid = "trams buses"\n'
                            '[widgets.trams]\ntype = "hsl_stops"\nname = "Example"\ncodes = ["H0665"]\n'
                            '[widgets.buses]\ntype = "hsl_stops"\ncodes = ["H2100"]\n'
                            '[decks.night]\nscreens = ["home"]\n'
                            '[schedule]\nwake_interval_minutes = 5\n'
                            '[[schedule.rules]]\nfrom = "23:00"\nto = "06:00"\nevery = 15\ndeck = "night"\n')
            local = Path(d) / "config.local.toml"
            local.write_text('[screens.mine]\ngrid = "trams"\n[widgets.trams]\ntype = "hsl_stops"\ncodes = ["H0086"]\n')
            cfg = config.load(path)
            self.assertEqual(cfg.wanted(hsl.StopRequest), {"trams": hsl.StopRequest("", ("H0086",))})  # no example name leaked
            self.assertEqual(list(cfg.screens), ["mine"])
            self.assertEqual(cfg.decks, {"all": decks.Deck("all", ("mine",), "mine")})  # no example deck
            # No example rule pointing at an example deck, nor its intervals.
            self.assertEqual(cfg.wake_schedule, schedule.WakeSchedule((), 1, "all"))
            local.write_text('[widgets.trams]\ntype = "hsl_stops"\ncodes = ["H0086"]\n')
            with self.assertRaisesRegex(ValueError, "no screens"):  # only widgets: the screens must come too
                config.load(path)
            local.write_text('[digitransit]\nkey = "k"\n')  # nothing of the setup: the examples stay
            self.assertEqual(list(config.load(path, local_overrides=True).screens), ["home"])


def quarter_prices(first: datetime, values: list[float]) -> elering.Prices:
    """Prices (€/MWh) for consecutive quarter hours from `first`."""
    start = first.astimezone(UTC)
    return elering.Prices(start, tuple(elering.Price(start + i * timedelta(minutes=15), v) for i, v in enumerate(values)))


class SunriseSunsetTest(unittest.TestCase):
    def test_the_scene_has_todays_sun_at_the_configured_location(self):
        from glance.__main__ import scene
        from glance.sources import Sources
        cfg = isolated_config(self, stops=[], lat=69.90864, lon=27.02843)  # Utsjoki
        sources = Sources.make(cfg)
        winter = scene(cfg, sources, datetime(2026, 12, 21, 12, tzinfo=HELSINKI)).sun
        self.assertEqual((winter.date, winter.polar), (date(2026, 12, 21), "night"))
        autumn = scene(cfg, sources, datetime(2026, 10, 25, 12, tzinfo=HELSINKI)).sun
        # FMI's sunrise for Utsjoki that day: 08:07:38.
        self.assertLess(abs(autumn.sunrise - datetime(2026, 10, 25, 8, 7, 38, tzinfo=HELSINKI)), timedelta(minutes=1))

    def time_row_greys(self, now: datetime, rising: bool, place: tuple[float, float] = (60.16952, 24.93545)) -> set[int]:
        """The greys drawn in the sunrise's time (the upper row) or the sunset's,
        right of its sun (by default in Helsinki, on 18 November: 08:26:28 and
        15:43:36). At 266 px wide the day's length is one line."""
        from PIL import Image, ImageDraw
        d = sun.day(*place, now.date(), HELSINKI)
        sc = render.Scene(now=now, language="fi", forecast=None, stops={}, sun=d)
        img = Image.new("L", (266, 150), 255)
        sunrise_sunset.draw(img, ImageDraw.Draw(img), sc, (0, 0, 266, 150), "u", widgets.SunriseSunset())
        img = draw.quantise(img)
        top = round(75 - (2 * sunrise_sunset.ROW + sunrise_sunset.GAP + sunrise_sunset.LINE) / 2)
        if not rising:
            top += sunrise_sunset.ROW
        box = (sunrise_sunset.PAD + sunrise_sunset.ICON_W + sunrise_sunset.ICON_GAP, top, 266, top + sunrise_sunset.ROW)
        return {grey for _, grey in img.crop(box).getcolors()}

    def test_a_time_turns_light_grey_from_its_own_minute(self):
        # Shown as 08.26 and 15.44, rounded to the nearest minute.
        self.assertIn(draw.BLACK, self.time_row_greys(datetime(2026, 11, 18, 8, 25, 59, tzinfo=HELSINKI), rising=True))
        self.assertEqual(self.time_row_greys(datetime(2026, 11, 18, 8, 26, tzinfo=HELSINKI), rising=True),
                         {draw.LIGHT, draw.WHITE})
        self.assertIn(draw.BLACK, self.time_row_greys(datetime(2026, 11, 18, 8, 26, tzinfo=HELSINKI), rising=False))
        self.assertIn(draw.BLACK, self.time_row_greys(datetime(2026, 11, 18, 15, 43, 59, tzinfo=HELSINKI), rising=False))
        self.assertEqual(self.time_row_greys(datetime(2026, 11, 18, 15, 44, tzinfo=HELSINKI), rising=False),
                         {draw.LIGHT, draw.WHITE})

    def test_no_sunrise_is_grey_since_nothing_is_to_come(self):
        utsjoki = (69.90864, 27.02843)  # 27 July: up all night, sets at 00.43 on the 28th
        noon = datetime(2026, 7, 27, 12, tzinfo=HELSINKI)
        self.assertEqual(self.time_row_greys(noon, rising=True, place=utsjoki), {draw.LIGHT, draw.WHITE})
        self.assertIn(draw.BLACK, self.time_row_greys(noon, rising=False, place=utsjoki))

    def test_in_one_column_nothing_is_cut_short_all_year(self):
        # One column of a six-column screen is 133 px wide, and the smallest
        # area the widget takes is narrower still: on every day of a year in
        # Helsinki and in Utsjoki, nothing below the times ends in "…", and the
        # widest time fits beside its sun.
        from glance.i18n import clock
        width = widgets.SunriseSunset.MIN_SIZE[0] - 2 * sunrise_sunset.PAD
        for lat, lon in ((60.16952, 24.93545), (69.90864, 27.02843)):
            for k in range(365):
                d = sun.day(lat, lon, date(2026, 1, 1) + timedelta(days=k), HELSINKI)
                for language in ("en", "fi"):
                    with self.subTest(lat=lat, on=d.date, language=language):
                        _, lines = sunrise_sunset.fitting(*sunrise_sunset.line_choices(d, language), width)
                        self.assertFalse(any(line.endswith("…") for line in lines))
        f = draw.font("display", 20)
        midnight = datetime(2026, 1, 1)
        widest = max((clock(language, midnight + timedelta(minutes=m)) for m in range(24 * 60) for language in ("en", "fi")),
                     key=f.getlength)
        self.assertLessEqual(f.getlength(widest), width - sunrise_sunset.ICON_W - sunrise_sunset.ICON_GAP)

    def test_the_suns_tell_rising_from_setting_when_past_too(self):
        icons = {(rising, past): sunrise_sunset.horizon(rising, past) for rising in (True, False) for past in (True, False)}
        self.assertEqual({icon.width for icon in icons.values()}, {sunrise_sunset.ICON_W})
        for past in (False, True):
            self.assertNotEqual(icons[True, past].tobytes(), icons[False, past].tobytes())
        for rising in (True, False):  # a passed sun has no black
            self.assertNotIn(draw.BLACK, {grey for _, grey in draw.quantise(icons[rising, True]).getcolors()})

    def test_both_times_are_drawn_the_same_size(self):
        # 11.11 alone would fit bigger than 23.59 beside it.
        width = 125 - 2 * sunrise_sunset.PAD - sunrise_sunset.ICON_W - sunrise_sunset.ICON_GAP
        f = sunrise_sunset.largest("display", ["11.11", "23.59"], range(36, 19, -1), width)
        self.assertEqual(f.size, sunrise_sunset.largest("display", ["23.59"], range(36, 19, -1), width).size)
        self.assertLess(f.size, sunrise_sunset.largest("display", ["11.11"], range(36, 19, -1), width).size)

    def test_without_the_sun_in_the_scene_it_fails_loudly(self):
        from PIL import Image, ImageDraw
        sc = render.Scene(now=datetime(2026, 11, 18, 12, tzinfo=HELSINKI), language="fi", forecast=None, stops={})
        img = Image.new("L", (266, 150), 255)
        with self.assertRaises(ValueError):
            sunrise_sunset.draw(img, ImageDraw.Draw(img), sc, (0, 0, 266, 150), "u", widgets.SunriseSunset())


class SpotPricesTest(unittest.TestCase):
    NOW = datetime(2027, 1, 19, 16, 42, 30, tzinfo=HELSINKI)

    def test_a_price_is_shown_in_cents_per_kwh_with_vat(self):
        self.assertAlmostEqual(spot_prices.cents(100.0), 12.55)  # 100 €/MWh = 10 c/kWh, plus 25.5 %
        self.assertAlmostEqual(spot_prices.cents(-20.0), -2.51)

    def test_decimals_in_english_and_finnish(self):
        self.assertEqual(i18n.decimal("en", 12.34), "12.3")
        self.assertEqual(i18n.decimal("fi", 12.34), "12,3")
        self.assertEqual(i18n.decimal("fi", -2.84), "−2,8")  # a real minus sign
        self.assertEqual(i18n.decimal("en", -0.04), "0.0")  # no minus on a zero

    def test_the_price_now_is_the_quarter_now_falls_in(self):
        start = datetime(2027, 1, 19, 16, 30, tzinfo=HELSINKI)
        prices = quarter_prices(start, [10.0, 20.0])
        self.assertEqual(prices.at(start).eur_per_mwh, 10.0)
        self.assertEqual(prices.at(start + timedelta(minutes=15) - timedelta(microseconds=1)).eur_per_mwh, 10.0)
        self.assertEqual(prices.at(start + timedelta(minutes=15)).eur_per_mwh, 20.0)
        self.assertIsNone(prices.at(start - timedelta(microseconds=1)))
        self.assertIsNone(prices.at(start + timedelta(minutes=30)))

    def test_bars_cover_the_next_24_hours_from_the_quarter_now(self):
        first = datetime(2027, 1, 19, 16, 30, tzinfo=HELSINKI)
        prices = quarter_prices(first, [100.0] * 4 + [200.0] * 60)  # 16 hours known, then none
        quarters = spot_prices.bars(prices, self.NOW)
        self.assertEqual(len(quarters), 96)
        self.assertEqual(quarters[0][0], first.astimezone(UTC))
        self.assertAlmostEqual(quarters[0][1], 12.55)
        self.assertAlmostEqual(quarters[4][1], 25.1)
        self.assertIsNone(quarters[64][1])  # not published yet

    def test_bars_span_24_real_hours_on_a_daylight_saving_day(self):
        now = datetime(2027, 3, 28, 1, 50, tzinfo=HELSINKI)  # clocks go from 03.00 to 04.00 tonight
        prices = quarter_prices(datetime(2027, 3, 28, 1, 45, tzinfo=HELSINKI), [50.0] * 96)
        quarters = spot_prices.bars(prices, now)
        self.assertEqual(len(quarters), 96)
        self.assertEqual(quarters[-1][0] - quarters[0][0], timedelta(hours=23, minutes=45))
        self.assertTrue(all(c is not None for _, c in quarters))
        self.assertEqual(quarters[-1][0].astimezone(HELSINKI).hour, 2)  # 01.45 + 24 h, an hour later on the wall

    def test_only_the_unknown_end_is_marked_as_not_published(self):
        first = datetime(2027, 1, 19, 16, 30, tzinfo=HELSINKI)
        prices = quarter_prices(first, [50.0] * 64)
        holed = replace(prices, prices=prices.prices[:10] + prices.prices[11:])  # one quarter left out
        self.assertEqual(spot_prices.unpublished_from(spot_prices.bars(holed, self.NOW)), 64)
        whole_day = quarter_prices(first, [50.0] * 96)
        self.assertIsNone(spot_prices.unpublished_from(spot_prices.bars(whole_day, self.NOW)))

    def draws(self, sc: render.Scene, size: tuple[int, int]) -> tuple[list, list]:
        """The spot price widget's texts (text, x, font, align) and its outlined boxes."""
        from PIL import Image, ImageDraw
        img = Image.new("L", size, 255)
        d = mock.Mock(wraps=ImageDraw.Draw(img))
        with mock.patch.object(spot_prices, "text_vcenter", wraps=spot_prices.text_vcenter) as text:
            spot_prices.draw(img, d, sc, (0, 0, *size), "w", widgets.SpotPrices())
        texts = [(c.args[3], c.args[1], c.args[4], c.args[6] if len(c.args) > 6 else "l") for c in text.call_args_list]
        boxes = [c.args[0] for c in d.rectangle.call_args_list if c.kwargs.get("outline") is not None]
        return texts, boxes

    def test_the_cheapest_hours_price_stays_whole_beside_the_unpublished_part(self):
        morning = datetime(2027, 1, 19, 8, 12, tzinfo=HELSINKI)
        values = [100.0] * 60 + [-95.0] * 4  # 08-24: the last hour, 23-24, is the cheapest; tomorrow isn't out
        prices = quarter_prices(datetime(2027, 1, 19, 8, 0, tzinfo=HELSINKI), values)
        sc = render.Scene(now=morning, language="fi", forecast=None, stops={}, spot_prices=prices)
        texts, boxes = self.draws(sc, widgets.SpotPrices.MIN_SIZE)
        label = i18n.decimal("fi", spot_prices.cents(-95.0))
        (_, x, f, _), = [t for t in texts if t[0] == label]
        self.assertLessEqual(x + f.getlength(label) / 2, boxes[0][0])  # ends before the outline starts

    def test_the_cheapest_hours_price_is_left_out_where_the_known_part_is_too_narrow(self):
        late_evening = datetime(2027, 1, 19, 23, 0, tzinfo=HELSINKI)  # one hour known, tomorrow not out
        prices = quarter_prices(late_evening, [-95.0] * 4)
        sc = render.Scene(now=late_evening, language="fi", forecast=None, stops={}, spot_prices=prices)
        texts, boxes = self.draws(sc, widgets.SpotPrices.MIN_SIZE)
        label = i18n.decimal("fi", spot_prices.cents(-95.0))
        self.assertEqual([t for t in texts if t[0] == label], [(label, 12 - 2, mock.ANY, "l")])  # only the price now

    def test_tomorrows_prices_still_missing_in_the_afternoon_are_late(self):
        afternoon = datetime(2027, 1, 19, 16, 0, tzinfo=HELSINKI)
        prices = quarter_prices(afternoon, [100.0] * 32)  # to midnight only
        for language, words in (("fi", "Huomisen hinnat myöhässä"), ("en", "Tomorrow's prices late")):
            sc = render.Scene(now=afternoon, language=language, forecast=None, stops={}, spot_prices=prices)
            texts, _ = self.draws(sc, (800, 150))
            with self.subTest(language=language):
                self.assertIn(words, [t[0] for t in texts])
                self.assertNotIn(spot_prices.WORDS[language]["tomorrow"], [t[0] for t in texts])

    def test_a_narrow_unpublished_part_says_when_in_fewer_words(self):
        early = datetime(2027, 1, 19, 6, 0, tzinfo=HELSINKI)  # 6 hours of tomorrow unknown: about 70 px
        prices = quarter_prices(early, [100.0] * 72)
        for language in ("fi", "en"):
            sc = render.Scene(now=early, language=language, forecast=None, stops={}, spot_prices=prices)
            texts, _ = self.draws(sc, widgets.SpotPrices.MIN_SIZE)
            with self.subTest(language=language):
                self.assertIn(spot_prices.WORDS[language]["tomorrow_short"], [t[0] for t in texts])

    def test_the_cheapest_hour_is_a_whole_clock_hour_still_to_come(self):
        first = datetime(2027, 1, 19, 16, 0, tzinfo=HELSINKI)
        values = [80.0] * 96
        values[0:4] = [10.0] * 4  # 16-17: cheapest, but half gone at 16.42
        values[4 * 5:4 * 6] = [20.0, 20.0, 30.0, 30.0]  # 21-22
        prices = quarter_prices(first, values)
        start, price = spot_prices.cheapest_hour(prices, self.NOW)
        self.assertEqual(start, datetime(2027, 1, 19, 21, 0, tzinfo=HELSINKI).astimezone(UTC))
        self.assertAlmostEqual(price, 25 / 10 * 1.255)
        on_the_hour = self.NOW.replace(minute=0, second=0)
        self.assertEqual(spot_prices.cheapest_hour(prices, on_the_hour)[0], first.astimezone(UTC))

    def test_without_a_price_for_now_it_says_so(self):
        from PIL import Image, ImageDraw
        stale = quarter_prices(self.NOW - timedelta(hours=3), [50.0] * 4)  # ended hours ago
        for prices in (None, stale):
            for language, words in (("en", "Spot price unavailable"), ("fi", "Pörssisähkön hintaa ei saatavilla")):
                img = Image.new("L", (800, 150), 255)
                sc = render.Scene(now=self.NOW, language=language, forecast=None, stops={}, spot_prices=prices)
                with mock.patch.object(spot_prices, "text_vcenter") as text:
                    spot_prices.draw(img, ImageDraw.Draw(img), sc, (0, 0, 800, 150), "w", widgets.SpotPrices())
                self.assertEqual([c.args[3] for c in text.call_args_list], [words])


class WidgetSizeTest(unittest.TestCase):
    """Widgets fill their rectangle and stay inside it, down to their minimum size."""

    NOW = datetime(2027, 1, 19, 8, 12, 4, tzinfo=HELSINKI)

    def worst_case(self, language: str, clock: bool) -> render.Scene:
        """Data that takes the most room: long texts, wide temperatures, three modes."""
        now = self.NOW
        symbol = max((s for s in range(1, 100) if fmi_symbols.symbol_info(s, language)[2]),
                     key=lambda s: len(fmi_symbols.symbol_info(s, language)[2]))
        start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=8)
        hours = [fmi.Hour(time=(start + timedelta(hours=i)).astimezone(UTC), temperature=-20.4 - (i % 2) * 8.2,
                          wind_speed=15.4, wind_direction=45.0, pop=100, symbol=symbol, feels_like=-35.4)
                 for i in range(48)]
        rows = [
            ("N550K", "Westendinasema via Otaniemi ja Keilaniemi", 0, True, "bus", True),  # "now"
            ("10", "Kirurginen sairaala, Kaivopuisto", 59 * 60, True, "tram", False),  # "59 min"
            ("I", "Tikkurila-Lentoasema-Huopalahti", 23 * 60, False, "rail", False),
        ] * 8
        deps = sorted((hsl.Departure(r, h, now + timedelta(seconds=t), rt, m, tr) for r, h, t, rt, m, tr in rows),
                      key=lambda d: d.time)
        stop = hsl.StopDepartures("Itäkeskus(M) Kauppakeskus Itis, laituri 12", deps, ("subway", "rail", "tram"))
        return render.Scene(now=now, language=language, forecast=fmi.Forecast(now, hours),
                            stops={"w": stop}, departure_clock=clock)

    def drawn_outside(self, widget, size: tuple[int, int], sc: render.Scene) -> tuple | None:
        """Bounding box of anything drawn outside the rectangle, or None."""
        from PIL import Image, ImageChops, ImageDraw
        img = Image.new("L", (1000, 700), 255)
        rect = (100, 100, 100 + size[0], 100 + size[1])
        widgets.module(widget).draw(img, ImageDraw.Draw(img), sc, rect, "w", widget)
        img = draw.quantise(img)
        ImageDraw.Draw(img).rectangle((rect[0], rect[1], rect[2] - 1, rect[3] - 1), fill=255)
        return ImageChops.invert(img).getbbox()

    def test_stays_inside(self):
        for widget in (widgets.FmiWeatherNow(), widgets.FmiWeatherHourly(), widgets.HslStops()):
            min_w, min_h = widget.MIN_SIZE
            # The minimum, today's sizes, and long thin shapes either way.
            for size in ((min_w, min_h), (267, 150), (533, 150), (267, 300), (800, min_h), (min_w, 450)):
                for language in ("en", "fi"):
                    for clock in (False, True):
                        sc = self.worst_case(language, clock)
                        for case in (sc, replace(sc, forecast=None, stops={"w": hsl.StopDepartures("—", [])})):
                            with self.subTest(widget=type(widget).__name__, size=size, language=language, clock=clock):
                                self.assertIsNone(self.drawn_outside(widget, size, case))

    def test_spot_prices_stay_inside(self):
        widget = widgets.SpotPrices()
        min_w, min_h = widget.MIN_SIZE
        # The market's limits, alternating, with the next day not yet published: the widest
        # numbers ("−62,8", "502,0"), the tallest bars both ways and the outline's words.
        extremes = quarter_prices(self.NOW - timedelta(minutes=15), [-500.0, 4000.0] * 40)
        for size in ((min_w, min_h), (533, 150), (800, 150), (min_w, 450), (800, min_h)):
            for language in ("en", "fi"):
                for prices in (extremes, None):
                    sc = replace(self.worst_case(language, False), spot_prices=prices)
                    with self.subTest(size=size, language=language, prices=prices is not None):
                        self.assertIsNone(self.drawn_outside(widget, size, sc))

    def test_sunrise_and_sunset_stay_inside(self):
        widget = widgets.SunriseSunset()
        min_w, min_h = widget.MIN_SIZE
        on = self.NOW.date()
        # The widest a day gets: two times of four digits, a day of 23 h 59 min
        # whose change is over an hour, both polar spells, and the night the
        # sun stays up (no sunrise).
        widest = sun.Day(on, self.NOW.replace(hour=20, minute=0), self.NOW.replace(hour=23, minute=58),
                         timedelta(hours=23, minutes=59), -timedelta(hours=23, minutes=58), None)
        days = (
            widest,
            sun.Day(on, None, None, timedelta(), timedelta(minutes=-58), "night", self.NOW + timedelta(days=50)),
            sun.Day(on, None, None, timedelta(days=1), timedelta(minutes=58), "day", self.NOW + timedelta(days=50)),
            sun.Day(on, None, None, timedelta(days=1), timedelta(), "day", None),
            replace(widest, sunrise=None),
        )
        for size in ((min_w, min_h), (133, 150), (267, 150), (533, 150), (min_w, 450), (800, min_h)):
            for language in ("en", "fi"):
                for d in days:
                    sc = replace(self.worst_case(language, False), sun=d)
                    with self.subTest(size=size, language=language, day=d):
                        self.assertIsNone(self.drawn_outside(widget, size, sc))

    def test_fill(self):
        self.assertEqual(fmi_weather_hourly.hours_fitting(533), 12)  # 4 of 6 columns: as before layouts were configurable
        self.assertEqual(fmi_weather_hourly.hours_fitting(267), 6)
        self.assertEqual(fmi_weather_hourly.hours_fitting(widgets.FmiWeatherHourly.MIN_SIZE[0]), 3)
        self.assertEqual(hsl_stops.departures_fitting(300), 8)  # 2 of 3 rows: as before
        self.assertEqual(hsl_stops.departures_fitting(450), 12)
        self.assertEqual(hsl_stops.departures_fitting(widgets.HslStops.MIN_SIZE[1]), 1)

    def test_too_small_fails_at_load(self):
        sections = {"w": {"type": "fmi_weather_now"}, "h": {"type": "fmi_weather_hourly"},
                    "s": {"type": "hsl_stops"}, "x": {"type": "hsl_stops"}, "u": {"type": "sunrise_sunset"}}

        def load(grid):
            return config._setup({"screens": {"s": {"grid": grid}}, "widgets": sections})

        load("w w w h h h h\ns s s s s s s")  # 3 of 7 columns: 343 px, enough
        for grid, message in (
            ("w h h h h h h\ns s s s s s s",
             r"'w' \(fmi_weather_now\) needs at least 245×140 px; in this 7×2 grid it gets 114×225: give it 3 columns"),
            ("s\n" * 7 + "x", r"'x' \(hsl_stops\) .* 1×8 grid it gets 800×56: give it 2 rows"),
            (".\n" * 8 + "x", r"'x' .* 1×9 grid it gets 800×50: give it 2 rows"),
            ("h" + " ." * 11 + "\n" + (". " * 12 + "\n") * 5,
             r"'h' .* 12×6 grid it gets 67×75: give it 3 columns and 2 rows"),
            ("w h\n" * 6 + "u h", r"'u' \(sunrise_sunset\) needs at least 125×130 px; in this 2×7 grid it gets 400×64: give it 3 rows"),
        ):
            with self.subTest(grid=grid), self.assertRaisesRegex(ValueError, message):
                load(grid)


class DeckTest(unittest.TestCase):
    DAY = decks.Deck("day", ("home", "trains", "weather"), "trains")
    NIGHT = decks.Deck("night", ("home",), "home")

    def test_neighbours(self):
        self.assertEqual(self.DAY.neighbours("trains"), ("home", "weather"))
        self.assertEqual(self.DAY.neighbours("home"), ("weather", "trains"))  # wrapping around
        self.assertEqual(decks.Deck("d", ("a", "b"), "a").neighbours("a"), ("b",))  # once, not twice
        self.assertEqual(self.NIGHT.neighbours("home"), ())

    def test_selection(self):
        sel = decks.Selection()
        self.assertEqual(sel.current(self.DAY), ("trains", 1))  # starts at the deck's start
        self.assertTrue(sel.step(self.DAY, 1))
        self.assertEqual(sel.current(self.DAY), ("weather", 2))
        self.assertTrue(sel.step(self.DAY, 1))
        self.assertEqual(sel.current(self.DAY), ("home", 0))  # wraps around
        self.assertTrue(sel.step(self.DAY, -1))
        self.assertEqual(sel.current(self.DAY), ("weather", 2))  # and back
        self.assertEqual(sel.current(self.DAY), ("weather", 2))  # stays across wakes
        # Drawing ahead for a minute in another deck doesn't switch to it.
        self.assertEqual(sel.peek(self.NIGHT), "home")
        self.assertEqual(sel.peek(self.DAY), "weather")
        self.assertEqual(sel.current(self.DAY), ("weather", 2))
        # A real switch does, and coming back starts the deck over.
        self.assertFalse(sel.step(self.NIGHT, 1))  # one screen: nothing to change
        self.assertEqual(sel.current(self.NIGHT), ("home", 0))
        self.assertEqual(sel.current(self.DAY), ("trains", 1))


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.cfg = isolated_config(self)
        hours = fmi.parse((DATA / "fmi_simple.xml").read_bytes())
        self.now = datetime(2026, 11, 9, 23, 30, tzinfo=HELSINKI)
        self.scene = render.Scene(
            now=self.now,
            language="en",
            forecast=fmi.Forecast(self.now - timedelta(minutes=5), hours),
            stops={w: hsl.placeholder(n, i, self.now) for i, (w, n) in enumerate(zip(self.cfg.wanted(hsl.StopRequest), "ABC"))},
            sun=sun.day(self.cfg.lat, self.cfg.lon, self.now.date(), HELSINKI),
        )

    def assert_panel_image(self, img):
        self.assertEqual(img.size, (800, 480))
        self.assertEqual(img.mode, "L")
        self.assertLessEqual({c for _, c in img.getcolors()}, {0, 85, 170, 255})

    def test_render(self):
        for lang in ("en", "fi"):
            self.assert_panel_image(render.render(self.cfg, replace(self.scene, language=lang)))

    def test_a_widget_that_fails_costs_only_its_area(self):
        from unittest import mock
        scene = self.scene
        whole = render.render(self.cfg, scene, "home")
        with mock.patch.object(hsl_stops, "draw", side_effect=RuntimeError("bad data")):
            with self.assertRaises(RuntimeError):  # on the command line and in tests: a mistake shows
                render.render(self.cfg, scene, "home")
            with self.assertLogs("glance", "ERROR") as logs:
                img = render.render(self.cfg, scene, "home", forgiving=True)  # as the server draws
        self.assertEqual(len(logs.records), 3)  # the three stop widgets
        lay = self.cfg.screens["home"]
        for area in lay.areas:
            box = lay.rect(area)
            if isinstance(self.cfg.widgets[area.name], widgets.HslStops):
                self.assertEqual(img.crop(box).getextrema(), (255, 255), area.name)  # empty
            else:
                self.assertEqual(img.crop(box).tobytes(), whole.crop(box).tobytes(), area.name)  # as ever
        self.assertEqual(img.crop((0, 0, 800, 30)).tobytes(), whole.crop((0, 0, 800, 30)).tobytes())

    def test_render_without_data(self):
        sc = replace(self.scene, forecast=None, stops={"trams": hsl.StopDepartures("Empty", [])})
        self.assert_panel_image(render.render(self.cfg, sc))

    def test_status_text(self):
        self.assertEqual(render._status_warnings(self.scene), [])
        live = replace(self.scene, departures_live=True, departures_fetched_at=self.now - timedelta(minutes=12))
        self.assertTrue(live.departures_stale)  # so the stops draw realtime times grey
        fresh = replace(live, departures_fetched_at=self.now - timedelta(seconds=40))
        self.assertFalse(fresh.departures_stale)
        # The status bar shows what the sources say, then its own warning.
        warned = replace(self.scene, warnings=("Weather 2 h old", "Departures 12 min old"), battery_percent=10)
        self.assertEqual(render._status_warnings(warned), ["Weather 2 h old", "Departures 12 min old", "Charge battery"])
        self.assert_panel_image(render.render(self.cfg, replace(live, warnings=("Departures 12 min old",))))

    def test_many_warnings_leave_the_date_and_clock_alone(self):
        from PIL import Image, ImageDraw
        every = {
            "fi": ("Säätietoja ei saatavilla", "Lähtötietoja ei saatavilla", "Pörssisähkön hintaa ei saatavilla"),
            "en": ("Weather unavailable", "Departures unavailable", "Spot price unavailable"),
        }
        for language, warnings in every.items():
            quiet = replace(self.scene, language=language, battery_percent=5)  # "Charge battery" too
            clock_end = 10 + draw.font("semibold", 17).getlength(
                f"{i18n.date(language, quiet.now)}   {i18n.clock(language, quiet.now)}")
            bars = []
            for sc in (quiet, replace(quiet, warnings=warnings)):
                img = Image.new("L", (render.W, render.BAR), 255)
                render.draw_status_bar(img, ImageDraw.Draw(img), sc)
                bars.append(img.crop((0, 0, round(clock_end) + render.PAGE_MARGIN, render.BAR)).tobytes())
            with self.subTest(language=language):
                self.assertEqual(bars[0], bars[1])
        f = draw.font("semibold", 15)
        self.assertEqual(render._fitting(["Weather unavailable", "Departures unavailable", "Charge battery"], f,
                                         f.getlength("Weather unavailable +2")), "Weather unavailable +2")
        self.assertEqual(render._fitting(["Weather unavailable", "Charge battery"], f, 1000),
                         "Weather unavailable · Charge battery")  # what fits is shown as before

    def test_page_dots(self):
        from PIL import ImageChops
        plain = render.render(self.cfg, self.scene)
        one = decks.Deck("night", ("home",), "home")
        self.assertEqual(render.render(self.cfg, self.scene, "home", one).tobytes(), plain.tobytes())  # a deck of one: none
        two = decks.Deck("day", ("home", "departures"), "home")
        dotted = render.render(self.cfg, self.scene, "home", two)
        box = ImageChops.difference(plain, dotted).getbbox()
        self.assertIsNotNone(box)
        self.assertTrue(380 < box[0] and box[2] < 420 and box[3] <= 30, box)  # only in the middle of the status bar
        second = render.render(self.cfg, self.scene, "departures", two)
        self.assertNotEqual(second.crop(box).tobytes(), dotted.crop(box).tobytes())  # the other dot filled
        # A long warning takes the room: no dots then.
        warned = replace(self.scene, forecast=None, departures_live=True, battery_percent=10,
                         warnings=("Weather unavailable", "Departures unavailable"))
        self.assertEqual(render.render(self.cfg, warned, "home", two).tobytes(),
                         render.render(self.cfg, warned, "home").tobytes())

    def test_g2(self):
        from PIL import Image
        img = render.render(self.cfg, self.scene)
        g2 = render.to_g2(img)
        self.assertEqual(len(g2), 800 * 480 // 4)
        # Unpacks to the same image: 0..3 per pixel, first pixel in the top bits.
        levels = [(byte >> shift) & 3 for byte in g2 for shift in (6, 4, 2, 0)]
        self.assertEqual(bytes(85 * v for v in levels), img.tobytes())
        tiny = Image.frombytes("L", (8, 1), bytes([0, 85, 170, 255, 255, 170, 85, 0]))
        self.assertEqual(render.to_g2(tiny), bytes([0x1B, 0xE4]))

    def test_departure_clock_times(self):
        stop = hsl.placeholder("A", 0, self.now)
        dep = stop.departures[1]  # 3 minutes from now
        minutes = hsl_stops.minutes_text(dep, self.scene)
        clock = hsl_stops.minutes_text(dep, replace(self.scene, departure_clock=True))
        self.assertEqual(minutes, ("3", "min"))
        self.assertEqual(clock, (i18n.clock("en", dep.time.astimezone(HELSINKI)), ""))
        self.assert_panel_image(render.render(self.cfg, replace(self.scene, departure_clock=True)))

    def test_png(self):
        png = render.to_png(render.render(self.cfg, self.scene))
        self.assertTrue(png.startswith(b"\x89PNG"))


EVERY_1 = schedule.WakeSchedule((), 1)
EVERY_5 = schedule.WakeSchedule((), 5)
# The example schedule in config.toml: weekday mornings every minute, nights
# every 15 minutes, otherwise every 5.
WEEK = schedule.WakeSchedule((
    schedule.WakeRule(frozenset(range(5)), 7 * 60, 9 * 60, 1),
    schedule.WakeRule(frozenset(range(7)), 23 * 60, 6 * 60, 15),
), 5)


class ScheduleTest(unittest.TestCase):
    def t(self, h, m, s=0, day=10):  # 2026-11-10 is a Tuesday, the 14th a Saturday
        return datetime(2026, 11, day, h, m, s, tzinfo=HELSINKI)

    def test_rules(self):
        self.assertEqual(WEEK.interval_at(self.t(7, 0)), 1)
        self.assertEqual(WEEK.interval_at(self.t(12, 0)), 5)  # default
        self.assertEqual(WEEK.interval_at(self.t(7, 0, day=14)), 5)  # Saturday morning
        self.assertEqual(WEEK.interval_at(self.t(23, 30)), 15)
        self.assertEqual(WEEK.interval_at(self.t(3, 0)), 15)  # past midnight
        self.assertEqual(WEEK.interval_at(self.t(9, 0)), 5)  # "to" exclusive ("from" inclusive: 07:00 above)

    def test_next_wake_lands_on_period_starts(self):
        # A period starting off the previous interval's grid is hit exactly.
        odd = schedule.WakeSchedule((schedule.WakeRule(frozenset(range(7)), 6 * 60 + 40, 7 * 60, 1),), 15)
        self.assertEqual(odd.next_wake(self.t(6, 31, 30)), self.t(6, 40))  # not 06:45
        self.assertEqual(WEEK.next_wake(self.t(9, 1, 30)), self.t(9, 5))
        self.assertEqual(WEEK.next_wake(self.t(22, 58)), self.t(23, 0))
        self.assertEqual(WEEK.next_wake(self.t(23, 0, 30)), self.t(23, 15))

    def test_wakes_per_day(self):
        def count(day):
            start = self.t(0, 0, day=day)
            return sum(WEEK.is_wake(start + timedelta(minutes=m)) for m in range(1440))
        self.assertEqual(count(10), 328)  # Tuesday
        self.assertEqual(count(14), 232)  # Saturday

    def test_plan_interval_for_display(self):
        self.assertEqual(schedule.plan(self.t(7, 0, 2), WEEK, 2).interval_minutes, 1)
        self.assertEqual(schedule.plan(self.t(12, 0, 2), WEEK, 2).interval_minutes, 5)
        # Waking a few seconds early for 09:00 draws the 09:00 image: 5-minute period.
        p = schedule.plan(self.t(8, 59, 55), WEEK, 2)
        self.assertEqual((p.display_time, p.interval_minutes), (self.t(9, 0), 5))
        self.assertEqual(p.sleep_seconds, 5 * 60 + 5 + 2)  # to 09:05:02

    def test_dst_change_counts_real_seconds(self):
        # 2027-03-28: Finland's clocks jump 03:00 -> 04:00. From 02:50 with 15-minute
        # nights, the next wake is 03:00 wall-clock = 04:00 EEST, 10 real minutes away.
        sched = schedule.WakeSchedule((), 15)
        now = datetime(2027, 3, 28, 2, 50, tzinfo=HELSINKI)
        p = schedule.plan(now, sched, 0)
        self.assertLessEqual(p.sleep_seconds, 15 * 60)

    def test_autumn_clock_change(self):
        # 2027-10-31: 04:00 EEST becomes 03:00 EET, so 03:00-03:59 happens twice.
        # Adding to a time on the wall clock forgets which pass it is in: the
        # next wake came out an hour in the past and the device was told 1 s.
        sched = schedule.WakeSchedule((), 15)
        first = datetime(2027, 10, 31, 3, 45, 2, tzinfo=HELSINKI)           # EEST
        second = datetime(2027, 10, 31, 3, 20, 0, tzinfo=HELSINKI, fold=1)  # EET, an hour and a bit later
        self.assertEqual(schedule.plan(first, sched, 2).sleep_seconds, 900)   # to 03:00 EET, not 04:00
        p = schedule.plan(second, sched, 2)
        self.assertEqual(p.sleep_seconds, 602)
        self.assertEqual(p.display_time, second)
        wake = sched.next_wake(first)
        self.assertEqual((wake.hour, wake.minute, wake.fold), (3, 0, 1))
        self.assertEqual(schedule.seconds_between(first, schedule.next_poll(first, 15)), 43)
        every_wake, t = [], datetime(2027, 10, 31, 2, 59, tzinfo=HELSINKI)
        while len(every_wake) < 9:
            t = sched.next_wake(t)
            every_wake.append(t.strftime("%H:%M %Z"))
        self.assertEqual(every_wake, ["03:00 EEST", "03:15 EEST", "03:30 EEST", "03:45 EEST",
                                      "03:00 EET", "03:15 EET", "03:30 EET", "03:45 EET", "04:00 EET"])

    def test_next_boundary(self):
        self.assertEqual(schedule.next_boundary(self.t(7, 30, 10), 1), self.t(7, 31))
        self.assertEqual(schedule.next_boundary(self.t(7, 30), 1), self.t(7, 31))  # strictly after
        self.assertEqual(schedule.next_boundary(self.t(7, 31, 10), 15), self.t(7, 45))
        self.assertEqual(schedule.next_boundary(self.t(23, 59, 30), 15), self.t(0, 0, day=11))

    def test_bad_intervals_fail_at_startup(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            for sched in ("wake_interval_minutes = 0", "wake_interval_minutes = 5\n[[schedule.rules]]\n"
                          'from = "07:00"\nto = "09:00"\nevery = 0'):
                path.write_text(f"{MINIMAL}[schedule]\n{sched}\n")
                with self.assertRaisesRegex(ValueError, "1..1440"):
                    config.load(path)
            for bad, message in (
                ("[digitransit]\npoll_lead_seconds = 60", "poll_lead_seconds"),  # the refresher would never wait
                ("[device]\nwake_offset_seconds = -30", "wake_offset_seconds"),
                ('[[schedule.rules]]\nfrom = "07:99"\nto = "09:00"\nevery = 1', "bad time"),
                ('[[schedule.rules]]\nfrom = "7"\nto = "09:00"\nevery = 1', "bad time"),
            ):
                path.write_text(f"{MINIMAL}{bad}\n")
                with self.assertRaisesRegex(ValueError, message):
                    config.load(path)
            path.write_text('[location]\nlat = 60.2\nlon = 24.9\n[screens.main]\ngrid = "a b"\n'
                            '[widgets.a]\ntype = "hsl_stops"\ncodes = [1234, "H0665"]\n'
                            '[widgets.b]\ntype = "hsl_stops"\ncode = 5678\n')
            self.assertEqual([s.ids for s in config.load(path).wanted(hsl.StopRequest).values()], [("1234", "H0665"), ("5678",)])  # text, not numbers
            path.write_text(f'{MINIMAL}[server]\nhostnames = "Pi.Lan"\n')
            self.assertEqual(config.load(path).hostnames, ("pi.lan",))  # one name, not its letters

    def test_bad_values_fail_at_startup_with_a_reason(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            rule = '[[schedule.rules]]\nfrom = "07:00"\nto = "09:00"\n'
            for bad, message in (
                (rule + 'days = "workdays"', "days must be"),
                (rule + 'days = ["mon", "xyz"]', "days must be"),
                (rule + "days = 1", "days must be"),
                ("[server]\nport = 99999", "port"),
                ("[server]\nport = true", "port"),
                ('[server]\nallowed_networks = ["vpn"]', "allowed_networks"),
                ("[fmi]\nrefresh_minutes = 0", "refresh_minutes"),
                ("[digitransit]\nstale_after_minutes = -1", "stale_after_minutes"),
                ("[battery]\nfull_mv = 42", "full_mv"),
            ):
                path.write_text(f"{MINIMAL}{bad}\n")
                with self.subTest(bad), self.assertRaisesRegex(ValueError, message):
                    config.load(path)
            path.write_text('[location]\nlat = 160.2\nlon = 24.9\n[screens.main]\ngrid = "."\n')
            with self.assertRaisesRegex(ValueError, "lat"):
                config.load(path)
            path.write_text(f'{MINIMAL}{rule}days = "Monday"\n[server]\nallowed_networks = "100.64.0.1/10"\n')
            cfg = config.load(path)
            self.assertEqual(cfg.wake_schedule.rules[0].days, frozenset({0}))  # one day, without a list
            self.assertEqual([str(n) for n in cfg.allowed_networks], ["100.64.0.0/10"])

    def test_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text('''
[location]
lat = 60.2
lon = 24.9
[screens.home]
grid = "."
[screens.trains]
grid = "."
[decks.day]
screens = ["home", "trains"]
start = "trains"
[decks.night]
screens = ["home"]
[schedule]
wake_interval_minutes = 5
[[schedule.rules]]
days = "weekdays"
from = "07:00"
to = "09:00"
every = 1
[[schedule.rules]]
days = ["sat", "Sunday"]
from = "23:00"
to = "06:00"
every = 15
deck = "night"
[[schedule.rules]]
from = "12:00"
to = "13:00"
deck = "night"
''')
            cfg = config.load(path)
        sched = cfg.wake_schedule
        self.assertEqual((sched.default_every, sched.default_deck), (5, "day"))  # default deck: the first
        self.assertEqual(sched.rules[0], schedule.WakeRule(frozenset(range(5)), 420, 540, 1))
        self.assertEqual(sched.rules[1].days, frozenset({5, 6}))
        self.assertEqual(sched.rules[1].end, 360)
        self.assertEqual(sched.rules[2].every, 5)  # no `every`: the default interval
        self.assertEqual(cfg.decks["day"], decks.Deck("day", ("home", "trains"), "trains"))
        self.assertEqual(cfg.start_screen, "trains")
        self.assertEqual(cfg.deck_at(self.t(8, 0)).name, "day")  # a rule without a deck: the default
        self.assertEqual(cfg.deck_at(self.t(12, 30)).name, "night")
        self.assertEqual(cfg.deck_at(self.t(23, 30, day=14)).name, "night")  # Saturday night

    def test_wakes_just_after_the_minute(self):
        p = schedule.plan(self.t(7, 30, 3), EVERY_1, 2)
        self.assertEqual((p.display_time, p.sleep_seconds), (self.t(7, 30, 3), 59))  # 07:31:02

    def test_early_wake_draws_the_coming_minute(self):
        # Sleep timer drift: the device arrives at 07:29:55 for the 07:30 update.
        p = schedule.plan(self.t(7, 29, 55), EVERY_1, 2)
        self.assertEqual(p.display_time, self.t(7, 30))
        self.assertEqual(p.sleep_seconds, 67)  # to 07:31:02, not 7 s

    def test_longer_interval_aligns_to_clock(self):
        p = schedule.plan(self.t(7, 32, 40), EVERY_5, 2)
        self.assertEqual(p.sleep_seconds, 2 * 60 + 20 + 2)  # 07:35:02
        p = schedule.plan(self.t(23, 58), EVERY_5, 2)
        self.assertEqual(p.sleep_seconds, 2 * 60 + 2)  # 00:00:02 next day

    def test_early_after_long_sleep(self):
        # ~1 % fast sleep timer: 9 s early after a 15-minute night sleep.
        p = schedule.plan(self.t(23, 14, 53), WEEK, 2)
        self.assertEqual((p.display_time, p.interval_minutes), (self.t(23, 15), 15))
        self.assertEqual(p.sleep_seconds, 15 * 60 + 7 + 2)  # to 23:30:02, not a wake 9 s later

    def test_button_press_mid_minute(self):
        p = schedule.plan(self.t(7, 30, 41), EVERY_1, 2)
        self.assertEqual((p.display_time, p.sleep_seconds), (self.t(7, 30, 41), 21))

    def test_prerendered(self):
        cache = schedule.Prerendered()
        self.assertIsNone(cache.get(self.t(7, 30, 2), "home", "/glance.png"))
        cache.put(self.t(7, 30), "home", {"/glance.png": b"png", "/glance.g2": b"g2"})
        self.assertEqual(cache.get(self.t(7, 30), "home", "/glance.png"), b"png")  # early wake, snapped
        self.assertEqual(cache.get(self.t(7, 30, 2), "home", "/glance.g2"), b"g2")  # on-time wake
        self.assertIsNone(cache.get(self.t(7, 30, 2), "trains", "/glance.png"))  # another screen selected since
        self.assertIsNone(cache.get(self.t(7, 30, 35), "home", "/glance.png"))  # mid-minute: draw fresh
        self.assertIsNone(cache.get(self.t(7, 31, 2), "home", "/glance.png"))  # next minute
        self.assertIsNone(cache.get(self.t(7, 30, 2), "home", "/other"))
        # The neighbours, drawn after it for the same minute, join it.
        cache.put(self.t(7, 30), "trains", {"/glance.png": b"trains"})
        self.assertEqual(cache.get(self.t(7, 30, 2), "home", "/glance.png"), b"png")
        self.assertEqual(cache.get(self.t(7, 30, 2), "trains", "/glance.png"), b"trains")
        # For a press that changes the screen: anywhere in that minute.
        late = self.t(7, 30, 50)
        self.assertIsNone(cache.get(late, "trains", "/glance.png"))
        self.assertEqual(cache.get(late, "trains", "/glance.png", max_late=timedelta(minutes=1)), b"trains")
        # A new minute starts afresh.
        cache.put(self.t(7, 31), "home", {"/glance.png": b"png2"})
        self.assertIsNone(cache.get(self.t(7, 31, 2), "trains", "/glance.png"))
        # Drawn from one fetch's departures; a later fetch has landed since.
        cache.put(self.t(7, 35), "home", {"/glance.png": b"old"}, data="fetch 1")
        self.assertEqual(cache.get(self.t(7, 35, 2), "home", "/glance.png", data="fetch 1"), b"old")
        self.assertIsNone(cache.get(self.t(7, 35, 2), "home", "/glance.png", data="fetch 2"))
        self.assertEqual(cache.get(self.t(7, 35, 2), "home", "/glance.png"), b"old")  # no data to compare
        cache.put(self.t(7, 35), "trains", {"/glance.png": b"new"}, data="fetch 2")  # redrawn: the old ones go
        self.assertIsNone(cache.get(self.t(7, 35, 2), "home", "/glance.png", data="fetch 2"))

    def test_recent_shares_one_drawing(self):
        recent = schedule.Recent()
        drawn = []

        def draw():
            drawn.append(1)
            return len(drawn)

        self.assertEqual(recent.get("a", "/x", draw, lambda n: b"x%d" % n), b"x1")
        self.assertEqual(recent.get("a", "/y", draw, lambda n: b"y%d" % n), b"y1")  # other format, same drawing
        self.assertEqual(recent.get("a", "/x", draw, lambda n: b"never"), b"x1")
        self.assertEqual(recent.get("b", "/x", draw, lambda n: b"x%d" % n), b"x2")  # new data: drawn afresh
        recent._made_at -= schedule.Recent.MAX_AGE  # too old to share
        self.assertEqual(recent.get("b", "/x", draw, lambda n: b"x%d" % n), b"x3")

    def test_next_poll(self):
        self.assertEqual(schedule.next_poll(self.t(7, 30, 10), 15), self.t(7, 30, 45))
        self.assertEqual(schedule.next_poll(self.t(7, 30, 45), 15), self.t(7, 31, 45))
        self.assertEqual(schedule.next_poll(self.t(7, 30, 50), 15), self.t(7, 31, 45))


class BatteryTest(unittest.TestCase):
    def test_percent(self):
        self.assertEqual(battery.percent(4250), 100)
        self.assertEqual(battery.percent(4200), 100)
        self.assertEqual(battery.percent(3840), 50)
        self.assertEqual(battery.percent(3845), 52)  # interpolated
        self.assertEqual(battery.percent(3270), 0)
        self.assertEqual(battery.percent(3000), 0)

    def test_reports(self):
        now = datetime(2026, 11, 10, 8, 0, tzinfo=HELSINKI)
        b = battery.Battery()
        self.assertIsNone(b.level(now))
        for bad in (None, "", "abc", "12", "9999"):
            b.report(bad, now)
        self.assertIsNone(b.level(now))
        b.report("3840", now)
        self.assertEqual(b.level(now + timedelta(minutes=30)), 50)
        self.assertIsNone(b.level(now + timedelta(hours=3)))  # too old to show

    def test_calibration(self):
        # A device that reads 4.14 V when full: shift readings so that is 100 %.
        self.assertEqual(battery.percent(4140), 94)
        self.assertEqual(battery.percent(4140, full_mv=4140), 100)
        self.assertEqual(battery.percent(3780, full_mv=4140), 50)  # = 3840 on the curve
        b = battery.Battery(full_mv=4140)
        now = datetime(2026, 11, 10, 8, 0, tzinfo=HELSINKI)
        b.report("4140", now)
        self.assertEqual(b.level(now), 100)

    def test_log_and_restore(self):
        now = datetime(2026, 11, 10, 8, 0, tzinfo=HELSINKI)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "battery.csv"
            b = battery.Battery(log_path=path)
            b.report("4100", now)
            b.report("bogus", now)  # not logged
            b.report("4090", now + timedelta(minutes=1))
            self.assertEqual(path.read_text().splitlines(), [
                "time,mv", "2026-11-10T08:00:00+02:00,4100", "2026-11-10T08:01:00+02:00,4090"])
            # A restarted server picks up the last reading.
            restarted = battery.Battery(log_path=path)
            self.assertEqual(restarted.level(now + timedelta(minutes=2)), battery.percent(4090))
            path.write_text("time,mv\n")  # only a header: nothing to restore
            self.assertIsNone(battery.Battery(log_path=path).level(now))

    def test_log_is_rate_limited(self):
        # Faster reports than the device ever sends are shown but not logged,
        # so requests from elsewhere on the network can't fill the disk.
        now = datetime(2026, 11, 10, 8, 0, tzinfo=HELSINKI)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "battery.csv"
            b = battery.Battery(log_path=path)
            for seconds, mv in ((0, 4100), (1, 3000), (29, 3001), (30, 4090)):
                b.report(str(mv), now + timedelta(seconds=seconds))
            self.assertEqual(path.read_text().splitlines(), [
                "time,mv", "2026-11-10T08:00:00+02:00,4100", "2026-11-10T08:00:30+02:00,4090"])
            b.report("3000", now + timedelta(seconds=31))
            self.assertEqual(b.level(now + timedelta(seconds=31)), battery.percent(3000))  # still shown

    def test_status_bar(self):
        now = datetime(2026, 11, 10, 8, 0, tzinfo=HELSINKI)
        sc = render.Scene(now=now, language="fi", forecast=fmi.Forecast(now, []),
                          stops={}, battery_percent=40)
        self.assertEqual(render._status_warnings(sc), [])
        self.assertEqual(render._status_warnings(replace(sc, battery_percent=15)), ["Lataa akku"])
        self.assertEqual(i18n.percent("fi", 87), "87\u202f%")
        self.assertEqual(i18n.percent("en", 87), "87%")


class TransitTest(unittest.TestCase):
    def test_refresh_during_fetch_waits_for_it(self):
        import threading, time
        from glance.sources.hsl import Transit
        cfg = isolated_config(self, digitransit_key="test", stops=[widgets.HslStops("A", ("HSL:1",))])
        transit = Transit(cfg)
        calls = []

        def slow_fetch(now):
            calls.append(now)
            time.sleep(0.3)
            with transit.lock:
                transit.fetched_at = now

        transit._fetch = slow_fetch
        startup = threading.Thread(target=transit.refresh)
        startup.start()
        time.sleep(0.05)
        transit.refresh(min_age_seconds=10)  # button press during the startup fetch
        self.assertIsNotNone(transit.fetched_at)  # waited, so data is there
        self.assertEqual(len(calls), 1)  # and didn't fetch twice
        startup.join()

    def test_refreshes_never_raise(self):
        # They run in the refresher thread: an exception there used to end it
        # for good, leaving the server up with no polling or pre-drawn images.
        from unittest import mock
        from glance.sources.hsl import Transit
        from glance.sources.fmi import Weather
        cfg = isolated_config(self, digitransit_key="test", stops=[widgets.HslStops("A", ("HSL:1",))])
        with mock.patch.object(fmi, "fetch", side_effect=RuntimeError("surprise")), \
                mock.patch.object(hsl, "fetch", side_effect=RuntimeError("surprise")), \
                self.assertLogs("glance", "ERROR") as logs:
            weather, transit = Weather(cfg), Transit(cfg)
            weather.refresh(force=True)
            transit.refresh()
        self.assertEqual(len(logs.records), 2)
        self.assertIsNone(weather.forecast)
        self.assertIsNone(transit.fetched_at)
        # A cache that can't be written doesn't lose the forecast either.
        hours = fmi.parse((DATA / "fmi_simple.xml").read_bytes())
        forecast = fmi.Forecast(datetime.now(UTC), hours)
        with mock.patch.object(fmi, "fetch", return_value=forecast), \
                mock.patch.object(fmi, "save_cache", side_effect=OSError("disk full")), \
                self.assertLogs("glance", "WARNING"):
            weather.refresh(force=True)
        self.assertIs(weather.forecast, forecast)

    def test_refresh_gives_up_waiting(self):
        # A request the device waits on (it gives up after 10 s) uses the last
        # good data if the fetch hasn't answered in time.
        import threading, time
        from glance.sources.hsl import Transit
        cfg = isolated_config(self, digitransit_key="test", stops=[widgets.HslStops("A", ("HSL:1",))])
        transit = Transit(cfg)
        release = threading.Event()
        transit._fetch = lambda now: release.wait(5)
        started = time.monotonic()
        transit.refresh(wait_seconds=0.2)
        self.assertLess(time.monotonic() - started, 1)
        transit.refresh(min_age_seconds=10, wait_seconds=0.2)  # and so does one queued behind it
        self.assertLess(time.monotonic() - started, 1)
        release.set()

    def test_no_thread_for_the_fetch(self):
        # At the service's thread limit: draw with the last good data, don't fail the request.
        import threading
        from unittest import mock
        from glance.sources.hsl import Transit
        transit = Transit(isolated_config(self, digitransit_key="test", stops=[widgets.HslStops("A", ("HSL:1",))]))
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("can't start new thread")), \
                self.assertLogs("glance", "WARNING"):
            transit.refresh(wait_seconds=1)

    def test_waiting_callers_share_one_thread(self):
        # A fetch that never finishes: every request used to leave a thread
        # stuck behind it, until the service's thread limit stopped the server.
        import threading
        from glance.sources.hsl import Transit
        cfg = isolated_config(self, digitransit_key="test", stops=[widgets.HslStops("A", ("HSL:1",))])
        transit = Transit(cfg)
        release = threading.Event()
        fetches = []
        transit._fetch = lambda now: (fetches.append(now), release.wait(10))
        transit.refresh(60, wait_seconds=0.01)
        stuck = transit.background
        for _ in range(10):
            transit.refresh(60, wait_seconds=0.01)
            self.assertIs(transit.background, stuck)  # waited on the same thread
        self.assertTrue(stuck.is_alive())
        self.assertEqual(len(fetches), 1)
        release.set()
        stuck.join(5)
        transit.refresh(wait_seconds=1)  # and once it has ended, the next caller starts a new one
        self.assertIsNot(transit.background, stuck)
        self.assertEqual(len(fetches), 2)

    def test_refresh_is_not_lost_behind_a_caller_content_with_older_data(self):
        # A plain request takes departures up to a minute old; its thread ends
        # without fetching. A button press that waited on that thread wants
        # them at most 10 s old, and must still get its fetch.
        import threading, time
        from glance.sources.hsl import Transit
        cfg = isolated_config(self, digitransit_key="test", stops=[widgets.HslStops("A", ("HSL:1",))])
        transit = Transit(cfg)
        fetches = []
        transit._fetch = fetches.append

        def someone_elses_thread():
            transit.background = threading.Thread(target=time.sleep, args=(0.2,), daemon=True)
            transit.background.start()

        transit.last_attempt = time.monotonic() - 30  # the last fetch was 30 s ago
        someone_elses_thread()
        transit.refresh(60, wait_seconds=2)  # 30 s old is fine for this caller
        self.assertEqual(len(fetches), 0)
        someone_elses_thread()
        transit.refresh(10, wait_seconds=2)  # not for this one
        self.assertEqual(len(fetches), 1)

    def test_stops_given_by_code_keep_their_departures_too(self):
        # The empty answer used to drop the stop codes as well, so the kept
        # departures couldn't be found: the widget showed its code and nothing.
        from unittest import mock
        from glance.sources.hsl import Transit
        cfg = isolated_config(self, digitransit_key="test", stops=[widgets.HslStops("", ("H0001",))])
        transit = Transit(cfg)
        dep = hsl.Departure("7", "Pasila", datetime.now(UTC) + timedelta(minutes=5), True, "tram")
        now = datetime.now(HELSINKI)

        def shown():
            stop = transit.stops(now)["s0"]
            return stop.name, stop.departures

        with mock.patch.object(hsl, "resolve_code", side_effect=hsl.HSLError("down")), \
                mock.patch.object(hsl, "fetch", wraps=hsl.fetch), self.assertLogs("glance", "WARNING"):
            transit.refresh()  # the code can't be looked up yet: nothing asked, nothing fetched
        self.assertIsNone(transit.fetched_at)  # so the status bar says "unavailable", not "no departures"
        with mock.patch.object(hsl, "resolve_code", return_value="HSL:1"), \
                mock.patch.object(hsl, "fetch", return_value=({"HSL:1": [dep]}, {"HSL:1": "Pasilan asema"}, {})):
            transit.refresh()
        self.assertEqual(shown(), ("Pasilan asema", [dep]))
        fetched_at = transit.fetched_at
        with mock.patch.object(hsl, "fetch", return_value=({}, {}, {})), self.assertLogs("glance", "WARNING"):
            transit.refresh()  # an empty answer
        self.assertEqual(shown(), ("Pasilan asema", [dep]))
        with mock.patch.object(hsl, "resolve_code", side_effect=hsl.HSLError("down")), \
                mock.patch.object(hsl, "fetch", return_value=({}, {}, {})), self.assertLogs("glance", "WARNING"):
            transit.refresh()  # and the next poll, with the lookup failing as well
        self.assertEqual(shown(), ("Pasilan asema", [dep]))
        self.assertEqual(transit.fetched_at, fetched_at)
        self.assertEqual(json.loads((cfg.cache_dir / "stop_codes.json").read_text()), {"H0001": "HSL:1"})

    def test_answer_without_the_stops_keeps_the_last_departures(self):
        from unittest import mock
        from glance.sources.hsl import Transit
        cfg = isolated_config(self, digitransit_key="test", stops=[widgets.HslStops("A", ("HSL:1",))])
        transit = Transit(cfg)
        dep = hsl.Departure("7", "Pasila", datetime.now(UTC) + timedelta(minutes=5), True, "tram")
        with mock.patch.object(hsl, "fetch", return_value=({"HSL:1": [dep]}, {"HSL:1": "Stop"}, {})):
            transit.refresh()
        fetched_at = transit.fetched_at
        for empty in ({}, {"HSL:ELSEWHERE": []}):
            with mock.patch.object(hsl, "fetch", return_value=(empty, {}, {})), self.assertLogs("glance", "WARNING"):
                transit.refresh()
            self.assertEqual(transit.by_stop, {"HSL:1": [dep]})
            self.assertEqual(transit.fetched_at, fetched_at)  # so the status bar says how old they are
        with mock.patch.object(hsl, "fetch", return_value=({"HSL:1": []}, {}, {})):
            transit.refresh()  # the stop with nothing leaving it is an answer
        self.assertEqual(transit.by_stop, {"HSL:1": []})

    def test_clock_set_back_does_not_hold_off_fetches(self):
        from unittest import mock
        from glance.sources.fmi import Weather
        cfg = isolated_config(self)
        hours = fmi.parse((DATA / "fmi_simple.xml").read_bytes())
        weather = Weather(cfg)
        weather.forecast = fmi.Forecast(datetime.now(UTC) + timedelta(days=2), hours)  # fetched "in the future"
        with mock.patch.object(fmi, "fetch", return_value=fmi.Forecast(datetime.now(UTC), hours)) as fetch:
            weather.refresh()
        self.assertEqual(fetch.call_count, 1)

    def test_failed_weather_fetch_is_retried_sooner(self):
        from unittest import mock
        from glance.sources.fmi import Weather
        weather = Weather(isolated_config(self))
        hours = fmi.parse((DATA / "fmi_simple.xml").read_bytes())
        with mock.patch.object(fmi, "fetch", side_effect=fmi.FMIError("down")) as fetch, self.assertLogs("glance", "WARNING"):
            weather.refresh()
            weather.refresh()                                  # too soon
            self.assertEqual(fetch.call_count, 1)
            weather.last_attempt -= 180                        # not the full 15 minutes
            weather.refresh()
            self.assertEqual(fetch.call_count, 2)
        with mock.patch.object(fmi, "fetch", return_value=fmi.Forecast(datetime.now(UTC), hours)) as fetch:
            weather.last_attempt -= 180
            weather.refresh()
            weather.last_attempt -= 180                        # after a success: the full interval again
            weather.refresh()
            self.assertEqual(fetch.call_count, 1)

    def test_failed_weather_fetch_keeps_the_last_good_forecast(self):
        from unittest import mock
        from glance.sources.fmi import Weather
        cfg = isolated_config(self)
        hours = fmi.parse((DATA / "fmi_simple.xml").read_bytes())
        good = fmi.Forecast(datetime.now(UTC) - timedelta(hours=1), hours)
        fmi.save_cache(good, cfg.cache_dir / "forecast.json")
        weather = Weather(cfg)  # starts from the cache, an hour old
        with mock.patch.object(fmi, "fetch", side_effect=fmi.FMIError("down")) as fetch, \
                self.assertLogs("glance", "WARNING") as logs:
            weather.refresh()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(weather.forecast, good)
        self.assertIn("1:00:", logs.output[0])  # says how old the forecast it kept is
        self.assertEqual(fmi.load_cache(cfg.cache_dir / "forecast.json"), good)

    def test_fresh_cached_forecast_is_not_fetched_again(self):
        from unittest import mock
        from glance.sources.fmi import Weather
        cfg = isolated_config(self)
        hours = fmi.parse((DATA / "fmi_simple.xml").read_bytes())
        fmi.save_cache(fmi.Forecast(datetime.now(UTC) - timedelta(minutes=1), hours), cfg.cache_dir / "forecast.json")
        weather = Weather(cfg)  # a restart, with a forecast fetched a minute ago
        with mock.patch.object(fmi, "fetch", return_value=fmi.Forecast(datetime.now(UTC), hours)) as fetch:
            weather.refresh()
        self.assertEqual(fetch.call_count, 0)

    def test_cached_forecast_without_a_new_parameter_is_fetched_at_once(self):
        from unittest import mock
        from glance.sources.fmi import Weather
        cfg = isolated_config(self)
        hours = fmi.parse((DATA / "fmi_simple.xml").read_bytes())
        old = fmi.Forecast(datetime.now(UTC) - timedelta(minutes=1), hours, parameters=("Temperature",))
        fmi.save_cache(old, cfg.cache_dir / "forecast.json")
        weather = Weather(cfg)
        with mock.patch.object(fmi, "fetch", return_value=fmi.Forecast(datetime.now(UTC), hours)) as fetch:
            weather.refresh()
        self.assertEqual(fetch.call_count, 1)

    def test_new_weather_counts_as_new_data_for_drawing(self):
        from glance.sources import Sources
        cfg = isolated_config(self)
        sources = Sources.make(cfg)
        before = sources.data_times()
        hours = fmi.parse((DATA / "fmi_simple.xml").read_bytes())
        sources.weather.forecast = fmi.Forecast(datetime.now(UTC), hours)
        self.assertNotEqual(sources.data_times(), before)  # so a drawing shared for a few seconds is redrawn
        self.assertIn(sources.weather, sources.periodic)  # and the refresher refreshes it

    def test_weather_says_when_its_forecast_is_missing_or_old(self):
        from glance.sources.fmi import Weather
        cfg = isolated_config(self)  # [fmi] stale_after_minutes = 45
        weather = Weather(cfg)
        now = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
        self.assertEqual(weather.warning(now, "en"), "Weather unavailable")
        weather.forecast = fmi.Forecast(now - timedelta(minutes=45), [])
        self.assertIsNone(weather.warning(now, "en"))
        weather.forecast = fmi.Forecast(now - timedelta(hours=2), [])
        self.assertEqual(weather.warning(now, "en"), "Weather 2 h old")
        self.assertEqual(weather.warning(now, "fi"), "Sää 2 h vanha")

    def test_departures_say_when_they_are_missing_or_old(self):
        from glance.sources.hsl import Transit
        cfg = isolated_config(self, digitransit_key="test", stops=[widgets.HslStops("A", ("HSL:1",))])
        transit = Transit(cfg)  # [digitransit] stale_after_minutes = 5
        now = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
        self.assertEqual(transit.warning(now, "en"), "Departures unavailable")  # live, never fetched
        transit.fetched_at = now - timedelta(minutes=12)
        self.assertEqual(transit.warning(now, "en"), "Departures 12 min old")
        self.assertEqual(transit.warning(now, "fi"), "Lähdöt 12 min vanhoja")
        transit.fetched_at = now - timedelta(seconds=40)
        self.assertIsNone(transit.warning(now, "en"))
        placeholders = Transit(isolated_config(self))  # no key: placeholders are never old
        self.assertIsNone(placeholders.warning(now, "en"))

    def test_status_bar_lists_weather_then_departures(self):
        from glance.sources import Sources
        cfg = isolated_config(self, digitransit_key="test", stops=[widgets.HslStops("A", ("HSL:1",))], weather=True)
        sources = Sources.make(cfg)
        now = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
        self.assertEqual(sources.warnings(now, "en"), ("Weather unavailable", "Departures unavailable"))
        sources.weather.forecast = fmi.Forecast(now, [])
        self.assertEqual(sources.warnings(now, "en"), ("Departures unavailable",))

    def test_weather_no_screen_shows_is_not_fetched_or_warned_about(self):
        from unittest import mock
        from glance.sources import Sources
        cfg = isolated_config(self, digitransit_key="test", stops=[widgets.HslStops("A", ("HSL:1",))])  # no weather widget
        sources = Sources.make(cfg)
        with mock.patch.object(fmi, "fetch", return_value=fmi.Forecast(datetime.now(UTC), [])) as fetch:
            for source in sources.periodic:  # as the refresher and `render` do
                source.refresh(force=True)
        fetch.assert_not_called()
        now = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
        self.assertEqual(sources.warnings(now, "en"), ("Departures unavailable",))

    def test_departures_no_screen_shows_are_not_warned_about(self):
        from glance.sources import Sources
        cfg = isolated_config(self, digitransit_key="test", stops=[], weather=True)  # a key, but no stop widget
        sources = Sources.make(cfg)
        now = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
        self.assertEqual(sources.warnings(now, "en"), ("Weather unavailable",))

    def test_scene_warns_at_its_own_time_and_language(self):
        from glance.__main__ import scene
        from glance.sources import Sources
        cfg = isolated_config(self, language="fi", digitransit_key="test", stops=[widgets.HslStops("A", ("HSL:1",))])
        sources = Sources.make(cfg)
        drawn_for = datetime(2026, 11, 10, 8, 0, tzinfo=HELSINKI)  # an image drawn ahead, not the clock now
        sources.weather.forecast = fmi.Forecast(drawn_for, [])
        sources.transit.fetched_at = drawn_for - timedelta(minutes=12)
        self.assertEqual(scene(cfg, sources, drawn_for).warnings, ("Lähdöt 12 min vanhoja",))

    def test_renumbered_stop_is_looked_up_again(self):
        from unittest import mock
        from glance.sources.hsl import Transit
        cfg = isolated_config(self, digitransit_key="test", stops=[widgets.HslStops("A", ("H0001",))])
        (cfg.cache_dir / "stop_codes.json").write_text('{"H0001": "HSL:OLD", "H0002": "HSL:ELSEWHERE"}')
        transit = Transit(cfg)
        seen = []

        def fetch(queries, key, language):
            seen.append(queries[0].ids)
            return ({sid: [] for sid in queries[0].ids if sid == "HSL:NEW"}, {}, {})

        with mock.patch.object(hsl, "fetch", side_effect=fetch), \
                mock.patch.object(hsl, "resolve_code", return_value="HSL:NEW") as lookup, self.assertLogs("glance", "WARNING"):
            transit.refresh()   # HSL doesn't know the cached id
            transit.refresh()   # so the code is looked up again
            transit.refresh()
        self.assertEqual(seen, [("HSL:OLD",), ("HSL:NEW",), ("HSL:NEW",)])
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(transit.code_ids, {"H0001": "HSL:NEW", "H0002": "HSL:ELSEWHERE"})  # unused codes left alone

    def test_unknown_stop_code_is_looked_up_once(self):
        from unittest import mock
        from glance.sources.hsl import Transit
        cfg = isolated_config(self, digitransit_key="test", stops=[widgets.HslStops("A", ("H9999",))])
        transit = Transit(cfg)
        with mock.patch.object(hsl, "resolve_code", return_value=None) as lookup, \
                mock.patch.object(hsl, "fetch", return_value=({}, {}, {})), self.assertLogs("glance", "WARNING"):
            transit.refresh()
            transit.refresh()
        self.assertEqual(lookup.call_count, 1)


class ServerTest(unittest.TestCase):
    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        from glance.__main__ import make_handler
        from glance.sources import Sources
        # No key: placeholder departures, no network.
        cfg = isolated_config(self)
        self.battery = battery.Battery()
        self.prerendered = schedule.Prerendered()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(cfg, Sources.make(cfg), self.prerendered, self.battery)
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_image_has_sleep_header(self):
        import urllib.request
        with urllib.request.urlopen(self.base + "/glance.g2") as r:
            self.assertEqual(r.headers["Content-Type"], "application/octet-stream")
            self.assertTrue(r.headers["X-Glance-Sleep"])
            self.assertEqual(len(r.read()), 96000)
        self.get("/glance.g2?battery_mv=3840").read()
        self.assertEqual(self.battery.level(datetime.now(HELSINKI)), 50)
        for path in ("/glance.png", "/glance.png?refresh=1&battery_mv=bogus"):
            with self.get(path) as r:
                self.assertEqual(r.headers["Content-Type"], "image/png")
                sleep = int(r.headers["X-Glance-Sleep"])
                # config.toml's schedule: up to 15 min, plus the early-wake margin
                # (27 s after a 15-minute sleep) and the 2 s offset.
                self.assertTrue(1 <= sleep <= 15 * 60 + 27 + 2, sleep)
                self.assertTrue(r.read().startswith(b"\x89PNG"))

    def get(self, path, client=True):
        """A request as the device or the preview page sends it (with the client header)."""
        import urllib.request
        from glance.__main__ import CLIENT_HEADER
        return urllib.request.urlopen(urllib.request.Request(
            self.base + path, headers={CLIENT_HEADER: "test"} if client else {}))

    def test_sleep_header_counts_the_drawing_time(self):
        # The device sleeps from when the answer arrives, not from when the request did.
        import time
        from unittest import mock
        fixed = schedule.Plan(datetime.now(HELSINKI).replace(second=30), sleep_seconds=300, interval_minutes=5)
        slow = render.render

        def draw(*a, **kw):
            time.sleep(1.6)
            return slow(*a, **kw)

        with mock.patch.object(schedule, "plan", return_value=fixed), mock.patch.object(render, "render", side_effect=draw):
            with self.get("/glance.png") as r:
                self.assertIn(int(r.headers["X-Glance-Sleep"]), (297, 298))

    def test_changes_need_the_client_header(self):
        # What a web page elsewhere can make a browser send: a plain GET.
        from unittest import mock
        from glance.sources.hsl import Transit
        with mock.patch.object(Transit, "refresh") as refresh:
            self.get("/glance.png?refresh=1&battery_mv=2600", client=False).read()
            self.assertIsNone(self.battery.level(datetime.now(HELSINKI)))
            self.assertNotIn(mock.call(10, wait_seconds=mock.ANY), refresh.mock_calls)  # not treated as a button press
            self.get("/glance.png?refresh=1&battery_mv=3840").read()
            self.assertEqual(self.battery.level(datetime.now(HELSINKI)), 50)
            self.assertIn(mock.call(10, wait_seconds=mock.ANY), refresh.mock_calls)

    def test_buttons_move_through_the_deck(self):
        from unittest import mock
        from urllib.request import urlopen
        # config.toml's example: by day the "day" deck, with two screens.
        noon = datetime.now(HELSINKI).replace(hour=12, minute=0, second=30)
        self.enterContext(mock.patch.object(schedule, "plan", return_value=schedule.Plan(noon, 300, 5)))
        with self.get("/glance.png") as r:
            self.assertEqual(r.headers["X-Glance-Screen"], "day: home (1/2)")
            self.assertEqual(r.headers["X-Glance-Deck-Size"], "2")
        with self.get("/glance.png?button=next") as r:
            self.assertEqual(r.headers["X-Glance-Screen"], "day: departures (2/2)")
        with self.get("/glance.g2") as r:  # a timed wake keeps the screen
            self.assertEqual(r.headers["X-Glance-Screen"], "day: departures (2/2)")
        with urlopen(self.base + "/glance.png?button=next") as r:  # without the client header: ignored
            self.assertEqual(r.headers["X-Glance-Screen"], "day: departures (2/2)")
        with self.get("/glance.png?button=next") as r:  # wraps around
            self.assertEqual(r.headers["X-Glance-Screen"], "day: home (1/2)")
        with self.get("/glance.png?button=prev") as r:
            self.assertEqual(r.headers["X-Glance-Screen"], "day: departures (2/2)")

    def test_one_screen_deck_answers_no_change(self):
        from unittest import mock
        night = datetime.now(HELSINKI).replace(hour=23, minute=30, second=5)
        fixed = schedule.Plan(night, sleep_seconds=300, interval_minutes=15)
        with mock.patch.object(schedule, "plan", return_value=fixed):
            with self.get("/glance.g2?button=next") as r:
                self.assertEqual(r.status, 204)  # the device keeps its image: no 6 s refresh
                self.assertEqual(r.read(), b"")
                self.assertEqual(r.headers["X-Glance-Sleep"], "300")
                self.assertEqual(r.headers["X-Glance-Deck-Size"], "1")
            with self.get("/glance.g2?button=refresh") as r:  # the green button still refreshes
                self.assertEqual((r.status, r.headers["X-Glance-Screen"]), (200, "night: home (1/1)"))

    def test_screen_change_uses_the_screen_drawn_ahead(self):
        from unittest import mock
        from glance.sources.hsl import Transit
        noon = datetime.now(HELSINKI).replace(hour=12, minute=0, second=40, microsecond=0)
        self.enterContext(mock.patch.object(schedule, "plan", return_value=schedule.Plan(noon, 260, 5)))
        self.prerendered.put(noon.replace(second=0), "departures", {"/glance.png": b"drawn ahead"})
        with mock.patch.object(render, "render") as draw, mock.patch.object(Transit, "refresh"):
            with self.get("/glance.png?button=next") as r:  # 40 s into the minute: still that minute's
                self.assertEqual(r.read(), b"drawn ahead")
            draw.assert_not_called()

    def test_dripping_request_is_dropped(self):
        # One byte at a time never trips a per-read timeout; the whole request has a deadline.
        import socket, time
        self.server.RequestHandlerClass.timeout = 0.5
        started = time.monotonic()
        with socket.create_connection(self.server.server_address) as s:
            s.settimeout(0.1)
            closed = False
            while not closed and time.monotonic() - started < 5:
                try:
                    s.sendall(b"G")
                    closed = s.recv(1) == b""
                except TimeoutError:
                    pass  # still open: nothing to read
                except OSError:
                    closed = True
        self.assertTrue(closed)
        self.assertLess(time.monotonic() - started, 3)

    def test_malformed_request_line(self):
        import socket
        with socket.create_connection(self.server.server_address) as s:
            s.sendall(b"GET http://[x/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            s.settimeout(5)
            self.assertIn(b" 400 ", s.recv(100))

    def test_bursts_share_one_drawing(self):
        import threading, urllib.request
        from unittest import mock
        with mock.patch.object(render, "render", wraps=render.render) as drawn:
            threads = [
                threading.Thread(target=lambda: urllib.request.urlopen(self.base + "/glance.png").read())
                for _ in range(8)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        # One drawing, or two if the minute changed in between: not eight.
        self.assertLessEqual(drawn.call_count, 2)

    def test_idle_connection_is_dropped(self):
        import socket
        self.server.RequestHandlerClass.timeout = 0.2
        with socket.create_connection(self.server.server_address) as s:
            s.settimeout(5)
            self.assertEqual(s.recv(1), b"")  # closed by the server, not held open

    def test_foreign_host_is_refused(self):
        import urllib.error, urllib.request
        def get(host):
            req = urllib.request.Request(self.base + "/glance.png", headers={"Host": host})
            try:
                return urllib.request.urlopen(req).status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertEqual(get("rebind.attacker.example:8080"), 403)
        self.assertEqual(get("192.168.1.50:8080"), 200)

    def test_host_allowed(self):
        from glance.__main__ import host_allowed
        for host in (None, "", "192.168.1.50:8080", "10.0.0.2", "[fe80::1]:8080", "[::1]", "localhost:8080",
                     "raspberrypi:8080", "Glance.local:8080", "glance.local.", "app.localhost"):
            self.assertTrue(host_allowed(host), host)
        for host in ("attacker.example", "attacker.example:8080", "192.168.1.50.attacker.example",
                     "glance.local.attacker.example", "a b", "[nonsense]", "x.local@attacker.example"):
            self.assertFalse(host_allowed(host), host)
        self.assertTrue(host_allowed("glance.home.example:8080", ("glance.home.example",)))
        self.assertTrue(host_allowed("my_pi.lan", ("my_pi.lan",)))  # listed names count as they are
        self.assertTrue(host_allowed("my_pi:8080"))

    def test_client_allowed(self):
        import ipaddress
        from glance.__main__ import client_allowed
        for address in ("192.168.1.50", "10.0.0.2", "172.16.5.1", "127.0.0.1", "169.254.3.4", "::1",
                        "fe80::1%eth0", "fd12:3456::1", "::ffff:192.168.1.50"):
            self.assertTrue(client_allowed(address), address)
        # Public addresses: what a port forwarded by mistake would let in.
        for address in ("8.8.8.8", "2001:4860:4860::8888", "::ffff:8.8.8.8", "100.64.0.1", "nonsense", "",
                        "2002:c000:204::1", "2001:0:4136:e378:8000:63bf:3fff:fdd2"):  # 6to4, Teredo: public clients
            self.assertFalse(client_allowed(address), address)
        vpn = (ipaddress.ip_network("100.64.0.0/10"),)
        self.assertTrue(client_allowed("100.64.0.1", vpn))
        self.assertFalse(client_allowed("8.8.8.8", vpn))
        self.assertFalse(client_allowed("2001:4860:4860::8888", vpn))  # not an error for the other family

    def test_public_address_is_refused(self):
        import urllib.error
        from unittest import mock
        with mock.patch("glance.__main__.client_allowed", return_value=False):
            for path in ("/", "/glance.png"):
                with self.assertRaises(urllib.error.HTTPError) as refused:
                    self.get(path)
                self.assertEqual(refused.exception.code, 403)

    def test_answer_headers(self):
        import http.client
        for path in ("/", "/glance.png", "/nothing-here"):
            conn = http.client.HTTPConnection(*self.server.server_address)
            conn.request("GET", path)
            r = conn.getresponse()
            r.read()
            conn.close()
            with self.subTest(path):
                self.assertEqual(r.headers["Server"], "Glance")  # no Python version
                self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(r.headers["X-Frame-Options"], "DENY")

    def test_log_escapes_control_characters(self):
        from glance.__main__ import log_safe
        self.assertEqual(log_safe('"GET /x\x1b[31m\x08 HTTP/1.1" 404 -'), '"GET /x\\x1b[31m\\x08 HTTP/1.1" 404 -')
        self.assertEqual(log_safe("a\\b\n"), "a\\\\b\\x0a")
        self.assertEqual(log_safe("Länsisatama 5°"), "Länsisatama 5°")


if __name__ == "__main__":
    unittest.main()
