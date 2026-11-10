"""Load config.toml, with optional overrides from config.local.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from .schedule import DAY_NAMES, WakeRule, WakeSchedule

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Stop:
    name: str  # "" = use HSL's name for the stop
    ids: tuple[str, ...] = ()  # stop codes ("H0665") and/or gtfsIds ("HSL:1174411")
    routes: frozenset[str] = frozenset()  # empty = all routes


@dataclass(frozen=True)
class Config:
    language: str
    tz: ZoneInfo
    lat: float
    lon: float
    host: str
    port: int
    weather_refresh_minutes: int
    weather_stale_after_minutes: int
    cache_dir: Path
    # Extra names the server may be called by (Host header), besides IP
    # addresses, single-label names and .local ones; see host_allowed().
    hostnames: tuple[str, ...] = ()
    digitransit_key: str | None = None
    departures_poll_lead_seconds: int = 15
    departures_min_refresh_seconds: int = 10
    departures_stale_after_minutes: int = 5
    wake_schedule: WakeSchedule = WakeSchedule()
    wake_offset_seconds: float = 2
    battery_full_mv: int = 4200  # what this device reads for a full battery
    stops: list[Stop] = field(default_factory=list)


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: Path | None = None, local_overrides: bool = True) -> Config:
    """local_overrides=False leaves out config.local.toml and the environment:
    the tests use it, so they don't depend on this machine's settings."""
    path = path or ROOT / "config.toml"
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    local = path.with_name("config.local.toml")
    if local_overrides and local.exists():
        with open(local, "rb") as f:
            raw = _merge(raw, tomllib.load(f))

    language = raw.get("language", "en")
    if language not in ("en", "fi"):
        raise ValueError(f"language must be 'en' or 'fi', not {language!r}")

    location = raw["location"]
    server = raw.get("server", {})
    weather = raw.get("weather", {})
    digitransit = raw.get("digitransit", {})
    device = raw.get("device", {})
    key = (local_overrides and os.environ.get("GLANCE_DIGITRANSIT_KEY")) or digitransit.get("key") or None
    hostnames = server.get("hostnames", [])
    if isinstance(hostnames, str):
        hostnames = [hostnames]
    cache_dir = Path(raw.get("cache", {}).get("dir", "cache"))
    if not cache_dir.is_absolute():
        cache_dir = path.parent / cache_dir

    return Config(
        language=language,
        tz=ZoneInfo(raw.get("timezone", "Europe/Helsinki")),
        lat=float(location["lat"]),
        lon=float(location["lon"]),
        host=server.get("host", "0.0.0.0"),
        port=int(server.get("port", 8080)),
        weather_refresh_minutes=int(weather.get("refresh_minutes", 15)),
        weather_stale_after_minutes=int(weather.get("stale_after_minutes", 45)),
        cache_dir=cache_dir,
        hostnames=tuple(str(h).rstrip(".").lower() for h in hostnames),
        digitransit_key=key,
        # Under a minute: the poll belongs to the minute it runs before.
        departures_poll_lead_seconds=_within(digitransit.get("poll_lead_seconds", 15), 0, 59, "poll_lead_seconds"),
        departures_min_refresh_seconds=_within(digitransit.get("min_refresh_seconds", 10), 0, 3600, "min_refresh_seconds"),
        departures_stale_after_minutes=int(digitransit.get("stale_after_minutes", 5)),
        wake_schedule=_wake_schedule(device),
        wake_offset_seconds=_within(device.get("wake_offset_seconds", 2), 0, 59, "wake_offset_seconds", float),
        battery_full_mv=int(raw.get("battery", {}).get("full_mv", 4200)),
        stops=[_stop(s) for s in raw.get("stops", [])],
    )


def _stop(raw: dict) -> Stop:
    # `codes` (as on the stop sign, e.g. "H0665") and `ids` (e.g. "HSL:1174411")
    # both end up in Stop.ids; codes are resolved to ids at runtime.
    ids = []
    for key in ("codes", "code", "ids", "id"):
        value = raw.get(key, [])
        # str(): an unquoted code (codes = [1234]) is a number in TOML.
        ids.extend(str(v) for v in ([value] if isinstance(value, (str, int)) else value))
    return Stop(
        name=raw.get("name", ""),
        ids=tuple(ids),
        routes=frozenset(str(r) for r in raw.get("routes", [])),
    )


_DAY_GROUPS = {"weekdays": DAY_NAMES[:5], "weekend": DAY_NAMES[5:], "all": DAY_NAMES}


def _minutes(text: str) -> int:
    try:
        hours, minutes = (int(part) for part in str(text).split(":"))
    except ValueError:
        raise ValueError(f"bad time {text!r}: use HH:MM") from None
    value = hours * 60 + minutes
    if not (0 <= minutes <= 59 and 0 <= value <= 24 * 60):
        raise ValueError(f"bad time {text!r}: use HH:MM")
    return value % (24 * 60)


def _within(value, low: float, high: float, name: str, number=int):
    value = number(value)
    if not low <= value <= high:
        raise ValueError(f"{name} must be {low}..{high}, not {value}")
    return value


def _wake_schedule(device: dict) -> WakeSchedule:
    rules = []
    for raw in device.get("schedule", []):
        days = raw.get("days", "all")
        names = _DAY_GROUPS[days] if isinstance(days, str) else days
        every = _interval(raw["every"], "schedule 'every'")
        rules.append(WakeRule(
            days=frozenset(DAY_NAMES.index(d.lower()[:3]) for d in names),
            start=_minutes(raw["from"]),
            end=_minutes(raw["to"]),
            every=every,
        ))
    return WakeSchedule(tuple(rules), _interval(device.get("wake_interval_minutes", 1), "wake_interval_minutes"))


def _interval(value, name: str) -> int:
    every = int(value)
    if not 1 <= every <= 24 * 60:
        raise ValueError(f"{name} must be 1..1440 minutes, not {every}")
    return every
