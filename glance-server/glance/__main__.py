"""Command line entry point.

    python -m glance render [-o glance.png] [--offline] [--lang fi] [--screen NAME]
    python -m glance serve [--port 8080]
    python -m glance stops "Pasila"     # find Digitransit stop ids
"""

from __future__ import annotations

import argparse
import io
import ipaddress
import logging
import re
import socket
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import battery, decks, render, schedule, sun
from .config import Config, load
from .sources import Sources, hsl

log = logging.getLogger("glance")


def scene(
    cfg: Config, sources: Sources, now: datetime | None = None,
    device_battery: battery.Battery | None = None, interval_minutes: int = 1,
) -> render.Scene:
    now = now or datetime.now(cfg.tz)
    transit = sources.transit
    # Before the data: if a refresh lands in between, the status bar warns
    # about data a moment older than drawn, rather than missing a warning.
    warnings = sources.warnings(now, cfg.language)
    return render.Scene(
        now=now,
        language=cfg.language,
        stops=transit.stops(now),
        forecasts=sources.weather.forecasts(),
        places_not_found=sources.weather.not_found(),
        departures_live=transit.live,
        departures_fetched_at=transit.fetched_at,
        departures_stale_after=transit.stale_after,
        battery_percent=device_battery.level(now) if device_battery else None,
        departure_clock=interval_minutes > 1,
        warnings=warnings,
        spot_prices=sources.electricity.prices,
        sun=sun.day(cfg.lat, cfg.lon, now.date(), cfg.tz),
    )


def refresh_periodic(sources: Sources, force: bool = False) -> None:
    """Every interval source's refresh at once, each in a thread of its own, so
    the round takes as long as the slowest answer rather than all of them: a
    slow FMI with several places must not push the refresher past the next
    departures poll. Each refresh checks its own interval (unless forced) and
    handles its own failures; never raises, since the refresher thread must
    live on."""
    def refresh(source) -> None:
        try:
            source.refresh(force)
        except Exception:
            log.exception("%s refresh failed", source.WHAT)

    started = []
    for source in sources.periodic:
        thread = threading.Thread(target=refresh, args=(source,), daemon=True)
        try:
            thread.start()
        except RuntimeError as e:  # the service's task limit (TasksMax) reached
            log.warning("%s refreshed without a thread of its own: %s", source.WHAT, e)
            refresh(source)
        else:
            started.append(thread)
    for thread in started:
        thread.join()


def cmd_render(cfg: Config, args: argparse.Namespace) -> None:
    sources = Sources.make(cfg)
    if not args.offline:
        refresh_periodic(sources, force=True)
        sources.transit.refresh()
    if args.screen and args.screen not in cfg.screens:
        raise SystemExit(f"no screen {args.screen!r}; the screens are: {', '.join(cfg.screens)}")
    sc = scene(cfg, sources)
    img = render.render(cfg, sc, args.screen, cfg.deck_at(sc.now))
    Path(args.output).write_bytes(render.to_png(img))
    print(f"wrote {args.output}")


def cmd_stops(cfg: Config, args: argparse.Namespace) -> None:
    key = cfg.source_settings.transit.key
    if not key:
        raise SystemExit("No Digitransit key: add [digitransit] key = \"...\" to config.local.toml")
    try:
        print(hsl.format_search(hsl.search(args.name, key)))
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
    cfg: Config, sources: Sources, prerendered: schedule.Prerendered | None = None,
    device_battery: battery.Battery | None = None, selection: decks.Selection | None = None,
) -> type[BaseHTTPRequestHandler]:
    prerendered = prerendered or schedule.Prerendered()
    device_battery = device_battery or battery.Battery()
    selection = selection or decks.Selection()
    recent = schedule.Recent()
    transit = sources.transit

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
                    # (Interval sources such as the weather are left to the
                    # refresher, which checks them every minute; fetching them
                    # here could keep the device waiting.)
                    transit.refresh(transit.settings.min_refresh_seconds, wait_seconds=UPSTREAM_WAIT)
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
                    # Drawn one at a time and shared for a few seconds (see
                    # schedule.Recent), unless the minute or the data changed.
                    key = (
                        plan.display_time.replace(second=0, microsecond=0), plan.interval_minutes, screen,
                        *sources.data_times(),
                    )
                    body = recent.get(key, url.path, lambda: render.render(cfg, scene(
                        cfg, sources, plan.display_time, device_battery, plan.interval_minutes,
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
    sources = Sources.make(cfg)
    transit = sources.transit
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
            try:
                refresh_periodic(sources)
            except Exception:
                log.exception("refreshing the interval sources failed")
            now = datetime.now(cfg.tz)
            next_poll = schedule.next_poll(now, transit.settings.poll_lead_seconds)
            if stop.wait(max(0.0, schedule.seconds_between(now, next_poll))):
                break
            try:
                minute = schedule.next_boundary(datetime.now(cfg.tz), 1)
                if not cfg.wake_schedule.is_wake(minute):
                    continue  # the device sleeps through this minute: no polling, no drawing
                transit.refresh(transit.settings.min_refresh_seconds, wait_seconds=POLL_WAIT)
                started = time.monotonic()
                interval = cfg.wake_schedule.interval_at(minute)
                # The screen that minute will show: the selected one, or the
                # start of another deck if the schedule switches at that minute.
                # Then its neighbours, for a button press that changes screens.
                deck = cfg.deck_at(minute)
                showing = selection.peek(deck)
                sc = scene(cfg, sources, minute, device_battery, interval)
                for screen in (showing, *deck.neighbours(showing)):
                    img = render.render(cfg, sc, screen, deck, forgiving=True)
                    prerendered.put(minute, screen, {path: encode(img) for path, (encode, _) in render.FORMATS.items()},
                                    data=sc.departures_fetched_at)
            except Exception:
                log.exception("pre-rendering failed; requests will render on demand")
                continue
            log.debug("pre-rendered %s in %.1f s", minute.strftime("%H:%M"), time.monotonic() - started)

    threading.Thread(target=refresher, daemon=True).start()
    Handler = make_handler(cfg, sources, prerendered, device_battery, selection)

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
