"""The weather source: FMI's open data forecast (WFS, "simple" feature format,
no API key), for [location] or for a place a widget names, its cache, and the
latest good forecast for each place the widgets ask for (Weather, one per
place, held together by Forecasts).

Docs: https://en.ilmatieteenlaitos.fi/open-data-manual
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import math
import urllib.error
import urllib.parse
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field, fields
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
    """What a weather widget asks this source for: the forecast for a town,
    which FMI looks up by its name, or for a point, or with neither, for
    [location]; and what the widget calls the place, if anything."""
    town: str | None = None
    lat: float | None = None
    lon: float | None = None
    # The widget's heading. Widgets showing one place share its forecast,
    # whatever they call it.
    name: str | None = field(default=None, compare=False)

    @property
    def home(self) -> bool:
        return self.town is None and self.lat is None

    def label(self) -> str | None:
        """How the status bar and the log name the place: its heading, its
        town or its coordinates; None for [location]."""
        if self.home:
            return None
        return self.name or self.town or f"{self.lat:.2f}, {self.lon:.2f}"


HOME = ForecastRequest()
TOWN_LONGEST = 60
NAME_LONGEST = 40
# Each place is fetched every refresh_minutes: eight every 15 minutes is about
# 770 requests a day, well inside FMI's fair use.
MAX_PLACES = 8


def parse_request(raw: dict) -> ForecastRequest:
    """A weather widget's place, from its settings: `place`, a town's name
    ("Mikkeli"), or `lat` and `lon`, or neither for [location]; and `name`, its
    heading, if any."""
    name = _name(raw["name"], "name", NAME_LONGEST) if "name" in raw else None
    point = [key for key in ("lat", "lon") if key in raw]
    if "place" in raw and point:
        raise ValueError("give the place by its name (place) or by its coordinates (lat and lon), not both")
    if "place" in raw:
        return ForecastRequest(town=_name(raw["place"], "place", TOWN_LONGEST), name=name)
    if len(point) == 1:
        raise ValueError(f"{point[0]} needs {'lon' if point[0] == 'lat' else 'lat'} too")
    if point:
        return ForecastRequest(lat=within(raw["lat"], -90, 90, "lat", float),
                               lon=within(raw["lon"], -180, 180, "lon", float), name=name)
    return ForecastRequest(name=name)


def _name(value, setting: str, longest: int) -> str:
    """A name from the config, trimmed: text of 1 to `longest` characters, with
    nothing that could break a log line."""
    if not isinstance(value, str):
        raise ValueError(f"{setting} must be text, like \"Mikkeli\", not {value!r}")
    text = value.strip()
    if not 1 <= len(text) <= longest:
        raise ValueError(f"{setting} must be 1 to {longest} characters, not {len(text)}")
    if any(unicodedata.category(c).startswith("C") for c in text):
        raise ValueError(f"{setting} {value!r} has a control character")
    return text


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
    "gml": "http://www.opengis.net/gml/3.2",
}
# The start of FMI's answer for a place name it doesn't know: "No locations
# found for the place with the requested language!"
NOT_FOUND = "No locations found"

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
    # Where FMI says the forecast is for (latitude, longitude): for a town, the
    # place it chose among those of that name. Not cached.
    position: tuple[float, float] | None = None

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


class PlaceNotFound(FMIError):
    """FMI knows no place of the name asked for."""


def latlon(lat: float, lon: float) -> dict[str, str]:
    """Where a forecast is for, as FMI's parameter: a point."""
    return {"latlon": f"{lat:.4f},{lon:.4f}"}


def build_url(where: dict[str, str], start: datetime, end: datetime) -> str:
    """`where`: latlon(), or {"place": a town's name}."""
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "getFeature",
        "storedquery_id": STORED_QUERY,
        **where,
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
        message = "; ".join(texts[:1]) or "FMI returned an exception report"
        raise (PlaceNotFound if message.startswith(NOT_FOUND) else FMIError)(message)

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


def parse_position(xml: bytes) -> tuple[float, float] | None:
    """Where an answer that parse() read says its forecast is for, if it says."""
    pos = ET.fromstring(xml).findtext(".//gml:pos", namespaces=NS)
    try:
        lat, lon = (float(v) for v in (pos or "").split())
    except ValueError:
        return None
    return lat, lon


