// Glance firmware: on every wake, read the battery, connect to Wi-Fi, fetch the
// image the server renders, draw it in the panel's 4 greys, then deep-sleep
// for as long as the server says (X-Glance-Sleep). Each wake starts from
// setup(). A press on any of the three buttons wakes it early: the two white
// ones on the left step through the server's deck of screens (?button=prev,
// ?button=next), the green one on the right asks for fresh data
// (?button=refresh). The battery voltage goes along with every request
// (?battery_mv=), and the server draws the level. A press is answered at once
// with a sound from the buzzer, since the new image takes 8 s or more to
// appear.
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
#include <sys/time.h>

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
// Buttons: right = GPIO3 (green), middle = GPIO4, left = GPIO5 (both white;
// checked on the device). Any of them wakes it. Pressed = low.
static constexpr gpio_num_t BUTTONS[] = {GPIO_NUM_3, GPIO_NUM_4, GPIO_NUM_5};
enum Button : uint8_t { NO_BUTTON, REFRESH, NEXT, PREV };
static constexpr Button BUTTON_ACTION[] = {REFRESH, NEXT, PREV};  // by BUTTONS index
static const char *const BUTTON_NAME[] = {"none", "refresh", "next", "prev"};  // as the server's ?button=
// Battery: GPIO21 switches on the voltage divider read on GPIO1 (half the
// battery voltage).
static constexpr int PIN_BATTERY_ADC = 1;
static constexpr int PIN_BATTERY_EN = 21;
// Buzzer: GPIO45 (Seeed's guides for the reTerminal E series), sounded with a
// square wave. Low when silent, and held low through deep sleep so it draws
// nothing. GPIO45 is also a strapping pin (it picks the flash voltage at
// reset, low being the default), so it is never left high.
static constexpr gpio_num_t PIN_BUZZER = GPIO_NUM_45;

static constexpr int DEFAULT_SLEEP_S = 60;  // if the server doesn't say
// The shortest sleeps the server may ask for repeatedly. One short sleep is
// normal (an update just before a wake minute), several in a row are not: a
// server fault that kept answering "1 s" would have the panel refreshing all
// the time until the battery was flat. After SHORT_SLEEPS_ALLOWED sleeps of
// SHORT_SLEEP_S or less in a row, the device sleeps DEFAULT_SLEEP_S instead.
static constexpr int SHORT_SLEEP_S = 10;
static constexpr uint8_t SHORT_SLEEPS_ALLOWED = 2;
RTC_DATA_ATTR static uint8_t short_sleeps = 0;  // in a row, kept across deep sleep
// The longest the device stays awake. A normal update takes ~9 s, the worst
// one (slow Wi-Fi, the offline mark) under a minute. Without a limit,
// anything that never returns keeps it awake until the battery is flat: the
// display library waits for the panel's BUSY line with no timeout (its
// CHECK_BUSY), so a panel that stopped answering would do that. At the limit
// the update counts as failed and the device sleeps (see on_awake_limit).
static constexpr uint64_t AWAKE_LIMIT_US = 90ULL * 1000 * 1000;
// Times in a row the limit ran out, kept across deep sleep; back to 0 when an
// update reaches its sleep by itself. It has its own count because `failures`
// is back at 0 once the image has arrived, before the panel refresh: a panel
// that hangs in the refresh would otherwise get the shortest retry every time
// (90 s awake, 30 s asleep, for ever).
RTC_DATA_ATTR static uint32_t awake_limit_hits = 0;
// A battery reading below this means the cell is empty (the server's curve
// calls 3270 mV 0 %). Wi-Fi and a panel refresh would only pull it lower, so
// the device skips the update and looks again later; on the charger the
// reading rises and updates resume. A reading of 0 (implausible, e.g. no
// battery fitted) doesn't count.
static constexpr int BATTERY_EMPTY_MV = 3300;
static constexpr int BATTERY_EMPTY_SLEEP_S = 15 * 60;
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
// Screens in the server's current deck, from its last answer (0 = not known
// yet). On a deck of one, a white button has nothing to change, so a press is
// ignored without waking the radio.
RTC_DATA_ATTR static uint8_t deck_size = 0;
// When the timer wake planned at the last sleep is due, in rtc_now_ms() terms:
// a press that is ignored goes back to sleep until then.
RTC_DATA_ATTR static int64_t wake_due_ms = 0;

