"""spot_prices: Finland's electricity spot price, with VAT: the price now, big,
and a bar per quarter hour for the next 24 hours, the cheapest coming hour in
black with its price above it. Data from Nord Pool's day-ahead market via
Elering."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from PIL import Image, ImageDraw

from .. import i18n
from ..draw import BLACK, DARK, LIGHT, WHITE, font, fit_text, text_vcenter
from ..layout import Rect
from ..scene import Scene
from ..sources.elering import STEP, Prices, PricesRequest

TYPE = "spot_prices"
WORDS = {
    "en": {
        "title": "Spot price", "unit": "c/kWh", "tomorrow": "Tomorrow's prices around 14",
        "unavailable": "Spot price unavailable",
    },
    "fi": {
        "title": "Pörssisähkö", "unit": "snt/kWh", "tomorrow": "Huomisen hinnat n. klo 14",
        "unavailable": "Pörssisähkön hintaa ei saatavilla",
    },
}
SETTINGS: set[str] = set()  # besides `type`

VAT = 0.255  # Finland's general rate, on electricity too
VIEW = timedelta(hours=24)
# The bars' scale reaches at least this (c/kWh with VAT), so a calm day's small
# differences don't look like swings.
SCALE_FLOOR = 10.0
LEFT_W = 150  # the price now, beside the bars
GRID_LABEL_W = 26  # the lines' values, either side of the bars


@dataclass(frozen=True)
class SpotPrices:
    TYPE: ClassVar[str] = TYPE
    wants: ClassVar[PricesRequest] = PricesRequest()
    MIN_SIZE: ClassVar[tuple[int, int]] = (530, 120)  # 96 bars of 3 px beside the price and the lines' values


def parse(raw: dict) -> SpotPrices:
    return SpotPrices()


def cents(eur_per_mwh: float) -> float:
    """What a kWh costs at this market price, with VAT, in cents."""
    return eur_per_mwh / 10 * (1 + VAT)


def bars(prices: Prices, now: datetime) -> list[tuple[datetime, float | None]]:
    """(start, c/kWh with VAT) for each quarter hour of the next 24 hours, from
    the one `now` falls in; None where the price isn't known yet."""
    first = _floor(now, STEP)
    starts = (first + i * STEP for i in range(round(VIEW / STEP)))
    return [(start, cents(p.eur_per_mwh) if (p := prices.at(start)) else None) for start in starts]


def cheapest_hour(prices: Prices, now: datetime) -> tuple[datetime, float] | None:
    """The clock hour, starting no earlier than the step `now` falls in and
    inside the 24 hours shown, whose average price is lowest: (start, c/kWh)."""
    first = _floor(now, STEP)
    hour = _floor(first + timedelta(hours=1) - STEP, timedelta(hours=1))
    found = []
    while hour + timedelta(hours=1) <= first + VIEW:
        steps = [prices.at(hour + j * STEP) for j in range(4)]
        if all(steps):
            found.append((hour, sum(cents(p.eur_per_mwh) for p in steps) / 4))
        hour += timedelta(hours=1)
    return min(found, key=lambda f: f[1], default=None)


