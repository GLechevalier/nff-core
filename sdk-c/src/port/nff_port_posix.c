/**
 * nff_port_posix.c — POSIX platform implementation (host unit tests).
 *
 * Provides: pthreads, clock_gettime, OpenSSL ECDSA, in-memory NVS,
 * mock MQTT loopback. OTA and panic hook are no-ops.
 *
 * Test helpers (prefixed nff_port_posix_) are declared at the bottom
 * and should be included only in test translation units.
 */

#if !defined(ARDUINO) && !defined(ESP_PLATFORM)

#include "nff.h"
#include "nff_port.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <stdint.h>
#include <stdarg.h>
#include <time.h>

/* ---- Platform-specific threading / sleep headers ---- */
#ifdef _WIN32
#  include <windows.h>
/* GetTickCount() returns ms since boot (wraps at ~49 days — fine for tests) */
#else
#  include <pthread.h>
#  include <unistd.h>
#endif

/* ------------------------------------------------------------------ */
/* Time                                                                 */
/* ------------------------------------------------------------------ */

uint32_t nff_port_millis(void) {
#ifdef _WIN32
    return (uint32_t)GetTickCount();
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000u + ts.tv_nsec / 1000000u);
#endif
}

void nff_port_delay_ms(uint32_t ms) {
#ifdef _WIN32
    Sleep(ms);
#else
    struct timespec ts = { (time_t)(ms / 1000), (long)((ms % 1000) * 1000000L) };
    nanosleep(&ts, NULL);
#endif
}

/* ------------------------------------------------------------------ */
/* Mutex                                                                */
/* ------------------------------------------------------------------ */

#ifdef _WIN32

nff_mutex_t nff_port_mutex_create(void) {
    CRITICAL_SECTION *cs = (CRITICAL_SECTION *)malloc(sizeof(CRITICAL_SECTION));
    if (cs) InitializeCriticalSection(cs);
    return (nff_mutex_t)cs;
}
void nff_port_mutex_lock(nff_mutex_t m)    { EnterCriticalSection((CRITICAL_SECTION *)m); }
void nff_port_mutex_unlock(nff_mutex_t m)  { LeaveCriticalSection((CRITICAL_SECTION *)m); }
void nff_port_mutex_destroy(nff_mutex_t m) {
    if (m) { DeleteCriticalSection((CRITICAL_SECTION *)m); free(m); }
}

#else

nff_mutex_t nff_port_mutex_create(void) {
    pthread_mutex_t *m = (pthread_mutex_t *)malloc(sizeof(pthread_mutex_t));
    if (m) pthread_mutex_init(m, NULL);
    return (nff_mutex_t)m;
}
void nff_port_mutex_lock(nff_mutex_t m) {
    pthread_mutex_lock((pthread_mutex_t *)m);
}
void nff_port_mutex_unlock(nff_mutex_t m) {
    pthread_mutex_unlock((pthread_mutex_t *)m);
}
void nff_port_mutex_destroy(nff_mutex_t m) {
    if (m) {
        pthread_mutex_destroy((pthread_mutex_t *)m);
        free(m);
    }
}

#endif /* _WIN32 */

/* ------------------------------------------------------------------ */
/* Task                                                                 */
/* ------------------------------------------------------------------ */

typedef struct { void (*fn)(void *); void *arg; } task_arg_t;

#ifdef _WIN32
static DWORD WINAPI task_shim_win(LPVOID a) {
    task_arg_t *ta = (task_arg_t *)a;
    ta->fn(ta->arg);
    free(ta);
    return 0;
}
#else
static void *task_shim(void *a) {
    task_arg_t *ta = (task_arg_t *)a;
    ta->fn(ta->arg);
    free(ta);
    return NULL;
}
#endif

