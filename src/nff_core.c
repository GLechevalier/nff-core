/**
 * nff_core.c — Public API surface and lifecycle state machine.
 *
 * Implements: nff_init, nff_connect, nff_loop, nff_start_task,
 * nff_register_command, nff_log, nff_get_state.
 *
 * State machine: UNINIT → CONNECTING → CONNECTED → (OTA_ACTIVE) → ERROR
 * Reconnect: exponential back-off 1 s → 2 → 4 → 8 → 16 → 32 → 60 s max.
 */

#include "nff_internal.h"
#include <string.h>
#include <stdio.h>
#include <stdarg.h>

#if NFF_BOOTSTRAP_ENABLED
#include "nff_store.h"
#include "nff_port.h"
#endif

/* ------------------------------------------------------------------ */
/* Global context                                                       */
/* ------------------------------------------------------------------ */

nff_ctx_t g_nff;

/* ------------------------------------------------------------------ */
/* Backoff table                                                        */
/* ------------------------------------------------------------------ */

static const uint32_t BACKOFF_TABLE_MS[] = {
    1000, 2000, 4000, 8000, 16000, 32000, 60000
};
#define BACKOFF_STEPS (sizeof(BACKOFF_TABLE_MS) / sizeof(BACKOFF_TABLE_MS[0]))

static void reset_backoff(void) {
    g_nff.reconnect_delay_ms = BACKOFF_TABLE_MS[0];
    g_nff.reconnect_next_ms  = 0;
}

static void advance_backoff(void) {
    g_nff.reconnect_next_ms = nff_port_millis() + g_nff.reconnect_delay_ms;
    /* Step to the next back-off level (saturate at max) */
    for (size_t i = 0; i + 1 < BACKOFF_STEPS; i++) {
        if (g_nff.reconnect_delay_ms == BACKOFF_TABLE_MS[i]) {
            g_nff.reconnect_delay_ms = BACKOFF_TABLE_MS[i + 1];
            return;
        }
    }
    g_nff.reconnect_delay_ms = BACKOFF_TABLE_MS[BACKOFF_STEPS - 1];
}

/* ------------------------------------------------------------------ */
/* nff_init                                                             */
/* ------------------------------------------------------------------ */

int nff_init(const nff_config_t *cfg) {
    if (!cfg || !cfg->device_id || !cfg->broker_host) return NFF_ERR_INVALID_ARG;

    memset(&g_nff, 0, sizeof(g_nff));
    g_nff.cfg   = cfg;
    g_nff.state = NFF_STATE_UNINIT;
    g_nff.lock  = nff_port_mutex_create();
    if (!g_nff.lock) return NFF_ERR_NO_MEM;

    reset_backoff();

#if NFF_BOOTSTRAP_ENABLED
    /* Claim enrollment (DEVICE_OWNERSHIP_DESIGN.md §8): NVS-first. If a per-device operational cert
     * was rolled over earlier it lives in NVS and wins; otherwise fall back to the shared firmware-
     * baked batch credential and run in BOOTSTRAP mode (device_id = runtime hardware id, since the
     * image is identical across the batch). */
    static nff_config_t s_loaded;
    static nff_config_t s_active;
    static char s_hwid[24];
    /* device_id == hwid == operational-cert CN in BOTH modes: the hwid the device announced is what
     * the fleet assigned as device_id and baked into the rolled-over cert's CN. NVS stores the creds
     * but not the id, so derive it from the hardware id either way — otherwise the claimed-mode MQTT
     * client_id is empty and the broker's identity ACL rejects the session (client_id must == cert CN). */
    nff_port_get_unique_id(s_hwid, sizeof(s_hwid));
    g_nff.mode = nff_store_is_claimed() ? NFF_MODE_CLAIMED : NFF_MODE_BOOTSTRAP;
    if (g_nff.mode == NFF_MODE_CLAIMED) {
        if (nff_store_load(cfg, &s_loaded) == NFF_OK) {
            s_loaded.device_id = s_hwid;
            g_nff.cfg = &s_loaded;
        } else {
            g_nff.mode = NFF_MODE_BOOTSTRAP;   /* corrupt NVS → re-enroll */
        }
    }
    if (g_nff.mode == NFF_MODE_BOOTSTRAP) {
        s_active = *cfg;
        s_active.device_id = s_hwid;
        g_nff.cfg = &s_active;
    }
#endif

    /* Set heartbeat interval from config or default */
    uint32_t hb_s = cfg->heartbeat_interval_s ? cfg->heartbeat_interval_s
                                               : NFF_HEARTBEAT_INTERVAL_S;
    g_nff.heartbeat_next_ms = nff_port_millis() + hb_s * 1000;

    /* Automatic OTA rollback: if the previous boot flashed a new image, this
       either arms its probation window or — if it crash-looped — reverts the
       boot partition and reboots now (does not return on hardware). */
    nff_ota_boot_check();

    /* Check for pending OTA result from previous boot (committed by the new
       image, or rolled_back queued by nff_ota_boot_check for the reverted one) */
    char pending[4] = {0};
    if (nff_port_nvs_get_str("ota_pending", pending, sizeof(pending)) == 0) {
        g_nff.pending_ota_result = true;
        nff_port_nvs_get_str("ota_version", g_nff.pending_ota_version,
                              sizeof(g_nff.pending_ota_version));
        /* committed = NVS key "ota_committed" is "1" */
        char committed[4] = {0};
        nff_port_nvs_get_str("ota_committed", committed, sizeof(committed));
        g_nff.pending_ota_committed = (committed[0] == '1');
        /* Clear NVS flags — only report once */
        nff_port_nvs_erase_key("ota_pending");
        nff_port_nvs_erase_key("ota_version");
        nff_port_nvs_erase_key("ota_committed");
        nff_port_nvs_commit();
    }

    /* Install crash handler and check previous crash */
    nff_crash_init();

    return NFF_OK;
}

