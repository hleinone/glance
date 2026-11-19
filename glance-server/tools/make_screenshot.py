"""Render the README screenshots from sample data: a winter weekday morning
on the example's first screen, and a summer afternoon on its second, with
the spot prices and the sunrise and sunset at the example's location.

    mise run screenshot        (writes ../docs/screenshot.png and screenshot-summer.png)

Uses the real renderer but made-up data, so the screenshots don't depend on
the weather, the market or the time of day, and never show anyone's own
stops. The departures are modelled on real ones at Pasila (routes,
destinations, which are trunk buses).
"""

from __future__ import annotations

import math
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glance import config, render, sun  # noqa: E402
from glance.sources import elering, fmi  # noqa: E402
from glance.sources.hsl import Departure, StopDepartures  # noqa: E402

TZ = ZoneInfo("Europe/Helsinki")
DOCS = Path(__file__).resolve().parent.parent.parent / "docs"
OUT = DOCS / "screenshot.png"
OUT_SUMMER = DOCS / "screenshot-summer.png"


def winter_morning() -> render.Scene:
    now = datetime(2027, 1, 19, 8, 12, 4, tzinfo=TZ)  # a Tuesday, in the every-minute period
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Hour by hour from midnight: (temperature, WeatherSymbol3, chance of precipitation %).
    # A clear, cold night; snow showers in the morning; cloud and light snow later.
    hourly = [
        (-9.2, 1, 0), (-9.4, 1, 0), (-9.6, 1, 0), (-9.5, 1, 0), (-9.3, 2, 0), (-9.0, 2, 5),
        (-8.6, 3, 10), (-8.1, 41, 40), (-7.4, 41, 60), (-6.6, 41, 50), (-5.7, 2, 20), (-4.6, 2, 10),
        (-3.8, 1, 0), (-3.2, 2, 0), (-3.4, 3, 20), (-3.9, 51, 60), (-4.4, 52, 80), (-4.8, 52, 90),
        (-5.3, 51, 60), (-5.9, 3, 30), (-6.5, 3, 10), (-7.1, 2, 0), (-7.6, 1, 0), (-8.0, 1, 0),
    ]
    hours = [
        fmi.Hour(
            time=(midnight + timedelta(hours=i)).astimezone(UTC),
            temperature=t, wind_speed=4.2, wind_direction=20.0, pop=pop, symbol=sym,
            feels_like=t - 6.3,
        )
        for i, (t, sym, pop) in enumerate(hourly)
    ]
    forecast = fmi.Forecast(fetched_at=now - timedelta(minutes=6), hours=hours)

    def deps(rows: list[tuple[str, str, int, bool, str, bool]]) -> list[Departure]:
        # (route, headsign, seconds from now, realtime, mode, trunk)
        return [Departure(r, h, now + timedelta(seconds=s), rt, m, tr) for r, h, s, rt, m, tr in rows]

    # By widget name in config.toml's example layout.
    stops = {
        "trams": StopDepartures("Pasila trams", deps([
            ("9", "Länsisatama", 25, True, "tram", False),
            ("7", "Meilahden sairaala", 95, True, "tram", False),
            ("2", "Olympiaterm.", 200, True, "tram", False),
            ("13", "Nihti", 290, True, "tram", False),
            ("9", "Ilmala", 380, True, "tram", False),
            ("7", "Länsisatama", 470, True, "tram", False),
            ("2", "Messukeskus", 610, False, "tram", False),
            ("13", "Länsi-Pasila", 700, False, "tram", False),
        ]), ("tram",)),
        "buses": StopDepartures("Pasila buses", deps([
            ("500", "Itäkeskus(M)", 80, True, "bus", True),
            ("59", "Herttoniemi(M)", 150, True, "bus", False),
            ("510", "Kivenlahti", 260, True, "bus", True),
            ("23", "Pirkkola", 330, True, "bus", False),
            ("500", "Munkkivuori", 420, True, "bus", True),
            ("518", "Kuninkaanmäki", 540, True, "bus", False),
            ("510", "Herttoniemi(M)", 640, False, "bus", True),
            ("59", "Malminkartano", 790, False, "bus", False),
        ]), ("bus",)),
        "trains": StopDepartures("Pasila trains", deps([
            ("I", "Tikkurila-Lentoasema", 70, True, "rail", False),
            ("K", "Kerava", 230, True, "rail", False),
            ("P", "Lentoasema", 400, True, "rail", False),
            ("T", "Riihimäki", 520, True, "rail", False),
            ("I", "Tikkurila-Lentoasema", 670, True, "rail", False),
            ("K", "Kerava", 830, False, "rail", False),
            ("P", "Lentoasema", 1000, False, "rail", False),
            ("R", "Riihimäki", 1180, False, "rail", False),
        ]), ("rail",)),
    }
    return render.Scene(
        now=now,
        language="en",
        forecasts={fmi.HOME: forecast},
        stops=stops,
        departures_live=True,
        departures_fetched_at=now - timedelta(seconds=19),
        battery_percent=87,
    )


