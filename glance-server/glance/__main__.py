"""Command line entry point.

    python -m glance render [-o glance.png] [--offline] [--lang fi]
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

from . import battery, departures, fmi, hsl, render, schedule
from .config import Config, load

log = logging.getLogger("glance")


class Weather:
    """Holds the latest good forecast; refreshes at most every N minutes."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cache_path = cfg.cache_dir / "forecast.json"
        self.forecast = fmi.load_cache(self.cache_path)
        self.last_attempt: datetime | None = None
        self.last_failed = False
        self.lock = threading.Lock()

    RETRY_AFTER = timedelta(minutes=2)  # after a failed fetch, instead of the full interval

    def refresh(self, force: bool = False) -> None:
        now = datetime.now(UTC)
        interval = timedelta(minutes=self.cfg.weather_refresh_minutes)
        with self.lock:
            wait = min(interval, self.RETRY_AFTER) if self.last_failed else interval
            if not force and self.last_attempt and now - self.last_attempt < wait:
                return
            current = self.forecast and self.forecast.parameters == fmi.PARAMETERS
            if not force and current and now - self.forecast.fetched_at < interval:
                return
            self.last_attempt = now
            self.last_failed = True  # until the fetch below succeeds
        # Never raises: this runs in the refresher thread, which must survive
        # whatever a fetch does (it once died for good on an error page).
        try:
            fc = fmi.fetch(self.cfg.lat, self.cfg.lon, self.cfg.tz, now)
        except fmi.FMIError as e:
            age = f"{(now - self.forecast.fetched_at)} old" if self.forecast else "none cached"
            log.warning("weather fetch failed (%s); using last good forecast (%s)", e, age)
            return
        except Exception:
            log.exception("weather fetch failed unexpectedly; using the last good forecast")
            return
        with self.lock:
            self.forecast = fc
            self.last_failed = False
        try:
            fmi.save_cache(fc, self.cache_path)
        except OSError as e:
            log.warning("could not save the weather cache: %s", e)
        log.info("weather updated: %d hours", len(fc.hours))


