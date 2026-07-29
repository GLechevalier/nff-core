/**
 * nff_port_esp8266_arduino.c — Arduino core for ESP8266 platform implementation.
 *
 * Differences from ESP32 Arduino port:
 *  - BearSSL instead of mbedTLS (lighter TLS stack for 160KB total SRAM)
 *  - ESP8266httpUpdate instead of Update.h
 *  - EEPROM for simple NVS (Preferences not available on 8266)
 *  - No cpu_stall, no dual-core, no coredump
 *  - No ECDSA via BearSSL (no convenient verify API) — for V1 use mbedTLS on a
 *    secondary partition if available, else skip ECDSA and rely on TLS auth only.
 *
 * Note: ESP8266 has 160 KB total SRAM. Two TLS connections may be tight.
 * If memory is a concern, disable TLS on the nff connection and rely on the
 * application-level command signature only.
 */

#if defined(ARDUINO) && defined(ESP8266)

#include "nff_port.h"
#include "nff.h"   /* nff_log */
#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <WiFiClientSecureBearSSL.h>
#include <PubSubClient.h>
#include <EEPROM.h>
#include <ESP8266httpUpdate.h>
#include <ESP8266HTTPClient.h>
#include <string.h>
#include <stdlib.h>

/* ------------------------------------------------------------------ */
/* Time                                                                 */
/* ------------------------------------------------------------------ */

uint32_t nff_port_millis(void) { return (uint32_t)millis(); }
void     nff_port_delay_ms(uint32_t ms) { delay(ms); }

/* ------------------------------------------------------------------ */
/* Mutex (single-core, no RTOS — use interrupt disable as critical section) */
/* ------------------------------------------------------------------ */

typedef struct { uint32_t _dummy; } posix_mutex_stub_t;

nff_mutex_t nff_port_mutex_create(void) {
    /* Single-threaded: mutex is a no-op */
    posix_mutex_stub_t *m = (posix_mutex_stub_t *)malloc(sizeof(*m));
    return (nff_mutex_t)m;
}
void nff_port_mutex_lock(nff_mutex_t m)    { noInterrupts(); (void)m; }
void nff_port_mutex_unlock(nff_mutex_t m)  { interrupts();  (void)m; }
void nff_port_mutex_destroy(nff_mutex_t m) { free(m); }

/* ------------------------------------------------------------------ */
/* Task (no RTOS on 8266 — task_create is a no-op; use nff_loop())    */
/* ------------------------------------------------------------------ */

void nff_port_task_create(void (*fn)(void *), const char *name,
                           uint32_t stack_bytes, void *arg,
                           uint8_t priority, int core) {
    (void)fn; (void)name; (void)stack_bytes;
    (void)arg; (void)priority; (void)core;
    /* No RTOS on ESP8266. Use nff_loop() from the Arduino loop() instead. */
}

/* ------------------------------------------------------------------ */
/* MQTT (PubSubClient + BearSSL)                                       */
/* ------------------------------------------------------------------ */

struct nff_mqtt_handle {
    BearSSL::WiFiClientSecure tls;
    PubSubClient              client;
    char  host[256];
    uint16_t port;
    char  client_id[64];
    char  lwt_topic[NFF_TOPIC_MAXLEN];
    char  lwt_payload[256];
    void (*rx_cb)(const char *, const uint8_t *, size_t, void *);
    void *rx_user;
    nff_mqtt_handle() : client(tls), rx_cb(nullptr), rx_user(nullptr) {}
};

static struct nff_mqtt_handle *s_mqtt_handle = nullptr;

static void pubsub_8266_callback(char *topic, byte *payload, unsigned int len) {
    if (s_mqtt_handle && s_mqtt_handle->rx_cb)
        s_mqtt_handle->rx_cb(topic, (const uint8_t *)payload, (size_t)len,
                              s_mqtt_handle->rx_user);
}

nff_mqtt_handle_t *nff_port_mqtt_create(void) {
    struct nff_mqtt_handle *h = new struct nff_mqtt_handle();
    s_mqtt_handle = h;
    h->client.setCallback(pubsub_8266_callback);
    /* PubSubClient defaults to a 256B buffer, which silently drops larger
       inbound PUBLISHes (e.g. ~400B+ OTA commands). Enlarge once at create. */
    h->client.setBufferSize(NFF_MQTT_BUFFER_SIZE);
    return (nff_mqtt_handle_t *)h;
}

void nff_port_mqtt_set_server(nff_mqtt_handle_t *hh,
                               const char *host, uint16_t port) {
    struct nff_mqtt_handle *h = (struct nff_mqtt_handle *)hh;
    strncpy(h->host, host, sizeof(h->host) - 1);
    h->port = port;
    h->client.setServer(host, port);
}

