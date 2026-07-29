/**
 * nff_port.h — Platform Abstraction Layer (PAL) contract.
 *
 * Each target platform supplies one implementation file:
 *   nff_port_esp32_idf.c     — ESP-IDF (any IDF target)
 *   nff_port_esp32_arduino.c — Arduino core for ESP32
 *   nff_port_esp8266_arduino.c — Arduino core for ESP8266
 *   nff_port_posix.c         — POSIX (host unit tests)
 *
 * All nine SDK modules call only nff_port_* functions — never platform
 * SDK functions directly. This file is the only cross-module contract.
 */

#ifndef NFF_PORT_H
#define NFF_PORT_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

/* ------------------------------------------------------------------ */
/* Time                                                                 */
/* ------------------------------------------------------------------ */

/** Milliseconds since boot. Wraps at UINT32_MAX (~49 days). */
uint32_t nff_port_millis(void);

/** Blocking delay. Must not be called from ISR context. */
void nff_port_delay_ms(uint32_t ms);

/* ------------------------------------------------------------------ */
/* Mutex                                                                */
/* ------------------------------------------------------------------ */

typedef void *nff_mutex_t;

nff_mutex_t nff_port_mutex_create(void);
void        nff_port_mutex_lock(nff_mutex_t m);
void        nff_port_mutex_unlock(nff_mutex_t m);
void        nff_port_mutex_destroy(nff_mutex_t m);

/* ------------------------------------------------------------------ */
/* Task                                                                 */
/* ------------------------------------------------------------------ */

/**
 * Create a task / thread.
 *
 * fn          — task function (runs until the process exits; nff tasks loop forever)
 * name        — debug name
 * stack_bytes — requested stack size
 * arg         — passed to fn
 * priority    — FreeRTOS priority (ignored on POSIX — thread priority is left default)
 * core        — CPU core to pin to (-1 = any, 0/1 for AMP on ESP32 dual-core)
 */
void nff_port_task_create(void (*fn)(void *), const char *name,
                           uint32_t stack_bytes, void *arg,
                           uint8_t priority, int core);

/* ------------------------------------------------------------------ */
/* MQTT transport                                                       */
/* ------------------------------------------------------------------ */

/**
 * Opaque MQTT handle. Each platform defines the concrete struct behind
 * this typedef in its own .c file. The SDK holds only a pointer.
 */
typedef struct nff_mqtt_handle nff_mqtt_handle_t;

/**
 * Allocate and zero-initialize a new MQTT handle.
 * Returns NULL on allocation failure.
 */
nff_mqtt_handle_t *nff_port_mqtt_create(void);

/** Set broker address. Call before nff_port_mqtt_connect(). */
void nff_port_mqtt_set_server(nff_mqtt_handle_t *h,
                               const char *host, uint16_t port);

/**
 * Configure mTLS credentials. All buffers are DER-encoded and remain
 * valid for the lifetime of the handle (SDK holds pointers to config).
 *
 * `inter` is the OPTIONAL project intermediate CA cert (may be NULL): when present the port must
 * present the client identity as a leaf -> intermediate chain so a broker that only trusts the
 * root can build leaf -> intermediate -> root. Required for the batch-claim / per-project-CA path
 * (DEVICE_OWNERSHIP_DESIGN.md §5 A1); NULL preserves the single-cert (leaf-only) behaviour.
 */
void nff_port_mqtt_set_tls(nff_mqtt_handle_t *h,
                            const uint8_t *ca,   size_t ca_len,
                            const uint8_t *cert, size_t cert_len,
                            const uint8_t *key,  size_t key_len,
                            const uint8_t *inter, size_t inter_len);

/**
 * Skip the broker-cert CN/SAN hostname check (chain verification stays ON). Runtime carrier of
 * nff_config_t.tls_skip_cn_check — set only by the QEMU L2 NVS identity loader, so one universal
 * image keeps full hostname verification on real hardware. Ports without the concept no-op.
 * Call after nff_port_mqtt_set_tls(), before nff_port_mqtt_connect().
 */
