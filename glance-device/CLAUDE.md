# glance-device

Arduino firmware for the reTerminal E1001, built with PlatformIO (`src/main.cpp`). On every wake it reads the battery, connects, fetches the image the server drew, refreshes the panel and deep-sleeps for as long as the server says. The architecture and the rules are in the root `CLAUDE.md`.

## Building and flashing

mise tasks here: `mise run build`, `mise run upload` (reTerminal on USB-C; it works while the device sleeps, since the USB bridge resets it into the bootloader) and `mise run monitor`. `mise install` sets up Python, `uv` and PlatformIO. Wi-Fi and the server's URL are in `include/secrets.h` (git-ignored; its template is beside it). Use the server's IP there: resolving a `.local` name costs time awake.

- `platformio.ini` pins the platform (`espressif32@8.0.0`), Seeed_GFX by commit, and the serial port pattern (`/dev/cu.usbserial-*`, the CH340 bridge; without it PlatformIO picked another port).
- Opening the serial port resets the device, so a log starts with "power on / reset". Log everything, not only our `[...]` lines, or crash reports are hidden.
- For a scripted log, mise's PlatformIO venv has pyserial; its interpreter is in the first line of `$(mise which pio)`. To reset the device without entering the bootloader, open the port with DTR and RTS off, then pulse RTS for 0.15 s.

## Hardware facts