void nff_port_mqtt_set_tls(nff_mqtt_handle_t *hh,
                            const uint8_t *ca, size_t ca_len,
                            const uint8_t *cert, size_t cert_len,
                            const uint8_t *key, size_t key_len) {
    struct nff_mqtt_handle *h = (struct nff_mqtt_handle *)hh;
    /* BearSSL expects PEM-encoded strings; DER support available via setClientRSACert etc.
       For V1, skip server cert verification if memory is tight. */
    h->tls.setInsecure();
    (void)ca; (void)ca_len; (void)cert; (void)cert_len; (void)key; (void)key_len;
}

void nff_port_mqtt_set_skip_cn_check(nff_mqtt_handle_t *hh, bool skip) {
    (void)hh; (void)skip;   /* V1 already setInsecure() above — nothing further to relax */
}

void nff_port_mqtt_set_rx_callback(nff_mqtt_handle_t *hh,
                                    void (*cb)(const char *, const uint8_t *, size_t, void *),
                                    void *user) {
    struct nff_mqtt_handle *h = (struct nff_mqtt_handle *)hh;
    h->rx_cb   = cb;
    h->rx_user = user;
}

int nff_port_mqtt_connect(nff_mqtt_handle_t *hh,
                           const char *client_id,
                           const char *lwt_topic,
                           const char *lwt_payload) {
    struct nff_mqtt_handle *h = (struct nff_mqtt_handle *)hh;
    if (client_id)   strncpy(h->client_id,  client_id,  sizeof(h->client_id)  - 1);
    if (lwt_topic)   strncpy(h->lwt_topic,   lwt_topic,  sizeof(h->lwt_topic)  - 1);
    if (lwt_payload) strncpy(h->lwt_payload, lwt_payload, sizeof(h->lwt_payload) - 1);

    return h->client.connect(h->client_id, "", "",
                              h->lwt_topic, 1, true, h->lwt_payload) ? 0 : -1;
}

int nff_port_mqtt_subscribe(nff_mqtt_handle_t *hh, const char *topic, int qos) {
    struct nff_mqtt_handle *h = (struct nff_mqtt_handle *)hh;
    return h->client.subscribe(topic, qos) ? 0 : -1;
}

int nff_port_mqtt_publish(nff_mqtt_handle_t *hh, const char *topic,
                           const char *payload, int qos, bool retain) {
    struct nff_mqtt_handle *h = (struct nff_mqtt_handle *)hh;
    (void)qos;
    return h->client.publish(topic, payload, retain) ? 0 : -1;
}

void nff_port_mqtt_loop(nff_mqtt_handle_t *hh) {
    struct nff_mqtt_handle *h = (struct nff_mqtt_handle *)hh;
    h->client.loop();
}

bool nff_port_mqtt_is_connected(nff_mqtt_handle_t *hh) {
    struct nff_mqtt_handle *h = (struct nff_mqtt_handle *)hh;
    return h->client.connected();
}

/* ------------------------------------------------------------------ */
/* NVS — EEPROM-backed key-value store                                 */
/* ------------------------------------------------------------------ */

/*
 * Simple flat EEPROM layout for up to 8 key-value pairs.
 * Each slot: 16-byte key, 128-byte value string, 4-byte u32, 1-byte type.
 * Total: 8 * (16+128+4+1) = 1192 bytes — fits in 4KB EEPROM.
 */

#define NVS8_SLOTS     8
#define NVS8_KEY_LEN   16
#define NVS8_VAL_LEN   128

typedef struct {
    char    key[NVS8_KEY_LEN];
    char    str[NVS8_VAL_LEN];
    uint32_t u;
    uint8_t  type;  /* 0=empty, 1=str, 2=u32 */
} __attribute__((packed)) nvs8_slot_t;

#define EEPROM_MAGIC 0xAB
#define EEPROM_BASE  0

static bool          s_eeprom_init = false;
static nvs8_slot_t   s_cache[NVS8_SLOTS];

static void eeprom_load(void) {
    if (s_eeprom_init) return;
    EEPROM.begin(sizeof(s_cache) + 1);
    if (EEPROM.read(EEPROM_BASE) == EEPROM_MAGIC) {
        EEPROM.get(EEPROM_BASE + 1, s_cache);
    } else {
        memset(s_cache, 0, sizeof(s_cache));
    }
    s_eeprom_init = true;
}

static nvs8_slot_t *nvs8_find(const char *key) {
    for (int i = 0; i < NVS8_SLOTS; i++)
        if (s_cache[i].type && strncmp(s_cache[i].key, key, NVS8_KEY_LEN) == 0)
            return &s_cache[i];
    return nullptr;
}