void nff_port_task_create(void (*fn)(void *), const char *name,
                           uint32_t stack_bytes, void *arg,
                           uint8_t priority, int core) {
    (void)name; (void)stack_bytes; (void)priority; (void)core;
    task_arg_t *ta = (task_arg_t *)malloc(sizeof(task_arg_t));
    ta->fn  = fn;
    ta->arg = arg;
#ifdef _WIN32
    HANDLE t = CreateThread(NULL, 0, task_shim_win, ta, 0, NULL);
    if (t) CloseHandle(t);
#else
    pthread_t t;
    pthread_create(&t, NULL, task_shim, ta);
    pthread_detach(t);
#endif
}

/* ------------------------------------------------------------------ */
/* Mock MQTT                                                            */
/* ------------------------------------------------------------------ */

#define MOCK_PUB_SLOTS 16
#define MOCK_PUB_TOPIC_LEN 128
#define MOCK_PUB_PAYLOAD_LEN 2048

typedef struct {
    char topic[MOCK_PUB_TOPIC_LEN];
    char payload[MOCK_PUB_PAYLOAD_LEN];
    int  qos;
    bool retain;
    bool used;
} mock_pub_slot_t;

struct nff_mqtt_handle {
    char   server_host[256];
    uint16_t server_port;
    bool   connected;
    void (*rx_cb)(const char *, const uint8_t *, size_t, void *);
    void  *rx_user;
    /* Outbound message store (ring) */
    mock_pub_slot_t pub_slots[MOCK_PUB_SLOTS];
    int    pub_head;
    /* LWT */
    char   lwt_topic[MOCK_PUB_TOPIC_LEN];
    char   lwt_payload[256];
};

nff_mqtt_handle_t *nff_port_mqtt_create(void) {
    nff_mqtt_handle_t *h = (nff_mqtt_handle_t *)calloc(1, sizeof(nff_mqtt_handle_t));
    return h;
}

void nff_port_mqtt_set_server(nff_mqtt_handle_t *h,
                               const char *host, uint16_t port) {
    if (!h) return;
    strncpy(h->server_host, host, sizeof(h->server_host) - 1);
    h->server_port = port;
}

void nff_port_mqtt_set_tls(nff_mqtt_handle_t *h,
                            const uint8_t *ca, size_t ca_len,
                            const uint8_t *cert, size_t cert_len,
                            const uint8_t *key, size_t key_len,
                            const uint8_t *inter, size_t inter_len) {
    (void)h; (void)ca; (void)ca_len;
    (void)cert; (void)cert_len; (void)key; (void)key_len;
    (void)inter; (void)inter_len;
    /* No actual TLS on POSIX mock */
}

void nff_port_mqtt_set_skip_cn_check(nff_mqtt_handle_t *h, bool skip) {
    (void)h; (void)skip;   /* no actual TLS on POSIX mock */
}

void nff_port_mqtt_set_rx_callback(nff_mqtt_handle_t *h,
                                    void (*cb)(const char *, const uint8_t *, size_t, void *),
                                    void *user) {
    if (!h) return;
    h->rx_cb   = cb;
    h->rx_user = user;
}

int nff_port_mqtt_connect(nff_mqtt_handle_t *h,
                           const char *client_id,
                           const char *lwt_topic,
                           const char *lwt_payload) {
    if (!h) return -1;
    (void)client_id;
    if (lwt_topic)   strncpy(h->lwt_topic,   lwt_topic,   sizeof(h->lwt_topic)   - 1);
    if (lwt_payload) strncpy(h->lwt_payload,  lwt_payload, sizeof(h->lwt_payload) - 1);
    h->connected = true;  /* Mock always connects */
    return 0;
}

int nff_port_mqtt_subscribe(nff_mqtt_handle_t *h, const char *topic, int qos) {
    (void)h; (void)topic; (void)qos;
    return 0;
}

int nff_port_mqtt_publish(nff_mqtt_handle_t *h,
                           const char *topic, const char *payload,
                           int qos, bool retain) {
    if (!h) return -1;
    int slot = h->pub_head % MOCK_PUB_SLOTS;
    strncpy(h->pub_slots[slot].topic,   topic,   MOCK_PUB_TOPIC_LEN   - 1);
    strncpy(h->pub_slots[slot].payload, payload, MOCK_PUB_PAYLOAD_LEN - 1);
    h->pub_slots[slot].topic[MOCK_PUB_TOPIC_LEN   - 1] = '\0';
    h->pub_slots[slot].payload[MOCK_PUB_PAYLOAD_LEN - 1] = '\0';
    h->pub_slots[slot].qos    = qos;
    h->pub_slots[slot].retain = retain;
    h->pub_slots[slot].used   = true;
    h->pub_head++;
    return 0;
}

