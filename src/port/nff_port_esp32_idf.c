/**
 * nff_port_esp32_idf.c — ESP-IDF platform implementation.
 *
 * Covers: FreeRTOS mutex/task, esp-mqtt event-driven MQTT, NVS, esp_https_ota,
 * mbedTLS ECDSA, esp_cpu_stall (dual-core AMP), esp_reset_reason (crash detect).
 */

#if defined(ESP_PLATFORM) && !defined(ARDUINO)

#include "nff.h"
#include "nff_port.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_system.h"
#include "esp_mac.h"
#include "esp_cpu.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "esp_https_ota.h"
#include "esp_ota_ops.h"
#include "esp_wifi.h"
#include "mqtt_client.h"
#include "mbedtls/ecdsa.h"
#include "mbedtls/ecp.h"
#include "mbedtls/sha256.h"
#include "mbedtls/base64.h"  /* DER->PEM so a leaf+intermediate chain can be presented */
#include <string.h>
#include <stdlib.h>
#include <stdio.h>

/* ------------------------------------------------------------------ */
/* Time                                                                 */
/* ------------------------------------------------------------------ */

uint32_t nff_port_millis(void) {
    return (uint32_t)(esp_timer_get_time() / 1000);
}

void nff_port_delay_ms(uint32_t ms) {
    vTaskDelay(pdMS_TO_TICKS(ms));
}

/* ------------------------------------------------------------------ */
/* Mutex                                                                */
/* ------------------------------------------------------------------ */

nff_mutex_t nff_port_mutex_create(void) {
    return (nff_mutex_t)xSemaphoreCreateMutex();
}

void nff_port_mutex_lock(nff_mutex_t m) {
    xSemaphoreTake((SemaphoreHandle_t)m, portMAX_DELAY);
}

void nff_port_mutex_unlock(nff_mutex_t m) {
    xSemaphoreGive((SemaphoreHandle_t)m);
}

void nff_port_mutex_destroy(nff_mutex_t m) {
    vSemaphoreDelete((SemaphoreHandle_t)m);
}

/* ------------------------------------------------------------------ */
/* Task                                                                 */
/* ------------------------------------------------------------------ */

void nff_port_task_create(void (*fn)(void *), const char *name,
                           uint32_t stack_bytes, void *arg,
                           uint8_t priority, int core) {
    int c = (core == 0) ? PRO_CPU_NUM : (core == 1) ? APP_CPU_NUM : tskNO_AFFINITY;
    xTaskCreatePinnedToCore((TaskFunction_t)fn, name,
                             stack_bytes / sizeof(StackType_t),
                             arg, priority, NULL, c);
}

/* ------------------------------------------------------------------ */
/* MQTT (esp-mqtt event-driven)                                        */
/* ------------------------------------------------------------------ */

struct nff_mqtt_handle {
    esp_mqtt_client_handle_t client;
    bool                     connected;
    const uint8_t           *ca;    size_t ca_len;
    const uint8_t           *cert;  size_t cert_len;
    const uint8_t           *key;   size_t key_len;
    const uint8_t           *inter; size_t inter_len;  /* project intermediate CA (optional) */
    bool                     skip_cn;  /* runtime QEMU concession (cfg->tls_skip_cn_check) */
    /* PEM forms built at connect (esp-mqtt wants PEM to send a multi-cert client chain). */
    char                    *ca_pem;
    char                    *cert_pem;  /* leaf, or leaf||intermediate chain */
    char                    *key_pem;
    char                     host[256];
    uint16_t                 port;
    char                     lwt_topic[128];
    char                     lwt_payload[256];
    void (*rx_cb)(const char *, const uint8_t *, size_t, void *);
    void *rx_user;
};

