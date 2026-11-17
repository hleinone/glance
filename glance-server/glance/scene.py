"""Everything a screen is drawn from: the time, the language and the data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .sources import elering, fmi
from .sources.hsl import StopDepartures, stale


@dataclass(frozen=True)
class Scene:
    now: datetime  # local, aware
    language: str
    forecast: fmi.Forecast | None
    stops: dict[str, StopDepartures]  # by stop widget name
    # Live departures: False while all stops show placeholders.
    departures_live: bool = False
    departures_fetched_at: datetime | None = None
    departures_stale_after: timedelta = timedelta(minutes=5)
    battery_percent: int | None = None  # from the device's last report, if recent
    # Departures as clock times (14.32) instead of minutes: when the device
    # updates only every few minutes, countdowns would go stale.
    departure_clock: bool = False
    # What the sources say is old or missing, for the status bar.
    warnings: tuple[str, ...] = ()
    spot_prices: elering.Prices | None = None

    @property
    def departures_stale(self) -> bool:
        return stale(self.departures_live, self.departures_fetched_at, self.now, self.departures_stale_after)