void nff_port_mqtt_set_skip_cn_check(nff_mqtt_handle_t *h, bool skip);

/**
 * Register the receive callback. Called from MQTT thread or ISR with
 * the topic, raw payload bytes, and length.
 * user is the value passed to this function.
 */
void nff_port_mqtt_set_rx_callback(
        nff_mqtt_handle_t *h,
        void (*cb)(const char *topic, const uint8_t *payload,
                   size_t len, void *user),
        void *user);

/**
 * Connect to the broker, registering a Last Will and Testament message
 * that the broker publishes automatically on unclean disconnect.
 *
 * lwt_topic, lwt_payload — retained, QoS 1 LWT
 * Returns 0 on success, negative on error.
 */
int nff_port_mqtt_connect(nff_mqtt_handle_t *h,
                           const char *client_id,
                           const char *lwt_topic,
                           const char *lwt_payload);

/** Subscribe to topic at given QoS. Returns 0 on success. */
int nff_port_mqtt_subscribe(nff_mqtt_handle_t *h,
                             const char *topic, int qos);

/**
 * Publish a payload.
 *
 * payload  — null-terminated C string
 * qos      — 0 or 1
 * retain   — broker retains the message for new subscribers
 * Returns 0 on success.
 */
int nff_port_mqtt_publish(nff_mqtt_handle_t *h,
                           const char *topic,
                           const char *payload,
                           int qos, bool retain);

/**
 * Non-blocking service loop.
 * On event-driven platforms (esp-mqtt): processes pending events.
 * On polling platforms (PubSubClient): calls client.loop().
 * Must return in < 1 ms when idle.
 */
void nff_port_mqtt_loop(nff_mqtt_handle_t *h);

/** Returns true if the MQTT connection is established. */
bool nff_port_mqtt_is_connected(nff_mqtt_handle_t *h);

/* ------------------------------------------------------------------ */
/* NVS / Flash key-value store                                         */
/* ------------------------------------------------------------------ */

/* All operations use the internal namespace "nff". */

int nff_port_nvs_set_str(const char *key, const char *value);
int nff_port_nvs_get_str(const char *key, char *out, size_t out_len);
int nff_port_nvs_set_u32(const char *key, uint32_t value);
int nff_port_nvs_get_u32(const char *key, uint32_t *out);
int nff_port_nvs_erase_key(const char *key);
int nff_port_nvs_commit(void);

/* ------------------------------------------------------------------ */
/* Unique hardware id                                                   */
/* ------------------------------------------------------------------ */

/**
 * Write a stable, per-device unique id (lowercase hex, no separators) to out.
 * Real ports use the efuse/WiFi MAC (e.g. "a1b2c3d4e5f6"); it is the device's
 * client_id and the key the fleet dedups/quotas enrollments by during bootstrap.
 */
void nff_port_get_unique_id(char *out, size_t out_len);

/* ------------------------------------------------------------------ */
/* OTA                                                                  */
/* ------------------------------------------------------------------ */

typedef void *nff_ota_handle_t;
#define NFF_OTA_INVALID_HANDLE ((nff_ota_handle_t)0)

/**
 * Begin an OTA session. expected_size may be 0 if unknown.
 * Returns NFF_OTA_INVALID_HANDLE on failure.
 */
nff_ota_handle_t nff_port_ota_begin(size_t expected_size);

/**
 * Write a firmware chunk. Must be called sequentially with consecutive
 * chunks. Returns 0 on success, negative on flash error.
 */
int nff_port_ota_write(nff_ota_handle_t h, const uint8_t *buf, size_t len);

/**
 * Finalise the OTA session: verify image integrity (platform-level),
 * switch the boot partition, and set the pending-reboot state.
 * Does NOT reboot — call nff_port_reboot() separately.
 * Returns 0 on success.
 */
int nff_port_ota_end(nff_ota_handle_t h);