static void mqtt_event_handler(void *handler_arg, esp_event_base_t base,
                                int32_t event_id, void *event_data) {
    struct nff_mqtt_handle *h = (struct nff_mqtt_handle *)handler_arg;
    esp_mqtt_event_handle_t ev = (esp_mqtt_event_handle_t)event_data;

    switch ((esp_mqtt_event_id_t)event_id) {
    case MQTT_EVENT_CONNECTED:
        h->connected = true;
        break;
    case MQTT_EVENT_DISCONNECTED:
        h->connected = false;
        break;
    case MQTT_EVENT_DATA:
        if (h->rx_cb) {
            /* ev->topic is NOT null-terminated; copy */
            char topic[NFF_TOPIC_MAXLEN];
            int tlen = ev->topic_len < (int)sizeof(topic) - 1
                       ? ev->topic_len : (int)sizeof(topic) - 1;
            memcpy(topic, ev->topic, (size_t)tlen);
            topic[tlen] = '\0';
            h->rx_cb(topic, (const uint8_t *)ev->data,
                     (size_t)ev->data_len, h->rx_user);
        }
        break;
    default:
        break;
    }
}

nff_mqtt_handle_t *nff_port_mqtt_create(void) {
    struct nff_mqtt_handle *h = calloc(1, sizeof(*h));
    return (nff_mqtt_handle_t *)h;
}

void nff_port_mqtt_set_server(nff_mqtt_handle_t *h,
                               const char *host, uint16_t port) {
    struct nff_mqtt_handle *mh = (struct nff_mqtt_handle *)h;
    strncpy(mh->host, host, sizeof(mh->host) - 1);
    mh->port = port;
}

/* Convert DER bytes to a null-terminated PEM string (heap-allocated, 64-col).
 * Mirrors the Arduino port's helper so both ports present an identical chain. */
static char *der_to_pem(const char *header, const char *footer,
                        const uint8_t *der, size_t der_len) {
    if (!der || !der_len) return NULL;
    size_t b64_len = 0;
    mbedtls_base64_encode(NULL, 0, &b64_len, der, der_len);

    uint8_t *b64 = (uint8_t *)malloc(b64_len + 1);
    if (!b64) return NULL;
    size_t actual = 0;
    if (mbedtls_base64_encode(b64, b64_len + 1, &actual, der, der_len) != 0) {
        free(b64);
        return NULL;
    }

    size_t hdr = strlen(header), ftr = strlen(footer);
    size_t lines = (actual + 63) / 64;
    char *pem = (char *)malloc(hdr + 1 + actual + lines + ftr + 2);
    if (!pem) { free(b64); return NULL; }

    char *p = pem;
    memcpy(p, header, hdr); p += hdr; *p++ = '\n';
    for (size_t pos = 0; pos < actual; ) {
        size_t n = (actual - pos > 64) ? 64 : (actual - pos);
        memcpy(p, b64 + pos, n); p += n; *p++ = '\n'; pos += n;
    }
    memcpy(p, footer, ftr); p += ftr; *p++ = '\n'; *p = '\0';
    free(b64);
    return pem;
}

void nff_port_mqtt_set_tls(nff_mqtt_handle_t *h,
                            const uint8_t *ca, size_t ca_len,
                            const uint8_t *cert, size_t cert_len,
                            const uint8_t *key, size_t key_len,
                            const uint8_t *inter, size_t inter_len) {
    struct nff_mqtt_handle *mh = (struct nff_mqtt_handle *)h;
    mh->ca       = ca;    mh->ca_len    = ca_len;
    mh->cert     = cert;  mh->cert_len  = cert_len;
    mh->key      = key;   mh->key_len   = key_len;
    mh->inter    = inter; mh->inter_len = inter_len;
}

void nff_port_mqtt_set_skip_cn_check(nff_mqtt_handle_t *h, bool skip) {
    struct nff_mqtt_handle *mh = (struct nff_mqtt_handle *)h;
    mh->skip_cn = skip;
}

void nff_port_mqtt_set_rx_callback(nff_mqtt_handle_t *h,
                                    void (*cb)(const char *, const uint8_t *,
                                               size_t, void *),
                                    void *user) {
    struct nff_mqtt_handle *mh = (struct nff_mqtt_handle *)h;
    mh->rx_cb   = cb;
    mh->rx_user = user;
}

