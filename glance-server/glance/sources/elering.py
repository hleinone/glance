"""The spot price source: Nord Pool's day-ahead electricity prices for Finland,
from Elering's open dashboard API (no key), what a price is, and the latest
good prices (Electricity).

Docs: https://dashboard.elering.ee/v3/api-docs (/api/nps/price)
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .. import i18n, upstream
from .periodic import Periodic

if TYPE_CHECKING:
    from ..config import Config

log = logging.getLogger(__name__)

PRICE_URL = "https://dashboard.elering.ee/api/nps/price"
AREA = "fi"  # the answer has every Baltic area and Finland
USER_AGENT = "glance-dashboard/0.1"
# Seconds; prices are fetched in the background, nothing waits on them. TIMEOUT
# is for each step (connecting, each read), DEADLINE for the whole answer.
TIMEOUT = 10
DEADLINE = 20
# Today's prices are known since yesterday afternoon and tomorrow's arrive
# around 14 Finnish time, so an hourly fetch shows them within the hour.
REFRESH = timedelta(hours=1)
# The market's step since October 2025: each price covers this long from its start.
STEP = timedelta(minutes=15)
# Prices outside this band (€/MWh) are left out: the market's limits are
# −500 and 4000, so anything beyond is not a price.
BAND = (-1000.0, 10000.0)
# Prices dated outside these years are left out, like fmi.YEARS.
YEARS = range(2000, 2100)


@dataclass(frozen=True)
class Price:
    start: datetime  # UTC
    eur_per_mwh: float  # the market price, without tax


@dataclass(frozen=True)
class Prices:
    fetched_at: datetime  # UTC
    prices: tuple[Price, ...]  # by start, one per step

    def at(self, t: datetime) -> Price | None:
        """The price for the step `t` falls in, if known."""
        return next((p for p in self.prices if p.start <= t < p.start + STEP), None)


@dataclass(frozen=True)
class PricesRequest:
    """What a spot price widget asks this source for: Finland's prices."""


class EleringError(Exception):
    pass


def build_url(start: datetime, end: datetime) -> str:
    """Only dates: nothing about the household goes with the request."""
    return f"{PRICE_URL}?{urllib.parse.urlencode({'start': _iso(start), 'end': _iso(end)})}"


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse(body: bytes) -> tuple[Price, ...]:
    """Finland's prices from an answer, by start. Anything but the expected
    JSON raises EleringError; single prices that can't be right are left out."""
    try:
        answer = json.loads(body)
    except (ValueError, RecursionError) as e:  # RecursionError: brackets nested too deep
        raise EleringError(f"unreadable answer: {e}") from e
    if not isinstance(answer, dict) or answer.get("success") is not True:
        raise EleringError("the answer says it failed")
    rows = answer.get("data", {}).get(AREA) if isinstance(answer.get("data"), dict) else None
    if not isinstance(rows, list):
        raise EleringError(f"no prices for {AREA!r} in the answer")
    by_start = {}
    for row in rows:
        price = _price(row)
        if price is not None:
            by_start[price.start] = price
    return tuple(by_start[t] for t in sorted(by_start))


