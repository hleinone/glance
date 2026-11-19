# Glance

A wall-mounted e-paper dashboard for a home in Helsinki: FMI weather, HSL public transport departures, Finland's electricity spot prices and today's sunrise and sunset. A small server draws the whole 800×480 screen as an image; the display wakes, downloads it, shows it and goes back to sleep.

This file is the overview and the rules. Each folder has its own `CLAUDE.md` with that part's details, measurements and reasons (Claude Code loads it when working in the folder): `glance-server/CLAUDE.md` and `glance-device/CLAUDE.md`. This installation's own details (hostnames, addresses, stops, schedule, times of day) are in `CLAUDE.local.md` (git-ignored).

## Hardware

- **Display:** Seeed reTerminal E1001: ESP32-S3, 7.5" e-paper, 800×480, four grey levels (0, 85, 170, 255), three buttons, battery powered. It runs this project's firmware in place of Seeed's SenseCraft firmware (Seeed's web flasher restores that).
- **Server:** a Raspberry Pi 1 Model B (ARMv6, one 700 MHz core), Raspbian 13, Python 3.13. It is slow: drawing one image takes ~2.2 s there against ~90 ms on the Mac, which is behind several choices in the server.
- **Development:** on a Mac, with tools from mise; each folder has its own `mise.toml` and tasks (`mise tasks`).

## Architecture (decided)