int nff_port_mqtt_connect(nff_mqtt_handle_t *h,
                           const char *client_id,
                           const char *lwt_topic,
                           const char *lwt_payload) {
    struct nff_mqtt_handle *mh = (struct nff_mqtt_handle *)h;
    if (lwt_topic)   strncpy(mh->lwt_topic,   lwt_topic,   sizeof(mh->lwt_topic)   - 1);
    if (lwt_payload) strncpy(mh->lwt_payload,  lwt_payload, sizeof(mh->lwt_payload) - 1);

    /* Destroy existing client if reconnecting */
    if (mh->client) {
        esp_mqtt_client_destroy(mh->client);
        mh->client = NULL;
    }

    char uri[320];
    snprintf(uri, sizeof(uri), "mqtts://%s:%u", mh->host, (unsigned)mh->port);

    /* Build PEM buffers from the DER inputs. esp-mqtt's authentication.certificate accepts a
     * concatenated leaf||intermediate PEM and sends both in the TLS Certificate message, so a
     * broker that only trusts the root can build leaf -> intermediate -> root. Leaf-only
     * (inter == NULL) preserves single-cert behaviour. Rebuilt each connect; freed here first so
     * a reconnect does not leak. */
    free(mh->ca_pem);   mh->ca_pem   = NULL;
    free(mh->cert_pem); mh->cert_pem = NULL;
    free(mh->key_pem);  mh->key_pem  = NULL;
    mh->ca_pem  = der_to_pem("-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----",
                             mh->ca, mh->ca_len);
    mh->key_pem = der_to_pem("-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----",
                             mh->key, mh->key_len);
    mh->cert_pem = der_to_pem("-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----",
                              mh->cert, mh->cert_len);
    if (mh->cert_pem && mh->inter && mh->inter_len) {
        char *inter_pem = der_to_pem("-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----",
                                     mh->inter, mh->inter_len);
        if (inter_pem) {
            char *chain = (char *)malloc(strlen(mh->cert_pem) + strlen(inter_pem) + 1);
            if (chain) {
                strcpy(chain, mh->cert_pem);   /* der_to_pem terminates the leaf with '\n' */
                strcat(chain, inter_pem);
                free(mh->cert_pem);
                mh->cert_pem = chain;
            }
            free(inter_pem);
        }
    }

    const esp_mqtt_client_config_t cfg = {
        .broker = {
            .address.uri = uri,
            .verification.certificate     = mh->ca_pem,
            .verification.certificate_len = mh->ca_pem ? strlen(mh->ca_pem) + 1 : 0,
            /* L2/QEMU only, decided at RUNTIME (the image is universal): the fleet server cert's
             * SAN omits the dialed guest IP, so the hostname check would reject it. Mirrors the
             * Python mock's tls_insecure_hostname. Only the QEMU NVS identity loader sets the
             * flag (via cfg->tls_skip_cn_check) — real hardware keeps CN/SAN verification on. */
            .verification.skip_cert_common_name_check = mh->skip_cn,
        },
        .credentials = {
            .client_id         = client_id,
            .authentication = {
                .certificate     = mh->cert_pem,
                .certificate_len = mh->cert_pem ? strlen(mh->cert_pem) + 1 : 0,
                .key             = mh->key_pem,
                .key_len         = mh->key_pem ? strlen(mh->key_pem) + 1 : 0,
            },
        },
        .session = {
            .last_will = {
                .topic   = mh->lwt_topic,
                .msg     = mh->lwt_payload,
                .qos     = 1,
                .retain  = 1,
            },
        },
        /* Match the Arduino ports: ensure the RX/TX buffer fits OTA-sized
           commands. esp-mqtt defaults to 1024, but set it explicitly. */
        .buffer = {
            .size = NFF_MQTT_BUFFER_SIZE,
        },
    };

    mh->client = esp_mqtt_client_init(&cfg);
    esp_mqtt_client_register_event(mh->client, ESP_EVENT_ANY_ID,
                                    mqtt_event_handler, mh);
    esp_mqtt_client_start(mh->client);
    /* Wait up to 5 s for connect */
    for (int i = 0; i < 50 && !mh->connected; i++) vTaskDelay(pdMS_TO_TICKS(100));
    return mh->connected ? 0 : -1;
}