/* ------------------------------------------------------------------ */
/* nff_connect                                                          */
/* ------------------------------------------------------------------ */

int nff_connect(void) {
    if (!g_nff.cfg) return NFF_ERR_UNINIT;

    g_nff.state = NFF_STATE_CONNECTING;
    /* nff_mqtt_init already opens the MQTT session (with LWT) and subscribes to the cmd topic.
       Do NOT connect a second time here: a duplicate CONNECT from the same client_id makes the
       broker tear down the just-subscribed session and open a fresh, unsubscribed one — session
       churn that left the device flapping (heartbeat once, then drop). */
    nff_mqtt_init();

    if (nff_port_mqtt_is_connected(g_nff.mqtt)) {
        g_nff.state = NFF_STATE_CONNECTED;
        nff_heartbeat_on_connect();
        nff_crash_check_and_report();
        nff_ota_check_pending_result();
        /* Belt-and-suspenders: confirm the running image valid on any normal
           (non-trial) connected boot, so a rollback-enabled bootloader can't
           independently revert it. The trial path withholds this until its
           health gate passes (see nff_ota_trial_tick). */
        if (!g_nff.ota_trial && !g_nff.ota_marked_valid) {
            nff_port_ota_mark_valid();
            g_nff.ota_marked_valid = true;
        }
        reset_backoff();
    } else {
        advance_backoff();
    }
    return nff_port_mqtt_is_connected(g_nff.mqtt) ? NFF_OK : NFF_ERR_MQTT;
}

/* ------------------------------------------------------------------ */
/* nff_loop (Arduino)                                                   */
/* ------------------------------------------------------------------ */

void nff_loop(void) {
    if (!g_nff.cfg) return;

    nff_mqtt_tick();
    nff_ota_pending_tick();   /* deferred OTA cmd — download runs here, off the rx callback */

    /* Trial probation runs even while disconnected: an image that can never reach
       the broker must still roll back at the confirm deadline (commit stays
       unreachable — the health gate requires a live MQTT connection). */
    if (g_nff.ota_trial) {
        nff_ota_trial_tick();   /* commit a healthy trial image, or roll back */
    }

    if (g_nff.state == NFF_STATE_CONNECTED) {
        nff_heartbeat_tick();
    }
}

/* ------------------------------------------------------------------ */
/* nff_start_task (ESP-IDF / FreeRTOS)                                 */
/* ------------------------------------------------------------------ */

static void nff_task_fn(void *arg) {
    (void)arg;
    for (;;) {
        nff_loop();
        nff_port_delay_ms(10);
    }
}

int nff_start_task(void) {
    if (!g_nff.cfg) return NFF_ERR_UNINIT;
    nff_port_task_create(nff_task_fn, "nff", 8192, NULL, 2, 0 /* Core 0 */);
    return NFF_OK;
}

/* ------------------------------------------------------------------ */
/* nff_register_command                                                 */
/* ------------------------------------------------------------------ */

int nff_register_command(const char *name, nff_cmd_handler_t handler, void *user_ctx) {
    if (!name || !handler) return NFF_ERR_INVALID_ARG;
    nff_port_mutex_lock(g_nff.lock);
    if (g_nff.num_cmds >= NFF_MAX_USER_CMDS) {
        nff_port_mutex_unlock(g_nff.lock);
        return NFF_ERR_NO_MEM;
    }
    g_nff.cmds[g_nff.num_cmds].name     = name;
    g_nff.cmds[g_nff.num_cmds].handler  = handler;
    g_nff.cmds[g_nff.num_cmds].user_ctx = user_ctx;
    g_nff.num_cmds++;
    nff_port_mutex_unlock(g_nff.lock);
    return NFF_OK;
}

/* ------------------------------------------------------------------ */
/* nff_register_health_check                                            */
/* ------------------------------------------------------------------ */

int nff_register_health_check(nff_health_check_t cb, void *user_ctx) {
    nff_port_mutex_lock(g_nff.lock);
    g_nff.health_cb   = cb;
    g_nff.health_user = user_ctx;
    nff_port_mutex_unlock(g_nff.lock);
    return NFF_OK;
}

/* ------------------------------------------------------------------ */
/* nff_log                                                              */
/* ------------------------------------------------------------------ */

/* Defined in nff_crash.c where the RTC buffer lives */
extern void nff_crash_log(const char *line);

void nff_log(const char *fmt, ...) {
    char line[NFF_LOG_LINE_LEN];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    nff_crash_log(line);
}

/* ------------------------------------------------------------------ */
/* nff_get_state                                                        */
/* ------------------------------------------------------------------ */

nff_state_t nff_get_state(void) {
    /* Surface probation distinctly while the connectivity/heartbeat machinery
       internally keeps state == CONNECTED during the trial window. */
    if (g_nff.ota_trial && g_nff.state == NFF_STATE_CONNECTED) {
        return NFF_STATE_OTA_TRIAL;
    }
    return g_nff.state;
}
