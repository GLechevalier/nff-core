/*
 * nff-power-meter — ESP32 nff workload profile
 *
 * Runs, in sequence, each thing nff actually makes an ESP32 DO — so the meter can price them.
 *
 * WHY NOT "each nff command"
 *
 * nff's commands (compile, doctor, clean, flash) are host-side: your PC works, the chip idles.
 * Measured against the shunt they all come out at zero, because they are. What genuinely costs
 * the device energy is the nff-sdk-c work: associating to WiFi, the TLS/MQTT session, and above
 * all an OTA — a megabyte over the radio, then a megabyte into flash. Those are the phases below.
 *
 * SEGMENTATION WITHOUT MARKER WIRES
 *
 * Every phase is bracketed by a MARKER: WiFi off, CPU parked, esp_light_sleep_start(). That drops
 * the board to a few mA — an unmistakable trough next to any active phase, and no extra wiring on
 * a breadboard that has already cost us enough. The host finds the troughs and attributes the
 * energy between them. The first marker of each cycle is LONGER, so the host can find the start.
 *
 * Every network phase re-associates from scratch and tears WiFi down again afterwards, so each is
 * a self-contained episode. Subtract CONNECT from any of them to get its marginal cost.
 *
 * SAFETY: the flash phases write to the *next* OTA partition and then esp_ota_abort(). The boot
 * partition is never changed — this measures the cost of an OTA write without ever installing one.
 *
 * BUILD
 *   1. cp include/wifi_secrets.h.example include/wifi_secrets.h  and fill it in
 *   2. pio run -t upload          (over the ESP32's own USB)
 *   3. UNPLUG the ESP32's USB     <-- it ties GND to the meter's and shorts the shunt
 *   4. Power it from the Nucleo's 5V pin, then run the host profiler.
 */

#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <esp_ota_ops.h>
#include <esp_sleep.h>

#include "wifi_secrets.h"  // WIFI_SSID, WIFI_PASS, BLOB_URL

#define MARKER_MS 2000u
#define MARKER_LONG_MS 5000u  // cycle start — the host locates the cycle by this longer trough
#define PHASE_MS 6000u
#define CHUNK 4096

static uint8_t g_chunk[CHUNK];

/* ---- the trough between phases: a few mA, unmistakable in the trace ---------------------- */
static void marker(uint32_t ms) {
  WiFi.disconnect(true, false);
  WiFi.mode(WIFI_OFF);
  setCpuFrequencyMhz(80);
  delay(50);  // let the radio actually power down before we sleep
  esp_sleep_enable_timer_wakeup((uint64_t)ms * 1000ULL);
  esp_light_sleep_start();
}

/* ---- phases ------------------------------------------------------------------------------ */

static void phase_idle(void) {
  setCpuFrequencyMhz(80);
  const uint32_t until = millis() + PHASE_MS;
  while (millis() < until) delay(50);
}

static void phase_cpu(void) {
  setCpuFrequencyMhz(240);
  volatile float acc = 1.0f;
  const uint32_t until = millis() + PHASE_MS;
  while (millis() < until) {
    for (int i = 0; i < 20000; i++) acc = acc * 1.000001f + 0.000001f;
  }
}

/* Associate from scratch. This is what nff_mqtt.c pays before it can say anything at all. */
static bool wifi_connect(uint32_t timeout_ms = 20000) {
  setCpuFrequencyMhz(240);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  const uint32_t until = millis() + timeout_ms;
  while (WiFi.status() != WL_CONNECTED && millis() < until) delay(10);
  return WiFi.status() == WL_CONNECTED;
}

static void phase_connect(void) { wifi_connect(); }

/* Connected and doing nothing — the standing cost of being on the fleet. */
static void phase_wifi_idle(void) {
  if (!wifi_connect()) return;
  const uint32_t until = millis() + PHASE_MS;
  while (millis() < until) delay(50);
}

/* A megabyte over the radio, thrown away. The receive half of an OTA. */
static void phase_download(void) {
  if (!wifi_connect()) {
    Serial.println("download: wifi connect FAILED");
    return;
  }
  uint32_t got = 0;
  HTTPClient http;
  http.begin(BLOB_URL);
  const int code = http.GET();
  if (code == HTTP_CODE_OK) {
    WiFiClient *s = http.getStreamPtr();
    while (http.connected()) {
      int n = s->readBytes(g_chunk, CHUNK);
      if (n <= 0) break;
      got += n;
    }
  } else {
    Serial.printf("download: HTTP GET failed: %d\n", code);
  }
  Serial.printf("download: %u KiB received\n", got / 1024);
  http.end();
}

/* A megabyte into flash, with the radio OFF. The write half of an OTA, in isolation.
 * Writes to the NEXT ota partition and aborts — the boot partition is never touched.
 *
 * The first byte MUST be 0xE9. esp_ota_write() sniffs the image header on the first chunk and
 * returns ESP_ERR_OTA_VALIDATE_FAILED for anything that is not a real app image, so a buffer of
 * plain filler is rejected instantly — the phase then "runs" in 0.3 s having written nothing, and
 * reports a beautifully small energy figure for work it never did. */
