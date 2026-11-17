"""The departures source: HSL's departures via the Digitransit routing API
(GraphQL, OTP2 GTFS schema), what a departure is (and placeholder ones), and
the latest departures for all stop widgets (Transit).

Departures carry absolute times, so a cached list stays correct as time
passes: minutes are worked out at render time.

Docs: https://digitransit.fi/en/developers/apis/1-routing-api/
Needs a free subscription key, sent as the `digitransit-subscription-key` header.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from .. import i18n, upstream
from ..checks import within

if TYPE_CHECKING:
    from ..config import Config

log = logging.getLogger(__name__)

KEY_VARIABLE = "GLANCE_DIGITRANSIT_KEY"


@dataclass(frozen=True)
class Settings:
    """[digitransit] in the config files, and the key from the environment."""
    key: str | None = None
    poll_lead_seconds: int = 15
    min_refresh_seconds: int = 10
    stale_after_minutes: int = 5


def parse_settings(raw: dict, environ: Mapping[str, str]) -> Settings:
    """environ: where the key variable is looked for first; empty for a config
    loaded without local overrides, as the tests load it."""
    return Settings(
        key=environ.get(KEY_VARIABLE) or raw.get("key") or None,
        # Under a minute: the poll belongs to the minute it runs before.
        poll_lead_seconds=within(raw.get("poll_lead_seconds", 15), 0, 59, "poll_lead_seconds"),
        min_refresh_seconds=within(raw.get("min_refresh_seconds", 10), 0, 3600, "min_refresh_seconds"),
        stale_after_minutes=within(raw.get("stale_after_minutes", 5), 1, 7 * 24 * 60, "[digitransit] stale_after_minutes"),
    )

ENDPOINT = "https://api.digitransit.fi/routing/v2/hsl/gtfs/v1"
GEOCODING_ENDPOINT = "https://api.digitransit.fi/geocoding/v1/search"
# HSL's GTFS route type for trunk bus routes (runkolinjat: 20, 30, 200, 500, 550, ...),
# drawn orange by HSL. 701 = regular city bus, 700 = regional bus, both blue.
TRUNK_ROUTE_TYPE = 702
USER_AGENT = "glance-dashboard/0.1"
# Seconds. Departures are polled 15 s before the minute and must be back, and
# the image drawn, before the device wakes just after it. TIMEOUT is for each
# step (connecting, each read), DEADLINE for the whole answer.
TIMEOUT = 8
DEADLINE = 10
# Text from the API is drawn as it comes; fitting it shrinks and cuts one
# character at a time, so absurdly long text is cut here first.
MAX_TEXT = 80
# Departures dated outside these years are left out, like fmi.YEARS.
YEARS = range(2000, 2100)

DEPARTURES_QUERY = """
query Departures($ids: [String], $n: Int, $lang: String) {
  stops(ids: $ids) {
    gtfsId
    name(language: $lang)
    vehicleMode
    stoptimesWithoutPatterns(numberOfDepartures: $n, omitNonPickups: true, omitCanceled: true) {
      serviceDay
      scheduledDeparture
      realtimeDeparture
      realtime
      headsign(language: $lang)
      trip {
        tripHeadsign(language: $lang)
        route { shortName mode type }
      }
    }
  }
}
"""

SEARCH_QUERY = """
query Search($name: String) {
  stops(name: $name) {
    gtfsId
    code
    name
    desc
    platformCode
    vehicleMode
    parentStation { name }
    patterns { headsign route { shortName } }
  }
}
"""


# --- what a departure is ----------------------------------------------------

# Order of the mode icons when a widget serves several modes.
MODE_ORDER = ("subway", "rail", "tram", "bus", "ferry")


def ordered_modes(modes) -> tuple[str, ...]:
    unique = set(modes)
    return tuple(sorted(unique, key=lambda m: MODE_ORDER.index(m) if m in MODE_ORDER else len(MODE_ORDER)))


@dataclass(frozen=True)
class Departure:
    route: str
    headsign: str
    time: datetime  # timezone-aware
    realtime: bool
    mode: str  # "tram", "bus", ... (anything but tram is drawn like a bus)
    trunk: bool = False  # HSL trunk route (runkolinja), orange at HSL; light grey badge here

    def minutes(self, now: datetime) -> int:
        return max(0, int((self.time - now).total_seconds() // 60))


@dataclass(frozen=True)
class StopDepartures:
    name: str
    departures: list[Departure]
    modes: tuple[str, ...] = ()  # vehicle modes served, in MODE_ORDER, for the header icons

    def upcoming(self, now: datetime, count: int) -> list[Departure]:
        # Keep a departure until its minute has passed, like the HSL displays do.
        return [d for d in self.departures if d.time > now - timedelta(seconds=30)][:count]


_PLACEHOLDERS: list[list[tuple[str, str, int, bool, str]]] = [
    [
        ("7", "Senaatintori", 1, True, "tram"),
        ("9", "Länsisatama", 3, True, "tram"),
        ("7", "Pasila", 8, True, "tram"),
        ("9", "Jätkäsaari", 11, True, "tram"),
        ("7", "Senaatintori", 15, False, "tram"),
        ("9", "Länsisatama", 19, False, "tram"),
        ("7", "Pasila", 24, False, "tram"),
        ("9", "Jätkäsaari", 27, False, "tram"),
    ],
    [
        ("59", "Pasilan asema", 0, True, "bus"),
        ("506", "Meilahti", 4, True, "bus"),
        ("23", "Ruskeasuo", 6, True, "bus"),
        ("59", "Itäkeskus (M)", 12, True, "bus"),
        ("506", "Viikki", 17, False, "bus"),
        ("23", "Central Railway Station", 21, False, "bus"),
        ("550", "Westendinasema", 26, False, "bus"),
        ("59", "Pasilan asema", 32, False, "bus"),
    ],
    [
        ("10", "Kirurgi", 2, True, "tram"),
        ("550", "Itäkeskus (M)", 5, True, "bus"),
        ("10", "Pikku Huopalahti", 9, True, "tram"),
        ("69", "Kalasatama (M)", 13, False, "bus"),
        ("550", "Westendinasema", 18, False, "bus"),
        ("10", "Kirurgi", 22, False, "tram"),
        ("69", "Kontula (M)", 29, False, "bus"),
        ("10", "Pikku Huopalahti", 64, False, "tram"),
    ],
]


_PLACEHOLDER_TRUNK = {"550"}  # a real trunk route, so placeholders show the style


def placeholder(name: str, index: int, now: datetime) -> StopDepartures:
    base = now + timedelta(seconds=30)
    rows = _PLACEHOLDERS[index % len(_PLACEHOLDERS)]
    return StopDepartures(
        name=name,
        departures=[
            Departure(route, headsign, base + timedelta(minutes=m), realtime, mode, route in _PLACEHOLDER_TRUNK)
            for route, headsign, m, realtime, mode in rows
        ],
        modes=ordered_modes(row[4] for row in rows),
    )


def stale(live: bool, fetched_at: datetime | None, now: datetime, stale_after: timedelta) -> bool:
    """Live departures not fetched yet, or fetched too long ago. Placeholder
    departures (no key, or no stops) are never stale."""
    return live and (fetched_at is None or now - fetched_at > stale_after)


WORDS = {
    "en": {"departures_old": "Departures {age} old", "departures_unavailable": "Departures unavailable"},
    "fi": {"departures_old": "Lähdöt {age} vanhoja", "departures_unavailable": "Lähtötietoja ei saatavilla"},
}


# --- the Digitransit client -------------------------------------------------

class HSLError(Exception):
    pass


def _request(req: urllib.request.Request) -> dict:
    try:
        payload = json.loads(upstream.read(req, TIMEOUT, DEADLINE))
    except urllib.error.HTTPError as e:
        hint = " (check the Digitransit key)" if e.code in (401, 403) else ""
        raise HSLError(f"HTTP {e.code}{hint}") from e
    except (OSError, ValueError, http.client.HTTPException) as e:  # HTTPException: e.g. a body cut short
        raise HSLError(str(e) or type(e).__name__) from e
    if not isinstance(payload, dict):
        raise HSLError("unexpected response (not a JSON object)")
    return payload


def _post(query: str, variables: dict, key: str) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "digitransit-subscription-key": key,
        },
    )
    payload = _request(req)
    if payload.get("errors"):
        raise HSLError(_unexpected(lambda: "; ".join(str(err.get("message", "?")) for err in payload["errors"][:2])))
    return payload.get("data") or {}


def _unexpected(read):
    """Run `read` over a response; a response of the wrong shape (a missing
    field, a string where a number belongs) is an HSLError like any other
    failed fetch, not a crash."""
    try:
        return read()
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError, OSError) as e:
        raise HSLError(f"unexpected response: {type(e).__name__}: {e}") from e


def _text(value) -> str:
    """A text field of an answer, cut to MAX_TEXT. Anything but text (a
    number, a list) is a response of the wrong shape: drawing it would fail."""
    if not isinstance(value, str):
        raise TypeError(f"expected text, not {type(value).__name__}")
    return value[:MAX_TEXT]


def is_gtfs_id(ref: str) -> bool:
    """'HSL:1174411' is a gtfsId; 'H0665' is a stop code as printed on the stop sign."""
    return ":" in ref


def stop_id_from_geocoding(data: dict, code: str) -> str | None:
    """Pick the HSL stop whose code matches exactly from a geocoding response."""
    for feature in data.get("features") or []:
        props = feature.get("properties") or {}
        gtfs = (props.get("addendum") or {}).get("GTFS") or {}
        if props.get("source") != "gtfshsl" or (gtfs.get("code") or "").upper() != code.upper():
            continue
        # id looks like "GTFS:HSL:1174411#H0665"
        ref = (props.get("id") or "").split("#", 1)[0].removeprefix("GTFS:")
        if is_gtfs_id(ref):
            return ref
    return None


def resolve_code(code: str, key: str) -> str | None:
    """Stop code (e.g. "H0665") -> gtfsId (e.g. "HSL:1174411"), via the geocoding API.

    The routing API's stop search matches names only, not codes.
    """
    url = GEOCODING_ENDPOINT + "?" + urllib.parse.urlencode({"text": code, "layers": "stop", "size": 10})
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "digitransit-subscription-key": key}
    )
    data = _request(req)
    return _unexpected(lambda: stop_id_from_geocoding(data, code))


@dataclass(frozen=True)
class StopQuery:
    """One stop widget: one or more stop ids (e.g. both platforms), optional route filter."""
    ids: tuple[str, ...]
    routes: frozenset[str] = frozenset()


_VIA = re.compile(r"\s+via\s.*$", re.IGNORECASE)


def short_headsign(headsign: str) -> str:
    """'Herttoniemi(M) via Kalasatama(M)' -> 'Herttoniemi(M)'.

    The destination is what people read; the route number already tells
    the variants apart, and the via part would push the destination into
    truncation on the narrow stop widgets.
    """
    return _VIA.sub("", headsign).strip() or headsign.strip()


def parse_departures(data: dict) -> dict[str, list[Departure]]:
    """Map stop gtfsId -> departures, from a DEPARTURES_QUERY response's `data`."""
    out: dict[str, list[Departure]] = {}
    for stop in data.get("stops") or []:
        if not stop:
            continue
        deps = []
        for st in stop.get("stoptimesWithoutPatterns") or []:
            trip = st.get("trip") or {}
            route = trip.get("route") or {}
            seconds = st.get("realtimeDeparture")
            if seconds is None:
                seconds = st.get("scheduledDeparture")
            if st.get("serviceDay") is None or seconds is None:
                continue
            when = datetime.fromtimestamp(st["serviceDay"] + seconds, UTC)
            if when.year not in YEARS:
                continue
            deps.append(
                Departure(
                    route=_text(route.get("shortName") or "?"),
                    headsign=short_headsign(_text(st.get("headsign") or trip.get("tripHeadsign") or "")),
                    time=when,
                    realtime=bool(st.get("realtime")),
                    mode=_text(route.get("mode") or "BUS").lower(),
                    trunk=route.get("type") == TRUNK_ROUTE_TYPE,
                )
            )
        out[_text(stop["gtfsId"])] = deps
    return out