- A XIAO ESP32-S3 with PSRAM and a UC8179 7.5" panel. Display SPI: SCK 7, MOSI 9, CS 10, DC 11, RST 12, BUSY 13.
- Logs and flashing go through an on-board USB-to-serial bridge on UART1 (GPIO43 TX, GPIO44 RX): `Serial1`, not USB CDC.
- Battery: drive GPIO21 high, read the ADC on GPIO1, double it.
- Buttons, pressed = low (checked on the device; Seeed's ESPHome page has them wrong): left white GPIO5, middle white GPIO4, right green GPIO3.
- Buzzer: GPIO45, sounded with a square wave from LEDC (checked on the device). GPIO45 is a strapping pin (flash voltage, low by default), so it is driven low when silent and held low through deep sleep (`gpio_hold_en` and `gpio_deep_sleep_hold_en`; released at boot). Wakes after a held sleep were seen to boot normally; the sleep current with the hold has not been measured.

## Drawing

Seeed_GFX (Seeed's TFT_eSPI fork), selected with `-DBOARD_SCREEN_COMBO=520`. Four greys come with `epaper.initGrayMode(GRAY_LEVEL4)`: a 4-bit frame buffer, `TFT_GRAY_0` (black) to `TFT_GRAY_3` (white). `epaper.update()` is a full refresh and wakes and sleeps the panel itself.

The server's `.g2` (2 bits per pixel, first pixel in the top bits, 0 = black) is unpacked straight into that buffer (2 pixels per byte, left pixel in the high nibble) as it downloads. Decoding a PNG took 2.2 s; this takes 0.2 s including the download.

Panel power: `begin()` switches the panel on, and the library switches it off only at the end of a refresh. When there is nothing to draw, `panel_off()` does it (`wake()`, then `sleep()`: the library's `sleep()` does nothing until the panel has been woken once). It takes about 1 s.

## An update, and what it costs

Wi-Fi 1.9 s (WPA2 and DHCP; the remembered channel and BSSID barely help), download 0.2 s, the full 4-grey refresh 6.2 s (the panel's time; the chip only waits), about 8.7 s awake in all. Wi-Fi is off during the refresh. Afterwards the device sleeps for `X-Glance-Sleep` less the time since the answer arrived, and wakes on time to the second.

The battery is read before Wi-Fi starts, since the radio makes the voltage sag (8 samples), and sent with every request.

## Buttons

- Any button wakes the device (ext1, any-low, internal RTC pull-ups with the RTC peripheral domain kept on; a button that is already down is left out of the mask). The button pins need `rtc_gpio_deinit` at boot after such a wake.
- Left white sends `?button=prev`, middle white `?button=next`, green `?button=refresh`.
- A press during an update is caught by an interrupt and gets its own update right after (`pending_button` in RTC memory, a 0.5 s sleep; the last press wins).
- On a deck of one screen (`X-Glance-Deck-Size`, kept in RTC memory) a white press goes straight back to sleep until the planned wake (`wake_due_ms`, on the RTC clock, which keeps counting through deep sleep). A `204` answer means nothing to draw.
- A failed attempt carries its button to the retry only if the server never answered, or if it was a refresh: a step the server already made would otherwise be made twice.
- A press that will change the screen gets a sound at once, since the new image takes about 10 s: one beep for the green button (2 kHz, 70 ms), a shorter, higher tick for the white ones (3 kHz, 25 ms), both at half duty. On a button wake it plays after the battery reading and before the panel and Wi-Fi start, about 50 ms after the firmware starts (the log's "since boot"; the boot before it, from the press, isn't counted and hasn't been measured). A press during an update gets its sound when that update ends (`setup()` is waiting in the panel's refresh until then), and the update it asked for is silent. Silent: timed wakes, retries, a white press on a deck of one, an empty battery. The same sound at all hours, since a press is always someone's own (the founder's choice, GL-3).

Lessons:

- The interrupt handler must be IRAM-safe: no `digitalRead()` or `millis()`. That crashed the chip when a press met Wi-Fi's flash access.
- Use falling edges, never the pin's level: the display driver blocks interrupts for long stretches, so the handler can run after the button is released.
- The button that woke the device is ignored for its first second (its release bounces).

## The offline mark

The device draws one mark itself, as a white badge at the right end of the status bar (x 704–800, over the battery level), with a black-and-white partial refresh (~1.1 s) that leaves the rest of the image alone: **offline** (a crossed-out cloud), from the third failed update in a row, and at once when a press fails, since the press's sound promised a change; once per outage. The next full refresh replaces it. There is no text because the built-in fonts have no ä or ö.

A press draws no mark of its own: its sound says it was heard (GL-3), which costs no partial refresh and doesn't flash the corner at every press. A press answered `204` leaves the panel as it is, unless the offline mark is up: then the image is fetched once more, as a timed wake would, to replace it.

Partial refresh works only in 1-bit mode (`deinitGrayMode()`). `push_mark()` runs the library's own sequence and also sends the inverse of the new image as the "old" one: the controller drives only the pixels that differ between the two, and what it held as "old" after a 4-grey refresh left smoothed edges undriven, which made the mark smudgy. It bypasses the library's sleep flag on purpose.

## When things go wrong

- Failed updates (Wi-Fi or server) are counted in RTC memory. Retries back off 30 s, then 1, 2, 5, 10 and 15 minutes, and return to normal on the first success. A button press retries at once. A failed attempt costs about 2 s awake, plus 1 s to power the panel down.
- Timeouts: Wi-Fi 10 s (after 5 s at the remembered access point), HTTP connect 4 s, HTTP 10 s. The HTTP client's 10 s limits each wait for more of the headers, not the whole of them: a server sending its headers a byte every 4 s held the device until the awake limit ended it. Once the headers are in, the image must arrive within what is left of 10 s from the start of the request, connecting included.
- `X-Glance-Sleep` is capped at 25 hours (`MAX_SLEEP_S`). After two sleeps of 10 s or less in a row the device sleeps 60 s instead: a server fault answering "1 s" each time would refresh the panel until the battery was flat.
- Awake limit: a one-shot timer started first thing in `setup()` (90 s; the worst normal update is under a minute) forces deep sleep with the backoff, counting its own hits in a row. Without it, anything that never returns keeps the device awake, and the display library waits for the panel's BUSY line with no timeout. Its callback runs in the esp_timer task beside the stuck `setup()`, so it only sets the wake-ups and sleeps.
- Empty battery: a reading under 3300 mV (and over 0) skips the update before the panel and radio are powered, and looks again in 15 minutes. The threshold is a guess from the server's curve; whether the board protects the cell itself is unknown.
- If the frame buffer wasn't allocated, the update counts as failed.
- To try these paths again after a change: a throwaway server that answers as needed (on the server's port with the real service stopped, or anywhere else with a test build whose `GLANCE_URL` points at it, which leaves the real service running), slow headers for the awake limit, `204` with a short or a too-long `X-Glance-Sleep`, `X-Glance-Deck-Size: 1`; nothing listening, for a press whose first attempt fails; and a test build with the threshold raised for the empty battery. Watch the serial log on USB.

## Not yet tried on the device

- A press whose request the server answered but whose image then failed: the retry should not carry a white button (the server already stepped), and should carry the green one.
- A frame buffer that wasn't allocated (it needs a test build).
- A white press answered `204` while the offline mark is up (the deck shrank to one screen during an outage): the device should fetch the image again to replace the mark.
- The awake limit's sleep with the buzzer hold.
- The deep-sleep current with GPIO45 held low: GL-9's logged discharge is what would show a drain.
