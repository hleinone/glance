"""Load config.toml, with optional overrides from config.local.toml."""

from __future__ import annotations

import ipaddress
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from . import layout as layout_
from . import widgets as widgets_
from .checks import within
from .decks import Deck
from .sources import SourceSettings
from .widgets import HslStops, Widget
from .schedule import DAY_NAMES, WakeRule, WakeSchedule

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    language: str
    tz: ZoneInfo
    lat: float
    lon: float
    host: str
    port: int
    source_settings: SourceSettings
    cache_dir: Path
    widgets: dict[str, Widget]
    screens: dict[str, layout_.Layout]
    decks: dict[str, Deck]
    # Extra names the server may be called by (Host header), besides IP
    # addresses, single-label names and .local ones; see host_allowed().
    hostnames: tuple[str, ...] = ()
    # Networks besides private ones that may reach the server; see client_allowed().
    allowed_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    wake_schedule: WakeSchedule = WakeSchedule()
    wake_offset_seconds: float = 2
    battery_full_mv: int = 4200  # what this device reads for a full battery

    @property
    def stops(self) -> dict[str, HslStops]:
        """The stop widgets on any screen, by name, in screen and layout order."""
        return {n: w for lay in self.screens.values() for n in lay.names if isinstance(w := self.widgets[n], HslStops)}

    @property
    def start_screen(self) -> str:
        """The default deck's starting screen (`render` without a screen, for example)."""
        return self.decks[self.wake_schedule.default_deck].start

    def deck_at(self, t) -> Deck:
        return self.decks[self.wake_schedule.deck_at(t)]


# The screen setup: these come from config.local.toml together, if it sets any
# of them, so config.toml's examples can't leak into it (an example widget's
# settings into a local one of the same name, an example rule's deck into a
# local schedule).
SETUP = ("widgets", "screens", "decks", "schedule")


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: Path | None = None, local_overrides: bool = True) -> Config:
    """local_overrides=False leaves out config.local.toml and the environment:
    the tests use it, so they don't depend on this machine's settings."""
    path = path or ROOT / "config.toml"
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    local = path.with_name("config.local.toml")
    if local_overrides and local.exists():
        with open(local, "rb") as f:
            local_raw = tomllib.load(f)
        if any(key in local_raw for key in SETUP):
            raw = {key: value for key, value in raw.items() if key not in SETUP}
        raw = _merge(raw, local_raw)

    language = raw.get("language", "en")
    if language not in ("en", "fi"):
        raise ValueError(f"language must be 'en' or 'fi', not {language!r}")

    device = raw.get("device", {})
    for old, new, used in (
        ("[stops]", "[widgets.<name>] with type = \"hsl_stops\"", "stops" in raw),
        ("[weather]", "[fmi]", "weather" in raw),
        ("[layout]", "[screens.<name>]", "layout" in raw),
        ("[[device.schedule]]", "[[schedule.rules]]", "schedule" in device),
        ("[device] wake_interval_minutes", "[schedule] wake_interval_minutes", "wake_interval_minutes" in device),
    ):
        if used:
            raise ValueError(f"{old} is no longer used: see config.toml ({new})")
    location = raw["location"]
    server = raw.get("server", {})
    hostnames = server.get("hostnames", [])
    if isinstance(hostnames, str):
        hostnames = [hostnames]
    networks = server.get("allowed_networks", [])
    if isinstance(networks, str):
        networks = [networks]
    try:
        networks = tuple(ipaddress.ip_network(str(n), strict=False) for n in networks)
    except ValueError as e:
        raise ValueError(f"[server] allowed_networks: {e} (use networks like \"100.64.0.0/10\")") from None
    cache_dir = Path(raw.get("cache", {}).get("dir", "cache"))
    if not cache_dir.is_absolute():
        cache_dir = path.parent / cache_dir

    return Config(
        language=language,
        tz=ZoneInfo(raw.get("timezone", "Europe/Helsinki")),
        lat=within(location["lat"], -90, 90, "[location] lat", float),
        lon=within(location["lon"], -180, 180, "[location] lon", float),
        host=server.get("host", "0.0.0.0"),
        port=within(server.get("port", 8080), 1, 65535, "[server] port"),
        source_settings=SourceSettings.parse(raw, os.environ if local_overrides else {}),
        cache_dir=cache_dir,
        **_setup(raw),
        hostnames=tuple(str(h).rstrip(".").lower() for h in hostnames),
        allowed_networks=networks,
        wake_offset_seconds=within(device.get("wake_offset_seconds", 2), 0, 59, "wake_offset_seconds", float),
        # Within what the device can report (battery.PLAUSIBLE_MV), and a LiPo can be full at.
        battery_full_mv=within(raw.get("battery", {}).get("full_mv", 4200), 3500, 5000, "[battery] full_mv"),
    )


