"""Command line entry point.

    python -m glance render [-o glance.png] [--offline] [--lang fi] [--screen NAME]
    python -m glance serve [--port 8080]
    python -m glance stops "Pasila"     # find Digitransit stop ids
"""

from __future__ import annotations

import argparse
import io
import ipaddress
import json
import logging
import re
import socket
import threading
import time
import urllib.parse
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import battery, decks, departures, hsl, render, schedule
from .config import Config, load
from .sources.fmi import Weather
from .sources.periodic import Periodic

log = logging.getLogger("glance")


class Transit:
    """Holds the latest departures for all stop widgets; refreshes every minute or so.

    Departures have no disk cache: they go stale within minutes, and a restart
    refetches. Stop codes ("H0665") are resolved to ids once and cached on disk.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.configured = [name for name, s in cfg.stops.items() if s.ids]
        self.live = bool(cfg.digitransit_key and self.configured)
        if not cfg.digitransit_key:
            log.info("no Digitransit key configured: showing placeholder departures")
        elif not self.configured:
            log.info("no stop codes configured: showing placeholder departures")
        self.codes_path = cfg.cache_dir / "stop_codes.json"
        self.code_ids: dict[str, str] = self._load_codes()
        self.unknown_codes: set[str] = set()  # looked up and not found: not asked again until a restart
        self.rechecked_codes: set[str] = set()  # cached ids that HSL no longer knew, looked up again once
        self.stale_codes: set[str] = set()  # of those, the ones still waiting for their new lookup
        self.by_stop: dict[str, list[departures.Departure]] = {}
        self.names: dict[str, str] = {}  # gtfsId -> HSL's stop name
        self.modes: dict[str, str] = {}  # gtfsId -> vehicle mode ("tram", "bus", ...)
        self.fetched_at: datetime | None = None
        self.last_attempt: float | None = None  # time.monotonic(), like Weather's
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
            if not hsl.is_gtfs_id(ref) and ref.upper() not in self.code_ids and ref.upper() not in self.unknown_codes
        } | self.stale_codes
        if not codes:
            return
        for code in sorted(codes):
            try:
                stop_id = hsl.resolve_code(code, self.cfg.digitransit_key)
            except hsl.HSLError as e:
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

    def _query(self, name: str) -> hsl.StopQuery:
        stop = self.cfg.stops[name]
        # .get, not a check and then a lookup: the fetch thread changes
        # code_ids while requests read it.
        found = (ref if hsl.is_gtfs_id(ref) else self.code_ids.get(ref.upper()) for ref in stop.ids)
        return hsl.StopQuery(tuple(sid for sid in found if sid), stop.routes)

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
                    fetch = self.background
                    if fetch is None or not fetch.is_alive():
                        fetch = threading.Thread(target=self.refresh, args=(min_age_seconds,), daemon=True)
                        try:
                            fetch.start()
                        except RuntimeError as e:  # no more threads (the service's limit): draw with what we have
                            log.warning("could not start a departures fetch: %s", e)
                            return
                        self.background = fetch
                fetch.join(max(0.0, give_up - time.monotonic()))
                attempt = self.last_attempt
                if fetch.is_alive() or (attempt is not None and (
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
            by_stop, names, modes = hsl.fetch(queries, self.cfg.digitransit_key, self.cfg.language)
        except hsl.HSLError as e:
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

    def stops(self, now: datetime) -> dict[str, departures.StopDepartures]:
        """By stop widget name."""
        with self.lock:
            by_stop, names, modes = self.by_stop, self.names, self.modes
        out = {}
        for i, (widget, stop) in enumerate(self.cfg.stops.items()):
            if self.live and widget in self.configured:
                query = self._query(widget)
                out[widget] = departures.StopDepartures(
                    self._name(stop, query, names),
                    hsl.select(by_stop, query),
                    departures.ordered_modes(modes[sid] for sid in query.ids if sid in modes),
                )
            else:
                name = stop.name or (stop.ids[0] if stop.ids else "—")
                out[widget] = departures.placeholder(name, i, now)
        return out

    @staticmethod
    def _name(stop, query: hsl.StopQuery, names: dict[str, str]) -> str:
        """The configured name, else HSL's name for the stop, else its code until known."""
        if stop.name:
            return stop.name
        for stop_id in query.ids:
            if stop_id in names:
                return names[stop_id]
        return stop.ids[0]