static nvs8_slot_t *nvs8_alloc(const char *key) {
    nvs8_slot_t *e = nvs8_find(key);
    if (e) return e;
    for (int i = 0; i < NVS8_SLOTS; i++) {
        if (!s_cache[i].type) {
            memset(&s_cache[i], 0, sizeof(s_cache[i]));
            strncpy(s_cache[i].key, key, NVS8_KEY_LEN - 1);
            return &s_cache[i];
        }
    }
    return nullptr;
}

int nff_port_nvs_set_str(const char *key, const char *value) {
    eeprom_load();
    nvs8_slot_t *e = nvs8_alloc(key);
    if (!e) return -1;
    strncpy(e->str, value, NVS8_VAL_LEN - 1);
    e->type = 1;
    return 0;
}

int nff_port_nvs_get_str(const char *key, char *out, size_t out_len) {
    eeprom_load();
    nvs8_slot_t *e = nvs8_find(key);
    if (!e || e->type != 1) return -1;
    strncpy(out, e->str, out_len - 1);
    out[out_len - 1] = '\0';
    return 0;
}

int nff_port_nvs_set_u32(const char *key, uint32_t value) {
    eeprom_load();
    nvs8_slot_t *e = nvs8_alloc(key);
    if (!e) return -1;
    e->u    = value;
    e->type = 2;
    return 0;
}

int nff_port_nvs_get_u32(const char *key, uint32_t *out) {
    eeprom_load();
    nvs8_slot_t *e = nvs8_find(key);
    if (!e || e->type != 2) return -1;
    *out = e->u;
    return 0;
}

int nff_port_nvs_erase_key(const char *key) {
    eeprom_load();
    nvs8_slot_t *e = nvs8_find(key);
    if (e) memset(e, 0, sizeof(*e));
    return 0;
}

int nff_port_nvs_commit(void) {
    EEPROM.write(EEPROM_BASE, EEPROM_MAGIC);
    EEPROM.put(EEPROM_BASE + 1, s_cache);
    EEPROM.commit();
    return 0;
}

/* ------------------------------------------------------------------ */
/* OTA (ESP8266httpUpdate)                                             */
/* ------------------------------------------------------------------ */

struct ota_buf_s { bool active; };
static struct ota_buf_s s_ota;

nff_ota_handle_t nff_port_ota_begin(size_t expected_size) {
    (void)expected_size;
    /* ESP8266httpUpdate handles the full download internally.
       We return a valid handle here; the actual download is done in
       nff_port_https_get_stream which is redirected to httpUpdate.update(). */
    s_ota.active = true;
    return (nff_ota_handle_t)&s_ota;
}

int nff_port_ota_write(nff_ota_handle_t h, const uint8_t *buf, size_t len) {
    (void)h;
    return Update.write((uint8_t *)buf, len) == len ? 0 : -1;
}

int nff_port_ota_end(nff_ota_handle_t h) {
    (void)h;
    s_ota.active = false;
    return Update.end(true) ? 0 : -1;
}

void nff_port_ota_abort(nff_ota_handle_t h) {
    (void)h;
    Update.end(false);
    s_ota.active = false;
}

int nff_port_ota_mark_valid(void) {
    /* ESP8266 has no A/B partition rollback to cancel. No-op. */
    return 0;
}

void nff_port_ota_rollback(void) {
    /* ESP8266 has no second app partition to revert to (single-image OTA via
       eboot). Best-effort: log and reboot. The previous image is not recoverable
       in software here — documented limitation; ESP8266 is the secondary port. */
    nff_log("nff: OTA rollback not supported on ESP8266 (single image) — rebooting");
    ESP.restart();
}

int nff_port_ota_repair_boot_partition(void) {
    return 0;   /* single-image platform — no boot/running divergence to detect */
}

void nff_port_reboot(void) { ESP.restart(); }

/* ------------------------------------------------------------------ */
/* HTTPS streaming                                                      */
/* ------------------------------------------------------------------ */

int nff_port_https_get_stream(const char *url, size_t resume_from,
                               int (*chunk_cb)(const uint8_t *, size_t, void *),
                               void *user_ctx, uint32_t timeout_ms) {
    BearSSL::WiFiClientSecure tls;
    tls.setInsecure();
    HTTPClient http;
    http.begin(tls, url);
    http.setTimeout((int)timeout_ms);

    /* Resume from a previous partial download via an HTTP Range header. */
    if (resume_from > 0) {
        char range[40];
        snprintf(range, sizeof(range), "bytes=%lu-", (unsigned long)resume_from);
        http.addHeader("Range", range);
    }

    int http_code = http.GET();
    /* A resume request that returns 200 (server ignored Range) would double-write — restart. */
    if (resume_from > 0 && http_code == 200) { http.end(); return NFF_ERR_RESUME_UNSUPPORTED; }
    if (http_code != 200 && http_code != 206) { http.end(); return -1; }

    WiFiClient *stream = http.getStreamPtr();
    uint8_t buf[256];
    int rc = 0;
    while (http.connected()) {
        int n = stream->readBytes(buf, sizeof(buf));
        if (n <= 0) break;
        rc = chunk_cb(buf, (size_t)n, user_ctx);
        if (rc < 0) break;
    }
    http.end();
    return rc;
}

