"""Data sources, one module per source: its client, its cache and its latest
good answer. Widgets never fetch; they draw what the scene holds.

A new source is a module here and a field in Sources (and in `periodic`, if
it is refreshed on an interval), plus a field in the Scene it fills. Each
source has `warning(now, language)`: what the status bar should say about its
data being old or missing, or None. A source with settings of its own reads
and checks its config section itself (`parse_settings`), and has a field in
SourceSettings.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from typing import TYPE_CHECKING

from . import fmi, hsl
from .fmi import Weather
from .hsl import Transit
from .periodic import Periodic

if TYPE_CHECKING:
    from ..config import Config


@dataclass(frozen=True)
class SourceSettings:
    """Each source's own section of the config files, read and checked by the
    source. The config holds it; the source made from that config reads its
    field. The names are the fields of Sources."""

    weather: fmi.Settings
    transit: hsl.Settings

    @classmethod
    def parse(cls, raw: dict, environ: Mapping[str, str]) -> SourceSettings:
        return cls(fmi.parse_settings(raw.get("fmi", {})), hsl.parse_settings(raw.get("digitransit", {}), environ))


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
        """The sources refreshed on their own intervals, of those a widget asks for."""
        return tuple(source for source in (self.weather,) if source.wanted)

    def warnings(self, now: datetime, language: str) -> tuple[str, ...]:
        """What the sources a widget asks for say is old or missing, in the order of the fields."""
        wanted = (source for f in fields(self) if (source := getattr(self, f.name)).wanted)
        found = (source.warning(now, language) for source in wanted)
        return tuple(w for w in found if w)

    def data_times(self) -> tuple[datetime | None, ...]:
        """When each source's data was fetched: a change means a new drawing."""
        return (self.transit.fetched_at, *(source.fetched_at for source in self.periodic))
