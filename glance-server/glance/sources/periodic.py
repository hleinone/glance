"""The latest good answer from a source fetched every so many minutes.

A failed or odd fetch keeps the last good answer, which is also cached on disk
so a restart shows it at once, and is retried after RETRY_AFTER instead of the
full interval. A source subclasses Periodic and says how to fetch, cache and
describe its answer; departures, polled just before each wake, are not one.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, Generic, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class Periodic(Generic[T]):
    RETRY_AFTER = timedelta(minutes=2)  # after a failed fetch, instead of the full interval
    WHAT: ClassVar[str]  # the source in log lines: "weather"
    ERRORS: ClassVar[tuple[type[Exception], ...]]  # the source's own failures, logged without a traceback

    def __init__(self, interval: timedelta, cache_path: Path):
        self.interval = interval
        self.cache_path = cache_path
        self.value: T | None = self._load(cache_path)
        # time.monotonic(), not the clock: the Pi has no real-time clock, and
        # a clock set backwards would otherwise hold off fetches until it
        # caught up.
        self.last_attempt: float | None = None
        self.last_failed = False
        self.lock = threading.Lock()

    @property
    def fetched_at(self) -> datetime | None:
        value = self.value
        return self._fetched_at(value) if value is not None else None

    def refresh(self, force: bool = False) -> None:
        now, tick = datetime.now(UTC), time.monotonic()
        with self.lock:
            value = self.value
            # Sooner after a failure, or after an answer the source wouldn't fetch the same way now.
            behind = self.last_failed or (value is not None and not self._current(value))
            wait = min(self.interval, self.RETRY_AFTER) if behind else self.interval
            if not force and self.last_attempt is not None and tick - self.last_attempt < wait.total_seconds():
                return
            # A cached answer "from the future" (the clock went back) is not fresh.
            if (not force and value is not None and self._current(value)
                    and timedelta(0) <= now - self._fetched_at(value) < self.interval):
                return
            self.last_attempt = tick
            self.last_failed = True  # until the fetch below succeeds
        # Never raises: this runs in the refresher thread, which must survive
        # whatever a fetch does (it once died for good on an error page).
        try:
            new = self._fetch(now)
        except self.ERRORS as e:
            age = f"{now - self._fetched_at(value)} old" if value is not None else "none cached"
            log.warning("%s fetch failed (%s); using the last good answer (%s)", self.WHAT, e, age)
            return
        except Exception:
            log.exception("%s fetch failed unexpectedly; using the last good answer", self.WHAT)
            return
        with self.lock:
            self.value = new
            self.last_failed = False
        try:
            self._save(new, self.cache_path)
        except OSError as e:
            log.warning("could not save the %s cache: %s", self.WHAT, e)
        log.info("%s updated: %s", self.WHAT, self._summary(new))

    # What a source says about itself.

    def _fetch(self, now: datetime) -> T:
        raise NotImplementedError

    def _load(self, path: Path) -> T | None:
        raise NotImplementedError

    def _save(self, value: T, path: Path) -> None:
        raise NotImplementedError

    def _fetched_at(self, value: T) -> datetime:
        raise NotImplementedError

    def _current(self, value: T) -> bool:
        """False for a cached answer the source would fetch differently now."""
        return True

    def _summary(self, value: T) -> str:
        raise NotImplementedError