def fetch(where: dict[str, str], tz: ZoneInfo, now: datetime | None = None) -> Forecast:
    """Fetch from local midnight today (for today's high/low) to ~36 h ahead.
    `where` as for build_url()."""
    now = now or datetime.now(UTC)
    local_midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    url = build_url(where, local_midnight, now + timedelta(hours=36))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        body = upstream.read(req, TIMEOUT, DEADLINE)
    except urllib.error.HTTPError as e:
        # FMI puts the reason in an XML exception report in the body; a proxy
        # in front of it may answer with anything.
        try:
            parse(upstream.read_error(e, DEADLINE))
        except FMIError as fe:  # PlaceNotFound stays one
            raise type(fe)(f"HTTP {e.code}: {fe}") from e
        except (OSError, http.client.HTTPException):
            pass
        raise FMIError(f"HTTP {e.code}") from e
    except (OSError, http.client.HTTPException) as e:  # HTTPException: e.g. a body cut short
        raise FMIError(str(e) or type(e).__name__) from e

    hours = parse(body)
    if not hours:
        raise FMIError("forecast contained no data")
    return Forecast(fetched_at=now, hours=hours, position=parse_position(body))


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
    "en": {
        "weather_old": "Weather {age} old",
        "weather_unavailable": "Weather unavailable",
        "weather_old_at": "Weather for {place} {age} old",
        "weather_unavailable_at": "Weather for {place} unavailable",
    },
    "fi": {
        "weather_old": "Sää {age} vanha",
        "weather_unavailable": "Säätietoja ei saatavilla",
        "weather_old_at": "Sää ({place}) {age} vanha",
        "weather_unavailable_at": "Säätietoja ei saatavilla ({place})",
    },
}


def cache_name(request: ForecastRequest) -> str:
    """[location]'s forecast keeps the file it always had."""
    if request == HOME:
        return "forecast.json"
    key = json.dumps([getattr(request, f.name) for f in fields(request) if f.compare])
    return f"forecast-{hashlib.sha256(key.encode()).hexdigest()[:16]}.json"


class Weather(Periodic[Forecast]):
    """Holds the latest good forecast for one place; refreshes at most every N minutes."""

    ERRORS = (FMIError,)

    def __init__(self, cfg: Config, request: ForecastRequest = HOME):
        self.cfg = cfg
        self.request = request
        # The log names the place as FMI was asked for it, not by its heading.
        if request.home:
            self.WHAT = "weather"
        else:
            self.WHAT = f"weather for {request.town or f'{request.lat:.4f}, {request.lon:.4f}'}"
        # Only fetched, and only warned about, while a widget on a screen asks for it.
        self.wanted = request in cfg.wanted(ForecastRequest).values()
        # FMI's last answer was that it knows no place of this name.
        self.not_found = False
        settings = cfg.source_settings.weather
        self.stale_after = timedelta(minutes=settings.stale_after_minutes)
        super().__init__(timedelta(minutes=settings.refresh_minutes), cfg.cache_dir / cache_name(request))

    def warning(self, now: datetime, language: str) -> str | None:
        """For the status bar: that the forecast is missing or old, else None."""
        forecast = self.forecast
        place = self.request.label()
        at = "" if self.request.home else "_at"
        if forecast is None:
            return i18n.t(WORDS, language, "weather_unavailable" + at, place=place)
        if now - forecast.fetched_at > self.stale_after:
            minutes = (now - forecast.fetched_at).total_seconds() / 60
            return i18n.t(WORDS, language, "weather_old" + at, age=i18n.age(minutes), place=place)
        return None

    @property
    def forecast(self) -> Forecast | None:
        return self.value

    @forecast.setter
    def forecast(self, forecast: Forecast | None) -> None:
        self.value = forecast

    def _where(self) -> dict[str, str]:
        if self.request.town is not None:
            return {"place": self.request.town}
        if self.request.lat is not None:
            return latlon(self.request.lat, self.request.lon)
        return latlon(self.cfg.lat, self.cfg.lon)

    def _fetch(self, now: datetime) -> Forecast:
        try:
            forecast = fetch(self._where(), self.cfg.tz, now)
        except PlaceNotFound:
            self.not_found = self.request.town is not None  # only a name can be unknown
            raise
        self.not_found = False
        return forecast

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
        hours = f"{len(forecast.hours)} hours"
        if self.request.town is None or forecast.position is None:
            return hours
        # For a town: where FMI took it to be, since it picks one place among
        # those of the same name.
        return f"{hours} at {forecast.position[0]:.4f}, {forecast.position[1]:.4f}"


class Forecasts:
    """The weather for every place the widgets on the screens ask for, each
    refreshed, cached and retried on its own, so one place's trouble never
    costs another its forecast."""

    def __init__(self, cfg: Config):
        requests = dict.fromkeys(cfg.wanted(ForecastRequest).values())
        if len(requests) > MAX_PLACES:
            raise ValueError(
                f"the weather widgets on the screens ask for {len(requests)} places; "
                f"at most {MAX_PLACES} can be shown"
            )
        self.places = {request: Weather(cfg, request) for request in requests}

    def forecasts(self) -> dict[ForecastRequest, Forecast]:
        """The latest good forecast of each place that has one."""
        return {request: f for request, weather in self.places.items() if (f := weather.forecast) is not None}

    def not_found(self) -> frozenset[ForecastRequest]:
        """The places FMI last said it doesn't know."""
        return frozenset(request for request, weather in self.places.items() if weather.not_found)
