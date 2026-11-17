# glance-server

Python 3.13 and Pillow. Fetches weather and departures, draws the screen, serves it; deployed to the Pi as a systemd user service. The architecture and the rules are in the root `CLAUDE.md`.

## Running

mise tasks (`mise tasks` lists them; mise makes and activates `.venv`):

```bash
mise install && mise run setup   # Python 3.13, .venv with Pillow
mise run test
mise run serve                   # http://localhost:8080/, with a preview that behaves like the device
mise run render                  # one image to glance.png (-- --screen NAME, --offline, --lang fi)
mise run stops Pasila            # find stop codes
mise run deploy                  # the tests, then deploy to $GLANCE_PI (set in mise.local.toml)
mise run screenshot              # ../docs/screenshot.png, from sample data
mise run icons                   # rebuild the icon PNGs (macOS only)
```

On the Pi: `journalctl --user-unit glance -f` for the log (`journalctl --user -u` finds nothing there), `systemctl --user restart glance`.

## Code

- `glance/__main__.py`: the command line (`render`, `serve`, `stops`; `-v` logs every fetch), the request handler and the refresher thread. It fetches nothing itself.
- `glance/sources/`: one module per data source, each holding its API client, its cache and its latest good data: `fmi.py` (`Weather`, and what a forecast is) and `hsl.py` (`Transit`, and what a departure is, with the placeholder departures). `Sources` (in `sources/__init__.py`) makes them once per process and is what the commands, the refresher, the request handler and `scene()` are handed. `periodic.py` is what a source refreshed every so many minutes shares (`Weather` now; spot prices and others next): the interval, keeping the last good answer, retrying sooner after a failure, the disk cache. `glance/upstream.py`: how every source reads an answer; a test fails if a source opens a connection any other way.
- `glance/schedule.py`: wake times, the moment an image is drawn for, images drawn ahead (`Prerendered`) and shared (`Recent`).
- `glance/config.py`: `config.toml` and `config.local.toml`, checked at load.
- `glance/layout.py`, `glance/decks.py`, `glance/widgets/`: screens, decks, widget types.
- `glance/render.py`, `glance/draw.py`, `glance/scene.py`, `glance/i18n.py`: drawing. `draw.py` imports nothing of ours, so widgets can use it without import cycles; it also holds the pen small icons are drawn with (`Pen`). `i18n.py` holds the formats for dates, times, ages and percentages; words belong to whoever shows them, each part keeping its own `WORDS` in English and Finnish (a widget, a source, the status bar).
- `glance/battery.py`: the battery level.
- `deploy/`, `tools/` (icons, screenshot), `tests/`.

## Data

