"""Render the 800×480 dashboard, snapped to the panel's four grey levels: the
status bar, then each grid area's widget (glance/widgets/)."""

from __future__ import annotations

import io
import logging

from PIL import Image, ImageDraw

from . import battery, i18n, widgets
from .config import Config
from .decks import Deck
from .draw import BLACK, WHITE, font, quantise, text_vcenter
from .layout import BAR, H, W
from .scene import Scene

__all__ = ["Scene", "render", "to_png", "to_g2", "FORMATS"]

log = logging.getLogger(__name__)

WORDS = {"en": {"battery_low": "Charge battery"}, "fi": {"battery_low": "Lataa akku"}}


def _status_warning(sc: Scene) -> str | None:
    """Right-hand status bar warning, or None while all data is fresh.

    No "Updated HH:MM" when all is well: data is fetched just before the
    minute, so it would always read one minute behind the clock.
    """
    warnings = list(sc.warnings)  # what the sources say is old or missing
    if sc.battery_percent is not None and sc.battery_percent <= battery.LOW_PERCENT:
        # The level itself is shown next to the badge; the badge says what to do.
        warnings.append(i18n.t(WORDS, sc.language, "battery_low"))
    return " · ".join(warnings) or None


def draw_status_bar(img: Image.Image, d: ImageDraw.ImageDraw, sc: Scene,
                    page: tuple[int, int] | None = None) -> None:
    """page: (index, count) of the screen in its deck, for the page dots."""
    d.rectangle((0, 0, W, BAR - 1), fill=BLACK)
    lang = sc.language
    left = f"{i18n.date(lang, sc.now)}   {i18n.clock(lang, sc.now)}"
    left_font = font("semibold", 17)
    text_vcenter(d, 10, BAR / 2, left, left_font, WHITE)
    right = W - 10
    if sc.battery_percent is not None:
        right = draw_battery(d, right, sc)
    warning = _status_warning(sc)
    if warning:  # inverted so a problem stands out from the black bar
        f = font("semibold", 15)
        tw = f.getlength(warning)
        d.rounded_rectangle((right - tw - 16, 4, right + 4, BAR - 5), radius=4, fill=WHITE)
        text_vcenter(d, right - 4, BAR / 2, warning, f, BLACK, align="r")
        right -= tw + 16
    if page and page[1] > 1:
        draw_page_dots(d, page, 10 + left_font.getlength(left), right)


# Page dots: which screen of the deck this is, centred in the status bar.
DOT, DOT_GAP = 8, 6  # diameter and gap (px)
MAX_DOTS = 8  # more screens than this: "3/12" instead
PAGE_MARGIN = 16  # clear space to the date on the left and the battery or warning on the right


def draw_page_dots(d: ImageDraw.ImageDraw, page: tuple[int, int], left: float, right: float) -> None:
    """The showing screen filled, the others as rings; between `left` and
    `right` (what's drawn either side), or not at all if they'd touch: a
    warning matters more."""
    index, count = page
    if count > MAX_DOTS:
        f = font("semibold", 15)
        text = f"{index + 1}/{count}"
        width = f.getlength(text)
    else:
        width = count * DOT + (count - 1) * DOT_GAP
    x0 = round(W / 2 - width / 2)
    if x0 - PAGE_MARGIN < left or x0 + width + PAGE_MARGIN > right:
        return
    if count > MAX_DOTS:
        text_vcenter(d, W / 2, BAR / 2, text, f, WHITE, align="m")
        return
    top = BAR // 2 - DOT // 2
    for i in range(count):
        x = x0 + i * (DOT + DOT_GAP)
        box = (x, top, x + DOT - 1, top + DOT - 1)
        if i == index:
            d.ellipse(box, fill=WHITE)
        else:
            d.ellipse(box, outline=WHITE, width=2)


def draw_battery(d: ImageDraw.ImageDraw, right: float, sc: Scene) -> float:
    """Battery icon + percentage, right-aligned at `right`; returns the new right edge."""
    f = font("medium", 15)
    text = i18n.percent(sc.language, sc.battery_percent)
    text_vcenter(d, right, BAR / 2, text, f, WHITE, align="r")
    x1 = right - f.getlength(text) - 6
    # Body 22x11 with a nub on the right, filled in proportion to the charge.
    bw, bh = 22, 11
    top = BAR / 2 - bh / 2
    x0 = x1 - 3 - bw
    d.rectangle((x0 + bw + 1, top + 3, x0 + bw + 2, top + bh - 3), fill=WHITE)  # nub
    d.rectangle((x0, top, x0 + bw, top + bh), outline=WHITE, width=1)
    fill_w = round((bw - 4) * max(0, min(100, sc.battery_percent)) / 100)
    if fill_w > 0:
        d.rectangle((x0 + 2, top + 2, x0 + 2 + fill_w - 1, top + bh - 2), fill=WHITE)
    return x0 - 14


def render(cfg: Config, sc: Scene, screen: str | None = None, deck: Deck | None = None,
           forgiving: bool = False) -> Image.Image:
    """One screen (by default the default deck's starting screen). With its
    deck, the status bar shows page dots (on a deck of two or more).

    forgiving: a widget that fails to draw leaves its area empty (and a line
    in the log) instead of failing the whole image. The server draws this
    way: data that got past the fetches' checks but can't be drawn then costs
    one widget, not the screen (the device would show its offline mark). Tests
    and `render` on the command line don't, so a mistake shows.
    """
    screen = screen or cfg.start_screen
    lay = cfg.screens[screen]
    img = Image.new("L", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    page = (deck.screens.index(screen), len(deck.screens)) if deck and screen in deck.screens else None
    draw_status_bar(img, d, sc, page)
    for area in lay.areas:
        w = cfg.widgets[area.name]
        rect = lay.rect(area)
        try:
            widgets.module(w).draw(img, d, sc, rect, area.name, w)
        except Exception:
            if not forgiving:
                raise
            log.exception("widget %r could not be drawn; leaving its area empty", area.name)
            d.rectangle((rect[0], rect[1], rect[2] - 1, rect[3] - 1), fill=WHITE)  # whatever it had drawn so far
    return quantise(img)


def to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    # Level 6, not optimize=True: on a Pi 1 that is 0.16 s instead of 1.1 s,
    # for a file only ~6 % bigger.
    img.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


def to_g2(img: Image.Image) -> bytes:
    """The panel's own format: 2 bits per pixel, 4 pixels per byte, first pixel
    in the top bits, rows top to bottom; 0 = black .. 3 = white (= the
    firmware's TFT_GRAY_0..3). 96,000 bytes for 800x480; the device copies it
    straight into its frame buffer instead of decoding a PNG (2.2 s on the ESP32).
    """
    index = img.point([min(3, (v + 42) // 85) for v in range(256)])
    # Pillow 11 (the Pi's) has no 2-bit packer for mode L, but has one for P.
    return Image.frombytes("P", img.size, index.tobytes()).tobytes("raw", "P;2")


# Image formats served, by URL path: encoder and Content-Type.
FORMATS = {
    "/glance.png": (to_png, "image/png"),
    "/glance.g2": (to_g2, "application/octet-stream"),
}