def _price(row) -> Price | None:
    if not isinstance(row, dict):
        return None
    stamp = row.get("timestamp")
    if not _number(stamp) or not _finite(stamp):
        return None
    try:
        start = datetime.fromtimestamp(stamp, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return _checked(start, row.get("price"))


def _finite(v: int | float) -> bool:
    """False for NaN, infinity, and an integer too big to be a float (JSON allows any length)."""
    try:
        return math.isfinite(v)
    except OverflowError:
        return False


def _number(v) -> bool:
    # bool is a number to Python, and neither true nor false is a time or a price.
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _checked(start: datetime, value) -> Price | None:
    """A price that can be right, from the answer or the cache, else None."""
    if not _number(value) or not _finite(value) or not BAND[0] <= value <= BAND[1]:
        return None
    if start.tzinfo is None or start.year not in YEARS:
        return None
    return Price(start.astimezone(UTC), float(value))


def fetch(tz: ZoneInfo, now: datetime) -> Prices:
    """Today's and, once published, tomorrow's prices, from local midnight."""
    midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (midnight + timedelta(days=2)).astimezone(UTC) - timedelta(milliseconds=1)
    req = urllib.request.Request(build_url(midnight, end), headers={"User-Agent": USER_AGENT})
    try:
        body = upstream.read(req, TIMEOUT, DEADLINE)
    except urllib.error.HTTPError as e:
        raise EleringError(f"HTTP {e.code}") from e
    except (OSError, http.client.HTTPException) as e:  # HTTPException: e.g. a body cut short
        raise EleringError(str(e) or type(e).__name__) from e
    # Only what was asked for: the widget looks each price up, so a longer answer costs every drawing.
    prices = tuple(p for p in parse(body) if midnight <= p.start <= end)
    if not prices:
        raise EleringError(f"no prices for {AREA!r} in the answer")
    return Prices(now, prices)


# --- cache -----------------------------------------------------------------

def save_cache(prices: Prices, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "fetched_at": prices.fetched_at.isoformat(),
        "prices": [[p.start.isoformat(), p.eur_per_mwh] for p in prices.prices],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def load_cache(path: Path) -> Prices | None:
    """Checked as the answer is: the file could be from an older version, or damaged."""
    try:
        data = json.loads(path.read_text())
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        if fetched_at.tzinfo is None:
            raise ValueError("fetched_at has no time zone")
        by_start = {}
        for start, value in data["prices"]:
            price = _checked(datetime.fromisoformat(start), value)
            if price is None:
                raise ValueError(f"a price that can't be right: {start} {value!r}")
            by_start[price.start] = price
        return Prices(fetched_at, tuple(by_start[t] for t in sorted(by_start)))
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError) as e:
        log.warning("ignoring unreadable spot price cache %s: %s", path, e)
        return None


# --- the latest good prices ------------------------------------------------

WORDS = {
    "en": {"unavailable": "Spot price unavailable"},
    "fi": {"unavailable": "Pörssisähkön hintaa ei saatavilla"},
}


class Electricity(Periodic[Prices]):
    """Holds the latest good spot prices; refreshes hourly, and sooner (at start,
    or after Periodic.RETRY_AFTER) when they don't cover the time now."""

    WHAT = "spot prices"
    ERRORS = (EleringError,)

    def __init__(self, cfg: Config):
        self.cfg = cfg
        # Only fetched, and only warned about, while a widget on a screen asks for them.
        self.wanted = bool(cfg.wanted(PricesRequest))
        super().__init__(REFRESH, cfg.cache_dir / "spot_prices.json")

    @property
    def prices(self) -> Prices | None:
        return self.value

    @prices.setter
    def prices(self, prices: Prices | None) -> None:
        self.value = prices

    def warning(self, now: datetime, language: str) -> str | None:
        """For the status bar: that there is no price for now, else None.
        Prices don't change once published, so old ones are as good as new."""
        prices = self.prices
        if prices is None or prices.at(now) is None:
            return i18n.t(WORDS, language, "unavailable")
        return None

    def _fetch(self, now: datetime) -> Prices:
        return fetch(self.cfg.tz, now)

    def _load(self, path: Path) -> Prices | None:
        return load_cache(path)

    def _save(self, prices: Prices, path: Path) -> None:
        save_cache(prices, path)

    def _fetched_at(self, prices: Prices) -> datetime:
        return prices.fetched_at

    def _current(self, prices: Prices) -> bool:
        return prices.at(datetime.now(UTC)) is not None

    def _summary(self, prices: Prices) -> str:
        return f"{len(prices.prices)} prices, to {prices.prices[-1].start + STEP:%Y-%m-%d %H:%M} UTC"
