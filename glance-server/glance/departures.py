"""Departure data model for the stop widgets, plus placeholder data.

Departures carry absolute times, so a cached list stays correct as time passes:
minutes are worked out at render time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


# Order of the mode icons when a widget serves several modes.
MODE_ORDER = ("subway", "rail", "tram", "bus", "ferry")


def ordered_modes(modes) -> tuple[str, ...]:
    unique = set(modes)
    return tuple(sorted(unique, key=lambda m: MODE_ORDER.index(m) if m in MODE_ORDER else len(MODE_ORDER)))


@dataclass(frozen=True)
class Departure:
    route: str
    headsign: str
    time: datetime  # timezone-aware
    realtime: bool
    mode: str  # "tram", "bus", ... (anything but tram is drawn like a bus)
    trunk: bool = False  # HSL trunk route (runkolinja), orange at HSL; light grey badge here

    def minutes(self, now: datetime) -> int:
        return max(0, int((self.time - now).total_seconds() // 60))


@dataclass(frozen=True)
class StopDepartures:
    name: str
    departures: list[Departure]
    modes: tuple[str, ...] = ()  # vehicle modes served, in MODE_ORDER, for the header icons

    def upcoming(self, now: datetime, count: int) -> list[Departure]:
        # Keep a departure until its minute has passed, like the HSL displays do.
        return [d for d in self.departures if d.time > now - timedelta(seconds=30)][:count]


_PLACEHOLDERS: list[list[tuple[str, str, int, bool, str]]] = [
    [
        ("7", "Senaatintori", 1, True, "tram"),
        ("9", "Länsisatama", 3, True, "tram"),
        ("7", "Pasila", 8, True, "tram"),
        ("9", "Jätkäsaari", 11, True, "tram"),
        ("7", "Senaatintori", 15, False, "tram"),
        ("9", "Länsisatama", 19, False, "tram"),
        ("7", "Pasila", 24, False, "tram"),
        ("9", "Jätkäsaari", 27, False, "tram"),
    ],
    [
        ("59", "Pasilan asema", 0, True, "bus"),
        ("506", "Meilahti", 4, True, "bus"),
        ("23", "Ruskeasuo", 6, True, "bus"),
        ("59", "Itäkeskus (M)", 12, True, "bus"),
        ("506", "Viikki", 17, False, "bus"),
        ("23", "Central Railway Station", 21, False, "bus"),
        ("550", "Westendinasema", 26, False, "bus"),
        ("59", "Pasilan asema", 32, False, "bus"),
    ],
    [
        ("10", "Kirurgi", 2, True, "tram"),
        ("550", "Itäkeskus (M)", 5, True, "bus"),
        ("10", "Pikku Huopalahti", 9, True, "tram"),
        ("69", "Kalasatama (M)", 13, False, "bus"),
        ("550", "Westendinasema", 18, False, "bus"),
        ("10", "Kirurgi", 22, False, "tram"),
        ("69", "Kontula (M)", 29, False, "bus"),
        ("10", "Pikku Huopalahti", 64, False, "tram"),
    ],
]


_PLACEHOLDER_TRUNK = {"550"}  # a real trunk route, so placeholders show the style


def placeholder(name: str, index: int, now: datetime) -> StopDepartures:
    base = now + timedelta(seconds=30)
    rows = _PLACEHOLDERS[index % len(_PLACEHOLDERS)]
    return StopDepartures(
        name=name,
        departures=[
            Departure(route, headsign, base + timedelta(minutes=m), realtime, mode, route in _PLACEHOLDER_TRUNK)
            for route, headsign, m, realtime, mode in rows
        ],
        modes=ordered_modes(row[4] for row in rows),
    )
