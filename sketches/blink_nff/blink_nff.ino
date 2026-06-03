#include <WiFi.h>
#include <nff.h>
#include "credentials.h"

#ifndef LED_BUILTIN
#define LED_BUILTIN 2
#endif

#define WIFI_SSID       "iPhone de lechevalier"
#define WIFI_PASS       "Pomme4217"
#define DEVICE_ID       "test-device-01"
#define NFF_BROKER_HOST "172.20.10.3"

// Blink rate changes with major version — OTA result is immediately visible on the LED:
//   v1.x.x → 1 000 ms (slow)
//   v2.x.x → 200 ms  (fast)
#define BLINK_MS  (NFF_FW_VERSION[0] == '1' ? 1000UL : 200UL)

NFF_CONFIG(g_cfg, DEVICE_ID, NFF_BROKER_HOST);

static const char *state_name(nff_state_t s) {
    switch (s) {
        case NFF_STATE_UNINIT:      return "UNINIT";
        case NFF_STATE_CONNECTING:  return "CONNECTING";
        case NFF_STATE_CONNECTED:   return "CONNECTED";
        case NFF_STATE_OTA_ACTIVE:  return "OTA_ACTIVE";
        case NFF_STATE_ERROR:       return "ERROR";
        default:                    return "UNKNOWN";
    }
}

void setup() {
    Serial.begin(9600);
    pinMode(LED_BUILTIN, OUTPUT);

    Serial.printf("blink_nff v%s  blink=%lums\n", NFF_FW_VERSION, BLINK_MS);

    Serial.printf("Connecting to %s", WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) {
        delay(500);
        Serial.print(".");
    }
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println(" FAILED — restarting in 5s");
        delay(5000);
        ESP.restart();
    }
    Serial.printf(" ok\nIP: %s\n", WiFi.localIP().toString().c_str());

    if (nff_init(&g_cfg) != NFF_OK) {
        Serial.println("[nff] init failed");
        return;
    }
    nff_connect();
    nff_log("blink_nff started, fw=%s, blink=%lums", NFF_FW_VERSION, BLINK_MS);
}

static unsigned long lastBlink = 0;
static bool          ledState  = false;
static unsigned long lastPrint = 0;

void loop() {
    nff_loop();

    unsigned long now = millis();

    if (now - lastBlink >= BLINK_MS) {
        lastBlink = now;
        ledState  = !ledState;
        digitalWrite(LED_BUILTIN, ledState ? HIGH : LOW);
    }

    if (now - lastPrint >= 5000) {
        lastPrint = now;
        nff_state_t s = nff_get_state();
        Serial.printf("[nff] state=%s  uptime=%lus  fw=%s\n",
                      state_name(s), now / 1000, NFF_FW_VERSION);
    }

    delay(10);
}