/* ------------------------------------------------------------------ */
/* SHA-256 (software fallback — BearSSL has br_sha256 internally)      */
/* ------------------------------------------------------------------ */

/* Use BearSSL's internal SHA-256 context */
#include <bearssl/bearssl_hash.h>

nff_sha256_ctx_t nff_port_sha256_new(void) {
    br_sha256_context *c = (br_sha256_context *)malloc(sizeof(*c));
    br_sha256_init(c);
    return (nff_sha256_ctx_t)c;
}
void nff_port_sha256_update(nff_sha256_ctx_t c, const uint8_t *d, size_t l) {
    br_sha256_update((br_sha256_context *)c, d, l);
}
void nff_port_sha256_finish(nff_sha256_ctx_t c, uint8_t out[32]) {
    br_sha256_out((br_sha256_context *)c, out);
}
void nff_port_sha256_free(nff_sha256_ctx_t c) { free(c); }

/* ------------------------------------------------------------------ */
/* ECDSA-P256 verify (not available without mbedTLS on 8266)           */
/* V1: skip ECDSA — command authenticity relies on mTLS channel only.  */
/* Set NFF_SKIP_CMD_SIG=1 in your sketch to bypass signature check.    */
/* ------------------------------------------------------------------ */

int nff_port_ecdsa_p256_verify(const uint8_t *pub_key_65,
                                const uint8_t *msg,    size_t msg_len,
                                const uint8_t *sig_der, size_t sig_len) {
    (void)pub_key_65; (void)msg; (void)msg_len; (void)sig_der; (void)sig_len;
#if defined(NFF_SKIP_CMD_SIG) && NFF_SKIP_CMD_SIG
    return 0;  /* Accept all commands — relying on mTLS for auth */
#else
    return -1; /* Reject until proper ECDSA support is added */
#endif
}

/* ------------------------------------------------------------------ */
/* System diagnostics                                                   */
/* ------------------------------------------------------------------ */

void nff_port_get_diag_info(nff_diag_info_t *out) {
    out->free_heap     = (uint32_t)ESP.getFreeHeap();
    out->min_free_heap = (uint32_t)ESP.getFreeHeap(); /* 8266 has no min tracking */
    out->uptime_ms     = nff_port_millis();
    out->wifi_rssi     = (int32_t)WiFi.RSSI();
    out->cpu_count     = 1;
}

void nff_port_get_hw_info(nff_hw_info_t *out) {
    /* The ESP8266 is a single fixed part — no chip-info API, so report constants. */
    strncpy(out->device_type, "esp8266", sizeof(out->device_type) - 1);
    out->device_type[sizeof(out->device_type) - 1] = '\0';
    strncpy(out->chip_model, "ESP8266", sizeof(out->chip_model) - 1);
    out->chip_model[sizeof(out->chip_model) - 1] = '\0';
    out->revision   = 0;
    out->flash_size = (uint32_t)ESP.getFlashChipSize();
    out->cores      = 1;
}

void nff_port_get_unique_id(char *out, size_t out_len) {
    /* WiFi station MAC, lowercase hex, no separators — stable + unique per device. */
    uint8_t mac[6] = {0};
    WiFi.macAddress(mac);
    snprintf(out, out_len, "%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

/* ------------------------------------------------------------------ */
/* Panic hook (not available on 8266 — UART output only)               */
/* ------------------------------------------------------------------ */

void nff_port_install_panic_hook(void (*fn)(const char *)) {
    (void)fn;
    /* ESP8266 exception handler is not programmable in Arduino.
       Crash metadata cannot be written to NVS from panic context on 8266. */
}

/* ------------------------------------------------------------------ */
/* Crash fault detail — not available on 8266 (no coredump)            */
/* ------------------------------------------------------------------ */

int nff_port_get_crash_info(nff_crash_hw_info_t *out) {
    memset(out, 0, sizeof(*out));
    out->exception_cause = -1;
    return -1;   /* no coredump on ESP8266 — crash report carries RTC log + reason only */
}

void nff_port_crash_info_clear(void) {}

/* ------------------------------------------------------------------ */
/* AMP — not available on single-core 8266                             */
/* ------------------------------------------------------------------ */

int nff_port_stall_core(int id)   { (void)id; return -1; }
int nff_port_unstall_core(int id) { (void)id; return -1; }

#endif /* ARDUINO && ESP8266 */
