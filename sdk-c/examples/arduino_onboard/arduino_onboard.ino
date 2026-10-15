/**
 * arduino_onboard.ino — nff-sdk-c GOLDEN onboarding image for the browser flasher.
 *
 * ONE byte-identical image for every user. It carries no real credentials — only empty,
 * magic-prefixed patch slots (see credentials.h). The dashboard flasher provisions the user's
 * project via /api/provision-batch and patches WiFi + the bootstrap creds into the .bin bytes at
 * flash time, then flashes. On boot the device connects on the shared batch credential, announces
 * UNCLAIMED, is auto-accepted, and the fleet rolls over a unique per-device cert (stored in NVS) —
 * it then reboots CLAIMED and comes online in the user's project.
 *
 * NFF_BOOTSTRAP_ENABLED=1 is set globally via build_opt.h so it reaches the library TUs
 * (nff_claim.c) and bumps the MQTT RX buffer to 8 KB for the multi-KB rollover_cert.
 */

#include <WiFi.h>
#include <nff.h>
#include "credentials.h"    // patchable slots + WIFI_SSID/WIFI_PASS + nff_onboard_fill_config

static nff_config_t g_cfg;

void setup() {
    Serial.begin(115200);
    nff_onboard_fill_config(&g_cfg);   // pointers/lengths from the patched slots

    Serial.printf("nff onboard: connecting to WiFi '%s'...\n", WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 30000) {
        delay(250);
        Serial.print(".");
    }
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("\nWiFi failed - restarting in 5s");
        delay(5000);
        ESP.restart();
    }
    Serial.printf("\nWiFi connected, IP: %s -> broker %s:8883\n",
                  WiFi.localIP().toString().c_str(), g_cfg.broker_host);

    // NVS-first: if a per-device operational cert is already stored, come up CLAIMED.
    if (nff_init(&g_cfg) != NFF_OK) {
        Serial.println("nff_init failed");
        return;
    }

    if (nff_get_mode() == NFF_MODE_BOOTSTRAP) {
        Serial.println("nff: BOOTSTRAP mode - announcing for enrollment");
        nff_bootstrap_run();   // connect on shared cred, announce, await rollover, reboot CLAIMED
    } else {
        Serial.println("nff: CLAIMED mode - connecting operationally");
        nff_connect();
    }
}

void loop() {
    nff_loop();
    delay(10);
}