def _setup(raw: dict) -> dict:
    """Widgets, screens, decks and the schedule, checked against each other."""
    widgets = _widgets(raw)
    screens = _screens(raw, widgets)
    decks = _decks(raw, screens)
    return {"widgets": widgets, "screens": screens, "decks": decks,
            "wake_schedule": _schedule(raw.get("schedule", {}), decks)}


def _widgets(raw: dict) -> dict[str, Widget]:
    widgets = {}
    for name, w in raw.get("widgets", {}).items():
        if not isinstance(w, dict):
            raise ValueError(f"widget {name!r}: expected a [widgets.{name}] section")
        kind = w.get("type")
        if kind not in widgets_.TYPES:
            raise ValueError(f"widget {name!r}: type must be one of {', '.join(widgets_.TYPES)}, not {kind!r}")
        module = widgets_.TYPES[kind]
        unknown = set(w) - module.SETTINGS - {"type"}
        if unknown:
            raise ValueError(f"widget {name!r} ({kind}): unknown setting(s) {', '.join(sorted(unknown))}")
        widgets[name] = module.parse(w)
    return widgets


def _screens(raw: dict, widgets: dict[str, Widget]) -> dict[str, layout_.Layout]:
    sections = raw.get("screens", {})
    if not sections:
        raise ValueError("no screens: add a [screens.<name>] section with a grid (see config.toml)")
    screens = {}
    for name, s in sections.items():
        if not isinstance(s, dict) or not s.get("grid"):
            raise ValueError(f"screen {name!r}: [screens.{name}] needs a grid")
        _no_unknown(s, {"grid"}, f"screen {name!r}")
        try:
            lay = layout_.parse(s["grid"])
            for widget in lay.names:
                if widget not in widgets:
                    raise ValueError(f"the grid names {widget!r}, but there is no [widgets.{widget}]")
            for area in lay.areas:
                _check_size(area, lay, widgets[area.name])
        except ValueError as e:
            raise ValueError(f"screen {name!r}: {e}") from None
        screens[name] = lay
    return screens


def _decks(raw: dict, screens: dict[str, layout_.Layout]) -> dict[str, Deck]:
    sections = raw.get("decks", {})
    if not sections:  # one deck of every screen, in order
        return {"all": Deck("all", tuple(screens), next(iter(screens)))}
    decks = {}
    for name, d in sections.items():
        if not isinstance(d, dict):
            raise ValueError(f"deck {name!r}: expected a [decks.{name}] section")
        _no_unknown(d, {"screens", "start"}, f"deck {name!r}")
        names = d.get("screens") or []
        if isinstance(names, str):
            names = [names]
        if not names:
            raise ValueError(f"deck {name!r}: screens = [...] is missing")
        for screen in names:
            if screen not in screens:
                raise ValueError(f"deck {name!r}: there is no [screens.{screen}]")
        if len(set(names)) != len(names):
            raise ValueError(f"deck {name!r}: a screen is listed twice")
        start = d.get("start", names[0])
        if start not in names:
            raise ValueError(f"deck {name!r}: start = {start!r} is not one of its screens")
        decks[name] = Deck(name, tuple(names), start)
    return decks


def _no_unknown(section: dict, allowed: set[str], what: str) -> None:
    unknown = set(section) - allowed
    if unknown:
        raise ValueError(f"{what}: unknown setting(s) {', '.join(sorted(unknown))}")