def scene(
    cfg: Config, weather: Weather, transit: Transit, now: datetime | None = None,
    device_battery: battery.Battery | None = None, interval_minutes: int = 1,
) -> render.Scene:
    now = now or datetime.now(cfg.tz)
    return render.Scene(
        now=now,
        language=cfg.language,
        forecast=weather.forecast,
        weather_stale_after=timedelta(minutes=cfg.weather_stale_after_minutes),
        stops=transit.stops(now),
        departures_live=transit.live,
        departures_fetched_at=transit.fetched_at,
        departures_stale_after=timedelta(minutes=cfg.departures_stale_after_minutes),
        battery_percent=device_battery.level(now) if device_battery else None,
        departure_clock=interval_minutes > 1,
    )


def cmd_render(cfg: Config, args: argparse.Namespace) -> None:
    weather, transit = Weather(cfg), Transit(cfg)
    if not args.offline:
        weather.refresh(force=True)
        transit.refresh()
    if args.screen and args.screen not in cfg.screens:
        raise SystemExit(f"no screen {args.screen!r}; the screens are: {', '.join(cfg.screens)}")
    sc = scene(cfg, weather, transit)
    img = render.render(cfg, sc, args.screen, cfg.deck_at(sc.now))
    Path(args.output).write_bytes(render.to_png(img))
    print(f"wrote {args.output}")


def cmd_stops(cfg: Config, args: argparse.Namespace) -> None:
    if not cfg.digitransit_key:
        raise SystemExit("No Digitransit key: add [digitransit] key = \"...\" to config.local.toml")
    try:
        print(hsl.format_search(hsl.search(args.name, cfg.digitransit_key)))
    except hsl.HSLError as e:
        raise SystemExit(f"stop search failed: {e}")


