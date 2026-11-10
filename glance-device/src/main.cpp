// Glance firmware: on every wake, read the battery, connect to Wi-Fi, fetch the
// image the server renders, draw it in the panel's 4 greys, then deep-sleep
// for as long as the server says (X-Glance-Sleep). Each wake starts from
// setup(). A press on any of the three buttons wakes it early and asks the
// server for fresh data (?refresh=1). The battery voltage goes along with
// every request (?battery_mv=), and the server draws the level. An update that
// asks for fresh data first draws a small "refreshing" mark in the status bar,
// since the new image takes 10 s or more to appear.
//
// The image comes as /glance.g2: the panel's own 4 greys at 2 bits per pixel
// (4 pixels per byte, first pixel in the top bits, 0 = black .. 3 = white =
// TFT_GRAY_0..3), 96,000 bytes. It is unpacked straight into the 4-bit frame
// buffer as it downloads: no PNG decoding (that took 2.2 s here) and no copy
// of the file in memory.
//
// Flashing works while it sleeps: the USB-serial bridge resets the chip into
// its bootloader.

#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <driver/rtc_io.h>
#include <esp_sleep.h>
#include <esp_timer.h>

#include "TFT_eSPI.h"
#include "secrets.h"

#ifndef EPAPER_ENABLE
#error "Seeed_GFX setup not selected: build with -DBOARD_SCREEN_COMBO=520 (see platformio.ini)"
#endif

// Logs go to UART1 (GPIO43 TX / GPIO44 RX), which the reTerminal's on-board
// USB-to-serial bridge exposes on the USB-C port.
#define LOG Serial1
static constexpr int PIN_LOG_RX = 44;
static constexpr int PIN_LOG_TX = 43;

static constexpr int WIDTH = 800;
static constexpr int HEIGHT = 480;
static constexpr size_t G2_BYTES = WIDTH * HEIGHT / 4;
// Kept short: a failed attempt is pure battery cost. A normal connect takes
// ~2 s, a download ~0.2 s.
static constexpr uint32_t WIFI_TIMEOUT_MS = 10000;
static constexpr uint32_t HTTP_CONNECT_TIMEOUT_MS = 4000;
static constexpr uint32_t HTTP_TIMEOUT_MS = 10000;
// Buttons: right = GPIO3, middle = GPIO4, left = GPIO5 (checked on the
// device). Any of them wakes it, and the log says which one did. Pressed = low.
static constexpr gpio_num_t BUTTONS[] = {GPIO_NUM_3, GPIO_NUM_4, GPIO_NUM_5};
// Battery: GPIO21 switches on the voltage divider read on GPIO1 (half the
// battery voltage).
static constexpr int PIN_BATTERY_ADC = 1;
static constexpr int PIN_BATTERY_EN = 21;

static constexpr int DEFAULT_SLEEP_S = 60;  // if the server doesn't say
// The longest sleep accepted from the server. Its schedule never asks for
// more than a day; a larger value (a bug, or something else answering at the
// server's address over plain HTTP) would leave the screen dead for weeks,
// and past 49 days would overflow the milliseconds below.
static constexpr int MAX_SLEEP_S = 25 * 60 * 60;
// After failures (Wi-Fi or server down), retry less and less often so a long
// outage doesn't drain the battery; back to normal on the first success. A
// button press always retries at once.
static constexpr int RETRY_SLEEP_S[] = {30, 60, 120, 300, 600, 900};
RTC_DATA_ATTR static uint32_t failures = 0;  // consecutive, kept across deep sleep
// From this many failures in a row, mark the screen as offline (once per outage).
static constexpr uint32_t OFFLINE_MARK_AFTER = 3;
RTC_DATA_ATTR static bool offline_mark_shown = false;

EPaper epaper;

// --- battery ----------------------------------------------------------------------