void nff_port_mqtt_loop(nff_mqtt_handle_t *h) {
    (void)h;
    /* Nothing to do in mock */
}

bool nff_port_mqtt_is_connected(nff_mqtt_handle_t *h) {
    return h && h->connected;
}

/* ------------------------------------------------------------------ */
/* NVS — in-memory key-value store                                     */
/* ------------------------------------------------------------------ */

#define NVS_MAX_ENTRIES 64
#define NVS_KEY_LEN      32
#define NVS_VAL_STR_LEN 256

typedef enum { NVS_TYPE_STR, NVS_TYPE_U32 } nvs_type_t;

typedef struct {
    char       key[NVS_KEY_LEN];
    nvs_type_t type;
    union {
        char     s[NVS_VAL_STR_LEN];
        uint32_t u;
    } val;
    bool used;
} nvs_entry_t;

static nvs_entry_t s_nvs[NVS_MAX_ENTRIES];

static nvs_entry_t *nvs_find(const char *key) {
    for (int i = 0; i < NVS_MAX_ENTRIES; i++)
        if (s_nvs[i].used && strcmp(s_nvs[i].key, key) == 0)
            return &s_nvs[i];
    return NULL;
}

static nvs_entry_t *nvs_alloc(const char *key) {
    /* Reuse existing slot */
    nvs_entry_t *e = nvs_find(key);
    if (e) return e;
    for (int i = 0; i < NVS_MAX_ENTRIES; i++) {
        if (!s_nvs[i].used) {
            memset(&s_nvs[i], 0, sizeof(s_nvs[i]));
            strncpy(s_nvs[i].key, key, NVS_KEY_LEN - 1);
            s_nvs[i].used = true;
            return &s_nvs[i];
        }
    }
    return NULL;
}

int nff_port_nvs_set_str(const char *key, const char *value) {
    nvs_entry_t *e = nvs_alloc(key);
    if (!e) return -1;
    e->type = NVS_TYPE_STR;
    strncpy(e->val.s, value, NVS_VAL_STR_LEN - 1);
    e->val.s[NVS_VAL_STR_LEN - 1] = '\0';
    return 0;
}

int nff_port_nvs_get_str(const char *key, char *out, size_t out_len) {
    nvs_entry_t *e = nvs_find(key);
    if (!e || e->type != NVS_TYPE_STR) return -1;
    strncpy(out, e->val.s, out_len - 1);
    out[out_len - 1] = '\0';
    return 0;
}

int nff_port_nvs_set_u32(const char *key, uint32_t value) {
    nvs_entry_t *e = nvs_alloc(key);
    if (!e) return -1;
    e->type  = NVS_TYPE_U32;
    e->val.u = value;
    return 0;
}

int nff_port_nvs_get_u32(const char *key, uint32_t *out) {
    nvs_entry_t *e = nvs_find(key);
    if (!e || e->type != NVS_TYPE_U32) return -1;
    *out = e->val.u;
    return 0;
}

int nff_port_nvs_erase_key(const char *key) {
    nvs_entry_t *e = nvs_find(key);
    if (e) memset(e, 0, sizeof(*e));
    return 0;
}

int nff_port_nvs_commit(void) { return 0; }

/* ------------------------------------------------------------------ */
/* OTA — no-ops on POSIX                                               */
/* ------------------------------------------------------------------ */

static int s_ota_active = 0;

nff_ota_handle_t nff_port_ota_begin(size_t expected_size) {
    (void)expected_size;
    s_ota_active = 1;
    return (nff_ota_handle_t)&s_ota_active;
}