int nff_port_mqtt_subscribe(nff_mqtt_handle_t *h, const char *topic, int qos) {
    struct nff_mqtt_handle *mh = (struct nff_mqtt_handle *)h;
    if (!mh->client) return -1;
    return esp_mqtt_client_subscribe(mh->client, topic, qos) >= 0 ? 0 : -1;
}

int nff_port_mqtt_publish(nff_mqtt_handle_t *h, const char *topic,
                           const char *payload, int qos, bool retain) {
    struct nff_mqtt_handle *mh = (struct nff_mqtt_handle *)h;
    if (!mh->client) return -1;
    int msg_id = esp_mqtt_client_publish(mh->client, topic, payload,
                                          (int)strlen(payload), qos, (int)retain);
    return (msg_id >= 0) ? 0 : -1;
}

void nff_port_mqtt_loop(nff_mqtt_handle_t *h) {
    (void)h;
    /* esp-mqtt is event-driven and runs its own task — no polling needed */
}

bool nff_port_mqtt_is_connected(nff_mqtt_handle_t *h) {
    struct nff_mqtt_handle *mh = (struct nff_mqtt_handle *)h;
    return mh && mh->connected;
}

/* ------------------------------------------------------------------ */
/* NVS                                                                  */
/* ------------------------------------------------------------------ */

static nvs_handle_t get_nvs_handle(void) {
    static nvs_handle_t h = 0;
    if (h == 0) {
        nvs_flash_init();
        nvs_open("nff", NVS_READWRITE, &h);
    }
    return h;
}

int nff_port_nvs_set_str(const char *key, const char *value) {
    return (nvs_set_str(get_nvs_handle(), key, value) == ESP_OK) ? 0 : -1;
}

int nff_port_nvs_get_str(const char *key, char *out, size_t out_len) {
    return (nvs_get_str(get_nvs_handle(), key, out, &out_len) == ESP_OK) ? 0 : -1;
}

int nff_port_nvs_set_u32(const char *key, uint32_t value) {
    return (nvs_set_u32(get_nvs_handle(), key, value) == ESP_OK) ? 0 : -1;
}

int nff_port_nvs_get_u32(const char *key, uint32_t *out) {
    return (nvs_get_u32(get_nvs_handle(), key, out) == ESP_OK) ? 0 : -1;
}

int nff_port_nvs_erase_key(const char *key) {
    return (nvs_erase_key(get_nvs_handle(), key) == ESP_OK) ? 0 : -1;
}

int nff_port_nvs_commit(void) {
    return (nvs_commit(get_nvs_handle()) == ESP_OK) ? 0 : -1;
}

/* ------------------------------------------------------------------ */
/* OTA                                                                  */
/* ------------------------------------------------------------------ */

struct ota_handle_s { esp_ota_handle_t esp_h; const esp_partition_t *part; };

nff_ota_handle_t nff_port_ota_begin(size_t expected_size) {
    struct ota_handle_s *h = malloc(sizeof(*h));
    if (!h) return NFF_OTA_INVALID_HANDLE;
    h->part = esp_ota_get_next_update_partition(NULL);
    if (!h->part || esp_ota_begin(h->part, expected_size, &h->esp_h) != ESP_OK) {
        free(h);
        return NFF_OTA_INVALID_HANDLE;
    }
    return (nff_ota_handle_t)h;
}

int nff_port_ota_write(nff_ota_handle_t hh, const uint8_t *buf, size_t len) {
    struct ota_handle_s *h = (struct ota_handle_s *)hh;
    return (esp_ota_write(h->esp_h, buf, len) == ESP_OK) ? 0 : -1;
}

int nff_port_ota_end(nff_ota_handle_t hh) {
    struct ota_handle_s *h = (struct ota_handle_s *)hh;
    if (esp_ota_end(h->esp_h) != ESP_OK) { free(h); return -1; }
    int rc = (esp_ota_set_boot_partition(h->part) == ESP_OK) ? 0 : -1;
    free(h);
    return rc;
}