/** Abort an in-progress OTA session and release resources. */
void nff_port_ota_abort(nff_ota_handle_t h);

/**
 * Confirm the currently-running image as valid, cancelling any pending
 * rollback. Called by the SDK once a freshly-flashed image passes its health
 * gate, and on any normal (non-trial) boot as belt-and-suspenders against a
 * rollback-enabled bootloader. Idempotent. Returns 0 on success.
 * On platforms without A/B rollback (e.g. ESP8266) this is a no-op returning 0.
 */
int nff_port_ota_mark_valid(void);

/**
 * Revert the boot partition to the previous (last-good) image and reboot.
 * Used by the automatic OTA rollback path. Does not return on real hardware.
 * On single-image platforms this degrades to a plain reboot.
 */
void nff_port_ota_rollback(void);

/**
 * Detect (and repair) a bootloader fallback: the bootloader was pointed at a
 * freshly-flashed image but rejected it at boot and fell back to this one, so
 * the configured boot partition no longer matches the running partition. When
 * that happens, point the boot selection back at the RUNNING image (making the
 * fallback durable across power cycles) and return 1. Return 0 when boot and
 * running match (the normal case). Called by nff_ota_boot_check() on a trial
 * boot so the SDK never runs probation — and never falsely commits — on an
 * image that isn't actually the trial. Platforms without A/B slots return 0.
 */
int nff_port_ota_repair_boot_partition(void);

/** Trigger a system reboot. Does not return. */
void nff_port_reboot(void);

/* ------------------------------------------------------------------ */
/* SHA-256 streaming (used internally by nff_ota)                      */
/* ------------------------------------------------------------------ */

typedef void *nff_sha256_ctx_t;

nff_sha256_ctx_t nff_port_sha256_new(void);
void             nff_port_sha256_update(nff_sha256_ctx_t ctx, const uint8_t *data, size_t len);
void             nff_port_sha256_finish(nff_sha256_ctx_t ctx, uint8_t out[32]);
void             nff_port_sha256_free(nff_sha256_ctx_t ctx);

/* ------------------------------------------------------------------ */
/* HTTPS streaming GET                                                  */
/* ------------------------------------------------------------------ */

/**
 * Perform an HTTPS GET and deliver the response body in chunks.
 *
 * resume_from — byte offset to resume from. When > 0 the port sends an HTTP
 *               "Range: bytes=<resume_from>-" header and delivers only the bytes
 *               [resume_from, total). If the server ignores Range and replies 200
 *               (full body) while resume_from > 0, the port returns
 *               NFF_ERR_RESUME_UNSUPPORTED *before* delivering any chunk, so the
 *               caller can restart the download cleanly without double-writing.
 * chunk_cb    — called repeatedly with successive body chunks;
 *               returning < 0 from chunk_cb aborts the download.
 * user_ctx    — forwarded to chunk_cb unchanged
 * timeout_ms  — overall download timeout (0 = platform default)
 * Returns 0 on success (all requested bytes delivered, HTTP 200/206), negative on error.
 */
int nff_port_https_get_stream(
        const char *url,
        size_t resume_from,
        int (*chunk_cb)(const uint8_t *buf, size_t len, void *user_ctx),
        void *user_ctx,
        uint32_t timeout_ms);

/* ------------------------------------------------------------------ */
/* Crypto — ECDSA-P256 verify                                          */
/* ------------------------------------------------------------------ */

/**
 * Verify an ECDSA-P256 signature.
 *
 * pub_key_65  — 65-byte uncompressed public key (0x04 || X || Y)
 * msg / len   — the raw message bytes (SDK passes SHA-256 pre-image)
 * sig_der     — DER-encoded signature (r, s)
 * Returns 0 if signature is valid, negative otherwise.
 */
int nff_port_ecdsa_p256_verify(const uint8_t *pub_key_65,
                                const uint8_t *msg,    size_t msg_len,
                                const uint8_t *sig_der, size_t sig_len);