class Transit:
    """Holds the latest departures for all stop widgets; refreshes every minute or so.

    Departures have no disk cache: they go stale within minutes, and a restart
    refetches. Stop codes ("H0665") are resolved to ids once and cached on disk.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.configured = {i for i, s in enumerate(cfg.stops) if s.ids}
        self.live = bool(cfg.digitransit_key and self.configured)
        if not cfg.digitransit_key:
            log.info("no Digitransit key configured: showing placeholder departures")
        elif not self.configured:
            log.info("no stop codes configured: showing placeholder departures")
        self.codes_path = cfg.cache_dir / "stop_codes.json"
        self.code_ids: dict[str, str] = self._load_codes()
        self.unknown_codes: set[str] = set()  # looked up and not found: not asked again until a restart
        self.rechecked_codes: set[str] = set()  # cached ids that HSL no longer knew, looked up again once
        self.by_stop: dict[str, list[departures.Departure]] = {}
        self.names: dict[str, str] = {}  # gtfsId -> HSL's stop name
        self.modes: dict[str, str] = {}  # gtfsId -> vehicle mode ("tram", "bus", ...)
        self.fetched_at: datetime | None = None
        self.last_attempt: datetime | None = None
        self.lock = threading.Lock()  # guards by_stop, names, modes, fetched_at
        self.fetch_lock = threading.Lock()

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
            ref.upper() for s in self.cfg.stops for ref in s.ids
            if not hsl.is_gtfs_id(ref) and ref.upper() not in self.code_ids and ref.upper() not in self.unknown_codes
        }
        if not codes:
            return
        for code in sorted(codes):
            try:
                stop_id = hsl.resolve_code(code, self.cfg.digitransit_key)
            except hsl.HSLError as e:
                log.warning("could not look up stop code %s (%s); will retry", code, e)
                continue
            if stop_id:
                log.info("stop code %s is %s", code, stop_id)
                self.code_ids[code] = stop_id
            else:
                log.warning("unknown stop code %s (check it on the stop sign or reittiopas)", code)
                self.unknown_codes.add(code)
        try:
            self.codes_path.parent.mkdir(parents=True, exist_ok=True)
            self.codes_path.write_text(json.dumps(self.code_ids, indent=1, sort_keys=True))
        except OSError as e:
            log.warning("could not save the stop codes: %s", e)

    def _query(self, i: int) -> hsl.StopQuery:
        stop = self.cfg.stops[i]
        ids = tuple(
            ref if hsl.is_gtfs_id(ref) else self.code_ids[ref.upper()]
            for ref in stop.ids
            if hsl.is_gtfs_id(ref) or ref.upper() in self.code_ids
        )
        return hsl.StopQuery(ids, stop.routes)

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
            fetch = threading.Thread(target=self.refresh, args=(min_age_seconds,), daemon=True)
            try:
                fetch.start()
            except RuntimeError as e:  # no more threads (the service's limit): draw with what we have
                log.warning("could not start a departures fetch: %s", e)
                return
            fetch.join(wait_seconds)
            return
        # One fetch at a time. A refresh that arrives during a fetch (e.g. a
        # button press right after startup) waits for it, then finds it recent
        # and uses its result, instead of skipping and drawing with no data.
        with self.fetch_lock:
            now = datetime.now(UTC)
            if self.last_attempt and now - self.last_attempt < timedelta(seconds=min_age_seconds):
                return
            self.last_attempt = now
            self._fetch(now)

    def _fetch(self, now: datetime) -> None:
        # Never raises, like Weather.refresh: the refresher thread must survive.
        try:
            self._resolve_codes()
            queries = [self._query(i) for i in sorted(self.configured)]
            by_stop, names, modes = hsl.fetch(queries, self.cfg.digitransit_key, self.cfg.language)
        except hsl.HSLError as e:
            log.warning("departures fetch failed (%s); keeping last good data from %s", e, self.fetched_at)
            return
        except Exception:
            log.exception("departures fetch failed unexpectedly; keeping last good data from %s", self.fetched_at)
            return
        with self.lock:
            self.by_stop, self.fetched_at = by_stop, now
            self.names.update(names)
            self.modes.update(modes)
        # A code whose cached id HSL no longer knows (stops get renumbered) is
        # looked up afresh at the next poll, once per run.
        for code, stop_id in list(self.code_ids.items()):
            if stop_id not in by_stop and code not in self.rechecked_codes and any(
                code == ref.upper() for s in self.cfg.stops for ref in s.ids
            ):
                log.warning("stop %s (%s) is unknown to HSL; looking the code up again", code, stop_id)
                self.rechecked_codes.add(code)
                del self.code_ids[code]
        log.debug("departures updated: %d departures from %d stops", sum(map(len, by_stop.values())), len(by_stop))

    def stops(self, now: datetime) -> list[departures.StopDepartures]:
        with self.lock:
            by_stop, names, modes = self.by_stop, self.names, self.modes
        out = []
        for i, stop in enumerate(self.cfg.stops):
            if self.live and i in self.configured:
                query = self._query(i)
                out.append(departures.StopDepartures(
                    self._name(stop, query, names),
                    hsl.select(by_stop, query),
                    departures.ordered_modes(modes[sid] for sid in query.ids if sid in modes),
                ))
            else:
                name = stop.name or (stop.ids[0] if stop.ids else "—")
                out.append(departures.placeholder(name, i, now))
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
    img = render.render(cfg, scene(cfg, weather, transit))
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
<p>Behaves like the device: <span id="s">loading…</span>
<button id="b">Button: refresh now</button>
· <a href="/glance.png" style="color:#ccc">glance.png</a></p></div>
<script>
  let timer;
  async function load(refresh) {
    clearTimeout(timer);
    let r;
    try {
      r = await fetch('/glance.png' + (refresh ? '?refresh=1' : ''),
                      { cache: 'no-store', headers: { 'X-Glance-Client': 'preview' } });
      if (!r.ok) throw new Error(r.status);
    } catch (e) {  // server down or restarting: try again, as the device does
      document.getElementById('s').textContent = 'server not answering, retrying in 30 s';
      timer = setTimeout(() => load(refresh), 30000);
      return;
    }
    const sleep = parseInt(r.headers.get('X-Glance-Sleep') || '60', 10);
    const img = document.getElementById('g');
    const old = img.src;
    img.src = URL.createObjectURL(await r.blob());
    if (old) URL.revokeObjectURL(old);
    const at = new Date(Date.now() + sleep * 1000).toLocaleTimeString();
    document.getElementById('s').textContent = `next wake in ${sleep} s (${at})`;
    timer = setTimeout(() => load(false), sleep * 1000);
  }
  document.getElementById('b').onclick = () => load(true);
  load(false);
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
# As http.server's own log_message does: control characters arriving in a
# request line must not reach the log (or a terminal showing it) raw.
_UNSAFE_IN_LOG = re.compile(r"[\x00-\x1f\x7f-\x9f\\]")


# Sent by the device and the preview page with every image request; see do_GET.
CLIENT_HEADER = "X-Glance-Client"
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
    device_battery: battery.Battery | None = None,
) -> type[BaseHTTPRequestHandler]:
    prerendered = prerendered or schedule.Prerendered()
    device_battery = device_battery or battery.Battery()
    recent = schedule.Recent()

    class Handler(BaseHTTPRequestHandler):
        # A request must arrive within this many seconds of connecting (see
        # _RequestDeadline): each connection holds a thread, and without a
        # limit an idle or dripping one holds it for ever.
        timeout = 10

        def setup(self) -> None:
            super().setup()
            self.rfile.close()
            self.rfile = io.BufferedReader(_RequestDeadline(self.connection, self.timeout))

        def do_GET(self) -> None:
            self.connection.settimeout(self.timeout)  # for sending the answer
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
                refresh = trusted and query.get("refresh") == ["1"]
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
                if not refresh:
                    body = prerendered.get(plan.display_time, url.path)
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
                        plan.display_time.replace(second=0, microsecond=0), plan.interval_minutes,
                        transit.fetched_at, forecast.fetched_at if forecast else None,
                    )
                    body = recent.get(key, url.path, lambda: render.render(cfg, scene(
                        cfg, weather, transit, plan.display_time, device_battery, plan.interval_minutes,
                    )), encode)
                # The device counts its sleep from when the answer arrives, so
                # take off what the wait for departures and the drawing took.
                sleep = max(1, round(plan.sleep_seconds - (time.monotonic() - planned_at)))
                self._send(200, content_type, body, {"X-Glance-Sleep": str(sleep)})
            elif url.path == "/":
                self._send(200, "text/html; charset=utf-8", PREVIEW_HTML.encode())
            else:
                self._send(404, "text/plain", b"not found\n")

        def _send(self, code: int, ctype: str, body: bytes, headers: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *a) -> None:
            log.info("%s %s", self.address_string(), log_safe(fmt % a))

    return Handler


def cmd_serve(cfg: Config, args: argparse.Namespace) -> None:
    weather, transit = Weather(cfg), Transit(cfg)
    prerendered = schedule.Prerendered()
    device_battery = battery.Battery(cfg.battery_full_mv, cfg.cache_dir / "battery.csv")
    stop = threading.Event()

    def refresher() -> None:
        # Departures: once at start, then just before each minute the device
        # wakes for (per the schedule), so they are fresh when it arrives; then
        # draw that minute's image so the device doesn't wait for it.
        # The refreshes handle their own failures; the loop still guards each
        # round, because if this thread ever ended, the process would live on
        # with no polling and no pre-drawn images, and nothing would restart it.
        transit.refresh()
        while not stop.is_set():
            try:
                # Right after a poll, so a slow answer can't delay the next one.
                weather.refresh()  # checks its own 15-minute interval
            except Exception:
                log.exception("weather refresh failed")
            now = datetime.now(cfg.tz)
            next_poll = schedule.next_poll(now, cfg.departures_poll_lead_seconds)
            if stop.wait(max(0.0, schedule.seconds_between(now, next_poll))):
                break
            try:
                minute = schedule.next_boundary(datetime.now(cfg.tz), 1)
                if not cfg.wake_schedule.is_wake(minute):
                    continue  # the device sleeps through this minute: no polling, no drawing
                transit.refresh(min_age_seconds=cfg.departures_min_refresh_seconds)
                started = time.monotonic()
                interval = cfg.wake_schedule.interval_at(minute)
                img = render.render(cfg, scene(cfg, weather, transit, minute, device_battery, interval))
                images = {path: encode(img) for path, (encode, _) in render.FORMATS.items()}
            except Exception:
                log.exception("pre-rendering failed; requests will render on demand")
                continue
            prerendered.put(minute, images)
            log.debug("pre-rendered %s in %.1f s", minute.strftime("%H:%M"), time.monotonic() - started)

    threading.Thread(target=refresher, daemon=True).start()
    Handler = make_handler(cfg, weather, transit, prerendered, device_battery)

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