def _check_size(area: layout_.Area, lay: layout_.Layout, widget: Widget) -> None:
    x0, y0, x1, y1 = lay.rect(area)
    min_w, min_h = widget.MIN_SIZE
    if x1 - x0 >= min_w and y1 - y0 >= min_h:
        return
    hints = []
    if x1 - x0 < min_w:
        hints.append(_cells_needed(min_w, area.col, lay.cols, lay.x, "column"))
    if y1 - y0 < min_h:
        hints.append(_cells_needed(min_h, area.row, lay.rows, lay.y, "row"))
    raise ValueError(
        f"widget {area.name!r} ({widget.TYPE}) needs at least {min_w}×{min_h} px; in this "
        f"{lay.cols}×{lay.rows} grid it gets {x1 - x0}×{y1 - y0}: give it {' and '.join(hints)}"
    )


def _cells_needed(need: int, start: int, count: int, edge, unit: str) -> str:
    """How many cells (from the area's first one) give `need` pixels."""
    for n in range(1, count + 1):
        if edge(min(start, count - n) + n) - edge(min(start, count - n)) >= need:
            return f"{n} {unit}s"
    return f"more {unit}s than the grid has (use fewer, bigger {unit}s)"


_DAY_GROUPS = {"weekdays": DAY_NAMES[:5], "weekend": DAY_NAMES[5:], "all": DAY_NAMES}


def _minutes(text: str) -> int:
    try:
        hours, minutes = (int(part) for part in str(text).split(":"))
    except ValueError:
        raise ValueError(f"bad time {text!r}: use HH:MM") from None
    value = hours * 60 + minutes
    if not (0 <= minutes <= 59 and 0 <= value <= 24 * 60):
        raise ValueError(f"bad time {text!r}: use HH:MM")
    return value % (24 * 60)


def _schedule(raw: dict, decks: dict[str, Deck]) -> WakeSchedule:
    _no_unknown(raw, {"default_deck", "wake_interval_minutes", "rules"}, "[schedule]")
    default_every = _interval(raw.get("wake_interval_minutes", 1), "wake_interval_minutes")
    default_deck = raw.get("default_deck", next(iter(decks)))
    if default_deck not in decks:
        raise ValueError(f"[schedule] default_deck: there is no [decks.{default_deck}]")
    rules = []
    for i, rule in enumerate(raw.get("rules", []), 1):
        what = f"schedule rule {i}"
        if not isinstance(rule, dict):
            raise ValueError(f"{what}: expected a [[schedule.rules]] section")
        _no_unknown(rule, {"days", "from", "to", "every", "deck"}, what)
        for key in ("from", "to"):
            if key not in rule:
                raise ValueError(f"{what}: {key} = \"HH:MM\" is missing")
        names = _days(rule.get("days", "all"), what)
        deck = rule.get("deck")
        if deck is not None and deck not in decks:
            raise ValueError(f"{what}: there is no [decks.{deck}]")
        rules.append(WakeRule(
            days=frozenset(DAY_NAMES.index(d) for d in names),
            start=_minutes(rule["from"]),
            end=_minutes(rule["to"]),
            # Without `every`, the default interval; without `deck`, the default deck.
            every=_interval(rule.get("every", default_every), f"{what} 'every'"),
            deck=deck,
        ))
    return WakeSchedule(tuple(rules), default_every, default_deck)


def _days(days, what: str) -> tuple[str, ...]:
    """A rule's `days`: "weekdays", "weekend", "all", one day or a list of days
    ("mon", "Friday"), as three-letter names."""
    if isinstance(days, str):
        if days.lower() in _DAY_GROUPS:
            return _DAY_GROUPS[days.lower()]
        days = [days]  # one day
    names = tuple(str(d).lower()[:3] for d in days) if isinstance(days, list) else ()
    if not names or any(n not in DAY_NAMES for n in names):
        raise ValueError(f"{what}: days must be \"weekdays\", \"weekend\", \"all\" or days like "
                         f"[\"mon\", \"fri\"], not {days!r}")
    return names


def _interval(value, name: str) -> int:
    every = int(value)
    if not 1 <= every <= 24 * 60:
        raise ValueError(f"{name} must be 1..1440 minutes, not {every}")
    return every
