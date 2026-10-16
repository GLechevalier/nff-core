/**
 * arduino_bootstrap.ino — nff-sdk-c batch-claim bootstrap example (DEVICE_OWNERSHIP_DESIGN.md §8/§12).
 *
 * This is the ONE firmware image you flash to a whole batch. Every board boots on the SHARED
 * bootstrap credential, announces itself UNCLAIMED, and idles until you Accept it in the dashboard;
 * the fleet then rolls over a unique per-device cert (stored in NVS) and the board reboots CLAIMED.
 * The image is identical across the batch — device_id is filled at runtime from the hardware id.
 *
 * Before flashing:
 *   1. Provision a batch:
 *        nff provision batch --project <project_id-uuid> --count 2 --out credentials.h
 *   2. Copy the generated credentials.h into THIS sketch folder (overwrites the placeholder).
 *   3. Set WIFI_SSID / WIFI_PASS to the hotspot the computer is also on, and HOST_IP to the
 *      computer's IP on that interface (ipconfig) — the broker the device connects to.
 *   4. Flash to COM10:  nff flash nff-sdk-c/examples/arduino_bootstrap
 *   5. Monitor at 115200 baud and watch for the announce; Accept in the dashboard Enroll tab.
 *
 * NOTE: NFF_BOOTSTRAP_ENABLED must be 1 (set below before any nff include). It gates the
 * bootstrap/rollover code paths AND bumps the MQTT RX buffer to 8 KB so the multi-KB rollover_cert
 * command isn't dropped. Keep Serial baud == `nff monitor` baud (115200) or the log is garbled.
 */

// NFF_BOOTSTRAP_ENABLED is set globally via build_opt.h (-DNFF_BOOTSTRAP_ENABLED=1) so the flag
// reaches the nff library translation units (nff_claim.c), not just this sketch. A bare #define
// here would only affect the .ino and the bootstrap functions would fail to link.
#include <WiFi.h>
#include <nff.h>
#include "credentials.h"    // shared bootstrap header from: nff provision batch ...

// ---- Configuration -------------------------------------------------------

#define WIFI_SSID  "YOUR_WIFI_SSID"          // router both the computer and ESP32 join (no client isolation)
#define WIFI_PASS  "YOUR_WIFI_PASS"
#define HOST_IP    "192.168.1.11"            // computer's $HOST_IP on the Freebox LAN (ipconfig WiFi)

// ---- nff bootstrap config ------------------------------------------------
// device_id is left empty on purpose; nff_init() fills it with the board's hardware id
// (efuse / WiFi MAC) so this image is identical across the whole batch.

static nff_config_t g_cfg = NFF_BOOTSTRAP_CONFIG_INITIALIZER(HOST_IP);

// --------------------------------------------------------------------------

void setup() {
    Serial.begin(115200);

    Serial.printf("Connecting to WiFi %s...\n", WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 30000) {
        delay(250);
        Serial.print(".");
    }
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("\nWiFi failed — restarting");
        ESP.restart();
    }
    Serial.printf("\nWiFi connected, IP: %s\n", WiFi.localIP().toString().c_str());

    // NVS-first: if a per-device operational cert is already stored, come up CLAIMED.
    if (nff_init(&g_cfg) != NFF_OK) {
        Serial.println("nff_init failed");
        return;
    }

    if (nff_get_mode() == NFF_MODE_BOOTSTRAP) {
        // Unclaimed: connect on the shared batch credential, announce, await rollover.
        // Reboots into CLAIMED mode once the fleet delivers the per-device cert.
        Serial.println("nff: BOOTSTRAP mode — announcing for enrollment");
        nff_bootstrap_run();
    } else {
        // Already claimed (cert loaded from NVS): connect operationally.
        Serial.println("nff: CLAIMED mode — connecting operationally");
        nff_connect();
    }
}

void loop() {
    nff_loop();
    delay(10);
}