// Battery voltage in mV, or 0 if the reading is implausible. Read before Wi-Fi
// starts, since the radio's current makes the voltage sag.
static int read_battery_mv() {
  pinMode(PIN_BATTERY_EN, OUTPUT);
  digitalWrite(PIN_BATTERY_EN, HIGH);
  delay(5);
  uint32_t sum = 0;
  for (int i = 0; i < 8; i++) sum += analogReadMilliVolts(PIN_BATTERY_ADC);
  digitalWrite(PIN_BATTERY_EN, LOW);
  const int mv = sum / 8 * 2;  // the divider halves it
  return (mv > 2500 && mv < 5000) ? mv : 0;
}

// --- network --------------------------------------------------------------------

// The access point found last time, kept across deep sleep: connecting to a
// known channel and BSSID skips the scan. Cleared if that fails.
RTC_DATA_ATTR static bool have_ap = false;
RTC_DATA_ATTR static int32_t ap_channel = 0;
RTC_DATA_ATTR static uint8_t ap_bssid[6] = {};

static uint32_t wifi_start = 0;

// Start connecting. It carries on in the background, so the refreshing mark
// can be drawn meanwhile; connect_wifi() waits for the result.
static void start_wifi() {
  wifi_start = millis();
  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);  // don't write credentials to flash on every wake
  if (have_ap) WiFi.begin(WIFI_SSID, WIFI_PASSWORD, ap_channel, ap_bssid, true);
  else WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

static bool wait_connected(uint32_t start, uint32_t timeout_ms) {
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - start > timeout_ms) return false;
    delay(20);
  }
  return true;
}

