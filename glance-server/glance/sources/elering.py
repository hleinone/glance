"""The spot price source: Nord Pool's day-ahead electricity prices for Finland,
from Elering's open dashboard API (no key), and what a price is.

Docs: https://dashboard.elering.ee/v3/api-docs (/api/nps/price)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# The market's step since October 2025; a price's end is the next one's start.
STEP = timedelta(minutes=15)


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
