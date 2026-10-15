/**
 * main.c — maintained nff Level-2 (QEMU) demo firmware.
 *
 * The Stage-0 spike (nff-mock/qemu/spike/firmware/main/main.c) PROVED the path, but compiled its
 * identity in via credentials.h. This productionizes it: identity + creds + boot_epoch are read from
 * NVS (spliced in per-device by the supervisor), so ONE generic image serves every mock. Networking
 * (openeth) and the NVS reader live in the shared nff_l2_net component.
 *
 * Boot order: NVS init -> openeth up (no WiFi radio in QEMU) -> sane clock (QEMU has no RTC) ->
 * load identity from NVS -> nff_init/connect/start_task. The real mTLS handshake, retained
 * heartbeat, OTA + crash-with-coredump-backtrace are all the unmodified SDK.
 *
 * Build: ESP-IDF (openeth is IDF-only). See ../README.md.
 */

#include "nff.h"
#include "nff_l2_net.h"

#include "esp_log.h"
#include "esp_system.h"
#include "nvs_flash.h"

static const char *TAG = "nff_l2";

/* OTA health gate (kept from the spike; harmless and exercises the rollback path). */
static bool on_health_check(void *ctx) {
    (void)ctx;
    return esp_get_free_heap_size() >= 40000;
}

void app_main(void) {
    nvs_flash_init();

    nff_l2_openeth_up();        /* 1. network up via Ethernet (no WiFi radio in QEMU) */
    nff_l2_set_clock_from_nvs();/* 2. sane clock BEFORE any TLS handshake */

    /* MUST be static: the SDK keeps a pointer to this config (nff.h), and the nff task uses it long
     * after app_main returns — a stack local would dangle and crash the heartbeat-topic format. */
    static nff_config_t cfg;
    if (nff_l2_load_config_from_nvs(&cfg) != 0) {   /* 3. per-device identity from spliced NVS */
        ESP_LOGE(TAG, "no usable identity in NVS — halting");
        return;
    }

    if (nff_init(&cfg) != NFF_OK) {
        ESP_LOGE(TAG, "nff_init failed");
        return;
    }
    nff_register_health_check(on_health_check, NULL);

    /* 4. mTLS to the broker, retained heartbeat, command subscribe — all REAL. */
    if (nff_connect() == NFF_OK) {
        ESP_LOGI(TAG, "NFF_L2: heartbeat published");   /* UART marker the supervisor scans for */
    } else {
        ESP_LOGW(TAG, "NFF_L2: nff_connect failed (will retry in nff task)");
    }
    nff_log("nff-l2 started, fw=%s", cfg.fw_version);   /* fallback-resolved in nvs_creds.c */

    nff_start_task();           /* nff task on Core 0 handles reconnect/heartbeat/commands/OTA */
}