// Wait for the connection start_wifi() began.
static bool connect_wifi() {
  const uint32_t start = wifi_start;
  uint32_t scan_start = wifi_start;
  bool fast = have_ap;
  if (fast && !wait_connected(start, 5000)) {
    LOG.println("[wifi] known AP failed, scanning");
    have_ap = false;
    fast = false;
    WiFi.disconnect();
    scan_start = millis();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  }
  if (!fast) {
    if (!wait_connected(scan_start, WIFI_TIMEOUT_MS)) {
      LOG.println("[wifi] timeout");
      return false;
    }
    ap_channel = WiFi.channel();
    memcpy(ap_bssid, WiFi.BSSID(), sizeof(ap_bssid));
    have_ap = true;
  }
  LOG.printf("[wifi] connected in %lu ms (%s), ip %s, rssi %d\n", millis() - start,
             fast ? "known AP" : "scan", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  return true;
}

struct Fetch {
  bool ok = false;
  int sleep_s = DEFAULT_SLEEP_S;  // as the server said when it answered
  uint32_t answered_at = 0;       // millis() when the response arrived
};

// Fetch the image into the frame buffer. On failure the frame buffer may be
// partly overwritten, but the panel keeps showing the previous image, since
// it only changes on update().
static Fetch fetch_and_draw(int battery_mv, bool refresh) {
  Fetch result;
  String url = GLANCE_URL;
  url += "?battery_mv=" + String(battery_mv);
  if (refresh) url += "&refresh=1";
  HTTPClient http;
  http.setConnectTimeout(HTTP_CONNECT_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.begin(url);
  // The server acts on the battery report and the refresh only in requests
  // with this header: a web page can make a browser at home send a plain GET
  // to the server, but not one with a header of its own.
  http.addHeader("X-Glance-Client", "device");
  const char *headers[] = {"X-Glance-Sleep"};
  http.collectHeaders(headers, 1);

  const uint32_t start = millis();
  const int code = http.GET();
  result.answered_at = millis();
  if (code != HTTP_CODE_OK) {
    LOG.printf("[http] GET failed: %d %s\n", code, http.errorToString(code).c_str());
    http.end();
    return result;
  }
  const int sleep_s = http.header("X-Glance-Sleep").toInt();
  if (sleep_s > MAX_SLEEP_S) {
    LOG.printf("[http] X-Glance-Sleep %d s is too long, sleeping %d s\n", sleep_s, MAX_SLEEP_S);
    result.sleep_s = MAX_SLEEP_S;
  } else if (sleep_s > 0) {
    result.sleep_s = sleep_s;
  }
  if (http.getSize() != (int)G2_BYTES) {
    LOG.printf("[http] unexpected size %d (want %u)\n", http.getSize(), G2_BYTES);
    http.end();
    return result;
  }

  // 4-bit frame buffer: 2 pixels per byte, left pixel in the high nibble.
  auto *fb = static_cast<uint8_t *>(epaper.getPointer());
  if (!fb) {  // the frame buffer wasn't allocated: a failed update, retried with backoff
    LOG.println("[epd] no frame buffer");
    http.end();
    return result;
  }
  WiFiClient *stream = http.getStreamPtr();
  static uint8_t buf[1024];
  size_t got = 0;
  while (got < G2_BYTES) {
    if (millis() - start > HTTP_TIMEOUT_MS) {
      LOG.printf("[http] read timeout after %u bytes\n", got);
      http.end();
      return result;
    }
    const size_t avail = stream->available();
    if (!avail) {
      if (!http.connected()) break;
      delay(1);
      continue;
    }
    const size_t n = stream->readBytes(buf, min(min(avail, sizeof(buf)), G2_BYTES - got));
    for (size_t i = 0; i < n; i++) {
      const uint8_t b = buf[i];  // 4 pixels: bits 7-6, 5-4, 3-2, 1-0
      uint8_t *out = fb + (got + i) * 2;
      out[0] = ((b >> 6) & 3) << 4 | ((b >> 4) & 3);
      out[1] = ((b >> 2) & 3) << 4 | (b & 3);
    }
    got += n;
  }
  http.end();
  if (got != G2_BYTES) {
    LOG.printf("[http] short read: %u of %u bytes\n", got, G2_BYTES);
    return result;
  }
  result.ok = true;
  LOG.printf("[http] image fetched in %lu ms, sleep %d s\n", millis() - start, result.sleep_s);
  return result;
}

// --- status bar marks -----------------------------------------------------------------

// Two marks are drawn by the device itself, both as a white badge at the right
// end of the status bar (black on the server's image, where the battery level
// is), with a partial refresh that leaves the rest of the screen as it is:
// "refreshing" (two circling arrows) when an update asks for fresh data, and
// "offline" (a crossed-out cloud) after failed updates. Partial refresh works
// only in black and white on this panel, so they are drawn in a 1-bit frame
// buffer. The next successful update redraws everything.
static constexpr int MARK_X = 704, MARK_W = 96, MARK_H = 30;  // x and width multiples of 8

static void thick_line(int x0, int y0, int x1, int y1, float w, uint32_t color) {
  const float dx = x1 - x0, dy = y1 - y0, len = sqrtf(dx * dx + dy * dy);
  const float nx = -dy / len * w / 2, ny = dx / len * w / 2;
  epaper.fillTriangle(x0 + nx, y0 + ny, x1 + nx, y1 + ny, x1 - nx, y1 - ny, color);
  epaper.fillTriangle(x0 + nx, y0 + ny, x1 - nx, y1 - ny, x0 - nx, y0 - ny, color);
}

// Cloud: three bumps on a flat base, centred at about (770, 15), with a slash
// through it (white-edged so it reads against the black cloud).
static void draw_offline_icon() {
  epaper.fillCircle(762, 17, 5, TFT_BLACK);
  epaper.fillCircle(770, 13, 7, TFT_BLACK);
  epaper.fillCircle(778, 17, 5, TFT_BLACK);
  epaper.fillRect(762, 17, 16, 6, TFT_BLACK);
  thick_line(758, 24, 782, 6, 6, TFT_WHITE);
  thick_line(758, 24, 782, 6, 2.5, TFT_BLACK);
}

// Two arrows chasing each other round a ring centred at (770, 15): a ring,
// two gaps cut into it, and an arrowhead at the clockwise end of each half.
static void draw_refresh_icon() {
  epaper.fillCircle(770, 15, 9, TFT_BLACK);
  epaper.fillCircle(770, 15, 6, TFT_WHITE);
  thick_line(770, 15, 781, 19, 6, TFT_WHITE);
  thick_line(770, 15, 759, 11, 6, TFT_WHITE);
  epaper.fillTriangle(773, 14, 782, 12, 779, 19, TFT_BLACK);
  epaper.fillTriangle(767, 16, 758, 18, 761, 11, TFT_BLACK);
}

// The library's panel sequences are macros written for its own member
// functions; these let them be used here.
static inline void writecommand(uint8_t c) { epaper.writecommand(c); }
static inline void writedata(uint8_t d) { epaper.writedata(d); }

// Partial refresh of the mark's part of the status bar from the 1-bit frame
// buffer. Same sequence as the library's updataPartial(), plus one step: the
// panel's controller picks each pixel's drive from two images, the one it
// believes is showing ("old") and the new one, and leaves a pixel alone where
// they agree. The library sends only the new one, so "old" was whatever the
// controller's memory held: after a 4-grey refresh, half of that image's
// data, in which dark grey counts as white and light grey as black. Such
// pixels (the smoothed edges of the battery level) were left as they were,
// which made the mark smudgy. Sending the inverse of the new image as "old"
// makes the controller drive every pixel, whatever is on the panel.
//
// Leaves the panel asleep. The library isn't told about any of this: it still
// takes the panel to be asleep, as it has since begin(), so its next update()
// wakes it as usual.
static void push_mark(const uint8_t *fb) {
  static constexpr int ROW_BYTES = MARK_W / 8;
  static uint8_t now[ROW_BYTES * MARK_H], before[sizeof(now)];
  for (int row = 0; row < MARK_H; row++)
    memcpy(now + row * ROW_BYTES, fb + row * (WIDTH / 8) + MARK_X / 8, ROW_BYTES);
  for (size_t i = 0; i < sizeof(now); i++) before[i] = ~now[i];
  const int x0 = MARK_X, y0 = 0, x1 = MARK_X + MARK_W - 1, y1 = MARK_H - 1;
  EPD_WAKEUP_PARTIAL();
  EPD_SET_WINDOW(x0, y0, x1, y1);
  EPD_PUSH_OLD_COLORS(MARK_W, MARK_H, before);
  EPD_PUSH_NEW_COLORS(MARK_W, MARK_H, now);
  EPD_UPDATE_PARTIAL();
  EPD_SLEEP();
}

// Draw a mark on the panel; false if it couldn't be drawn. Ends with the panel
// asleep and the frame buffer in 1-bit mode.
static bool show_mark(void (*draw_icon)(), const char *name) {
  epaper.deinitGrayMode();  // 1-bit frame buffer
  const auto *fb = static_cast<const uint8_t *>(epaper.getPointer());
  if (!fb) {
    LOG.printf("[epd] no frame buffer for the %s mark\n", name);
    return false;
  }
  epaper.fillRect(MARK_X, 0, MARK_W, MARK_H, TFT_BLACK);
  epaper.fillRoundRect(744, 4, 52, 22, 4, TFT_WHITE);
  draw_icon();
  const uint32_t start = millis();
  push_mark(fb);
  LOG.printf("[epd] %s mark drawn in %lu ms\n", name, millis() - start);
  return true;
}

// Power the panel down when there is nothing to draw. begin() switches the
// panel's power on, and the library switches it off only at the end of a
// refresh, so after a failed update it would stay powered through the sleep
// before the retry (up to 15 minutes at a time in an outage). The library's
// sleep() does nothing until the panel has been woken once, hence wake()
// first: a reset and re-init, which changes nothing on the screen.
static void panel_off() {
  const uint32_t start = millis();
  epaper.wake();
  epaper.sleep();
  LOG.printf("[epd] panel powered down in %lu ms\n", millis() - start);
}

// --- main -------------------------------------------------------------------------

// --- buttons while awake -------------------------------------------------------------

// A press while awake (during an update) can't wake us, so catch it with an
// interrupt and do another update with fresh data right after this one.
//
// Only falling edges (press = low) are used, never the pin's current level:
// during the panel refresh Seeed's driver keeps interrupts blocked for long
// stretches, so the handler can run after the button is already released.
// Bounces don't matter (one press or several, the result is one refresh). The
// button that woke us is ignored for its first second: that's its release
// bouncing, not a new press.
static constexpr int NUM_BUTTONS = sizeof(BUTTONS) / sizeof(BUTTONS[0]);
static constexpr uint32_t WAKE_BUTTON_IGNORE_MS = 1000;
static volatile uint32_t armed_at[NUM_BUTTONS];  // ignore edges before this (ms since boot)
static volatile bool pressed_awake[NUM_BUTTONS];
static volatile uint32_t edges[NUM_BUTTONS];     // for the log
// A press while awake, or one whose update failed: carried over sleep to the next update.
RTC_DATA_ATTR static bool refresh_pending = false;

// Interrupt context, possibly while flash is busy (Wi-Fi does that): only
// IRAM-safe calls (no millis(); esp_timer_get_time() is safe).
static void IRAM_ATTR on_button_press(void *arg) {
  const int i = (int)(intptr_t)arg;
  const uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
  edges[i] = edges[i] + 1;
  if ((int32_t)(now - armed_at[i]) >= 0) pressed_awake[i] = true;
}

static void watch_buttons() {
  LOG.print("[button] at boot:");
  for (int i = 0; i < NUM_BUTTONS; i++) {
    const bool down = digitalRead(BUTTONS[i]) == LOW;
    LOG.printf(" GPIO%d=%s", BUTTONS[i], down ? "DOWN" : "up");
    armed_at[i] = down ? (uint32_t)(esp_timer_get_time() / 1000) + WAKE_BUTTON_IGNORE_MS : 0;
    pressed_awake[i] = false;
    edges[i] = 0;
    attachInterruptArg(BUTTONS[i], on_button_press, (void *)(intptr_t)i, FALLING);
  }
  LOG.println();
}

// True if a button was pressed while awake (and logs which).
static bool pressed_while_awake() {
  bool any = false;
  for (int i = 0; i < NUM_BUTTONS; i++) {
    if (edges[i]) LOG.printf("[button] GPIO%d: %lu edges while awake\n", BUTTONS[i], (unsigned long)edges[i]);
    if (pressed_awake[i]) {
      LOG.printf("[button] GPIO%d pressed while awake\n", BUTTONS[i]);
      any = true;
    }
  }
  return any;
}

static void deep_sleep(uint32_t ms) {
  if (pressed_while_awake()) {
    refresh_pending = true;
    ms = 500;  // come straight back for the update the press asked for
  }
  for (gpio_num_t pin : BUTTONS) detachInterrupt(pin);
  LOG.printf("[glance] deep sleep %lu ms\n", ms);
  LOG.flush();
  if (WiFi.getMode() != WIFI_OFF) {
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
  }
  esp_sleep_enable_timer_wakeup(uint64_t(ms) * 1000ULL);
  // Wake on any button (pressed = low). The RTC domain stays powered so the
  // internal pull-ups hold the pins high while asleep. A button that is
  // already down (held, or stuck) is left out, or it would wake us at once.
  uint64_t mask = 0;
  for (gpio_num_t pin : BUTTONS) {
    rtc_gpio_pullup_en(pin);
    rtc_gpio_pulldown_dis(pin);
    if (digitalRead(pin) == HIGH) mask |= 1ULL << pin;
    else LOG.printf("[button] GPIO%d is low, not waking on it\n", pin);
  }
  if (mask) {
    esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH, ESP_PD_OPTION_ON);
    esp_sleep_enable_ext1_wakeup(mask, ESP_EXT1_WAKEUP_ANY_LOW);
  }
  esp_deep_sleep_start();
}

void setup() {
  const uint32_t boot = millis();
  LOG.begin(115200, SERIAL_8N1, PIN_LOG_RX, PIN_LOG_TX);
  // Hand the button pins back from the RTC (sleep-time) domain to normal GPIO.
  for (gpio_num_t pin : BUTTONS) {
    rtc_gpio_deinit(pin);
    pinMode(pin, INPUT_PULLUP);
  }
  const esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();
  const bool button_wake = cause == ESP_SLEEP_WAKEUP_EXT1;
  watch_buttons();
  const bool refresh = button_wake || refresh_pending;
  refresh_pending = false;
  if (button_wake) {
    const uint64_t pins = esp_sleep_get_ext1_wakeup_status();
    for (gpio_num_t pin : BUTTONS)
      if (pins & (1ULL << pin)) LOG.printf("\n[glance] button wake: GPIO%d\n", pin);
  } else {
    LOG.printf("\n[glance] %s%s\n", cause == ESP_SLEEP_WAKEUP_TIMER ? "timer wake" : "power on / reset",
               refresh ? " (refresh: button pressed while awake)" : "");
  }
  const int battery_mv = read_battery_mv();
  LOG.printf("[battery] %d mV\n", battery_mv);
  epaper.begin();
  start_wifi();
  // Fresh data takes a while (the server refetches and redraws, then the
  // panel's 6 s refresh): show at once that the press was noticed. Wi-Fi
  // connects meanwhile, so this costs little or no extra time awake.
  const bool refresh_mark = refresh && show_mark(draw_refresh_icon, "refreshing");
  epaper.initGrayMode(GRAY_LEVEL4);

  Fetch fetch;
  if (connect_wifi()) fetch = fetch_and_draw(battery_mv, refresh);
  if (!fetch.ok) {
    // A button press that didn't get through still wants fresh data: the
    // retry is a timer wake, so carry the request over to it.
    if (refresh) refresh_pending = true;
    failures++;
    const size_t n = sizeof(RETRY_SLEEP_S) / sizeof(RETRY_SLEEP_S[0]);
    const int retry_s = RETRY_SLEEP_S[min<size_t>(failures, n) - 1];
    LOG.printf("[glance] update failed (%lu in a row), retrying in %d s\n", (unsigned long)failures, retry_s);
    if (WiFi.getMode() != WIFI_OFF) {
      WiFi.disconnect(true);
      WiFi.mode(WIFI_OFF);
    }
    // The refreshing mark can't be left up, and what was under it is gone, so
    // a failed refresh shows the offline mark at once.
    if (refresh_mark || (failures >= OFFLINE_MARK_AFTER && !offline_mark_shown)) {
      show_mark(draw_offline_icon, "offline");  // ends with the panel asleep
      offline_mark_shown = true;
    } else {
      panel_off();
    }
    deep_sleep(retry_s * 1000UL);
  }
  if (failures) LOG.printf("[glance] back online after %lu failed updates\n", (unsigned long)failures);
  failures = 0;
  offline_mark_shown = false;  // the full refresh below replaces the mark

  // Wi-Fi is only needed for the download: switch it off for the ~6 s refresh.
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  const uint32_t refresh_start = millis();
  epaper.update();  // full 4-grey refresh; wakes and re-sleeps the panel itself
  LOG.printf("[epd] refreshed in %lu ms; awake %lu ms in all\n", millis() - refresh_start, millis() - boot);
  // The server timed the sleep from when it answered: subtract what has
  // passed since (download, refresh), so wakes don't drift later.
  const uint32_t elapsed = millis() - fetch.answered_at;
  const uint32_t want = fetch.sleep_s * 1000UL;
  deep_sleep(want > elapsed + 1000 ? want - elapsed : 1000);
}

void loop() {}  // never reached: setup() always ends in deep sleep