- **The server draws, the device shows.** The server fetches all data, draws the screen snapped to the four greys and serves it over HTTP on the home network: `/glance.g2` for the device (the panel's own 2-bit format, 96,000 bytes), `/glance.png` and a preview page at `/` for browsers. The firmware stays trivial and the radio is on for about 2 s per update.
- **Pull only.** The device runs on battery, and a deep-sleeping ESP32 can't receive anything: push or long polling would mean keeping Wi-Fi on.
- **The server decides when the device wakes.** Each image answer carries `X-Glance-Sleep: <seconds>`, aimed 2 s after the next wake minute of the schedule in the config (every minute at busy times, less often otherwise). The same schedule rules pick the deck of screens that is showing. Departures are polled 15 s before each wake minute and the image is drawn ahead, so the device is answered at once.
- **The device reports, the server interprets.** Every request carries the battery voltage (`?battery_mv=`) and, after a press, the button (`?button=prev|next|refresh`). The server turns the voltage into a percentage and keeps track of which screen is showing, so neither needs reflashing to change.
- **What the two share** (change one side and the other must follow, deployed and flashed together): the request's query and its `X-Glance-Client` header; the answer's `X-Glance-Sleep` and `X-Glance-Deck-Size` headers; `204` for "nothing to change"; the `.g2` format.
- **Ruled out:** SenseCraft's own data sources (simple JSON GETs only; FMI is XML, HSL is GraphQL with a key). ESPHome (its driver for this panel is black and white only). Keeping the deck's screens on the device so a press needs no Wi-Fi (they would go to flash at every wake: a few years of flash life, and every wake paying for a rare press).

## Rules

How this project is worked on. The folders' files have the specifics; these hold everywhere.

1. **Nothing about this installation in a tracked file.** Hostnames, addresses, user names, the real stops, coordinates and schedule, the state of the household's machines, and the time of day of anything it did all go in `CLAUDE.local.md` or the git-ignored local config (`config.local.toml`, `mise.local.toml`, `secrets.h`). Tracked examples, tests and the screenshot use Pasila. Wording counts too ("night wakes" gives a time away). Before a commit, look for such details in what changed.
2. **Say what was checked, and how.** "Works" means it ran: against the live API, on the Pi, on the device. What is built but hasn't run where it matters goes in that folder's "Not yet tried" list, and comes off when it has.
3. **Fail towards the last good state.** A failed or odd fetch keeps the last good data and shows its age; a bad config stops the deploy, not the running service (not yet true of every bad config: see Where it stands); a failed update on the device costs a retry, not the battery. No error may end a background thread or leave the device awake.
4. **Check at the edges.** Whatever comes from outside (config values, API answers, request parameters, the server's answer on the device) is checked where it enters, with a message that says what to change. Text and numbers from an API are data to draw, never paths or commands.
5. **Battery first on the device.** Every second awake and every panel refresh costs; whatever can be decided on the server is decided there.
6. **Few dependencies, pinned.** The server needs only Pillow (Debian's package on the Pi). The firmware's platform and library are pinned by version and commit; raise them deliberately.
7. **A fix comes with a test that fails without it**, and tests depend on nothing of this machine: no local config, network, real cache, proxy or time of day.
8. **Deploy only with `mise run deploy`** (it runs the tests first); flash with `mise run upload`. Every push that changes the server runs its tests first and is stopped if one fails (`.githooks/pre-push`, switched on per clone with `mise run hooks` in `glance-server/`); don't skip it with `--no-verify`.
9. **Third-party files keep their licence.** Fonts, icons and saved API answers get a line in `THIRD_PARTY.md` and keep their licence files beside them. The HSL pictograms are copyleft (EUPL/AGPL), confined to their own files; don't imply this is an HSL product. The project's own code is MIT (`LICENSE`).
10. **Write down decisions, not the story.** A decision goes in the `CLAUDE.md` of its part, with its date and reason where they matter; how it was reached is in git. Replace what is no longer true instead of adding to it. Ideas go in `IDEAS.md` (`IDEAS.local.md` for ones naming the household's services), are read only when ideas are asked about, and come out when built.
11. **The README is the public face.** Keep its claims true when behaviour changes, and redraw its screenshot (`mise run screenshot`, from sample data) when the screen design does.

## Security model

A home network: the server has no login and speaks plain HTTP, by choice (its client is a deep-sleeping ESP32, and the dashboard holds nothing worse than which stops are near home and when the household is up). It must not be reachable from the internet. The measures taken within that are listed in `glance-server/CLAUDE.md` (Security measures); keep them when changing the code around them.

Accepted, not fixed: anyone on the home network can view the dashboard, send a false battery level or a refresh (by adding the client header) or keep the service's threads busy; something answering at the server's address could feed the device its own image; the Wi-Fi password can be read from the device's flash over USB (flash encryption burns eFuses for good). Fixing those needs a shared secret or TLS on both sides, left out to keep the firmware trivial. A guest or IoT network for the device is the answer if it matters.

## Where it stands (2026-11-19)

Running: the server on the Pi as of 5a0543e (night icons in the weather widgets, GL-6), deployed 2026-11-19; the device's next request after it was answered with no warning. The firmware on the device as of a026594 (a sound on a press in place of the refreshing mark, GL-3), flashed 2026-11-18; its paths were seen on the device with a stand-in server, except the two its review added, which are in the device's "Not yet tried" with the sleep current. What neither part has seen yet is in their lists.

Open:

1. Battery: GL-3 was M1's last planned flash, so a full discharge can now be logged in `cache/battery.csv` on the Pi (flashing over USB charges the cell). Once it is, fit the percentage curve from it, or at least check the low end, where the warning appears.
2. A failed deploy can leave the service down until a good one (GL-11). One refused by its check has already put the new code and config on the Pi, so the service's next restart loads them; one whose service doesn't stay up has already restarted it with them. Both seen on the Pi.
3. The wait for network time leaves its `sleep` behind when it runs out, and systemd logs "Found left-over process" as the service starts. Harmless (GL-12).
4. A weather cache whose `fetched_at` has no time zone stops weather fetches until the file is deleted: each refresh fails comparing it with the clock, outside the part that catches fetch failures (GL-14). Only a damaged or hand-edited file has one; the source always writes the time zone.
5. The paths in the two "Not yet tried" lists.
6. Maybe: a fixed device IP to skip DHCP (~1 s per wake); hiding departures that leave before the next wake.
