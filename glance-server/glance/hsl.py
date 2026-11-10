"""HSL departures via the Digitransit routing API (GraphQL, OTP2 GTFS schema).

Docs: https://digitransit.fi/en/developers/apis/1-routing-api/
Needs a free subscription key, sent as the `digitransit-subscription-key` header.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

from .departures import Departure

log = logging.getLogger(__name__)

ENDPOINT = "https://api.digitransit.fi/routing/v2/hsl/gtfs/v1"
GEOCODING_ENDPOINT = "https://api.digitransit.fi/geocoding/v1/search"
# HSL's GTFS route type for trunk bus routes (runkolinjat: 20, 30, 200, 500, 550, ...),
# drawn orange by HSL. 701 = regular city bus, 700 = regional bus, both blue.
TRUNK_ROUTE_TYPE = 702
USER_AGENT = "glance-dashboard/0.1"
# Seconds. Departures are polled 15 s before the minute and must be back, and
# the image drawn, before the device wakes just after it.
TIMEOUT = 8

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


class HSLError(Exception):
    pass


def _request(req: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.load(resp)
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
            deps.append(
                Departure(
                    route=route.get("shortName") or "?",
                    headsign=short_headsign(st.get("headsign") or trip.get("tripHeadsign") or ""),
                    time=datetime.fromtimestamp(st["serviceDay"] + seconds, UTC),
                    realtime=bool(st.get("realtime")),
                    mode=(route.get("mode") or "BUS").lower(),
                    trunk=route.get("type") == TRUNK_ROUTE_TYPE,
                )
            )
        out[stop["gtfsId"]] = deps
    return out


def parse_stop_names(data: dict) -> dict[str, str]:
    """Map stop gtfsId -> stop name, from a DEPARTURES_QUERY response's `data`."""
    return {s["gtfsId"]: s["name"] for s in data.get("stops") or [] if s and s.get("name")}


def parse_stop_modes(data: dict) -> dict[str, str]:
    """Map stop gtfsId -> vehicle mode ("tram", "bus", ...), from a DEPARTURES_QUERY response."""
    return {s["gtfsId"]: s["vehicleMode"].lower() for s in data.get("stops") or [] if s and s.get("vehicleMode")}


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
