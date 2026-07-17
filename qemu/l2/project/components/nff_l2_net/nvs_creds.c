/**
 * nvs_creds.c — read this device's identity/credentials from NVS and build an nff_config_t.
 *
 * The supervisor splices a per-device NVS partition into the emulated flash at launch (see
 * nff-mock/qemu/nff_mock_l2/credentials.py for the namespace + key contract), so ONE generic L2
 * image serves every device. Certs/key/CA are stored as DER (X.509 DER certs + PKCS#8 DER key — the
 * forms nff_config_t takes; the SDK wraps the key as a PKCS#8 "PRIVATE KEY" PEM), read verbatim as
 * blobs. The splice (credentials.py materialize_der_creds) converts the fleet-provisioned PEM to DER.
 */

#include "nff_l2_net.h"

#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"
#include <sys/time.h>
#include <stdlib.h>
#include <string.h>

static const char *TAG = "nff_l2_creds";

/* Build-injected firmware identity (Part 4 passes -DNFF_*_TOKEN; nff.h stringifies them). Fall back
 * so the component always compiles even for a bare demo build with no tokens. */
#ifndef NFF_FW_VERSION
#  define NFF_FW_VERSION "0.0.0-l2"
#endif
#ifndef NFF_BUILD_ID
#  define NFF_BUILD_ID ""
#endif
#ifndef NFF_FQBN
#  define NFF_FQBN "esp32:esp32:esp32"
#endif
/* L2 is an emulated device, not real silicon. It must advertise "nff-qemu-esp32" in its heartbeat so
 * the OTA plane (nff-ota build_and_deploy_source) routes it to the real server-side compile path
 * instead of L1's regex analyzer. The port's silicon probe would report plain "esp32" (the QEMU
 * esp32 machine == CONFIG_IDF_TARGET), so we override via cfg->device_type. This file is L2-only. */
#ifndef NFF_DEVICE_TYPE
#  define NFF_DEVICE_TYPE "nff-qemu-esp32"
#endif

/* The NVS key names — MUST mirror nff_mock_l2/credentials.py. */
#define K_DEVICE_ID   "device_id"
#define K_PROJECT_ID  "project_id"
#define K_BROKER_HOST "broker_host"
#define K_BOOT_EPOCH  "boot_epoch"
#define K_DEVICE_CRT  "device_crt"
#define K_DEVICE_KEY  "device_key"
#define K_CA_CRT      "ca_crt"
#define K_PROJECT_CA  "proj_ca_crt"
#define K_CMD_VKEY    "cmd_vkey"

/* ---- small helpers ---------------------------------------------------------------- */

/* Read an NVS string into a freshly-malloc'd, null-terminated buffer (caller owns it forever). */
static char *nvs_dup_str(nvs_handle_t h, const char *key) {
    size_t len = 0;
    if (nvs_get_str(h, key, NULL, &len) != ESP_OK || len == 0) return NULL;
    char *buf = (char *)malloc(len);
    if (!buf) return NULL;
    if (nvs_get_str(h, key, buf, &len) != ESP_OK) { free(buf); return NULL; }
    return buf;
}

/* Read an NVS blob into a malloc'd buffer (caller owns it forever). DER certs/keys are passed to the
 * SDK by pointer+length verbatim. */
static uint8_t *nvs_dup_blob(nvs_handle_t h, const char *key, size_t *out_len) {
    size_t len = 0;
    if (nvs_get_blob(h, key, NULL, &len) != ESP_OK || len == 0) return NULL;
    uint8_t *buf = (uint8_t *)malloc(len);
    if (!buf) return NULL;
    if (nvs_get_blob(h, key, buf, &len) != ESP_OK) { free(buf); return NULL; }
    *out_len = len;
    return buf;
}

/* ---- public API ------------------------------------------------------------------- */