/* ------------------------------------------------------------------ */
/* System diagnostics                                                   */
/* ------------------------------------------------------------------ */

typedef struct {
    uint32_t free_heap;
    uint32_t min_free_heap;
    uint32_t uptime_ms;
    int32_t  wifi_rssi;   /* dBm; 0 if WiFi not available */
    uint8_t  cpu_count;   /* 1 or 2 */
} nff_diag_info_t;

void nff_port_get_diag_info(nff_diag_info_t *out);

/* ------------------------------------------------------------------ */
/* Hardware identity                                                    */
/* ------------------------------------------------------------------ */

/**
 * Static hardware identity, detected at runtime. Reported in the heartbeat so
 * the server can gate OTA deployments by device_type (a device can't lie about
 * its own silicon). Fields a platform can't determine are left empty/zero.
 *
 * device_type is the canonical nff family string — lowercase, no separators:
 * "esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c6", "esp32h2", "esp8266"
 * ("" if unknown). It MUST match the strings used in an artifact's device_types.
 */
typedef struct {
    char     device_type[16]; /* canonical family, e.g. "esp32s3" ("" if unknown) */
    char     chip_model[24];  /* raw model string, e.g. "ESP32-D0WD-V3" */
    uint8_t  revision;        /* silicon revision */
    uint32_t flash_size;      /* bytes */
    uint8_t  cores;           /* CPU core count */
} nff_hw_info_t;

void nff_port_get_hw_info(nff_hw_info_t *out);

/* ------------------------------------------------------------------ */
/* Panic hook                                                           */
/* ------------------------------------------------------------------ */

/**
 * Install a function called synchronously from the panic handler.
 * The function runs in ISR / non-OS context with the heap potentially
 * corrupted. It must only call nff_port_nvs_* (flash writes) and
 * signal-safe operations. Must be installed at most once.
 */
void nff_port_install_panic_hook(void (*fn)(const char *reason));

/* ------------------------------------------------------------------ */
/* Crash fault detail (read at boot, not in panic context)            */
/* ------------------------------------------------------------------ */

#ifndef NFF_CRASH_BT_MAX
#  define NFF_CRASH_BT_MAX 16
#endif

/**
 * On-device fault detail recovered after a crash. On ESP32 this comes from the
 * coredump-to-flash summary (esp_core_dump_get_summary) read on the next clean
 * boot — which works even on the Arduino port where the panic hook can't run.
 * Fields a platform can't determine are left zero/empty.
 */
typedef struct {
    uint32_t pc;                       /* faulting program counter (0 if unknown) */
    uint32_t fault_addr;               /* faulting data address (0 if unknown)    */
    int      exception_cause;          /* exception cause (-1 if unknown)         */
    char     task_name[16];            /* crashing task ("" if unknown)           */
    uint32_t backtrace[NFF_CRASH_BT_MAX]; /* return addresses, innermost first    */
    uint8_t  backtrace_len;            /* valid entries in backtrace[] (0 = none) */
    bool     valid;                    /* true if a fault summary was recovered   */
} nff_crash_hw_info_t;

/**
 * Fill *out with the fault detail from the last crash, if any is stored.
 * Returns 0 and sets out->valid=true when a summary was found, negative otherwise
 * (out is always zero-initialised first). Safe to call on a clean boot.
 */
int nff_port_get_crash_info(nff_crash_hw_info_t *out);

/** Erase the stored crash image so the next boot doesn't re-report it. */
void nff_port_crash_info_clear(void);

/* ------------------------------------------------------------------ */
/* AMP — asymmetric multiprocessing helpers                            */
/* ------------------------------------------------------------------ */

/**
 * Stall / unstall a CPU core. On single-core platforms returns -1.
 * On ESP32 dual-core, wraps esp_cpu_stall / esp_cpu_unstall.
 * id = 0 or 1.
 */
int nff_port_stall_core(int id);
int nff_port_unstall_core(int id);

#ifdef __cplusplus
}
#endif

#endif /* NFF_PORT_H */