int nff_port_ota_write(nff_ota_handle_t h, const uint8_t *buf, size_t len) {
    (void)h; (void)buf; (void)len;
    return 0;
}

int nff_port_ota_end(nff_ota_handle_t h) {
    (void)h;
    s_ota_active = 0;
    return 0;
}

void nff_port_ota_abort(nff_ota_handle_t h) {
    (void)h;
    s_ota_active = 0;
}

static volatile int s_reboot_called      = 0;
static volatile int s_ota_mark_valid_called = 0;
static volatile int s_ota_rollback_called   = 0;

int nff_port_ota_mark_valid(void) {
    s_ota_mark_valid_called = 1;
    return 0;
}

void nff_port_ota_rollback(void) {
    /* In tests, rollback is observed via the flag; simulate the reboot but
       don't actually exit so the test can drive the next "boot". */
    s_ota_rollback_called = 1;
    s_reboot_called       = 1;
}

int nff_port_ota_repair_boot_partition(void) {
    return 0;   /* no A/B slots on the POSIX mock — boot always matches running */
}

void nff_port_reboot(void) {
    s_reboot_called = 1;
    /* In tests, reboot is simulated; don't actually exit */
}

/* ------------------------------------------------------------------ */
/* HTTPS streaming — feedable mock (lets tests exercise resume)        */
/* ------------------------------------------------------------------ */

/* Test-controlled response body. When unset, the stream is a no-op returning 0 (preserves the
   old behaviour for tests that mock OTA at the nff_ota.c layer). */
static const uint8_t *s_ota_body     = NULL;
static size_t         s_ota_body_len = 0;
static size_t         s_ota_drop_at  = 0;   /* >0: drop the first call after delivering this many bytes (from resume_from) */
static int            s_ota_dropped  = 0;   /* set once the simulated drop has fired */
static int            s_ota_force_200 = 0;  /* simulate a server that ignores Range (returns 200) */

int nff_port_https_get_stream(const char *url, size_t resume_from,
                               int (*chunk_cb)(const uint8_t *, size_t, void *),
                               void *user_ctx, uint32_t timeout_ms) {
    (void)url; (void)timeout_ms;
    if (!s_ota_body) return 0;   /* no body configured: legacy no-op */

    /* Server ignored Range: report it the first time a resume is attempted. */
    if (resume_from > 0 && s_ota_force_200) return NFF_ERR_RESUME_UNSUPPORTED;
    if (resume_from > s_ota_body_len) return -1;

    size_t off = resume_from;
    while (off < s_ota_body_len) {
        /* Simulate a mid-stream drop exactly once. */
        if (s_ota_drop_at > 0 && !s_ota_dropped && off >= s_ota_drop_at) {
            s_ota_dropped = 1;
            return -1;   /* transient failure; caller resumes from `off` */
        }
        size_t chunk = s_ota_body_len - off;
        if (chunk > 512) chunk = 512;
        int rc = chunk_cb(s_ota_body + off, chunk, user_ctx);
        if (rc < 0) return rc;
        off += chunk;
    }
    return 0;
}

/* Test helpers: configure the mock body / failure injection. */
void nff_port_posix_set_ota_body(const uint8_t *body, size_t len) {
    s_ota_body = body; s_ota_body_len = len;
    s_ota_drop_at = 0; s_ota_dropped = 0; s_ota_force_200 = 0;
}
void nff_port_posix_set_ota_drop_at(size_t n) { s_ota_drop_at = n; s_ota_dropped = 0; }
void nff_port_posix_set_ota_force_200(int on)  { s_ota_force_200 = on ? 1 : 0; }
void nff_port_posix_reset_ota_body(void) {
    s_ota_body = NULL; s_ota_body_len = 0;
    s_ota_drop_at = 0; s_ota_dropped = 0; s_ota_force_200 = 0;
}

/* ------------------------------------------------------------------ */
/* SHA-256 — pure-C fallback (no external deps needed for host tests)  */
/* ------------------------------------------------------------------ */

#include "nff_sha256_fallback.h"