// Milliseconds on the RTC timer, which keeps counting through deep sleep
// (millis() starts over at every wake).
static int64_t rtc_now_ms() {
  timeval tv;
  gettimeofday(&tv, nullptr);
  return int64_t(tv.tv_sec) * 1000 + tv.tv_usec / 1000;
}
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

// --- sound ----------------------------------------------------------------------

// A press that will change the screen is answered at once with a sound, since
// the new image takes about 10 s to appear: one beep for the green button
// (fresh data), a shorter, higher tick for the white ones (another screen).
// Timed wakes, retries and presses with nothing to change are silent.
struct Sound {
  uint32_t hz;
  uint32_t ms;
};
static constexpr Sound REFRESH_SOUND = {2000, 70};
static constexpr Sound STEP_SOUND = {3000, 25};
// Of the 8-bit duty cycle: below half is quieter (a softer sound).
static constexpr uint32_t SOUND_DUTY = 128;

// The pin may still be held from the last deep sleep: release it, and keep it low.
static void buzzer_quiet() {
  gpio_hold_dis(PIN_BUZZER);
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);
}

// Keep the buzzer low through deep sleep (a digital pin would otherwise float).
static void buzzer_hold_for_sleep() {
  gpio_hold_en(PIN_BUZZER);
  gpio_deep_sleep_hold_en();
}

static void play_press_sound(Button button) {
  const Sound s = button == REFRESH ? REFRESH_SOUND : STEP_SOUND;
  const uint32_t at = millis();
  buzzer_quiet();
  if (!ledcAttach(PIN_BUZZER, s.hz, 8)) {
    LOG.println("[sound] could not drive the buzzer");
    return;
  }
  ledcWrite(PIN_BUZZER, SOUND_DUTY);
  delay(s.ms);
  ledcWrite(PIN_BUZZER, 0);
  ledcDetach(PIN_BUZZER);
  buzzer_quiet();
  LOG.printf("[sound] %s at %lu ms since boot\n", BUTTON_NAME[button], (unsigned long)at);
}

// --- network --------------------------------------------------------------------

// The access point found last time, kept across deep sleep: connecting to a
// known channel and BSSID skips the scan. Cleared if that fails.
RTC_DATA_ATTR static bool have_ap = false;
RTC_DATA_ATTR static int32_t ap_channel = 0;
RTC_DATA_ATTR static uint8_t ap_bssid[6] = {};

static uint32_t wifi_start = 0;

// Start connecting. It carries on in the background while the frame buffer
// is set up; connect_wifi() waits for the result.
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
  bool answered = false;          // the server got the request (any HTTP status)
  bool no_change = false;         // 204: nothing to draw (a white button on a deck of one)
  int sleep_s = DEFAULT_SLEEP_S;  // as the server said when it answered
  uint32_t answered_at = 0;       // millis() when the response arrived
};

