// clean_blink_l2 — the CLEAN L2 (QEMU) baseline the e2e cold-boots before it seeds a buggy update.
//
// Same structure as e2e/fixtures/buggy_blink_l2.ino (NVS identity via the nff_l2_net helpers, WiFi->
// openeth shim), but the sensor read is GUARDED, so the device connects and holds online forever.
// The self-heal e2e spawns this, then OTAs a buggy image onto it (which crash-loops), then the agent
// ships a fix. Compiled the same way (build_arduino_shim.sh) so it picks up the SDK heartbeat
// device_type override (reports "nff-qemu-esp32" via nff_l2_net/nvs_creds.c).

#include <WiFi.h>          // redefined to the openeth shim by the force-included nff_wifi_shim.h
#include <nff.h>
#include "nff_l2_net.h"    // nff_l2_openeth_up / set_clock_from_nvs / load_config_from_nvs

static nff_config_t g_cfg;   // static: the nff task keeps a pointer to it after setup() returns

struct Reading { int celsius; };

static Reading *read_sensor() __attribute__((noinline));
static Reading *read_sensor() {
    static Reading r;
    r.celsius = 21;
    volatile bool bus_busy = false;   // clean baseline: the bus is available, a reading is returned
    return bus_busy ? (Reading *)0 : &r;
}

void setup() {
    Serial.begin(9600);
    WiFi.begin("nff-l2", "openeth");
    while (WiFi.status() != WL_CONNECTED) {
        delay(200);
    }
    nff_l2_set_clock_from_nvs();
    if (nff_l2_load_config_from_nvs(&g_cfg) != 0) {
        Serial.println("[nff-l2] no usable identity in NVS — halting");
        return;
    }
    if (nff_init(&g_cfg) != NFF_OK) {
        Serial.println("[nff-l2] nff_init failed");
        return;
    }
    nff_connect();
    nff_log("clean_blink_l2 started, fw=%s", g_cfg.fw_version);
}

void loop() {
    nff_loop();   // keep MQTT/OTA/heartbeat/commands serviced

    Reading *r = read_sensor();
    if (r != NULL) {                       // GUARDED — the fix the agent would apply to the buggy twin
        Serial.printf("temp=%dC\n", r->celsius);
    }
    delay(1000);
}