nff_sha256_ctx_t nff_port_sha256_new(void) {
    nff_sha256_fallback_t *c = (nff_sha256_fallback_t *)malloc(sizeof(*c));
    if (c) nff_sha256_fallback_init(c);
    return (nff_sha256_ctx_t)c;
}
void nff_port_sha256_update(nff_sha256_ctx_t c, const uint8_t *d, size_t l) {
    nff_sha256_fallback_update((nff_sha256_fallback_t *)c, d, l);
}
void nff_port_sha256_finish(nff_sha256_ctx_t c, uint8_t out[32]) {
    nff_sha256_fallback_finish((nff_sha256_fallback_t *)c, out);
}
void nff_port_sha256_free(nff_sha256_ctx_t c) { free(c); }

/* ------------------------------------------------------------------ */
/* ECDSA-P256 verify — no-op on host (tests mock nff_security_verify)  */
/* ------------------------------------------------------------------ */

/* The POSIX port does not ship a real ECDSA verify implementation.
   Tests override nff_port_ecdsa_p256_verify with their own definition
   (returning 0 = accept-all). Production ports (ESP32-IDF, ESP32-Arduino)
   use mbedTLS for real verification. */
__attribute__((weak))
int nff_port_ecdsa_p256_verify(const uint8_t *pub_key_65,
                                const uint8_t *msg,    size_t msg_len,
                                const uint8_t *sig_der, size_t sig_len) {
    (void)pub_key_65; (void)msg; (void)msg_len; (void)sig_der; (void)sig_len;
    return -1;  /* reject by default; tests override this */
}

/* ------------------------------------------------------------------ */
/* System diagnostics                                                   */
/* ------------------------------------------------------------------ */

/* Test-overridable min_free_heap so the OTA heap-floor gate can be exercised. */
static uint32_t s_diag_min_heap = 180000;

void nff_port_get_diag_info(nff_diag_info_t *out) {
    out->free_heap     = 200000;
    out->min_free_heap = s_diag_min_heap;
    out->uptime_ms     = nff_port_millis();
    out->wifi_rssi     = -55;
    out->cpu_count     = 1;
}

void nff_port_posix_set_min_heap(uint32_t v) { s_diag_min_heap = v; }
void nff_port_posix_reset_diag(void)         { s_diag_min_heap = 180000; }

void nff_port_get_hw_info(nff_hw_info_t *out) {
    /* Host build: no real silicon. Leave device_type empty (treated as "unknown"
     * by the OTA gate) and report a recognizable model for diagnostics. */
    memset(out, 0, sizeof(*out));
    snprintf(out->chip_model, sizeof(out->chip_model), "posix");
    out->cores = 1;
}

void nff_port_get_unique_id(char *out, size_t out_len) {
    /* Host build: a fixed mock "MAC". Real ports return the efuse/WiFi MAC. */
    snprintf(out, out_len, "deadbeef0001");
}

/* ------------------------------------------------------------------ */
/* Panic hook                                                           */
/* ------------------------------------------------------------------ */

static void (*s_panic_hook)(const char *) = NULL;

void nff_port_install_panic_hook(void (*fn)(const char *)) {
    s_panic_hook = fn;
}

/* Test helper: simulate a panic */
void nff_port_posix_trigger_panic(const char *reason) {
    if (s_panic_hook) s_panic_hook(reason);
}

/* ------------------------------------------------------------------ */
/* Crash fault detail — test-settable mock (real ports use coredump)   */
/* ------------------------------------------------------------------ */

static nff_crash_hw_info_t s_crash_hw;   /* zero => valid=false */

int nff_port_get_crash_info(nff_crash_hw_info_t *out) {
    memset(out, 0, sizeof(*out));
    if (!s_crash_hw.valid) return -1;
    *out = s_crash_hw;
    return 0;
}

void nff_port_crash_info_clear(void) {
    memset(&s_crash_hw, 0, sizeof(s_crash_hw));
}

/* Test helper: stage a coredump summary the next nff_port_get_crash_info will return. */
void nff_port_posix_set_crash_info(const nff_crash_hw_info_t *info) {
    if (info) s_crash_hw = *info;
    else      memset(&s_crash_hw, 0, sizeof(s_crash_hw));
}