**FMI** (`sources/fmi.py`): open data WFS, no key. Stored query `fmi::forecast::edited::weather::scandinavia::point::simple`, hourly Temperature, WindSpeedMS, WindDirection, PoP, WeatherSymbol3 and FeelsLike, from local midnight (so today's high and low cover the whole day; FMI returns past hours) to now + 36 h. Refreshed at most every 15 minutes by the refresher thread, never in a request; a failed fetch is retried after 2 minutes. Cached in `cache/forecast.json` with the parameters it was fetched with, so a cache from before a parameter was added is refetched at start.

**HSL** (`sources/hsl.py`): Digitransit routing API v2 (`https://api.digitransit.fi/routing/v2/hsl/gtfs/v1`), key in the `digitransit-subscription-key` header (from `config.local.toml` or `GLANCE_DIGITRANSIT_KEY`).

- One query for every stop on every screen: `stops(ids:)` → `stoptimesWithoutPatterns`, which leaves out cancelled trips and drop-off-only stops. A stop's name and vehicle mode (for its pictogram) come with it.
- Departure time = `serviceDay + realtimeDeparture`, kept as an absolute time, so cached departures keep counting down.
- Stops are configured by the code on their sign (`H0665`). The routing API searches names only, so codes are resolved once through the geocoding API (`/geocoding/v1/search`, source `gtfshsl`, exact `addendum.GTFS.code`) and cached in `cache/stop_codes.json`.
- A widget may merge several stops (both platforms) and filter routes.
- The " via …" part of a headsign is dropped so destinations fit.
- Trunk routes are GTFS route type 702 (701 is a city bus, 700 regional); the API's colour fields are empty.
- Without a key, or for a widget without codes, placeholder departures are shown.

Both clients were verified against the live APIs; `tests/data/` has a real answer from each.

**Reading an answer** (`upstream.py`): with a deadline for reading the whole answer, headers included (Digitransit 10 s, FMI 20 s; urllib's own timeout only limits each read, so every read from the connection waits at most what is left of the deadline; connecting, with its TLS handshake, gets what is left once per address tried, and the name lookup is not limited), in pieces up to a 2 MB limit, and with redirects refused (urllib would send the key on to the redirect's target).

**Battery** (`battery.py`): the device sends its voltage; a LiPo curve turns it into a percentage, drawn with an icon in the status bar, with a "Charge battery" badge at 15 % or less. A report is ignored if implausible or over 2 hours old. `[battery] full_mv` is what this device reads when full (a real one reads below the curve's 4.20 V); readings are shifted by the difference. Every report is appended to `cache/battery.csv` (about 600 lines, 18 KB a day), and its last line restores the level after a restart.

## When things go wrong

- `Weather.refresh` and `Transit.refresh` never raise: every way a fetch fails becomes `FMIError` or `HSLError`, or is caught and logged, and the refresher loop guards each round as well. (One unexpected answer once ended the refresher thread for good, with nothing to restart it.)
- The last good data stays. The right of the status bar is empty while data is fresh; weather over 45 minutes old or departures over 5 minutes old get an inverted badge ("Departures 12 min old"). Each source says when its data counts as old or missing and in what words (`warning()`), and the status bar lists what they say, then its own "Charge battery"; and stale realtime minutes are drawn grey like timetable ones.
- An answer with none of the stops asked for is a failed fetch, not "no departures". When no stop can be asked for (its codes not looked up yet), nothing counts as fetched.
- A stop code HSL doesn't know is looked up once per run. A cached id HSL no longer knows (stops get renumbered) is looked up again once per run, and keeps its old id until a new one comes (`Transit.stale_codes`), so departures kept from before can still be found.
- Data that can't be drawn is stopped twice. The parsers take times without a zone as UTC, leave out hours and departures dated outside 2000–2099, symbol codes outside 0–999 and NaN or infinite values, cut HSL text to 80 characters and refuse fields that aren't text. And the server draws with `render(..., forgiving=True)`: a widget that raises leaves its area empty and a traceback in the log. Tests and `render` on the command line stay strict, so the deploy's pre-check still catches mistakes.
- Rate limits run on `time.monotonic()`: the Pi has no clock of its own, and a clock set back must not hold off fetches.

## Wake schedule and timing

- `[schedule]` has `wake_interval_minutes`, `default_deck` and `[[schedule.rules]]` (`days`, `from`, `to`, optional `every` and `deck`). The first matching rule wins; `days` are of the moment itself; `to` is exclusive; a rule may run past midnight (`schedule.py`: `WakeRule`, `WakeSchedule`). A minute is a wake if it is a multiple of its own rule's interval since midnight, so a period's first minute is hit exactly.
- `X-Glance-Sleep` aims `wake_offset_seconds` (2) after the next wake minute, less the time the request took: the device counts from when the answer arrives.
- A request shortly before a wake minute is drawn for that minute and sleeps to the one after. The device's timer runs about 1 % fast, so the margin is 3 % of the interval that ends at the wake, at least 10 s.
- With an interval over a minute, departures show clock times (14.32) instead of countdowns (`Scene.departure_clock`), which would go stale.
- All stepping and subtracting of times goes through UTC (`_step`, `seconds_between`): wall-clock arithmetic went wrong in the repeated autumn hour.
- The refresher polls departures `poll_lead_seconds` (15) before each wake minute, waits up to 10 s for the answer (`POLL_WAIT`), then draws that minute's image for the showing screen and its neighbours in the deck (`Prerendered`). That image is served to requests up to 20 s into the minute, to a screen change anywhere in the minute, and not at all once newer departures have arrived.
- Other requests (the preview, a late device) refetch departures over 60 s old; a button refresh refetches at most every 10 s (`min_refresh_seconds`). They wait at most 4 s (`UPSTREAM_WAIT`), then draw with the last good data: the device gives up after 10 s.
- One departures fetch runs at a time, and everyone who waits for it shares one background thread (`Transit.background`). A thread each meant that a fetch which never ended collected a stuck thread per request, up to the unit's limit.
- Drawing on demand happens one at a time and is shared for 5 s (`Recent`), keyed by the minute, the screen and the data's fetch times.

## Config

`config.toml` is the documented example (Pasila). `config.local.toml` (git-ignored) overrides it: sections merge, lists are replaced whole, and the screen setup (`[widgets]`, `[screens]`, `[decks]`, `[schedule]`) comes from the local file as a group if it has any of them (`config.SETUP`), so no example leaks into a real setup. Everything is checked at load (types, ranges, names that must exist, unknown settings, widget sizes), so a mistake stops the deploy's pre-check instead of breaking drawings later. Sections that were renamed (`[[stops]]`, `[weather]`, `[layout]`, `[[device.schedule]]`) fail with a pointer to the new ones.

## Screens, decks and widgets

- A screen (`[screens.<name>] grid`) is a picture of widget names, like CSS `grid-template-areas`: words per line are columns, lines are rows, each name fills a rectangle, `.` is empty. Cells share the 800×450 px below the status bar equally (`Layout.rect`).
- A widget (`[widgets.<name>]`) has a `type`, named for its look. The type knows its data provider, so the config never names one. The types are `fmi_weather_now`, `fmi_weather_hourly` and `hsl_stops`.
- Each type is one module in `glance/widgets/` holding everything about it: `TYPE`, `SETTINGS`, `parse()`, its fill rule and `draw()`. `widgets/__init__.py` lists them by hand; adding a type is a module and a line there. Drawing every widget can use goes in `draw.py`, never in another widget's module. Pictures one widget draws stay in its module (HSL's mode pictograms in `hsl_stops.py`, the wind arrow in `fmi_weather_now.py`); pictures a family of widgets shares get a module named for them (`fmi_symbols.py`, for the two FMI weather widgets). A widget never fetches: its data comes from a source in `glance/sources/` through the scene, and a new provider is a new module there (`Periodic` if it is refreshed on an interval), a field in `Sources` (and in its `periodic` list) with a `warning()` for the status bar, and a field in `Scene` with its line in `scene()`.
- Widgets fill their rectangle: as many hourly columns as fit at 41 px, as many departure rows as fit at 32 px. Current weather keeps its one design, at the top left of a bigger area.
- Minimum sizes are in pixels, since the same cells are a different size in another grid (`MIN_SIZE`: current weather 245×140, hourly 139×130, stops 200×72). They were chosen by drawing worst-case data. A grid that gives a widget less fails at load, saying what to change ("give it 3 columns"). `WidgetSizeTest` draws every type with worst-case data at several sizes and fails if anything lands outside its rectangle. On the panel at those sizes (250×141, 139×131 and 200×72, which 16×16, 23×31 and 4×25 grids give), each read well with real data; the worst-case data was drawn only in the tests.
- A deck (`[decks.<name>]`) is an ordered list of screens with a `start`; without decks, all screens form one. The schedule picks the deck. `decks.Selection` (in memory, shared by the device and the preview) keeps the showing screen across wakes, and starts over only when the schedule switches decks or the server restarts.
- `?button=prev|next` step through the deck, wrapping around. On a deck of one the answer is `204` with `X-Glance-Sleep`. Answers carry `X-Glance-Deck-Size` (the device skips a pointless press) and `X-Glance-Screen` (for the preview).

## The screen

Settled with the user on the real panel. The default is a 6×3 grid: current weather 2×1, the hourly forecast 4×1 (12 hours) and three stop widgets 2×2 (8 departures each).

- **Status bar** (30 px, black): date and time on the left; page dots in the middle on a deck of two or more (the showing screen filled; "3/12" above 8 screens); on the right the battery level, and a warning badge only when something is wrong: what the sources report, then a low battery. No "updated" time: data fetched just before the minute made it read a minute behind.
- **Current weather:** icon, a big temperature with today's high over low right after it (the temperature shrinks rather than overlap them), the condition, then "feels like" and wind.
- **Hourly forecast:** temperature, icon, chance of precipitation (every hour, 0 % too, since a gap looked like an error; rounded to 10 %), hour.
- **Stops:** HSL's mode pictograms and the stop name, then per departure a route badge (filled black for a tram, outlined for a bus, light grey with an outline for a trunk bus; dark grey was too close to black on the panel), the destination and the minutes (black for realtime, dark grey for timetable only).
- The device draws its own marks over the right end of the bar (x 704–800): only the battery level belongs there.

Design rules learned on the panel:

- Only four greys; antialiasing is snapped to them. Light grey works for large fills, not for small text.
- Icons need bold strokes: hairlines and coloured details vanish, most of all at 36 px.
- Route numbers and minutes are read first: when space is tight, shrink the destination.
- Long names shrink to a minimum size, then end in "…".
- Temperatures use a real minus sign (−3°) and never overlap the icon.

Icons and fonts:

- Weather icons are FMI's own WeatherSymbol3 symbols, adapted for four greys by `tools/build_fmi_icons.py` (thicker outlines, a grey sun, black lightning and snowflakes) and committed as PNGs at 76 and 36 px, so the Pi never renders SVG. The hand-drawn symbols in `widgets/fmi_symbols.py` are only the fallback for a code without a PNG.
- Stop headers use HSL's mode pictograms, cropped and scaled to 28 px by `tools/build_hsl_icons.py` (about the most the header fits).
- Both tools render with macOS Quick Look (always at 304 px, since smaller thumbnails come out at the wrong scale; ImageMagick lost the outlines) and fetch a missing source from a pinned commit.
- Fonts: bundled Inter 4.1.

The README screenshot is drawn from sample data by `tools/make_screenshot.py`: a winter weekday morning at Pasila, in English, with English stop names. If a second one is ever wanted, the user wants a summer one.

## Deploy

`deploy/deploy.sh`, always through `mise run deploy`:

1. rsyncs the code to `~/glance-server` and copies `config.local.toml` with mode 600;
2. makes a venv with `--system-site-packages` and compiles the modules (the sandboxed service can't);
3. draws one image with the new code and config; if that fails, the deploy stops and the old service is left running, but the new code and config are already in place, so the service's next restart loads them (an open fault, in the root file);
4. installs and restarts the systemd **user** service (no sudo; linger makes it start at boot);
5. fails if the service isn't up, or has restarted, 12 s later (the code takes ~4 s to load on the Pi). This is what catches a config that draws but can't serve, such as a port below 1024 (`PermissionError` in the log), but only after the restart: the service is then down, restarting every 10 s, until a good deploy (an open fault, in the root file). Its "restarts: 0" can show while the service is failing, between two attempts.

The unit waits up to 2 minutes for network time (the Pi has no clock of its own), then starts anyway; `TimeoutStartSec=150` keeps systemd's 90 s start limit from ending that wait as a failure. With the network out at boot, the service starts when the wait runs out and serves the cached weather, with no departures until the network is back. Until then the clock is the time saved at the last shutdown, behind by however long the Pi was off.

Choices made because the server is a Pi 1 (revisit on other hardware; no switches in advance): images are drawn ahead; `poll_lead_seconds` is 15 (5 would do on fast hardware); PNGs use `compress_level=6` (0.16 s against 1.1 s for `optimize=True`); Pillow is Debian's `python3-pil` (piwheels' ARMv6 wheel lacked system libraries), which is why `Pillow>=9.1` is a range and not a pin. To judge new hardware, time `render.render` and `render.to_png` there.

## Security measures

Within the model in the root file. Keep them when changing the code around them.

- **A request must arrive within 10 s** of connecting (`Handler.timeout`, `_RequestDeadline`): idle and dripping connections are dropped. A socket timeout alone only limits each read.
- **Home network addresses only** (`client_allowed`): public addresses get 403, so a port forwarded by mistake exposes nothing. `[server] allowed_networks` adds ranges (a VPN's). The server listens on IPv4 only.
- **Host header check** (`host_allowed`): IP addresses, single-label names, `.local`, localhost and `[server] hostnames` pass; others get 403. This stops DNS rebinding.
- **Changes need the `X-Glance-Client` header:** battery reports, refreshes and screen steps count only with it. A page elsewhere can make a browser send a plain GET, but not a header without a CORS preflight, which is never answered.
- **Bounded work:** drawing on demand is shared (`Recent`; the battery level is deliberately not in its key), the battery log takes one line per 30 s, there is one background fetch thread, and the unit has `TasksMax=64`.
- **Answers:** `Server: Glance`, `nosniff`, `X-Frame-Options: DENY`. Log lines are escaped (`log_safe`).
- **The unit is sandboxed** (`deploy/glance.service`): of the home directory it sees only its own folder, read-only except `cache/`; the system is read-only; no new privileges; system-call and address-family filters; `UMask=0077`; `Restart=always`. All of it works in a user unit without root. To try a new option, run a throwaway `systemd-run --user` unit on the Pi with the same properties. `MemoryMax` and `ProtectProc` are left out because they do nothing there. A `[cache] dir` elsewhere needs its own `BindPaths`.
- **Secrets:** none in the repository; `config.local.toml` is mode 600 here and on the Pi.

## Tests

`tests/test_glance.py`, run by `mise run test`, before every deploy, and before every push that changes this folder (`../.githooks/pre-push`, on once `mise run hooks` has run in the clone; it tests a copy of each pushed commit, not the working tree). `isolated_config` gives a test `config.toml` alone, a temporary cache and no key; fetches are replaced at `upstream._open`. The suite passes with outside connections blocked, in other time zones, and with `http_proxy` or the key variable set.

## Not yet tried

Built and tested, but not seen happening on the running system:

- The refusal of a public address. It needs a request from one, and was left untried on purpose: the tests cover it, and trying it means giving the Pi a public address.
- The battery curve's low end: `full_mv` shifts readings by a constant, which may not hold where the warning appears.
- Running since 2026-11-16 but not seen happening yet: a renumbered stop keeping its old id until the new one is found, and a widget that can't be drawn left empty (each logs a warning or an error when it happens); the parsers leaving out values they can't draw, and a departures fetch still running after a request stopped waiting for it (neither logs anything).
- The deploy's one-time setup check and its linger warning: trying them means undoing the Pi's setup.
