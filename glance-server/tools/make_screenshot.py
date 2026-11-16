"""Render the README screenshot from sample data: a winter weekday morning.

    mise run screenshot        (writes ../docs/screenshot.png)

Uses the real renderer but made-up data, so the screenshot doesn't depend on
the weather or the time of day, and never shows anyone's own stops. The
departures are modelled on real ones at Pasila (routes, destinations, which
are trunk buses). Add a second scene (summer) here if another screenshot is
ever needed.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glance import config, render  # noqa: E402
from glance.sources import fmi  # noqa: E402
from glance.sources.hsl import Departure, StopDepartures  # noqa: E402

TZ = ZoneInfo("Europe/Helsinki")
OUT = Path(__file__).resolve().parent.parent.parent / "docs" / "screenshot.png"


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
        forecast=forecast,
        weather_stale_after=timedelta(minutes=45),
        stops=stops,
        departures_live=True,
        departures_fetched_at=now - timedelta(seconds=19),
        battery_percent=87,
    )


def main() -> None:
    # config.toml alone: its example layout, not the local one.
    cfg = replace(config.load(local_overrides=False), language="en")
    img = render.render(cfg, winter_morning())
    OUT.parent.mkdir(exist_ok=True)
    img.save(OUT, optimize=True)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