void nff_port_ota_abort(nff_ota_handle_t hh) {
    struct ota_handle_s *h = (struct ota_handle_s *)hh;
    esp_ota_abort(h->esp_h);
    free(h);
}

int nff_port_ota_mark_valid(void) {
    /* Cancel any pending rollback for the running image. Harmless/idempotent on
       a non-rollback bootloader (returns ESP_ERR_NOT_SUPPORTED → treat as ok). */
    esp_err_t e = esp_ota_mark_app_valid_cancel_rollback();
    return (e == ESP_OK || e == ESP_ERR_NOT_SUPPORTED || e == ESP_ERR_INVALID_STATE) ? 0 : -1;
}

void nff_port_ota_rollback(void) {
    /* During probation the running partition is the new image; the other OTA
       slot still holds the previous (last-good) image and is the "next" update
       partition. Point the bootloader back at it and reboot — works regardless
       of CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE. */
    const esp_partition_t *prev = esp_ota_get_next_update_partition(NULL);
    if (prev) esp_ota_set_boot_partition(prev);
    esp_restart();
}

int nff_port_ota_repair_boot_partition(void) {
    const esp_partition_t *running = esp_ota_get_running_partition();
    const esp_partition_t *boot    = esp_ota_get_boot_partition();
    /* Compare by flash address, not pointer — the two lookups may return distinct
       esp_partition_t instances for the same slot. */
    if (!running || !boot || running->address == boot->address) return 0;
    /* The bootloader rejected the configured image and fell back to us: make
       the fallback durable so a power cycle doesn't retry the bad image. */
    esp_ota_set_boot_partition(running);
    return 1;
}

void nff_port_reboot(void) {
    esp_restart();
}

/* ------------------------------------------------------------------ */
/* HTTPS streaming (using esp_http_client)                             */
/* ------------------------------------------------------------------ */

#include "esp_http_client.h"

typedef struct {
    int (*chunk_cb)(const uint8_t *, size_t, void *);
    void *user_ctx;
    int   error;
    size_t resume_from;     /* >0 when this is a resume request */
    esp_http_client_handle_t client;
    int    checked_status;  /* 0 until we've validated the status code on first data */
} https_ctx_t;

static esp_err_t http_event_cb(esp_http_client_event_t *evt) {
    https_ctx_t *ctx = (https_ctx_t *)evt->user_data;
    if (evt->event_id == HTTP_EVENT_ON_DATA && !ctx->error) {
        /* On the first data event, validate the status. A resume request that comes back as 200
           (server ignored Range) would double-write the partition — refuse before any chunk. */
        if (!ctx->checked_status) {
            ctx->checked_status = 1;
            if (ctx->resume_from > 0 &&
                esp_http_client_get_status_code(ctx->client) == 200) {
                ctx->error = NFF_ERR_RESUME_UNSUPPORTED;
                return ESP_OK;
            }
        }
        int rc = ctx->chunk_cb((const uint8_t *)evt->data,
                                (size_t)evt->data_len, ctx->user_ctx);
        if (rc < 0) ctx->error = rc;
    }
    return ESP_OK;
}

