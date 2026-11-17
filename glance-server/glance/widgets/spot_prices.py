"""spot_prices: Finland's electricity spot price, with VAT: the price now, big,
and bars for the next 24 hours, with the cheapest coming hour named. Data from
Nord Pool's day-ahead market via Elering."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from PIL import Image, ImageDraw

from .. import i18n
from ..draw import BLACK, DARK, LIGHT, font, fit_text, text_vcenter
from ..layout import Rect
from ..scene import Scene
from ..sources.elering import STEP, Prices, PricesRequest

TYPE = "spot_prices"
WORDS = {
    "en": {
        "title": "Spot price", "unit": "c/kWh", "cents": "{price} c",
        "cheapest": "Cheapest hour {hours}: {price}", "tomorrow": "Tomorrow's prices around 14",
        "unavailable": "Spot price unavailable",
    },
    "fi": {
        "title": "Pörssisähkö", "unit": "snt/kWh", "cents": "{price} snt",
        "cheapest": "Halvin tunti {hours}: {price}", "tomorrow": "Huomisen hinnat n. klo 14",
        "unavailable": "Pörssisähkön hintaa ei saatavilla",
    },
}
SETTINGS: set[str] = set()  # besides `type`

VAT = 0.255  # Finland's general rate, on electricity too
VIEW = timedelta(hours=24)
BAR = timedelta(minutes=15)  # one bar per market step; timedelta(hours=1) draws hourly averages
# The bars' scale reaches at least this (c/kWh with VAT), so a calm day's small
# differences don't look like swings.
SCALE_FLOOR = 10.0
LEFT_W = 150  # the price now, beside the bars


@dataclass(frozen=True)
class SpotPrices:
    TYPE: ClassVar[str] = TYPE
    wants: ClassVar[PricesRequest] = PricesRequest()
    MIN_SIZE: ClassVar[tuple[int, int]] = (480, 120)  # 96 bars of 3 px beside the price


def parse(raw: dict) -> SpotPrices:
    return SpotPrices()


def cents(eur_per_mwh: float) -> float:
    """What a kWh costs at this market price, with VAT, in cents."""
    return eur_per_mwh / 10 * (1 + VAT)


def bars(prices: Prices, now: datetime, bar: timedelta) -> list[tuple[datetime, float | None]]:
    """(start, c/kWh with VAT) for each bar of the next 24 hours, from the one
    `now` falls in; None where the price isn't known yet."""
    first = _floor(now, bar)
    out = []
    for i in range(round(VIEW / bar)):
        start = first + i * bar
        steps = [prices.at(start + j * STEP) for j in range(max(1, round(bar / STEP)))]
        known = [cents(p.eur_per_mwh) for p in steps if p is not None]
        out.append((start, sum(known) / len(known) if len(known) == len(steps) else None))
    return out


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

    cx0, cx1 = x0 + pad + LEFT_W + 12, x1 - pad
    cheapest = cheapest_hour(prices, sc.now)
    if cheapest:
        start, price = cheapest
        local = start.astimezone(sc.now.tzinfo)
        hours = f"{local.hour:02d}–{(local + timedelta(hours=1)).hour:02d}"
        line = i18n.t(WORDS, lang, "cheapest", hours=hours,
                      price=i18n.t(WORDS, lang, "cents", price=i18n.decimal(lang, price)))
        f, line = fit_text(line, "medium", 15, 12, cx1 - cx0 - 50)
        text_vcenter(d, cx0, y0 + pad + 8, line, f, BLACK)
    _chart(img, d, sc, prices, (cx0, y0 + pad + 24, cx1, y1 - pad - 18))


def _chart(img: Image.Image, d: ImageDraw.ImageDraw, sc: Scene, prices: Prices, box: Rect) -> None:
    x0, y0, x1, y1 = box
    shown = bars(prices, sc.now, BAR)
    known = [c for _, c in shown if c is not None]
    low, high = min(0.0, *known), max(SCALE_FLOOR, *known)
    slot = (x1 - x0) / len(shown)
    gap = 1 if slot >= 3 else 0

    def y(c: float) -> float:
        return y1 - (c - low) / (high - low) * (y1 - y0)

    zero = y(0)
    small = font("medium", 13)
    # The highest price shown, at the top right: the scale the bars are read against.
    text_vcenter(d, x1, y0 - 16, i18n.decimal(sc.language, max(known)), small, DARK, "r")
    for i, (start, c) in enumerate(shown):
        left, right = round(x0 + i * slot), round(x0 + (i + 1) * slot) - gap
        local = start.astimezone(sc.now.tzinfo)
        if local.hour == 0 and local.minute == 0 and i:
            d.line((left - 1, y0, left - 1, y1), fill=LIGHT)  # midnight
        if local.minute == 0 and local.hour % 3 == 0:
            text_vcenter(d, left, y1 + 10, f"{local.hour:02d}", small, DARK, "m")
        if c is not None:
            top, bottom = sorted((y(c), zero))
            d.rectangle((left, round(top), max(left, right - 1), max(round(top), round(bottom) - 1)),
                        fill=BLACK if i == 0 else DARK)
    d.line((x0, round(zero), x1, round(zero)), fill=DARK)

    unknown = [i for i, (_, c) in enumerate(shown) if c is None]
    if unknown:
        left, right = round(x0 + unknown[0] * slot), round(x0 + (unknown[-1] + 1) * slot) - 1
        d.rectangle((left, y0, right, y1), outline=LIGHT, width=2)
        f, text = fit_text(i18n.t(WORDS, sc.language, "tomorrow"), "medium", 14, 11, right - left - 12)
        if right - left > 60:
            text_vcenter(d, (left + right) / 2, (y0 + y1) / 2, text, f, DARK, "m")