static void phase_flash_write(void) {
  setCpuFrequencyMhz(240);
  const esp_partition_t *part = esp_ota_get_next_update_partition(NULL);
  if (!part) {
    Serial.println("flash_write: NO OTA PARTITION — nothing measured");
    return;
  }
  esp_ota_handle_t h;
  esp_err_t err = esp_ota_begin(part, OTA_SIZE_UNKNOWN, &h);  // this erases the partition too
  if (err != ESP_OK) {
    Serial.printf("flash_write: esp_ota_begin failed: %s\n", esp_err_to_name(err));
    return;
  }

  memset(g_chunk, 0xA5, CHUNK);
  g_chunk[0] = 0xE9;  // ESP_IMAGE_HEADER_MAGIC — without this, the first write is rejected

  uint32_t written = 0;
  for (int i = 0; i < 256; i++) {  // 256 x 4 KiB = 1 MiB
    err = esp_ota_write(h, g_chunk, CHUNK);
    if (err != ESP_OK) {
      Serial.printf("flash_write: esp_ota_write failed at %u KiB: %s\n", written / 1024,
                    esp_err_to_name(err));
      break;
    }
    written += CHUNK;
    if (i == 0) g_chunk[0] = 0xA5;  // only the first chunk carries the header
  }
  Serial.printf("flash_write: wrote %u KiB\n", written / 1024);
  esp_ota_abort(h);  // NEVER esp_ota_set_boot_partition() — we are measuring, not installing
}

/* The real thing: receive and write concurrently, exactly as nff_ota.c does.
 * The served blob starts with 0xE9 so esp_ota_write() accepts it (see phase_flash_write). */
static void phase_ota(void) {
  if (!wifi_connect()) {
    Serial.println("ota: wifi connect FAILED");
    return;
  }
  const esp_partition_t *part = esp_ota_get_next_update_partition(NULL);
  if (!part) {
    Serial.println("ota: NO OTA PARTITION — nothing measured");
    return;
  }
  esp_ota_handle_t h;
  esp_err_t err = esp_ota_begin(part, OTA_SIZE_UNKNOWN, &h);
  if (err != ESP_OK) {
    Serial.printf("ota: esp_ota_begin failed: %s\n", esp_err_to_name(err));
    return;
  }

  uint32_t written = 0;
  HTTPClient http;
  http.begin(BLOB_URL);
  const int code = http.GET();
  if (code == HTTP_CODE_OK) {
    WiFiClient *s = http.getStreamPtr();
    while (http.connected()) {
      int n = s->readBytes(g_chunk, CHUNK);
      if (n <= 0) break;
      err = esp_ota_write(h, g_chunk, n);
      if (err != ESP_OK) {
        Serial.printf("ota: esp_ota_write failed at %u KiB: %s\n", written / 1024,
                      esp_err_to_name(err));
        break;
      }
      written += n;
    }
  } else {
    Serial.printf("ota: HTTP GET failed: %d\n", code);
  }
  Serial.printf("ota: %u KiB received and written\n", written / 1024);
  http.end();
  esp_ota_abort(h);
}

/* ---- sequence ---------------------------------------------------------------------------- */

void setup() {
  // The prints go nowhere once USB is unplugged (which it must be, to measure) — but with USB
  // in, they are the only way to see a phase silently doing nothing. The 0.3 s "flash_write"
  // that wrote zero bytes looked exactly like a cheap flash write until it was printed.
  Serial.begin(115200);
  delay(200);
  Serial.println("\n=== nff workload profile ===");
  const esp_partition_t *p = esp_ota_get_next_update_partition(NULL);
  if (p) {
    Serial.printf("OTA target partition: %s  %u KiB @ 0x%06x\n", p->label, p->size / 1024,
                  p->address);
  } else {
    Serial.println("!! NO SPARE OTA PARTITION — the flash phases cannot run");
  }
  Serial.printf("blob: %s\n", BLOB_URL);

  WiFi.persistent(false);
  WiFi.mode(WIFI_OFF);
}

void loop() {
  marker(MARKER_LONG_MS);  // cycle start
  phase_idle();            // 1. idle       — radio off, CPU parked
  marker(MARKER_MS);
  phase_cpu();             // 2. cpu        — radio off, CPU flat out
  marker(MARKER_MS);
  phase_connect();         // 3. connect    — associate from cold
  marker(MARKER_MS);
  phase_wifi_idle();       // 4. wifi_idle  — connect, then sit there connected
  marker(MARKER_MS);
  phase_download();        // 5. download   — connect, then pull 1 MiB
  marker(MARKER_MS);
  phase_flash_write();     // 6. flash      — erase + write 1 MiB, radio off
  marker(MARKER_MS);
  phase_ota();             // 7. ota        — connect, pull 1 MiB, write it to flash
}