int nff_port_https_get_stream(const char *url, size_t resume_from,
                               int (*chunk_cb)(const uint8_t *, size_t, void *),
                               void *user_ctx, uint32_t timeout_ms) {
    https_ctx_t ctx = { chunk_cb, user_ctx, 0, resume_from, NULL, 0 };
    esp_http_client_config_t cfg = {
        .url          = url,
        .event_handler = http_event_cb,
        .user_data     = &ctx,
        .timeout_ms   = (int)timeout_ms,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    ctx.client = client;

    /* Resume from a previous partial download via an HTTP Range header. */
    if (resume_from > 0) {
        char range[40];
        snprintf(range, sizeof(range), "bytes=%lu-", (unsigned long)resume_from);
        esp_http_client_set_header(client, "Range", range);
    }

    esp_err_t err = esp_http_client_perform(client);
    esp_http_client_cleanup(client);
    if (ctx.error) return ctx.error;     /* includes NFF_ERR_RESUME_UNSUPPORTED */
    if (err != ESP_OK) return -1;
    return 0;
}

/* ------------------------------------------------------------------ */
/* SHA-256                                                              */
/* ------------------------------------------------------------------ */

nff_sha256_ctx_t nff_port_sha256_new(void) {
    mbedtls_sha256_context *c = malloc(sizeof(*c));
    mbedtls_sha256_init(c);
    mbedtls_sha256_starts(c, 0);
    return (nff_sha256_ctx_t)c;
}
void nff_port_sha256_update(nff_sha256_ctx_t c, const uint8_t *d, size_t l) {
    mbedtls_sha256_update((mbedtls_sha256_context *)c, d, l);
}
void nff_port_sha256_finish(nff_sha256_ctx_t c, uint8_t out[32]) {
    mbedtls_sha256_finish((mbedtls_sha256_context *)c, out);
}
void nff_port_sha256_free(nff_sha256_ctx_t c) {
    mbedtls_sha256_free((mbedtls_sha256_context *)c);
    free(c);
}

/* ------------------------------------------------------------------ */
/* ECDSA-P256 verify                                                    */
/* ------------------------------------------------------------------ */

int nff_port_ecdsa_p256_verify(const uint8_t *pub_key_65,
                                const uint8_t *msg,    size_t msg_len,
                                const uint8_t *sig_der, size_t sig_len) {
    mbedtls_ecdsa_context ctx;
    mbedtls_ecdsa_init(&ctx);
    int rc = -1;

    if (mbedtls_ecp_group_load(&ctx.MBEDTLS_PRIVATE(grp),
                                MBEDTLS_ECP_DP_SECP256R1) != 0) goto out;
    if (mbedtls_ecp_point_read_binary(&ctx.MBEDTLS_PRIVATE(grp),
                                       &ctx.MBEDTLS_PRIVATE(Q),
                                       pub_key_65, 65) != 0) goto out;

    uint8_t digest[32];
    mbedtls_sha256(msg, msg_len, digest, 0);
    rc = (mbedtls_ecdsa_read_signature(&ctx, digest, 32, sig_der, sig_len) == 0)
         ? 0 : -1;
out:
    mbedtls_ecdsa_free(&ctx);
    return rc;
}

/* ------------------------------------------------------------------ */
/* System diagnostics                                                   */
/* ------------------------------------------------------------------ */

#include "esp_heap_caps.h"
#include "esp_wifi.h"

void nff_port_get_diag_info(nff_diag_info_t *out) {
    out->free_heap     = (uint32_t)esp_get_free_heap_size();
    out->min_free_heap = (uint32_t)esp_get_minimum_free_heap_size();
    out->uptime_ms     = nff_port_millis();
    out->cpu_count     = 2;

    wifi_ap_record_t ap;
    out->wifi_rssi = (esp_wifi_sta_get_ap_info(&ap) == ESP_OK)
                     ? (int32_t)ap.rssi : 0;
}

#include "esp_chip_info.h"
#include "esp_flash.h"

void nff_port_get_hw_info(nff_hw_info_t *out) {
    esp_chip_info_t info;
    esp_chip_info(&info);

    /* CONFIG_IDF_TARGET is already the canonical family ("esp32","esp32s3",...),
     * which is exactly the device_type vocabulary the OTA gate matches on. */
    snprintf(out->device_type, sizeof(out->device_type), "%s", CONFIG_IDF_TARGET);

    const char *model;
    switch (info.model) {
        case CHIP_ESP32:   model = "ESP32";    break;
        case CHIP_ESP32S2: model = "ESP32-S2"; break;
        case CHIP_ESP32S3: model = "ESP32-S3"; break;
        case CHIP_ESP32C3: model = "ESP32-C3"; break;
        case CHIP_ESP32C6: model = "ESP32-C6"; break;
        case CHIP_ESP32H2: model = "ESP32-H2"; break;
        default:           model = "ESP32";    break;
    }
    snprintf(out->chip_model, sizeof(out->chip_model), "%s", model);
    out->revision = (uint8_t)info.revision;
    out->cores    = (uint8_t)info.cores;

    uint32_t flash = 0;
    if (esp_flash_get_size(NULL, &flash) != ESP_OK) flash = 0;
    out->flash_size = flash;
}

void nff_port_get_unique_id(char *out, size_t out_len) {
    /* efuse-burned factory MAC — stable, unique per device. Lowercase hex, no separators. */
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(out, out_len, "%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

/* ------------------------------------------------------------------ */
/* Panic hook                                                           */
/* ------------------------------------------------------------------ */

#include "esp_system.h"

/* Synchronous panic-reason capture relied on non-public IDF APIs (esp_set_panic_handler /
 * esp_panic_info_t / esp_default_panic_handler) that do not exist on IDF 5.x. On ESP-IDF the
 * real crash-detail path is the coredump-to-flash summary read at the NEXT boot
 * (nff_port_get_crash_info -> esp_core_dump_get_summary, below), which yields the fault PC +
 * a resolvable backtrace without any panic-context work. So the hook is a documented no-op
 * here; nff_crash.c uses the coredump summary and degrades gracefully when the hook never fires.
 * (If a synchronous reason is ever required, override via the linker `--wrap esp_panic_handler`.) */
void nff_port_install_panic_hook(void (*fn)(const char *)) {
    (void)fn;
}

/* ------------------------------------------------------------------ */
/* Crash fault detail — from the coredump-to-flash summary             */
/* ------------------------------------------------------------------ */

/* Requires CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH + ELF format in sdkconfig (add a coredump
 * partition). esp_core_dump_get_summary() returns the fault PC + backtrace recovered at boot —
 * no panic-context work and no ELF parsing on-device. */
#if defined(CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH)
#include "esp_core_dump.h"

int nff_port_get_crash_info(nff_crash_hw_info_t *out) {
    memset(out, 0, sizeof(*out));
    out->exception_cause = -1;

    esp_core_dump_summary_t *s =
        (esp_core_dump_summary_t *)malloc(sizeof(esp_core_dump_summary_t));
    if (!s) return -1;

    int rc = -1;
    if (esp_core_dump_get_summary(s) == ESP_OK) {
        out->pc = s->exc_pc;
        snprintf(out->task_name, sizeof(out->task_name), "%s", s->exc_task);
#if CONFIG_IDF_TARGET_ARCH_XTENSA
        out->exception_cause = (int)s->ex_info.exc_cause;
        out->fault_addr      = s->ex_info.exc_vaddr;
#endif
        uint32_t depth = s->exc_bt_info.depth;
        if (depth > NFF_CRASH_BT_MAX) depth = NFF_CRASH_BT_MAX;
        for (uint32_t i = 0; i < depth; i++) out->backtrace[i] = s->exc_bt_info.bt[i];
        out->backtrace_len = (uint8_t)depth;
        out->valid = true;
        rc = 0;
    }
    free(s);
    return rc;
}

void nff_port_crash_info_clear(void) { esp_core_dump_image_erase(); }

#else  /* coredump disabled in this build */

int  nff_port_get_crash_info(nff_crash_hw_info_t *out) {
    memset(out, 0, sizeof(*out)); out->exception_cause = -1; return -1;
}
void nff_port_crash_info_clear(void) {}

#endif

/* ------------------------------------------------------------------ */
/* AMP                                                                  */
/* ------------------------------------------------------------------ */

int nff_port_stall_core(int id) {
#if !CONFIG_FREERTOS_UNICORE
    esp_cpu_stall(id);
    return 0;
#else
    (void)id;
    return -1;
#endif
}

int nff_port_unstall_core(int id) {
#if !CONFIG_FREERTOS_UNICORE
    esp_cpu_unstall(id);
    return 0;
#else
    (void)id;
    return -1;
#endif
}

#endif /* ESP_PLATFORM && !ARDUINO */
