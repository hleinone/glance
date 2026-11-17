"""The weather source: FMI's open data forecast (WFS, "simple" feature format,
no API key), its cache, and the latest good forecast (Weather).

Docs: https://en.ilmatieteenlaitos.fi/open-data-manual
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .. import i18n, upstream
from ..checks import within
from .periodic import Periodic

if TYPE_CHECKING:
    from ..config import Config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    """[fmi] in the config files."""
    refresh_minutes: int = 15
    stale_after_minutes: int = 45


@dataclass(frozen=True)
class ForecastRequest:
    """What a weather widget asks this source for: the forecast at [location]."""


def parse_settings(raw: dict) -> Settings:
    return Settings(
        refresh_minutes=within(raw.get("refresh_minutes", 15), 1, 24 * 60, "[fmi] refresh_minutes"),
        stale_after_minutes=within(raw.get("stale_after_minutes", 45), 1, 7 * 24 * 60, "[fmi] stale_after_minutes"),
    )

WFS_URL = "https://opendata.fmi.fi/wfs"
STORED_QUERY = "fmi::forecast::edited::weather::scandinavia::point::simple"
PARAMETERS = ("Temperature", "WindSpeedMS", "WindDirection", "PoP", "WeatherSymbol3", "FeelsLike")

NS = {
    "wfs": "http://www.opengis.net/wfs/2.0",
    "BsWfs": "http://xml.fmi.fi/schema/wfs/2.0",
    "ows": "http://www.opengis.net/ows/1.1",
}

USER_AGENT = "glance-dashboard/0.1"
# Seconds; weather is fetched in the background, nothing waits on it. TIMEOUT
# is for each step (connecting, each read), DEADLINE for the whole answer.
TIMEOUT = 10
DEADLINE = 20
# Hours dated outside these years are left out: nothing real, and a time near
# the edge of what datetime holds can't be converted to local time for drawing.
YEARS = range(2000, 2100)


@dataclass(frozen=True)
class Hour:
    time: datetime  # timezone-aware, UTC
    temperature: float | None
    wind_speed: float | None
    wind_direction: float | None  # degrees, direction the wind blows FROM
    pop: float | None  # probability of precipitation, %
    symbol: int | None  # WeatherSymbol3 code
    # FMI's "feels like" temperature (wind chill / heat). Defaulted so caches
    # written before it was added still load.
    feels_like: float | None = None


@dataclass(frozen=True)
class Forecast:
    fetched_at: datetime  # UTC
    hours: list[Hour]
    # The parameters it was fetched with: a cache from before a parameter was
    # added is refetched at once instead of waiting out the refresh interval.
    parameters: tuple[str, ...] = PARAMETERS

    def current(self, now: datetime) -> Hour | None:
        """The forecast step closest to `now`."""
        if not self.hours:
            return None
        return min(self.hours, key=lambda h: abs((h.time - now).total_seconds()))

    def upcoming(self, now: datetime, count: int) -> list[Hour]:
        """Full hours strictly after `now`."""
        return [h for h in self.hours if h.time > now][:count]

    def day_range(self, day_start: datetime, day_end: datetime) -> tuple[float, float] | None:
        temps = [
            h.temperature
            for h in self.hours
            if day_start <= h.time < day_end and h.temperature is not None
        ]
        return (min(temps), max(temps)) if temps else None


class FMIError(Exception):
    pass


def build_url(lat: float, lon: float, start: datetime, end: datetime) -> str:
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "getFeature",
        "storedquery_id": STORED_QUERY,
        "latlon": f"{lat:.4f},{lon:.4f}",
        "parameters": ",".join(PARAMETERS),
        "timestep": "60",
        "starttime": _iso(start),
        "endtime": _iso(end),
    }
    return f"{WFS_URL}?{urllib.parse.urlencode(params)}"


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _value(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        v = float(text)
    except ValueError:
        return None
    # FMI sends NaN for a missing value. Infinity is dropped too: it can't be
    # drawn, and from the cache it would break every image until the next fetch.
    return v if math.isfinite(v) else None


def parse(xml: bytes) -> list[Hour]:
    """Hours from a response body. Anything that isn't the expected XML (an
    error page from a proxy, a cut-off body) raises FMIError like FMI's own
    exception reports do, so callers have one failure to handle."""
    try:
        return _parse(xml)
    except (ET.ParseError, ValueError) as e:
        raise FMIError(f"unreadable response: {e}") from e


def _parse(xml: bytes) -> list[Hour]:
    root = ET.fromstring(xml)
    if root.tag == f"{{{NS['ows']}}}ExceptionReport":
        texts = [t.text or "" for t in root.iterfind(".//ows:ExceptionText", NS)]
        raise FMIError("; ".join(texts[:1]) or "FMI returned an exception report")

    by_time: dict[datetime, dict[str, float | None]] = {}
    for el in root.iterfind(".//BsWfs:BsWfsElement", NS):
        time_text = el.findtext("BsWfs:Time", namespaces=NS)
        name = el.findtext("BsWfs:ParameterName", namespaces=NS)
        if not time_text or not name:
            continue
        t = datetime.fromisoformat(time_text.replace("Z", "+00:00"))
        if t.tzinfo is None:
            # FMI's times are UTC and say so ("Z"). One without a zone can't
            # be compared with the clock, which would fail every drawing.
            t = t.replace(tzinfo=UTC)
        if t.year not in YEARS:
            continue
        by_time.setdefault(t, {})[name] = _value(
            el.findtext("BsWfs:ParameterValue", namespaces=NS)
        )

    hours = []
    for t in sorted(by_time):
        v = by_time[t]
        symbol = v.get("WeatherSymbol3")
        hours.append(
            Hour(
                time=t,
                temperature=v.get("Temperature"),
                wind_speed=v.get("WindSpeedMS"),
                wind_direction=v.get("WindDirection"),
                pop=v.get("PoP"),
                # A code is a small number; anything else has no icon (and a
                # huge one made a file name too long to look for).
                symbol=int(symbol) if symbol is not None and 0 <= symbol < 1000 else None,
                feels_like=v.get("FeelsLike"),
            )
        )
    return hours


def fetch(lat: float, lon: float, tz: ZoneInfo, now: datetime | None = None) -> Forecast:
    """Fetch from local midnight today (for today's high/low) to ~36 h ahead."""
    now = now or datetime.now(UTC)
    local_midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    url = build_url(lat, lon, local_midnight, now + timedelta(hours=36))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        body = upstream.read(req, TIMEOUT, DEADLINE)
    except urllib.error.HTTPError as e:
        # FMI puts the reason in an XML exception report in the body; a proxy
        # in front of it may answer with anything.
        try:
            parse(upstream.read_error(e, DEADLINE))
        except FMIError as fe:
            raise FMIError(f"HTTP {e.code}: {fe}") from e
        except (OSError, http.client.HTTPException):
            pass
        raise FMIError(f"HTTP {e.code}") from e
    except (OSError, http.client.HTTPException) as e:  # HTTPException: e.g. a body cut short
        raise FMIError(str(e) or type(e).__name__) from e

    hours = parse(body)
    if not hours:
        raise FMIError("forecast contained no data")
    return Forecast(fetched_at=now, hours=hours)


# --- cache -----------------------------------------------------------------

def save_cache(forecast: Forecast, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "fetched_at": forecast.fetched_at.isoformat(),
        "parameters": list(forecast.parameters),
        "hours": [{**asdict(h), "time": h.time.isoformat()} for h in forecast.hours],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def _finite(v):
    """A cached number as it is, unless it is NaN or infinite (JSON allows both)."""
    return None if isinstance(v, float) and not math.isfinite(v) else v


def load_cache(path: Path) -> Forecast | None:
    try:
        data = json.loads(path.read_text())
        return Forecast(
            fetched_at=datetime.fromisoformat(data["fetched_at"]),
            parameters=tuple(data.get("parameters", ())),
            hours=[
                Hour(**{k: _finite(v) for k, v in h.items()} | {"time": datetime.fromisoformat(h["time"])})
                for h in data["hours"]
            ],
        )
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError) as e:
        log.warning("ignoring unreadable weather cache %s: %s", path, e)
        return None


# --- the latest good forecast ----------------------------------------------

WORDS = {
    "en": {"weather_old": "Weather {age} old", "weather_unavailable": "Weather unavailable"},
    "fi": {"weather_old": "Sää {age} vanha", "weather_unavailable": "Säätietoja ei saatavilla"},
}


class Weather(Periodic[Forecast]):
    """Holds the latest good forecast; refreshes at most every N minutes."""

    WHAT = "weather"
    ERRORS = (FMIError,)

    def __init__(self, cfg: Config):
        self.cfg = cfg
        # Only fetched, and only warned about, while a widget on a screen asks for it.
        self.wanted = bool(cfg.wanted(ForecastRequest))
        settings = cfg.source_settings.weather
        self.stale_after = timedelta(minutes=settings.stale_after_minutes)
        super().__init__(timedelta(minutes=settings.refresh_minutes), cfg.cache_dir / "forecast.json")

    def warning(self, now: datetime, language: str) -> str | None:
        """For the status bar: that the forecast is missing or old, else None."""
        forecast = self.forecast
        if forecast is None:
            return i18n.t(WORDS, language, "weather_unavailable")
        if now - forecast.fetched_at > self.stale_after:
            minutes = (now - forecast.fetched_at).total_seconds() / 60
            return i18n.t(WORDS, language, "weather_old", age=i18n.age(minutes))
        return None

    @property
    def forecast(self) -> Forecast | None:
        return self.value

    @forecast.setter
    def forecast(self, forecast: Forecast | None) -> None:
        self.value = forecast

    def _fetch(self, now: datetime) -> Forecast:
        return fetch(self.cfg.lat, self.cfg.lon, self.cfg.tz, now)

    def _load(self, path: Path) -> Forecast | None:
        return load_cache(path)

    def _save(self, forecast: Forecast, path: Path) -> None:
        save_cache(forecast, path)

    def _fetched_at(self, forecast: Forecast) -> datetime:
        return forecast.fetched_at

    def _current(self, forecast: Forecast) -> bool:
        # A cache from before a parameter was added is refetched at once.
        return forecast.parameters == PARAMETERS

    def _summary(self, forecast: Forecast) -> str:
        return f"{len(forecast.hours)} hours"