def parse_stop_names(data: dict) -> dict[str, str]:
    """Map stop gtfsId -> stop name, from a DEPARTURES_QUERY response's `data`."""
    return {s["gtfsId"]: _text(s["name"]) for s in data.get("stops") or [] if s and s.get("name")}


def parse_stop_modes(data: dict) -> dict[str, str]:
    """Map stop gtfsId -> vehicle mode ("tram", "bus", ...), from a DEPARTURES_QUERY response."""
    return {s["gtfsId"]: _text(s["vehicleMode"]).lower() for s in data.get("stops") or [] if s and s.get("vehicleMode")}


def select(by_stop: dict[str, list[Departure]], q: StopQuery) -> list[Departure]:
    """Merge a widget's stops, apply its route filter, sort by time."""
    deps = [d for sid in q.ids for d in by_stop.get(sid, [])]
    if q.routes:
        deps = [d for d in deps if d.route in q.routes]
    return sorted(deps, key=lambda d: d.time)


def fetch(
    queries: list[StopQuery], key: str, language: str
) -> tuple[dict[str, list[Departure]], dict[str, str], dict[str, str]]:
    """Departures, stop names and stop vehicle modes, all keyed by gtfsId."""
    ids = sorted({sid for q in queries for sid in q.ids})
    if not ids:
        return {}, {}, {}
    # Fetch extra when filtering by route, so 8 remain after the filter.
    n = 30 if any(q.routes for q in queries) else 12
    data = _post(DEPARTURES_QUERY, {"ids": ids, "n": n, "lang": language}, key)
    by_stop, names, modes = _unexpected(
        lambda: (parse_departures(data), parse_stop_names(data), parse_stop_modes(data))
    )
    missing = [sid for sid in ids if sid not in by_stop]
    if missing:
        log.warning("unknown stop ids: %s", ", ".join(missing))
    return by_stop, names, modes


