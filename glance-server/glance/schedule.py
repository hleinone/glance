"""When the device should wake next, and what moment an image is drawn for.

The device wakes on a schedule from config.toml: rules like "Mon-Fri
07:00-09:00 every minute", a default interval otherwise. The same rules pick
the deck of screens shown (glance/decks.py). Wakes land on the
schedule's minutes (multiples of the interval since midnight), aimed a couple
of seconds after the minute changes so the clock on screen is right. The
device's sleep timer drifts a little, so a request that arrives a few seconds
before a wake minute is treated as belonging to it.

All stepping and subtracting of times goes through UTC (`_step`,
`seconds_between`). Python does arithmetic on two times of the same time zone
on the wall clock, which is an hour off across a DST change, and adding to a
time forgets which pass of the repeated autumn hour it was in: that once made
the "next wake" an hour in the past, and the device was told to sleep 1 s.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# A request this close before a wake minute is drawn for that minute. The
# device's sleep timer runs ~1 % fast (measured: 3 s early after 5 min), so
# the margin grows with the sleep before the wake: 3 % of the interval, at
# least 10 s (10 s for 1- and 5-minute periods, 27 s after a 15-minute sleep).
# Not more: a request that much before the minute (a button press, say) would
# be drawn a minute ahead.
EARLY_MIN = 10.0
EARLY_FRACTION = 0.03
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class WakeRule:
    days: frozenset[int]  # 0 = Monday, of the moment itself (after midnight counts as the next day)
    start: int  # minutes since midnight
    end: int  # exclusive; end <= start means the rule runs past midnight
    every: int  # minutes between wakes
    deck: str | None = None  # None = the schedule's default deck

    def covers(self, t: datetime) -> bool:
        if t.weekday() not in self.days:
            return False
        m = t.hour * 60 + t.minute
        if self.start < self.end:
            return self.start <= m < self.end
        return m >= self.start or m < self.end


@dataclass(frozen=True)
class WakeSchedule:
    rules: tuple[WakeRule, ...] = ()  # the first matching rule wins
    default_every: int = 1
    default_deck: str = ""

    def rule_at(self, t: datetime) -> WakeRule | None:
        for rule in self.rules:
            if rule.covers(t):
                return rule
        return None

    def interval_at(self, t: datetime) -> int:
        rule = self.rule_at(t)
        return rule.every if rule else self.default_every

    def deck_at(self, t: datetime) -> str:
        rule = self.rule_at(t)
        return rule.deck if rule and rule.deck else self.default_deck

    def is_wake(self, t: datetime) -> bool:
        """Whether the minute t falls in is one the device wakes for."""
        return (t.hour * 60 + t.minute) % self.interval_at(t) == 0

    def next_wake(self, now: datetime) -> datetime:
        """The first wake minute strictly after now."""
        t = _next_minute(now)
        for _ in range(2 * 24 * 60):
            if self.is_wake(t):
                return t
            t = _step(t, minutes=1)
        return t  # unreachable with sane rules (every <= 1440)


def _step(t: datetime, **delta: float) -> datetime:
    """t + timedelta(**delta) in real time, in t's time zone."""
    return (t.astimezone(UTC) + timedelta(**delta)).astimezone(t.tzinfo)


def _next_minute(now: datetime) -> datetime:
    """The start of the minute after the one `now` is in."""
    return _step(now.astimezone(UTC).replace(second=0, microsecond=0).astimezone(now.tzinfo), minutes=1)


def seconds_between(a: datetime, b: datetime) -> float:
    """b - a in real seconds (via UTC, so DST changes count)."""
    return (b.astimezone(UTC) - a.astimezone(UTC)).total_seconds()


_seconds = seconds_between


@dataclass(frozen=True)
class Plan:
    display_time: datetime  # the moment the image is drawn for
    sleep_seconds: int  # until the next wake
    interval_minutes: int  # the schedule's interval at display_time