/* ------------------------------------------------------------------ */
/* AMP — not available on POSIX                                        */
/* ------------------------------------------------------------------ */

int nff_port_stall_core(int id)   { (void)id; return -1; }
int nff_port_unstall_core(int id) { (void)id; return -1; }

/* ------------------------------------------------------------------ */
/* Test helper API                                                      */
/* ------------------------------------------------------------------ */

/**
 * Simulate an incoming MQTT message (as if the nff broker published it).
 * Calls the registered rx callback directly.
 */
void nff_port_posix_inject_mqtt(nff_mqtt_handle_t *h,
                                 const char *topic,
                                 const char *payload) {
    if (!h || !h->rx_cb) return;
    h->rx_cb(topic, (const uint8_t *)payload, strlen(payload), h->rx_user);
}

/**
 * Return the last message published to a given topic, or NULL if none.
 * The returned pointer points into an internal static buffer — copy if needed.
 */
const char *nff_port_posix_get_published(nff_mqtt_handle_t *h, const char *topic) {
    if (!h) return NULL;
    /* Search from newest to oldest */
    int total = h->pub_head < MOCK_PUB_SLOTS ? h->pub_head : MOCK_PUB_SLOTS;
    for (int i = total - 1; i >= 0; i--) {
        int slot = i % MOCK_PUB_SLOTS;
        if (h->pub_slots[slot].used &&
            strcmp(h->pub_slots[slot].topic, topic) == 0) {
            return h->pub_slots[slot].payload;
        }
    }
    return NULL;
}

/**
 * Return the newest message on a topic whose payload contains `needle`, or NULL.
 * Unlike get_published (last message only), this searches the whole retained
 * history — needed since ota_progress ticks now follow the ota_started ack on
 * the same response topic.
 */
const char *nff_port_posix_find_published(nff_mqtt_handle_t *h,
                                          const char *topic,
                                          const char *needle) {
    if (!h) return NULL;
    int total = h->pub_head < MOCK_PUB_SLOTS ? h->pub_head : MOCK_PUB_SLOTS;
    for (int i = total - 1; i >= 0; i--) {
        int slot = i % MOCK_PUB_SLOTS;
        if (h->pub_slots[slot].used &&
            strcmp(h->pub_slots[slot].topic, topic) == 0 &&
            strstr(h->pub_slots[slot].payload, needle) != NULL) {
            return h->pub_slots[slot].payload;
        }
    }
    return NULL;
}

/** Clear all published messages. */
void nff_port_posix_clear_published(nff_mqtt_handle_t *h) {
    if (!h) return;
    memset(h->pub_slots, 0, sizeof(h->pub_slots));
    h->pub_head = 0;
}

/**
 * Simulate a WiFi/MQTT drop: mark the mock socket disconnected. The next nff_mqtt_tick sees
 * !is_connected(), reconnects (nff_port_mqtt_connect always re-connects the mock), and re-runs
 * the post-connect setup. Test-only.
 */
void nff_port_posix_force_disconnect(nff_mqtt_handle_t *h) {
    if (!h) return;
    h->connected = false;
}

/** Reset in-memory NVS (call between test cases). */
void nff_port_posix_reset_nvs(void) {
    memset(s_nvs, 0, sizeof(s_nvs));
}

/** Check whether nff_port_reboot() has been called. */
int nff_port_posix_reboot_called(void) { return s_reboot_called; }
void nff_port_posix_reset_reboot(void) { s_reboot_called = 0; }

/** OTA rollback observability for tests. */
int  nff_port_posix_ota_rollback_called(void)   { return s_ota_rollback_called; }
int  nff_port_posix_ota_mark_valid_called(void) { return s_ota_mark_valid_called; }
void nff_port_posix_reset_ota_flags(void) {
    s_ota_rollback_called   = 0;
    s_ota_mark_valid_called = 0;
}

#endif /* !ARDUINO && !ESP_PLATFORM */
