"""Sunrise, sunset and the day's length, calculated from a place and a date.

NOAA's solar formulas (https://gml.noaa.gov/grad/solcalc/calcdetails.html),
with the sun's upper edge at the horizon after refraction (−0.833°), as FMI's
own times use: within seconds of FMI's at Finland's latitudes. Nothing is
fetched. `day` answers for any latitude, longitude and date the config
accepts, since the scene is built from it for every drawing.

A day's sunrise is the one before its solar noon and its sunset the one after,
so in the far north in summer the sunset can fall after midnight.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo

HORIZON = -0.833  # degrees: the sun's upper edge, after refraction
# The longest a polar night or day lasts is about 186 days, at the poles.
POLAR_SEARCH_DAYS = 200
J2000 = datetime(2000, 1, 1, 12, tzinfo=UTC)
HALF_DAY = timedelta(hours=12)


@dataclass(frozen=True)
class Day:
    date: date
    # Local. None on a polar night; the sunrise also on the one day a year the
    # sun was up all night but sets the next, the sunset when it won't set.
    sunrise: datetime | None
    sunset: datetime | None
    length: timedelta  # from sunrise to sunset; 0 on a polar night, 24 h when the sun doesn't set
    change: timedelta  # since yesterday
    # "night": the sun doesn't rise today; "day": it doesn't set after today's noon.
    polar: Literal["night", "day"] | None
    # In a polar spell: the next sunrise (after a polar night) or sunset (after
    # a polar day), local. None if not within POLAR_SEARCH_DAYS.
    polar_ends: datetime | None = None


@dataclass(frozen=True)
class _Events:
    noon: datetime
    sunrise: datetime | None
    sunset: datetime | None
    polar: Literal["night", "day"] | None


def _position(t: datetime) -> tuple[float, float]:
    """The sun's declination (degrees) and the equation of time (minutes) at t."""
    c = (t - J2000).total_seconds() / 86400 / 36525
    mean_long = math.radians((280.46646 + c * (36000.76983 + c * 0.0003032)) % 360)
    anomaly = math.radians(357.52911 + c * (35999.05029 - 0.0001537 * c))
    ecc = 0.016708634 - c * (0.000042037 + 0.0000001267 * c)
    centre = (
        math.sin(anomaly) * (1.914602 - c * (0.004817 + 0.000014 * c))
        + math.sin(2 * anomaly) * (0.019993 - 0.000101 * c)
        + math.sin(3 * anomaly) * 0.000289
    )
    omega = math.radians(125.04 - 1934.136 * c)
    apparent_long = math.radians(math.degrees(mean_long) + centre - 0.00569 - 0.00478 * math.sin(omega))
    obliquity = math.radians(
        23 + (26 + (21.448 - c * (46.815 + c * (0.00059 - c * 0.001813))) / 60) / 60 + 0.00256 * math.cos(omega)
    )
    declination = math.asin(math.sin(obliquity) * math.sin(apparent_long))
    y = math.tan(obliquity / 2) ** 2
    eot = 4 * math.degrees(
        y * math.sin(2 * mean_long)
        - 2 * ecc * math.sin(anomaly)
        + 4 * ecc * y * math.sin(anomaly) * math.cos(2 * mean_long)
        - 0.5 * y * y * math.sin(4 * mean_long)
        - 1.25 * ecc * ecc * math.sin(2 * anomaly)
    )
    return math.degrees(declination), eot


def _cos_hour_angle(lat: float, t: datetime) -> float:
    """At the sun's declination at t: below −1 it doesn't set, above 1 it doesn't rise."""
    phi, delta = math.radians(lat), math.radians(_position(t)[0])
    return (math.sin(math.radians(HORIZON)) - math.sin(phi) * math.sin(delta)) / (math.cos(phi) * math.cos(delta))


def _solar_noon(lon: float, near: datetime) -> datetime:
    """The solar noon closest to `near` (UTC)."""
    noon = near
    for _ in range(2):
        midnight = datetime.combine(noon.date(), time(), UTC)
        noon = midnight + timedelta(minutes=720 - 4 * lon - _position(noon)[1])
        noon += timedelta(days=round((near - noon) / timedelta(days=1)))
    return noon


def _event(lat: float, noon: datetime, sign: int) -> datetime:
    """Sunrise (sign −1) before or sunset (sign 1) after this solar noon."""
    noon_eot = _position(noon)[1]
    t = noon
    for _ in range(3):
        hour_angle = math.degrees(math.acos(max(-1.0, min(1.0, _cos_hour_angle(lat, t)))))
        t = noon + timedelta(minutes=noon_eot - _position(t)[1] + sign * 4 * hour_angle)
    return t


def _events(lat: float, lon: float, on: date, tz: ZoneInfo) -> _Events:
    noon = _solar_noon(lon, datetime.combine(on, time(12), tz).astimezone(UTC))
    if _cos_hour_angle(lat, noon) > 1:
        return _Events(noon, None, None, "night")
    # The sun rises before noon only if it was down around the solar midnight
    # before, and sets after it only if it goes down around the one after.
    sunrise = _event(lat, noon, -1) if _cos_hour_angle(lat, noon - HALF_DAY) >= -1 else None
    sunset = _event(lat, noon, 1) if _cos_hour_angle(lat, noon + HALF_DAY) >= -1 else None
    return _Events(noon, sunrise, sunset, None if sunset else "day")


def _length(events: _Events) -> timedelta:
    if events.polar == "night":
        return timedelta()
    rise = events.sunrise or events.noon - HALF_DAY
    set_ = events.sunset or events.noon + HALF_DAY
    return min(set_ - rise, timedelta(days=1))


def _polar_ends(lat: float, lon: float, on: date, tz: ZoneInfo, polar: str) -> datetime | None:
    for days in range(1, POLAR_SEARCH_DAYS + 1):
        later = _events(lat, lon, on + timedelta(days=days), tz)
        if found := later.sunrise if polar == "night" else later.sunset:
            return found.astimezone(tz)
    return None


@lru_cache(maxsize=8)
def day(lat: float, lon: float, on: date, tz: ZoneInfo) -> Day:
    """The sun on a local date at a place, north and east positive."""
    today = _events(lat, lon, on, tz)
    yesterday = _events(lat, lon, on - timedelta(days=1), tz)
    length = _length(today)
    return Day(
        date=on,
        sunrise=today.sunrise.astimezone(tz) if today.sunrise else None,
        sunset=today.sunset.astimezone(tz) if today.sunset else None,
        length=length,
        change=length - _length(yesterday),
        polar=today.polar,
        polar_ends=_polar_ends(lat, lon, on, tz, today.polar) if today.polar else None,
    )