def summer_afternoon() -> render.Scene:
    """A sunny, windy June Tuesday afternoon: the morning's departures moved
    to it, and spot prices that fall below zero around midday."""
    morning = winter_morning()
    now = datetime(2027, 6, 15, 16, 42, 4, tzinfo=TZ)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Hour by hour from midnight: (temperature, WeatherSymbol3, chance of precipitation %).
    hourly = [
        (13.1, 1, 0), (12.6, 1, 0), (12.2, 1, 0), (12.0, 1, 0), (12.4, 1, 0), (13.5, 1, 0),
        (14.9, 1, 0), (16.2, 2, 0), (17.4, 2, 0), (18.5, 2, 0), (19.6, 2, 10), (20.4, 2, 10),
        (21.1, 3, 20), (21.6, 3, 30), (21.4, 21, 50), (20.8, 21, 40), (20.9, 2, 20), (20.6, 2, 10),
        (19.8, 2, 0), (18.7, 1, 0), (17.5, 1, 0), (16.2, 1, 0), (15.1, 1, 0), (14.3, 1, 0),
    ]
    hours = [
        fmi.Hour(
            time=(midnight + timedelta(hours=i)).astimezone(UTC),
            temperature=t, wind_speed=7.4, wind_direction=230.0, pop=pop, symbol=sym, feels_like=t - 1.2,
        )
        for i, (t, sym, pop) in enumerate(hourly * 2)  # tomorrow alike
    ]
    hours = [h for h in hours if h.time < (midnight + timedelta(days=1, hours=12)).astimezone(UTC)]

    def price(i: int) -> float:
        """€/MWh for the i-th quarter hour from midnight: cheap overnight, below
        zero when the sun is high, dearer in the evening."""
        h = (i / 4) % 24
        solar = 75 * math.exp(-((h - 13) / 3.2) ** 2)
        evening = 55 * math.exp(-((h - 20) / 1.7) ** 2)
        return round(38 + evening - solar + 4 * math.sin(i * 2.3), 2)

    prices = elering.Prices(now - timedelta(minutes=40), tuple(
        elering.Price((midnight + i * elering.STEP).astimezone(UTC), price(i)) for i in range(2 * 96)))

    shift = now - morning.now
    stops = {name: replace(stop, departures=[replace(d, time=d.time + shift) for d in stop.departures])
             for name, stop in morning.stops.items()}
    return replace(
        morning, now=now, forecasts={fmi.HOME: fmi.Forecast(fetched_at=now - timedelta(minutes=6), hours=hours)},
        stops=stops, departures_fetched_at=now - timedelta(seconds=19), battery_percent=64, spot_prices=prices,
    )


def main() -> None:
    # config.toml alone: its example layout, not the local one.
    cfg = replace(config.load(local_overrides=False), language="en")
    DOCS.mkdir(exist_ok=True)
    for out, scene, screen in ((OUT, winter_morning(), None), (OUT_SUMMER, summer_afternoon(), "departures")):
        scene = replace(scene, sun=sun.day(cfg.lat, cfg.lon, scene.now.date(), cfg.tz))
        render.render(cfg, scene, screen).save(out, optimize=True)
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
