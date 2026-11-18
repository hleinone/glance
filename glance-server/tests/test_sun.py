import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from glance import sun

HELSINKI = ZoneInfo("Europe/Helsinki")
# FMI's coordinates for each place, from its open data timeseries service.
PLACES = {"Helsinki": (60.16952, 24.93545), "Rovaniemi": (66.5, 25.71667), "Utsjoki": (69.90864, 27.02843)}
# Sunrise and sunset in Finnish time, as published by FMI's open data
# timeseries service (opendata.fmi.fi, CC BY 4.0, © Finnish Meteorological
# Institute), copied by hand: the solstices, both days the clocks change, and
# the last and first days of Utsjoki's polar night.
FMI = [
    ("Helsinki", "2026-12-21", "09:23:49", "15:12:45"),
    ("Helsinki", "2026-06-21", "03:54:01", "22:50:06"),
    ("Helsinki", "2026-03-29", "06:54:48", "19:56:41"),
    ("Helsinki", "2026-10-25", "07:24:49", "16:42:43"),
    ("Rovaniemi", "2026-12-21", "11:07:53", "13:22:26"),
    ("Rovaniemi", "2026-03-29", "06:42:27", "20:03:19"),
    ("Rovaniemi", "2026-10-25", "07:49:45", "16:11:17"),
    ("Utsjoki", "2026-03-29", "06:29:56", "20:05:47"),
    ("Utsjoki", "2026-10-25", "08:07:38", "15:42:43"),
    ("Utsjoki", "2026-11-25", "11:30:00", "12:26:40"),
    ("Utsjoki", "2027-01-17", "11:50:00", "12:54:18"),
]
MINUTE = timedelta(minutes=1)


def at(day: str, clock: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=HELSINKI)


def day(place: str, on: str) -> sun.Day:
    return sun.day(*PLACES[place], date.fromisoformat(on), HELSINKI)


class SunTest(unittest.TestCase):
    def test_sunrise_and_sunset_within_a_minute_of_fmi(self):
        for place, on, sunrise, sunset in FMI:
            with self.subTest(place=place, on=on):
                d = day(place, on)
                self.assertIsNone(d.polar)
                self.assertLess(abs(d.sunrise - at(on, sunrise)), MINUTE)
                self.assertLess(abs(d.sunset - at(on, sunset)), MINUTE)

    def test_the_day_length_and_its_change_since_yesterday(self):
        # FMI's day lengths (minutes): Utsjoki 88 on 24 November, 57 on the
        # 25th; 0 on 16 January, 64 on the 17th. Helsinki 437 on 18 November.
        self.assertEqual(round(day("Helsinki", "2026-11-18").length / MINUTE), 437)
        self.assertEqual(round(day("Utsjoki", "2026-11-25").change / MINUTE), 57 - 88)
        self.assertEqual(round(day("Utsjoki", "2027-01-17").change / MINUTE), 64)
        self.assertGreater(day("Helsinki", "2026-03-29").change, timedelta())  # spring: the days grow

    def test_a_day_the_sun_does_not_set_or_rise_on_is_24_hours_long(self):
        # FMI's day lengths (minutes): Utsjoki 1440 on 27 July (up all night,
        # sets after midnight) and 1354 on the 28th; Rovaniemi 1440 on 6 July
        # and 1394 on the 7th.
        for place, on, length, change in (("Utsjoki", "2026-07-27", 1440, 0), ("Utsjoki", "2026-07-28", 1354, 1354 - 1440),
                                          ("Rovaniemi", "2026-07-06", 1440, 0), ("Rovaniemi", "2026-07-07", 1394, 1394 - 1440),
                                          ("Utsjoki", "2026-05-16", 1440, None)):
            with self.subTest(place=place, on=on):
                d = day(place, on)
                self.assertAlmostEqual(d.length / MINUTE, length, delta=1)
                if change is not None:
                    self.assertAlmostEqual(d.change / MINUTE, change, delta=1)

    def test_polar_night_says_when_the_sun_next_rises(self):
        d = day("Utsjoki", "2026-12-21")
        self.assertEqual((d.polar, d.sunrise, d.sunset, d.length), ("night", None, None, timedelta()))
        self.assertLess(abs(d.polar_ends - at("2027-01-17", "11:50:00")), MINUTE)  # FMI: 17 January 11:50
        self.assertEqual(day("Utsjoki", "2026-11-26").polar, "night")  # FMI: none from the 26th

    def test_midnight_sun_says_when_the_sun_next_sets(self):
        d = day("Utsjoki", "2026-06-21")
        self.assertEqual((d.polar, d.sunrise, d.sunset, d.length), ("day", None, None, timedelta(days=1)))
        # FMI, for 27 July: the sun was up all night, and sets at 00:43:21 on the 28th.
        self.assertLess(abs(d.polar_ends - at("2026-07-28", "00:43:21")), MINUTE)
        last = day("Utsjoki", "2026-07-27")
        self.assertIsNone(last.sunrise)
        self.assertIsNone(last.polar)
        self.assertLess(abs(last.sunset - at("2026-07-28", "00:43:21")), MINUTE)

    def test_rovaniemis_midnight_sun_ends_on_7_july(self):
        # FMI's rows: no sunrise on 6 July, one at 01:39:06 on the 7th, and
        # that day's sunset at 00:53:31 on the 8th. So the sun first sets in
        # the small hours of the 7th. (FMI's own "next sunset" for a date in
        # the spell says 9 July, which its rows for the 7th contradict.)
        d = day("Rovaniemi", "2026-06-21")
        self.assertEqual(d.polar, "day")
        self.assertEqual(d.polar_ends.date(), date(2026, 7, 7))
        self.assertLess(d.polar_ends, at("2026-07-07", "01:39:06"))
        seventh = day("Rovaniemi", "2026-07-07")
        self.assertLess(abs(seventh.sunrise - at("2026-07-07", "01:39:06")), MINUTE)
        self.assertLess(abs(seventh.sunset - at("2026-07-08", "00:53:31")), MINUTE)

    def test_the_first_day_of_midnight_sun_has_a_sunrise_and_no_sunset(self):
        d = day("Utsjoki", "2026-05-16")  # FMI: rises at 01:43:11, then not down until July
        self.assertEqual(d.polar, "day")
        self.assertLess(abs(d.sunrise - at("2026-05-16", "01:43:11")), MINUTE)
        self.assertIsNone(d.sunset)

    def test_answers_for_every_place_and_date_the_config_accepts(self):
        # The scene is built from it for every drawing: an exception here would
        # stop every image. The poles, the date line, and time zones far from
        # the place's own.
        zones = (ZoneInfo("UTC"), ZoneInfo("Pacific/Kiritimati"), ZoneInfo("America/Adak"))
        lats = sorted({*range(-90, 91, 6), -90.0, -66.6, 66.6, 89.9, 90.0})
        for lat in lats:
            for lon in (-180.0, 0.0, 180.0):
                for tz in zones:
                    for k in range(0, 366, 13):
                        on = date(2026, 1, 1) + timedelta(days=k)
                        with self.subTest(lat=lat, lon=lon, tz=tz, on=on):
                            d = sun.day(float(lat), lon, on, tz)
                            self.assertTrue(timedelta() <= d.length <= timedelta(days=1))


if __name__ == "__main__":
    unittest.main()