// Fetch the image into the frame buffer. On failure the frame buffer may be
// partly overwritten, but the panel keeps showing the previous image, since
// it only changes on update().
static Fetch fetch_and_draw(int battery_mv, Button button) {
  Fetch result;
  String url = GLANCE_URL;
  url += "?battery_mv=" + String(battery_mv);
  if (button != NO_BUTTON) url += String("&button=") + BUTTON_NAME[button];
  HTTPClient http;
  http.setConnectTimeout(HTTP_CONNECT_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.begin(url);
  // The server acts on the battery report and the refresh only in requests
  // with this header: a web page can make a browser at home send a plain GET
  // to the server, but not one with a header of its own.
  http.addHeader("X-Glance-Client", "device");
  const char *headers[] = {"X-Glance-Sleep", "X-Glance-Deck-Size"};
  http.collectHeaders(headers, 2);

  const uint32_t start = millis();
  const int code = http.GET();
  result.answered_at = millis();
  result.answered = code > 0;
  if (code != HTTP_CODE_OK && code != HTTP_CODE_NO_CONTENT) {
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
  const int size = http.header("X-Glance-Deck-Size").toInt();
  if (size > 0) deck_size = min(size, 255);
  if (code == HTTP_CODE_NO_CONTENT) {
    LOG.printf("[http] no change (deck of %d), sleep %d s\n", size, result.sleep_s);
    http.end();
    result.ok = result.no_change = true;
    return result;
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

// --- the offline mark -----------------------------------------------------------------

// The offline mark is drawn by the device itself, as a white badge at the
// right end of the status bar (black on the server's image, where the battery
// level is), with a partial refresh that leaves the rest of the screen as it
// is: a crossed-out cloud, after failed updates. Partial refresh works only in
// black and white on this panel, so it is drawn in a 1-bit frame buffer. The
// next successful update redraws everything.
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

// Draw the offline mark on the panel. Ends with the panel asleep and the frame
// buffer in 1-bit mode.
static void show_offline_mark() {
  epaper.deinitGrayMode();  // 1-bit frame buffer
  const auto *fb = static_cast<const uint8_t *>(epaper.getPointer());
  if (!fb) {
    LOG.println("[epd] no frame buffer for the offline mark");
    return;
  }
  epaper.fillRect(MARK_X, 0, MARK_W, MARK_H, TFT_BLACK);
  epaper.fillRoundRect(744, 4, 52, 22, 4, TFT_WHITE);
  draw_offline_icon();
  const uint32_t start = millis();
  push_mark(fb);
  LOG.printf("[epd] offline mark drawn in %lu ms\n", millis() - start);
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
// interrupt and do another update for it right after this one.
//
// Only falling edges (press = low) are used, never the pin's current level:
// during the panel refresh Seeed's driver keeps interrupts blocked for long
// stretches, so the handler can run after the button is already released.
// Bounces don't matter (one press or several, the result is one update, for
// the button pressed last). The button that woke us is ignored for its first
// second: that's its release bouncing, not a new press.
static constexpr int NUM_BUTTONS = sizeof(BUTTONS) / sizeof(BUTTONS[0]);
static constexpr uint32_t WAKE_BUTTON_IGNORE_MS = 1000;
static volatile uint32_t armed_at[NUM_BUTTONS];  // ignore edges before this (ms since boot)
static volatile bool pressed_awake[NUM_BUTTONS];
static volatile uint32_t edges[NUM_BUTTONS];     // for the log
static volatile int last_pressed = -1;           // BUTTONS index of the latest press while awake
// A press while awake, or one whose update failed: carried over sleep to the next update.
RTC_DATA_ATTR static Button pending_button = NO_BUTTON;

// Interrupt context, possibly while flash is busy (Wi-Fi does that): only
// IRAM-safe calls (no millis(); esp_timer_get_time() is safe).
static void IRAM_ATTR on_button_press(void *arg) {
  const int i = (int)(intptr_t)arg;
  const uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
  edges[i] = edges[i] + 1;
  if ((int32_t)(now - armed_at[i]) >= 0) {
    pressed_awake[i] = true;
    last_pressed = i;
  }
}

static void watch_buttons() {
  LOG.print("[button] at boot:");
  for (int i = 0; i < NUM_BUTTONS; i++) {
    const bool down = digitalRead(BUTTONS[i]) == LOW;
    LOG.printf(" GPIO%d=%s", BUTTONS[i], down ? "DOWN" : "up");
    armed_at[i] = down ? (uint32_t)(esp_timer_get_time() / 1000) + WAKE_BUTTON_IGNORE_MS : 0;
    pressed_awake[i] = false;
    edges[i] = 0;
    last_pressed = -1;
    attachInterruptArg(BUTTONS[i], on_button_press, (void *)(intptr_t)i, FALLING);
  }
  LOG.println();
}

// Whether a white button has anything to do: not on a deck of one screen.
static bool worth_doing(Button b) {
  return !((b == PREV || b == NEXT) && deck_size == 1);
}

// The button pressed last while awake (and logs them), or NO_BUTTON.
static Button pressed_while_awake() {
  for (int i = 0; i < NUM_BUTTONS; i++) {
    if (edges[i]) LOG.printf("[button] GPIO%d: %lu edges while awake\n", BUTTONS[i], (unsigned long)edges[i]);
    if (pressed_awake[i]) LOG.printf("[button] GPIO%d pressed while awake\n", BUTTONS[i]);
  }
  const int i = last_pressed;
  return i >= 0 ? BUTTON_ACTION[i] : NO_BUTTON;
}

// Wake from deep sleep on any button (pressed = low). The RTC domain stays
// powered so the internal pull-ups hold the pins high while asleep. A button
// that is already down (held, or stuck) is left out, or it would wake us at
// once.
static void wake_on_buttons() {
  uint64_t mask = 0;
  for (gpio_num_t pin : BUTTONS) {
    rtc_gpio_pullup_en(pin);
    rtc_gpio_pulldown_dis(pin);
    if (digitalRead(pin) == HIGH) mask |= 1ULL << pin;
  }
  if (mask) {
    esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH, ESP_PD_OPTION_ON);
    esp_sleep_enable_ext1_wakeup(mask, ESP_EXT1_WAKEUP_ANY_LOW);
  }
}

// Seconds to the retry after a failed update: longer the more have gone wrong
// in a row (failed updates, or updates that ran into the awake limit).
static int retry_sleep_s() {
  const size_t n = sizeof(RETRY_SLEEP_S) / sizeof(RETRY_SLEEP_S[0]);
  const uint32_t in_a_row = max<uint32_t>(max<uint32_t>(failures, awake_limit_hits), 1);
  return RETRY_SLEEP_S[min<size_t>(in_a_row, n) - 1];
}

// The awake limit ran out: setup() is stuck somewhere (see AWAKE_LIMIT_US).
// Runs in the esp_timer task, beside the stuck setup(), so it does the least
// it can: count a failed update, set the wake-ups, sleep. Deep sleep powers
// the radio down whatever state Wi-Fi was in.
static void on_awake_limit(void *) {
  failures = failures + 1;
  awake_limit_hits = awake_limit_hits + 1;
  const int retry_s = retry_sleep_s();
  LOG.println("[glance] still awake at the limit: giving up on this update");
  LOG.flush();
  wake_due_ms = rtc_now_ms() + retry_s * 1000LL;
  esp_sleep_enable_timer_wakeup(uint64_t(retry_s) * 1000000ULL);
  wake_on_buttons();
  buzzer_hold_for_sleep();
  esp_deep_sleep_start();
}

static void start_awake_limit() {
  esp_timer_create_args_t args = {};
  args.callback = on_awake_limit;
  args.name = "awake_limit";
  esp_timer_handle_t timer = nullptr;
  if (esp_timer_create(&args, &timer) != ESP_OK || esp_timer_start_once(timer, AWAKE_LIMIT_US) != ESP_OK)
    LOG.println("[glance] could not start the awake limit");
}

static void deep_sleep(uint32_t ms) {
  awake_limit_hits = 0;  // got here by itself: nothing hung
  const Button pressed = pressed_while_awake();
  if (pressed != NO_BUTTON && worth_doing(pressed)) {
    // Its sound now, as soon as it is noticed: the panel's refresh kept the
    // chip busy. The update it asked for follows silently.
    play_press_sound(pressed);
    pending_button = pressed;
    ms = 500;  // come straight back for the update the press asked for
  } else if (pressed != NO_BUTTON) {
    LOG.printf("[button] %s: one screen in the deck, nothing to change\n", BUTTON_NAME[pressed]);
  }
  wake_due_ms = rtc_now_ms() + ms;
  for (gpio_num_t pin : BUTTONS) {
    detachInterrupt(pin);
    if (digitalRead(pin) == LOW) LOG.printf("[button] GPIO%d is low, not waking on it\n", pin);
  }
  LOG.printf("[glance] deep sleep %lu ms\n", ms);
  LOG.flush();
  if (WiFi.getMode() != WIFI_OFF) {
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
  }
  esp_sleep_enable_timer_wakeup(uint64_t(ms) * 1000ULL);
  wake_on_buttons();
  buzzer_hold_for_sleep();
  esp_deep_sleep_start();
}

// How long to sleep after an update the server answered: what it said, less
// what has passed since its answer arrived (download, refresh), so wakes
// don't drift later. Not short sleeps over and over, though (SHORT_SLEEP_S).
static uint32_t sleep_ms_after(const Fetch &fetch) {
  int sleep_s = fetch.sleep_s;
  if (sleep_s > SHORT_SLEEP_S) {
    short_sleeps = 0;
  } else if (short_sleeps < SHORT_SLEEPS_ALLOWED) {
    short_sleeps++;
  } else {
    LOG.printf("[glance] the server keeps asking for short sleeps (%d s): sleeping %d s\n", sleep_s, DEFAULT_SLEEP_S);
    sleep_s = DEFAULT_SLEEP_S;
  }
  const uint32_t elapsed = millis() - fetch.answered_at;
  const uint32_t want = sleep_s * 1000UL;
  return want > elapsed + 1000 ? want - elapsed : 1000;
}

void setup() {
  const uint32_t boot = millis();
  LOG.begin(115200, SERIAL_8N1, PIN_LOG_RX, PIN_LOG_TX);
  start_awake_limit();
  buzzer_quiet();  // held low through the sleep just ended
  // Hand the button pins back from the RTC (sleep-time) domain to normal GPIO.
  for (gpio_num_t pin : BUTTONS) {
    rtc_gpio_deinit(pin);
    pinMode(pin, INPUT_PULLUP);
  }
  const esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();
  watch_buttons();
  Button button = pending_button;
  pending_button = NO_BUTTON;
  bool pressed_now = false;  // woken by a press that has something to do: it gets its sound
  if (cause == ESP_SLEEP_WAKEUP_EXT1) {
    const uint64_t pins = esp_sleep_get_ext1_wakeup_status();
    for (int i = 0; i < NUM_BUTTONS; i++)
      if (pins & (1ULL << BUTTONS[i])) button = BUTTON_ACTION[i];
    LOG.printf("\n[glance] button wake: %s\n", BUTTON_NAME[button]);
    if (!worth_doing(button)) {
      // A white button on a deck of one: nothing to change, so no radio and
      // no panel. Back to sleep until the planned wake.
      const int64_t left = wake_due_ms - rtc_now_ms();
      LOG.printf("[button] one screen in the deck, nothing to change; %lld ms to the planned wake\n", (long long)left);
      if (left > 1000) deep_sleep(left);
      button = NO_BUTTON;  // the planned wake is due anyway: a normal update
    }
    pressed_now = button != NO_BUTTON;
  } else {
    LOG.printf("\n[glance] %s%s%s\n", cause == ESP_SLEEP_WAKEUP_TIMER ? "timer wake" : "power on / reset",
               button != NO_BUTTON ? ", for a press: " : "", button != NO_BUTTON ? BUTTON_NAME[button] : "");
  }
  const int battery_mv = read_battery_mv();
  LOG.printf("[battery] %d mV\n", battery_mv);
  if (battery_mv > 0 && battery_mv < BATTERY_EMPTY_MV) {
    // Before the panel and the radio are powered: see BATTERY_EMPTY_MV.
    LOG.printf("[battery] empty: no update, looking again in %d s\n", BATTERY_EMPTY_SLEEP_S);
    deep_sleep(BATTERY_EMPTY_SLEEP_S * 1000UL);
  }
  // After the battery reading (nothing else drawing current while it is read),
  // before the panel and the radio start.
  if (pressed_now) play_press_sound(button);
  epaper.begin();
  start_wifi();
  epaper.initGrayMode(GRAY_LEVEL4);

  Fetch fetch;
  if (connect_wifi()) fetch = fetch_and_draw(battery_mv, button);
  if (fetch.no_change) {
    // Nothing to draw: the panel keeps its image. Sleep as the server said.
    if (failures) LOG.printf("[glance] back online after %lu failed updates\n", (unsigned long)failures);
    failures = 0;
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    panel_off();
    deep_sleep(sleep_ms_after(fetch));
  }
  if (!fetch.ok) {
    // A press that didn't get through still wants doing: the retry is a timer
    // wake, so carry it over. Not a screen step the server already made (it
    // answered, then the download failed): that would step twice. The retry
    // shows the new screen anyway.
    if (button == REFRESH || (button != NO_BUTTON && !fetch.answered)) pending_button = button;
    failures++;
    const int retry_s = retry_sleep_s();
    LOG.printf("[glance] update failed (%lu in a row), retrying in %d s\n", (unsigned long)failures, retry_s);
    if (WiFi.getMode() != WIFI_OFF) {
      WiFi.disconnect(true);
      WiFi.mode(WIFI_OFF);
    }
    // A press heard its sound and waits for the screen to change, so a failed
    // one shows the offline mark at once; otherwise from the third failure in
    // a row. Once per outage: it stays up until a full refresh replaces it.
    if (!offline_mark_shown && (button != NO_BUTTON || failures >= OFFLINE_MARK_AFTER)) {
      show_offline_mark();  // ends with the panel asleep
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
  deep_sleep(sleep_ms_after(fetch));
}

void loop() {}  // never reached: setup() always ends in deep sleep