def _floor(t: datetime, step: timedelta) -> datetime:
    """`t` rounded down to a whole step since midnight UTC (Finland's offset is whole hours)."""
    t = t.astimezone(UTC)
    midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + ((t - midnight) // step) * step


def draw(img: Image.Image, d: ImageDraw.ImageDraw, sc: Scene, rect: Rect, name: str, w: SpotPrices) -> None:
    x0, y0, x1, y1 = rect
    pad = 12
    prices = sc.spot_prices
    now_price = prices.at(sc.now) if prices else None
    if prices is None or now_price is None:
        f, text = fit_text(i18n.t(WORDS, sc.language, "unavailable"), "medium", 16, 12, x1 - x0 - 2 * pad)
        text_vcenter(d, x0 + pad, (y0 + y1) / 2, text, f, DARK)
        return
    lang = sc.language

    # The price now, beside the bars.
    text_vcenter(d, x0 + pad, y0 + pad + 8, i18n.t(WORDS, lang, "title"), font("medium", 15), DARK)
    f, value = fit_text(i18n.decimal(lang, cents(now_price.eur_per_mwh)), "display", 56, 30, LEFT_W - 8)
    cy = (y0 + y1) / 2 + 4
    text_vcenter(d, x0 + pad - 2, cy, value, f, BLACK)
    text_vcenter(d, x0 + pad, cy + 38, i18n.t(WORDS, lang, "unit"), font("medium", 15), DARK)

    cx0, cx1 = x0 + pad + LEFT_W + 12 + GRID_LABEL_W, x1 - pad - GRID_LABEL_W
    _chart(d, sc, prices, cheapest_hour(prices, sc.now), (cx0, y0 + pad + 8, cx1, y1 - pad - 18))


def grid_step(span: float) -> float:
    """A round step (1, 2 or 5 times a power of ten) giving at most four lines over `span`."""
    step = 10 ** math.floor(math.log10(span / 4))
    return next(step * m for m in (1, 2, 5, 10) if span / (step * m) <= 4)


def _chart(d: ImageDraw.ImageDraw, sc: Scene, prices: Prices, cheapest: tuple[datetime, float] | None,
           box: Rect) -> None:
    """Light grey bars over light grey lines, the cheapest hour black with its price above it."""
    x0, y0, x1, y1 = box
    shown = bars(prices, sc.now)
    known = [c for _, c in shown if c is not None]
    step = grid_step(max(SCALE_FLOOR, *known) - min(0.0, *known))
    # The scale ends on lines, so the top and bottom lines are labelled too.
    low, high = math.floor(min(0.0, *known) / step) * step, math.ceil(max(SCALE_FLOOR, *known) / step) * step
    slot = (x1 - x0) / len(shown)
    gap = 1 if slot >= 3 else 0

    def y(c: float) -> float:
        return y1 - (c - low) / (high - low) * (y1 - y0)

    small = font("medium", 12)
    places = 0 if step >= 1 else 1
    for k in range(round(low / step), round(high / step) + 1):
        value = k * step
        line_y = round(y(value))
        d.line((x0, line_y, x1 - 1, line_y), fill=DARK if value == 0 else LIGHT)
        label = i18n.decimal(sc.language, value, places)
        text_vcenter(d, x0 - 5, line_y, label, small, DARK, "r")
        text_vcenter(d, x1 + 5, line_y, label, small, DARK)

    zero = y(0)
    in_cheapest = set()
    if cheapest:
        in_cheapest = {i for i, (start, _) in enumerate(shown) if cheapest[0] <= start < cheapest[0] + timedelta(hours=1)}
    for i, (start, c) in enumerate(shown):
        left, right = round(x0 + i * slot), round(x0 + (i + 1) * slot) - gap
        local = start.astimezone(sc.now.tzinfo)
        if local.minute == 0 and local.hour % 3 == 0:
            text_vcenter(d, left, y1 + 10, f"{local.hour:02d}", font("medium", 13), DARK, "m")
        if c is not None:
            top, bottom = sorted((y(c), zero))
            d.rectangle((left, round(top), max(left, right - 1), max(round(top), round(bottom) - 1)),
                        fill=BLACK if i in in_cheapest else LIGHT)
    if in_cheapest:
        first, last = min(in_cheapest), max(in_cheapest)
        # Clear of the bars it sits over: the hour's and a few either side.
        near = [c for _, c in shown[max(0, first - 3):last + 4] if c is not None]
        label_y = max(min(y(max(near)), zero) - 12, y0 - 4)
        text_vcenter(d, x0 + (first + last + 1) / 2 * slot, label_y, i18n.decimal(sc.language, cheapest[1]),
                     font("semibold", 13), BLACK, "m")

    unknown = [i for i, (_, c) in enumerate(shown) if c is None]
    if unknown:
        left, right = round(x0 + unknown[0] * slot), round(x0 + (unknown[-1] + 1) * slot) - 1
        d.rectangle((left, y0, right, y1), fill=WHITE, outline=LIGHT, width=2)
        f, text = fit_text(i18n.t(WORDS, sc.language, "tomorrow"), "medium", 14, 11, right - left - 12)
        if right - left > 60:
            text_vcenter(d, (left + right) / 2, (y0 + y1) / 2, text, f, DARK, "m")