PREVIEW_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Glance preview</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { margin: 0; min-height: 100vh; display: grid; place-items: center;
         background: #3a3a3a; font: 14px system-ui, sans-serif; color: #ccc; }
  .frame { background: #1c1c1c; padding: 22px; border-radius: 14px; }
  img { display: block; width: 800px; max-width: calc(100vw - 76px); height: auto;
        image-rendering: pixelated; background: #fff; }
  p { text-align: center; margin: 10px 0 0; }
  button { font: inherit; margin-left: 8px; }
</style></head>
<body><div><div class="frame"><img id="g" alt="Glance"></div>
<p><span id="screen"></span> · behaves like the device: <span id="s">loading…</span></p>
<p>Buttons: <button id="prev">◀ previous screen</button><button id="next">next screen ▶</button>
<button id="b">refresh now</button>
· <a href="/glance.png" style="color:#ccc">glance.png</a></p></div>
<script>
  let timer;
  // press: '' (a timed wake), 'prev', 'next' or 'refresh', like the device's buttons.
  async function load(press) {
    clearTimeout(timer);
    let r;
    try {
      r = await fetch('/glance.png' + (press ? '?button=' + press : ''),
                      { cache: 'no-store', headers: { 'X-Glance-Client': 'preview' } });
      if (!r.ok) throw new Error(r.status);
    } catch (e) {  // server down or restarting: try again, as the device does
      document.getElementById('s').textContent = 'server not answering, retrying in 30 s';
      timer = setTimeout(() => load(press), 30000);
      return;
    }
    const sleep = parseInt(r.headers.get('X-Glance-Sleep') || '60', 10);
    const at = new Date(Date.now() + sleep * 1000).toLocaleTimeString();
    let note = '';
    if (r.status === 204) {  // nothing to change: the device keeps its image
      note = ' (only one screen in this deck: nothing to change)';
    } else {
      const img = document.getElementById('g');
      const old = img.src;
      img.src = URL.createObjectURL(await r.blob());
      if (old) URL.revokeObjectURL(old);
      document.getElementById('screen').textContent = r.headers.get('X-Glance-Screen') || '';
    }
    document.getElementById('s').textContent = `next wake in ${sleep} s (${at})${note}`;
    timer = setTimeout(() => load(''), sleep * 1000);
  }
  for (const [id, press] of [['prev', 'prev'], ['next', 'next'], ['b', 'refresh']])
    document.getElementById(id).onclick = () => load(press);
  load('');
</script></body></html>
"""


def host_allowed(host: str | None, extra: tuple[str, ...] = ()) -> bool:
    """Whether a request's Host header names this server as a home network would.

    Guards against DNS rebinding: a web page on the internet pointing its own
    domain at this server's address, so that a browser at home lets the page
    read the dashboard. Such a request carries the page's domain as Host. Names
    only a local network can give are let through: IP addresses, single-label
    names ("raspberrypi"), .local (mDNS) and localhost, plus `extra` (config
    [server] hostnames). No Host at all is not a browser, so it's fine too.
    """
    if not host:
        return True
    try:
        name = urllib.parse.urlsplit("//" + host).hostname or ""
    except ValueError:
        return False
    name = name.rstrip(".").lower()
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if name in extra:
        return True
    if not _HOSTNAME.fullmatch(name):
        return False
    return "." not in name or name.endswith((".local", ".localhost"))


_HOSTNAME = re.compile(r"[a-z0-9_]([a-z0-9._-]*[a-z0-9_])?")


def client_allowed(address: str, extra: tuple = ()) -> bool:
    """Whether a request comes from an address a home network has.

    The server has no login and must not be reachable from the internet, but
    nothing else enforces that: a port forwarded on the router by mistake (or
    by UPnP) would open it to everyone. Requests from public addresses are
    refused, so such a mistake exposes nothing. Private, loopback and
    link-local addresses are let through, plus the networks in `extra`
    (config [server] allowed_networks: a VPN's range, say).
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    # 6to4 and Teredo addresses stand for clients on the public internet,
    # though Python files them under "private" (not globally routed as such).
    tunnelled = ip.version == 6 and (ip.sixtofour or ip.teredo)
    if (ip.is_private or ip.is_loopback or ip.is_link_local) and not tunnelled:
        return True
    return any(ip.version == net.version and ip in net for net in extra)
# As http.server's own log_message does: control characters arriving in a
# request line must not reach the log (or a terminal showing it) raw.
_UNSAFE_IN_LOG = re.compile(r"[\x00-\x1f\x7f-\x9f\\]")


# Sent by the device and the preview page with every image request; see do_GET.
CLIENT_HEADER = "X-Glance-Client"
# How long the refresher waits for a poll's departures before drawing the
# coming minute with the last good ones: the poll is 15 s before the minute,
# and the drawing must be done when the device wakes. The fetch itself carries
# on in the background, so one that never finishes can't stop the refresher.
POLL_WAIT = 10.0
# How long a request waits for fresh departures before using the last good
# ones. The device gives up after 10 s, and drawing takes ~2.5 s of that on a
# Pi 1 (twice that if another drawing is under way).
UPSTREAM_WAIT = 4.0


class _RequestDeadline(io.RawIOBase):
    """Reads a connection's request, which must all arrive within `seconds`.

    A socket timeout alone limits each read, so a client sending a byte every
    few seconds would keep its thread for as long as it liked.
    """

    def __init__(self, sock: socket.socket, seconds: float) -> None:
        self._sock = sock
        self._deadline = time.monotonic() + seconds

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request not received in time")
        self._sock.settimeout(remaining)
        return self._sock.recv_into(buffer)


def log_safe(text: str) -> str:
    return _UNSAFE_IN_LOG.sub(lambda m: "\\\\" if m[0] == "\\" else f"\\x{ord(m[0]):02x}", text)


def make_handler(
    cfg: Config, weather: Weather, transit: Transit, prerendered: schedule.Prerendered | None = None,
    device_battery: battery.Battery | None = None, selection: decks.Selection | None = None,
) -> type[BaseHTTPRequestHandler]:
    prerendered = prerendered or schedule.Prerendered()
    device_battery = device_battery or battery.Battery()
    selection = selection or decks.Selection()
    recent = schedule.Recent()

    class Handler(BaseHTTPRequestHandler):
        # A request must arrive within this many seconds of connecting (see
        # _RequestDeadline): each connection holds a thread, and without a
        # limit an idle or dripping one holds it for ever.
        timeout = 10
        # Not the standard library's "BaseHTTP/0.6 Python/3.13.5": no need to
        # tell every client the Python version.
        def version_string(self) -> str:
            return "Glance"

        def setup(self) -> None:
            super().setup()
            self.rfile.close()
            self.rfile = io.BufferedReader(_RequestDeadline(self.connection, self.timeout))

        def do_GET(self) -> None:
            self.connection.settimeout(self.timeout)  # for sending the answer
            if not client_allowed(self.client_address[0], cfg.allowed_networks):
                self._send(403, "text/plain", b"this server answers only on the home network (see [server] allowed_networks)\n")
                return
            if not host_allowed(self.headers.get("Host"), cfg.hostnames):
                self._send(403, "text/plain", b"unknown host name: use the server's address, or add the name to [server] hostnames\n")
                return
            try:
                url = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(url.query)
            except ValueError:
                self._send(400, "text/plain", b"bad request\n")
                return
            if url.path in render.FORMATS:
                encode, content_type = render.FORMATS[url.path]
                body = None
                # Requests that change something (a battery report, a refresh)
                # count only with this header, which the device and the
                # preview page send. A web page elsewhere can make a browser
                # at home request an image from here, but can't add a header
                # to it: for that the browser first asks the server's
                # permission (a CORS preflight), which this server never gives.
                trusted = self.headers.get(CLIENT_HEADER) is not None
                # A button: "prev" / "next" move through the deck's screens,
                # "refresh" (or refresh=1, as older firmware sends) refetches.
                button = (query.get("button") or [None])[0] if trusted else None
                refresh = trusted and (query.get("refresh") == ["1"] or button == "refresh")
                step = {"prev": -1, "next": 1}.get(button)
                if trusted:
                    # The device reports its battery with every request.
                    device_battery.report((query.get("battery_mv") or [None])[0], datetime.now(cfg.tz))
                if refresh:
                    # Button press on the device: fresh departures, rate-limited.
                    # (Weather is left to the refresher, which checks it every
                    # minute; fetching it here could keep the device waiting.)
                    transit.refresh(cfg.departures_min_refresh_seconds, wait_seconds=UPSTREAM_WAIT)
                plan = schedule.plan(datetime.now(cfg.tz), cfg.wake_schedule, cfg.wake_offset_seconds)
                planned_at = time.monotonic()
                deck = cfg.deck_at(plan.display_time)
                # How many screens the deck has: the device skips a white
                # button press altogether on a deck of one.
                deck_size = {"X-Glance-Deck-Size": str(len(deck.screens))}
                if step and not selection.step(deck, step):
                    # One screen in this deck: nothing to change. The device
                    # keeps its image and goes back to sleep (no 6 s refresh).
                    sleep = max(1, round(plan.sleep_seconds - (time.monotonic() - planned_at)))
                    self._send(204, "text/plain", b"", {"X-Glance-Sleep": str(sleep), **deck_size})
                    return
                screen, position = selection.current(deck)
                if step:  # the screen drawn ahead for a press, if it's still that minute
                    body = prerendered.get(plan.display_time, screen, url.path, max_late=timedelta(minutes=1),
                                           data=transit.fetched_at)
                elif not refresh:
                    body = prerendered.get(plan.display_time, screen, url.path, data=transit.fetched_at)
                if body is None:
                    # Not the pre-drawn image (a request between wakes, e.g. the
                    # browser preview): departures may not have been polled for a
                    # while, since polling follows the device's schedule.
                    if not refresh:  # a refresh has just asked, and waited, once already
                        transit.refresh(60, wait_seconds=UPSTREAM_WAIT)
                    forecast = weather.forecast
                    # Drawn one at a time and shared for a few seconds (see
                    # schedule.Recent), unless the minute or the data changed.
                    key = (
                        plan.display_time.replace(second=0, microsecond=0), plan.interval_minutes, screen,
                        transit.fetched_at, forecast.fetched_at if forecast else None,
                    )
                    body = recent.get(key, url.path, lambda: render.render(cfg, scene(
                        cfg, weather, transit, plan.display_time, device_battery, plan.interval_minutes,
                    ), screen, deck, forgiving=True), encode)
                # The device counts its sleep from when the answer arrives, so
                # take off what the wait for departures and the drawing took.
                sleep = max(1, round(plan.sleep_seconds - (time.monotonic() - planned_at)))
                self._send(200, content_type, body, {
                    "X-Glance-Sleep": str(sleep),
                    # For the preview page (and logs): which screen this is.
                    "X-Glance-Screen": f"{deck.name}: {screen} ({position + 1}/{len(deck.screens)})",
                    **deck_size,
                })
            elif url.path == "/":
                self._send(200, "text/html; charset=utf-8", PREVIEW_HTML.encode())
            else:
                self._send(404, "text/plain", b"not found\n")

        def _send(self, code: int, ctype: str, body: bytes, headers: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # Take the Content-Type as given; and no page elsewhere may show
            # this one in a frame (and so get a click on its buttons).
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *a) -> None:
            log.info("%s %s", self.address_string(), log_safe(fmt % a))

    return Handler


def cmd_serve(cfg: Config, args: argparse.Namespace) -> None:
    weather, transit = Weather(cfg), Transit(cfg)
    periodic: tuple[Periodic, ...] = (weather,)  # sources refreshed on their own intervals
    prerendered = schedule.Prerendered()
    device_battery = battery.Battery(cfg.battery_full_mv, cfg.cache_dir / "battery.csv")
    selection = decks.Selection()
    stop = threading.Event()

    def refresher() -> None:
        # Departures: once at start, then just before each minute the device
        # wakes for (per the schedule), so they are fresh when it arrives; then
        # draw that minute's image so the device doesn't wait for it.
        # The refreshes handle their own failures; the loop still guards each
        # round, because if this thread ever ended, the process would live on
        # with no polling and no pre-drawn images, and nothing would restart it.
        transit.refresh(wait_seconds=POLL_WAIT)
        while not stop.is_set():
            # Right after a poll, so a slow answer can't delay the next one.
            for source in periodic:
                try:
                    source.refresh()  # checks its own interval
                except Exception:
                    log.exception("%s refresh failed", source.WHAT)
            now = datetime.now(cfg.tz)
            next_poll = schedule.next_poll(now, cfg.departures_poll_lead_seconds)
            if stop.wait(max(0.0, schedule.seconds_between(now, next_poll))):
                break
            try:
                minute = schedule.next_boundary(datetime.now(cfg.tz), 1)
                if not cfg.wake_schedule.is_wake(minute):
                    continue  # the device sleeps through this minute: no polling, no drawing
                transit.refresh(cfg.departures_min_refresh_seconds, wait_seconds=POLL_WAIT)
                started = time.monotonic()
                interval = cfg.wake_schedule.interval_at(minute)
                # The screen that minute will show: the selected one, or the
                # start of another deck if the schedule switches at that minute.
                # Then its neighbours, for a button press that changes screens.
                deck = cfg.deck_at(minute)
                showing = selection.peek(deck)
                sc = scene(cfg, weather, transit, minute, device_battery, interval)
                for screen in (showing, *deck.neighbours(showing)):
                    img = render.render(cfg, sc, screen, deck, forgiving=True)
                    prerendered.put(minute, screen, {path: encode(img) for path, (encode, _) in render.FORMATS.items()},
                                    data=sc.departures_fetched_at)
            except Exception:
                log.exception("pre-rendering failed; requests will render on demand")
                continue
            log.debug("pre-rendered %s in %.1f s", minute.strftime("%H:%M"), time.monotonic() - started)

    threading.Thread(target=refresher, daemon=True).start()
    Handler = make_handler(cfg, weather, transit, prerendered, device_battery, selection)

    server = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    log.info("serving on http://%s:%d/ (image at /glance.png, /glance.g2)", cfg.host, cfg.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="glance")
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument("--lang", choices=("en", "fi"), help="override the configured language")
    parser.add_argument("-v", "--verbose", action="store_true", help="also log every data fetch")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("render", help="render one PNG")
    r.add_argument("-o", "--output", default="glance.png")
    r.add_argument("--offline", action="store_true", help="use cached weather only")
    r.add_argument("--screen", help="which screen (default: the default deck's first)")
    s = sub.add_parser("serve", help="serve /glance.png over HTTP")
    s.add_argument("--port", type=int, help="override the configured port")
    st = sub.add_parser("stops", help="search HSL stops by name to find their ids")
    st.add_argument("name")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load(args.config)
    overrides = {}
    if args.lang:
        overrides["language"] = args.lang
    if getattr(args, "port", None):
        overrides["port"] = args.port
    if overrides:
        from dataclasses import replace
        cfg = replace(cfg, **overrides)

    {"render": cmd_render, "serve": cmd_serve, "stops": cmd_stops}[args.command](cfg, args)


if __name__ == "__main__":
    main()