def search(name: str, key: str) -> list[dict]:
    data = _post(SEARCH_QUERY, {"name": name}, key)
    return _unexpected(lambda: data.get("stops") or [])


def format_search(stops: list[dict]) -> str:
    lines = []
    for s in sorted(stops, key=lambda s: (s.get("name") or "", s.get("code") or "")):
        parent = (s.get("parentStation") or {}).get("name")
        where = ", ".join(x for x in (s.get("desc"), f"platform {s['platformCode']}" if s.get("platformCode") else None, parent) if x)
        lines.append(f"{s.get('code') or s['gtfsId']:<7} {s.get('name')}  ({(s.get('vehicleMode') or '?').lower()}{', ' + where if where else ''})")
        seen = []
        for p in s.get("patterns") or []:
            label = f"{p['route']['shortName']} → {p.get('headsign') or '?'}"
            if label not in seen:
                seen.append(label)
        if seen:
            lines.append("        " + "; ".join(seen))
    return "\n".join(lines) or "no stops found"


# --- the latest departures -------------------------------------------------

class Transit:
    """Holds the latest departures for all stop widgets; refreshes every minute or so.

    Departures have no disk cache: they go stale within minutes, and a restart
    refetches. Stop codes ("H0665") are resolved to ids once and cached on disk.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.settings = cfg.source_settings.transit
        self.configured = [name for name, s in cfg.stops.items() if s.ids]
        self.live = bool(self.settings.key and self.configured)
        if not self.settings.key:
            log.info("no Digitransit key configured: showing placeholder departures")
        elif not self.configured:
            log.info("no stop codes configured: showing placeholder departures")
        self.stale_after = timedelta(minutes=self.settings.stale_after_minutes)
        self.codes_path = cfg.cache_dir / "stop_codes.json"
        self.code_ids: dict[str, str] = self._load_codes()
        self.unknown_codes: set[str] = set()  # looked up and not found: not asked again until a restart
        self.rechecked_codes: set[str] = set()  # cached ids that HSL no longer knew, looked up again once
        self.stale_codes: set[str] = set()  # of those, the ones still waiting for their new lookup
        self.by_stop: dict[str, list[Departure]] = {}
        self.names: dict[str, str] = {}  # gtfsId -> HSL's stop name
        self.modes: dict[str, str] = {}  # gtfsId -> vehicle mode ("tram", "bus", ...)
        self.fetched_at: datetime | None = None
        self.last_attempt: float | None = None  # time.monotonic(), like Periodic's
        self.lock = threading.Lock()  # guards by_stop, names, modes, fetched_at
        self.fetch_lock = threading.Lock()
        # The one thread fetching for callers that wait only so long (see refresh).
        self.background: threading.Thread | None = None
        self.background_lock = threading.Lock()

    def _load_codes(self) -> dict[str, str]:
        try:
            return json.loads(self.codes_path.read_text())
        except FileNotFoundError:
            return {}
        except ValueError:
            log.warning("ignoring unreadable %s", self.codes_path)
            return {}

    def _resolve_codes(self) -> None:
        codes = {
            ref.upper() for s in self.cfg.stops.values() for ref in s.ids
            if not is_gtfs_id(ref) and ref.upper() not in self.code_ids and ref.upper() not in self.unknown_codes
        } | self.stale_codes
        if not codes:
            return
        for code in sorted(codes):
            try:
                stop_id = resolve_code(code, self.settings.key)
            except HSLError as e:
                log.warning("could not look up stop code %s (%s); will retry", code, e)
                continue
            self.stale_codes.discard(code)
            if stop_id:
                log.info("stop code %s is %s", code, stop_id)
                self.code_ids[code] = stop_id
            elif code in self.code_ids:
                # A code HSL knew before and has no stop for now: the id it
                # had stays (its departures, if any, are still the best there is).
                log.warning("stop code %s is no longer known to HSL; keeping %s", code, self.code_ids[code])
            else:
                log.warning("unknown stop code %s (check it on the stop sign or reittiopas)", code)
                self.unknown_codes.add(code)
        try:
            self.codes_path.parent.mkdir(parents=True, exist_ok=True)
            self.codes_path.write_text(json.dumps(self.code_ids, indent=1, sort_keys=True))
        except OSError as e:
            log.warning("could not save the stop codes: %s", e)

    def _query(self, name: str) -> StopQuery:
        stop = self.cfg.stops[name]
        # .get, not a check and then a lookup: the fetch thread changes
        # code_ids while requests read it.
        found = (ref if is_gtfs_id(ref) else self.code_ids.get(ref.upper()) for ref in stop.ids)
        return StopQuery(tuple(sid for sid in found if sid), stop.routes)

    def refresh(self, min_age_seconds: float = 0, wait_seconds: float | None = None) -> None:
        """Fetch now, unless the last attempt was less than min_age_seconds ago.

        Scheduled polls run just before each minute boundary (see the server's
        refresher); a button press on the device asks for one more.

        wait_seconds: return after this long even if the fetch is still
        running (it carries on in the background and its result is used by
        later requests). For requests the device is waiting on: it gives up
        after 10 s, so the last good data in time beats fresh data too late.
        """
        if not self.live:
            return
        if wait_seconds is not None:
            # One background thread for everyone who waits: while it runs,
            # later callers wait on it too instead of starting their own. A
            # thread each meant that a fetch which never finished collected
            # one stuck thread per request, up to the service's limit, after
            # which the server could answer nobody.
            called, give_up = time.monotonic(), time.monotonic() + wait_seconds
            # Twice at most: the thread found running may have been started
            # for a caller content with older data (a plain request takes
            # departures up to a minute old, a button press 10 s) and ended
            # without fetching. Then this caller starts its own.
            for _ in range(2):
                with self.background_lock:
                    worker = self.background
                    if worker is None or not worker.is_alive():
                        worker = threading.Thread(target=self.refresh, args=(min_age_seconds,), daemon=True)
                        try:
                            worker.start()
                        except RuntimeError as e:  # no more threads (the service's limit): draw with what we have
                            log.warning("could not start a departures fetch: %s", e)
                            return
                        self.background = worker
                worker.join(max(0.0, give_up - time.monotonic()))
                attempt = self.last_attempt
                if worker.is_alive() or (attempt is not None and (
                        attempt >= called or time.monotonic() - attempt < min_age_seconds)):
                    return  # out of time, or there has been a fetch recent enough for this caller
            return
        # One fetch at a time. A refresh that arrives during a fetch (e.g. a
        # button press right after startup) waits for it, then finds it recent
        # and uses its result, instead of skipping and drawing with no data.
        with self.fetch_lock:
            tick = time.monotonic()
            if self.last_attempt is not None and tick - self.last_attempt < min_age_seconds:
                return
            self.last_attempt = tick
            self._fetch(datetime.now(UTC))

    def _fetch(self, now: datetime) -> None:
        # Never raises, like Weather.refresh: the refresher thread must survive.
        try:
            self._resolve_codes()
            queries = [self._query(name) for name in self.configured]
            by_stop, names, modes = fetch(queries, self.settings.key, self.cfg.language)
        except HSLError as e:
            log.warning("departures fetch failed (%s); keeping last good data from %s", e, self.fetched_at)
            return
        except Exception:
            log.exception("departures fetch failed unexpectedly; keeping last good data from %s", self.fetched_at)
            return
        asked = {sid for q in queries for sid in q.ids}
        if not asked:
            # No stop could be asked for (its codes not looked up yet, or not
            # known to HSL): no answer, so nothing to show as fresh. The
            # status bar says departures are unavailable; the log says why.
            log.debug("no departures fetched: none of the stop codes is known yet")
            return
        if not asked & by_stop.keys():
            # An answer with none of the stops in it (empty data during an
            # outage at their end, say) is a failed fetch, not "no departures":
            # the last good ones stay, and the status bar says how old they are.
            log.warning("departures fetch failed (the answer had none of the stops asked for); "
                        "keeping last good data from %s", self.fetched_at)
            self._recheck_codes(by_stop)  # unless the stops were renumbered: then the next poll finds them
            return
        with self.lock:
            self.by_stop, self.fetched_at = by_stop, now
            self.names.update(names)
            self.modes.update(modes)
        self._recheck_codes(by_stop)
        log.debug("departures updated: %d departures from %d stops", sum(map(len, by_stop.values())), len(by_stop))

    def _recheck_codes(self, by_stop: dict) -> None:
        """A code whose cached id HSL no longer knows (stops get renumbered) is
        looked up afresh at the next poll, once per run. Until that lookup
        gives a new id the old one stays: the answer may simply have been
        empty (an outage at their end), and without the id the departures
        kept from before couldn't be shown."""
        for code, stop_id in list(self.code_ids.items()):
            if stop_id not in by_stop and code not in self.rechecked_codes and any(
                code == ref.upper() for s in self.cfg.stops.values() for ref in s.ids
            ):
                log.warning("stop %s (%s) is unknown to HSL; looking the code up again", code, stop_id)
                self.rechecked_codes.add(code)
                self.stale_codes.add(code)

    def warning(self, now: datetime, language: str) -> str | None:
        """For the status bar: that live departures are missing or old, else None."""
        fetched_at = self.fetched_at
        if not stale(self.live, fetched_at, now, self.stale_after):
            return None
        if fetched_at is None:
            return i18n.t(WORDS, language, "departures_unavailable")
        minutes = (now - fetched_at).total_seconds() / 60
        return i18n.t(WORDS, language, "departures_old", age=i18n.age(minutes))

    def stops(self, now: datetime) -> dict[str, StopDepartures]:
        """By stop widget name."""
        with self.lock:
            by_stop, names, modes = self.by_stop, self.names, self.modes
        out = {}
        for i, (widget, stop) in enumerate(self.cfg.stops.items()):
            if self.live and widget in self.configured:
                query = self._query(widget)
                out[widget] = StopDepartures(
                    self._name(stop, query, names),
                    select(by_stop, query),
                    ordered_modes(modes[sid] for sid in query.ids if sid in modes),
                )
            else:
                name = stop.name or (stop.ids[0] if stop.ids else "—")
                out[widget] = placeholder(name, i, now)
        return out

    @staticmethod
    def _name(stop, query: StopQuery, names: dict[str, str]) -> str:
        """The configured name, else HSL's name for the stop, else its code until known."""
        if stop.name:
            return stop.name
        for stop_id in query.ids:
            if stop_id in names:
                return names[stop_id]
        return stop.ids[0]
