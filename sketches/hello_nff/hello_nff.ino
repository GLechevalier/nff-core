#include <WiFi.h>
#include <nff.h>
#include "credentials.h"

#ifndef LED_BUILTIN
#define LED_BUILTIN 2
#endif

#define WIFI_SSID      "iPhone de lechevalier"       // <-- fill in before flashing
#define WIFI_PASS      "Pomme4217"   // <-- fill in before flashing
#define DEVICE_ID      "test-device-01"
#define NFF_BROKER_HOST "172.20.10.3" // PC hotspot IP

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

static void on_led(const char *payload, char *resp, size_t resp_len, void *ctx) {
    (void)ctx;
    bool on = strstr(payload, "\"on\":true") != NULL;
    digitalWrite(LED_BUILTIN, on ? HIGH : LOW);
    snprintf(resp, resp_len, "{\"type\":\"led_ack\",\"on\":%s}", on ? "true" : "false");
    nff_log("led cmd: on=%d", (int)on);
}

void setup() {
    Serial.begin(9600);
    pinMode(LED_BUILTIN, OUTPUT);

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
    nff_register_command("led", on_led, NULL);
    nff_connect();
    nff_log("hello_nff started, fw=%s", NFF_FW_VERSION);
    Serial.printf("[nff] connecting to broker %s:8883...\n", NFF_BROKER_HOST);
}

static unsigned long lastPrint = 0;

void loop() {
    nff_loop();

    if (millis() - lastPrint >= 2000) {
        lastPrint = millis();
        nff_state_t s = nff_get_state();
        Serial.printf("[nff] state=%s  uptime=%lus\n", state_name(s), millis() / 1000);
    }
    delay(10);
}
