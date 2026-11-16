"""Data sources, one module per source: its client, its cache and its latest
good answer. Widgets never fetch; they draw what the scene holds.

A new source is a module here and a field in Sources (and in `periodic`, if
it is refreshed on an interval), plus a field in the Scene it fills.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from .fmi import Weather
from .hsl import Transit
from .periodic import Periodic

if TYPE_CHECKING:
    from ..config import Config


@dataclass(frozen=True)
class Sources:
    """Every source, made once per process."""

    weather: Weather
    transit: Transit

    @classmethod
    def make(cls, cfg: Config) -> Sources:
        return cls(Weather(cfg), Transit(cfg))

    @property
    def periodic(self) -> tuple[Periodic, ...]:
        """The sources refreshed on their own intervals."""
        return (self.weather,)

    def data_times(self) -> tuple[datetime | None, ...]:
        """When each source's data was fetched: a change means a new drawing."""
        return (self.transit.fetched_at, *(source.fetched_at for source in self.periodic))