void nff_l2_set_clock_from_nvs(void) {
    int64_t epoch = 1750000000;   /* 2025-06-15 floor; overridden by NVS when present */
    nvs_handle_t h;
    if (nvs_open("nff", NVS_READONLY, &h) == ESP_OK) {
        char buf[24] = {0};
        size_t len = sizeof(buf);
        if (nvs_get_str(h, K_BOOT_EPOCH, buf, &len) == ESP_OK) {
            int64_t v = strtoll(buf, NULL, 10);
            if (v > 1000000000LL) epoch = v;   /* sanity floor (2001-09-09) */
        }
        nvs_close(h);
    }
    struct timeval tv = { .tv_sec = (time_t)epoch, .tv_usec = 0 };
    settimeofday(&tv, NULL);
    ESP_LOGI(TAG, "clock set to boot_epoch=%lld (no SNTP)", (long long)epoch);
}

int nff_l2_load_config_from_nvs(nff_config_t *cfg) {
    memset(cfg, 0, sizeof(*cfg));

    nvs_handle_t h;
    if (nvs_open("nff", NVS_READONLY, &h) != ESP_OK) {
        ESP_LOGE(TAG, "no 'nff' NVS namespace — was the identity spliced in?");
        return -1;
    }

    cfg->device_id   = nvs_dup_str(h, K_DEVICE_ID);
    cfg->project_id  = nvs_dup_str(h, K_PROJECT_ID);
    cfg->broker_host = nvs_dup_str(h, K_BROKER_HOST);
    cfg->broker_port = 8883;

    /* DER blobs straight from NVS — X.509 DER certs + PKCS#8 DER key (the splice converted the
     * fleet PEM). nff_config_t takes DER; the SDK's set_tls wraps them back to PEM for esp-tls. */
    cfg->client_cert = nvs_dup_blob(h, K_DEVICE_CRT, &cfg->client_cert_len);
    cfg->client_key  = nvs_dup_blob(h, K_DEVICE_KEY, &cfg->client_key_len);
    cfg->ca_cert     = nvs_dup_blob(h, K_CA_CRT,     &cfg->ca_cert_len);
    /* Project intermediate CA is optional (untenanted dev has none). When present, the SDK presents
     * leaf||intermediate so the broker chains to root. */
    cfg->intermediate_cert = nvs_dup_blob(h, K_PROJECT_CA, &cfg->intermediate_cert_len);

    /* Command-verify key: the 65-byte raw P-256 public point, stored verbatim (no PEM). Optional —
     * absent it, connect+heartbeat work but signed commands can't be verified. */
    cfg->cmd_verify_key = nvs_dup_blob(h, K_CMD_VKEY, &cfg->cmd_verify_key_len);

    /* OTA-adopted firmware version: the SDK's OTA commit path (nff_ota.c nff_ota_commit) records the
     * running image's version under "fw_adopted" so heartbeat telemetry reflects what actually
     * booted, not the build-time NFF_FW_VERSION baked into this generic L2 image. Without this, an
     * L2 forever heartbeats the baked constant and migration 0080's version-anchored recovery check
     * (semver_gt) can never observe a fix. Absent (never-OTA'd) => NULL => keep the baked default. */
    char *adopted_fw = nvs_dup_str(h, "fw_adopted");
    if (adopted_fw && !adopted_fw[0]) { free(adopted_fw); adopted_fw = NULL; }

    nvs_close(h);

    /* Firmware-image identity is baked at build time, not per-device — but the OTA-adopted version,
     * when present, overrides the baked default so post-OTA telemetry reports the real version. */
    cfg->fw_version           = adopted_fw ? adopted_fw : NFF_FW_VERSION;
    cfg->build_id             = NFF_BUILD_ID;
    cfg->fqbn                 = NFF_FQBN;
    cfg->device_type          = NFF_DEVICE_TYPE;  /* "nff-qemu-esp32" — overrides the silicon probe */
    cfg->heartbeat_interval_s = 30;
    cfg->batch_id             = NULL;

    if (!cfg->device_id || !cfg->client_cert || !cfg->client_key || !cfg->ca_cert) {
        ESP_LOGE(TAG, "incomplete NVS identity (id/cert/key/ca) — cannot connect");
        return -1;
    }
    ESP_LOGI(TAG, "loaded identity '%s' (project '%s') from NVS",
             cfg->device_id, cfg->project_id ? cfg->project_id : "");
    return 0;
}