def plan(now: datetime, schedule: WakeSchedule, offset_seconds: float) -> Plan:
    """Plan for a request arriving at `now` (local, aware).

    The device should wake `offset_seconds` after a wake minute: enough for
    the minute to have changed by the time the request arrives, and little
    enough that the clock is right for nearly the whole minute.
    """
    wake = schedule.next_wake(now)
    display_time = now
    slept = schedule.interval_at(_step(wake, minutes=-1)) * 60  # the sleep that ends at this wake
    if _seconds(now, wake) <= max(EARLY_MIN, EARLY_FRACTION * slept):
        # Woke early: draw the image for the coming wake and aim for the next.
        display_time = wake
        wake = schedule.next_wake(wake)
    sleep = _seconds(now, wake) + offset_seconds
    return Plan(
        display_time=display_time,
        sleep_seconds=max(1, round(sleep)),
        interval_minutes=schedule.interval_at(display_time),
    )


def next_boundary(now: datetime, interval_minutes: int) -> datetime:
    """The next minute strictly after now whose time of day is a multiple of
    interval_minutes since local midnight."""
    t = _next_minute(now)
    while (t.hour * 60 + t.minute) % interval_minutes:
        t = _step(t, minutes=1)
    return t


class Prerendered:
    """Images drawn in advance for one upcoming minute: the screen showing and
    its neighbours in the deck.

    Drawing takes ~2.5 s on a Pi 1, too long to keep the device's radio on for,
    so the server draws each wake minute's image just before the minute starts.
    It is served to requests near the start of that minute (when the device
    wakes); later requests get a fresh image with exact countdowns. The
    neighbours are for a button press that changes the screen: anywhere in the
    same minute, a quick answer matters more than countdowns a few seconds old.
    """

    MAX_LATE = timedelta(seconds=20)

    def __init__(self) -> None:
        # One tuple, swapped in a single assignment, so readers in other
        # threads never pair one minute with another minute's image.
        self._entry: tuple[datetime, object, dict[str, dict[str, bytes]]] | None = None

    def put(self, minute: datetime, screen: str, images: dict[str, bytes], data: object = None) -> None:
        """images: encoded image per URL path ("/glance.png", "/glance.g2").
        Screens put for the same minute are kept together.

        data: what the images were drawn from (the departures' fetch time).
        A `get` naming other data finds nothing: when a slow fetch lands
        after the drawing, the request draws afresh instead of showing the
        device the previous poll's departures for a whole interval."""
        entry = self._entry
        screens = dict(entry[2]) if entry and entry[:2] == (minute, data) else {}
        screens[screen] = images
        self._entry = (minute, data, screens)

    def get(self, display_time: datetime, screen: str, path: str,
            max_late: timedelta = MAX_LATE, data: object = None) -> bytes | None:
        entry = self._entry
        minute = display_time.replace(second=0, microsecond=0)
        if entry is None or entry[0] != minute or display_time - minute > max_late:
            return None
        if data is not None and entry[1] != data:
            return None
        return entry[2].get(screen, {}).get(path)


class Recent:
    """The last image drawn on demand, reused for a few seconds.

    Requests outside the pre-drawn window (the browser preview, a button
    press, a late device) are drawn when they arrive, which keeps a Pi 1 busy
    for ~2.5 s each. Anyone on the network can send them, so they are drawn
    one at a time, and requests for the same data within MAX_AGE seconds share
    one drawing: a burst costs one drawing per MAX_AGE, not one per request.
    Countdowns are then at most that many seconds old.
    """

    MAX_AGE = 5.0  # seconds

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: object = None
        self._made_at = 0.0
        self._image: object = None
        self._encoded: dict[str, bytes] = {}

    def get(self, key: object, path: str, draw: Callable[[], object], encode: Callable[[object], bytes]) -> bytes:
        """The image for `key` encoded for `path`, drawing it only if needed.

        key: what the image depends on besides the passing seconds (the
        minute, the data's fetch times); a new key is always drawn afresh.
        """
        with self._lock:
            if self._key != key or time.monotonic() - self._made_at >= self.MAX_AGE:
                image = draw()
                self._key, self._image, self._encoded = key, image, {}
                self._made_at = time.monotonic()
            if path not in self._encoded:
                self._encoded[path] = encode(self._image)
            return self._encoded[path]


def next_poll(now: datetime, lead_seconds: float) -> datetime:
    """The next poll time, lead_seconds before a minute boundary (strictly after now)."""
    t = _step(next_boundary(now, 1), seconds=-lead_seconds)
    return t if seconds_between(now, t) > 0 else _step(t, minutes=1)
