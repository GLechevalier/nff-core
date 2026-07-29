/* nff_internal.h — shared state, not part of the public API */
#ifndef NFF_INTERNAL_H
#define NFF_INTERNAL_H

#include "nff.h"
#include "nff_port.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ */
/* Global context (defined in nff_core.c)                              */
/* ------------------------------------------------------------------ */

typedef struct {
    const nff_config_t *cfg;
    nff_state_t         state;
    nff_mutex_t         lock;

    /* MQTT */
    nff_mqtt_handle_t  *mqtt;

    /* Reconnect backoff */
    uint32_t            reconnect_delay_ms;   /* current back-off interval */
    uint32_t            reconnect_next_ms;    /* nff_port_millis() target */

    /* Heartbeat timer */
    uint32_t            heartbeat_next_ms;

    /* Pending OTA result to publish after MQTT reconnect */
    bool                pending_ota_result;
    char                pending_ota_version[32];
    bool                pending_ota_committed; /* true=committed, false=rolled_back */

    /* Deferred OTA command: the rx callback (esp-mqtt task on IDF) parks the
       verified payload here; nff_loop (nff task) drains it so the callback
       returns immediately and PUBACK/keepalive keep flowing. Guarded by lock. */
    bool                ota_cmd_pending;
    char                ota_cmd_buf[NFF_CMD_MAXLEN];

    /* Automatic OTA rollback: probation state for a freshly-flashed image */
    bool                ota_trial;             /* true while awaiting the health gate */
    uint32_t            ota_trial_deadline_ms; /* nff_port_millis() target to commit-or-rollback by */
    uint32_t            ota_health_since_ms;   /* when the trial image first became healthy (0=not yet); soak window */
    bool                ota_marked_valid;      /* current image already confirmed valid this boot */
    nff_health_check_t  health_cb;             /* optional app self-test; NULL = connectivity only */
    void               *health_user;

    /* User command registry */
    struct {
        const char        *name;
        nff_cmd_handler_t  handler;
        void              *user_ctx;
    } cmds[NFF_MAX_USER_CMDS];
    int num_cmds;

#if NFF_BOOTSTRAP_ENABLED
    nff_mode_t mode;   /* CLAIMED vs BOOTSTRAP, decided at nff_init */
#endif
} nff_ctx_t;

extern nff_ctx_t g_nff;

/* ------------------------------------------------------------------ */
/* Topic builder helpers (inlined for code size)                       */
/* ------------------------------------------------------------------ */

#include <string.h>
#include <stdio.h>

/* Topics are namespaced by project: nff/{project_id}/devices/{device_id}/{type}. The broker
 * derives (project_id, device_id) from the verified cert and only permits this namespace.
 *
 * NFF_TSTR: %s on a NULL crashes newlib's ROM strlen (LoadProhibited in _svfprintf_r) — seen
 * live when a credential-less config reached nff_mqtt_init with project_id NULL. A malformed
 * topic just fails to match at the broker; a NULL must never take the device down. */
#define NFF_TSTR(s) ((s) ? (s) : "")
static inline void nff_topic_cmd(const nff_ctx_t *c, char *buf) {
    snprintf(buf, NFF_TOPIC_MAXLEN, "nff/%s/devices/%s/cmd",
             NFF_TSTR(c->cfg->project_id), NFF_TSTR(c->cfg->device_id));
}
static inline void nff_topic_response(const nff_ctx_t *c, char *buf) {
    snprintf(buf, NFF_TOPIC_MAXLEN, "nff/%s/devices/%s/response",
             NFF_TSTR(c->cfg->project_id), NFF_TSTR(c->cfg->device_id));
}
static inline void nff_topic_heartbeat(const nff_ctx_t *c, char *buf) {
    snprintf(buf, NFF_TOPIC_MAXLEN, "nff/%s/devices/%s/heartbeat",
             NFF_TSTR(c->cfg->project_id), NFF_TSTR(c->cfg->device_id));
}
static inline void nff_topic_crash(const nff_ctx_t *c, char *buf) {
    snprintf(buf, NFF_TOPIC_MAXLEN, "nff/%s/devices/%s/crash",
             NFF_TSTR(c->cfg->project_id), NFF_TSTR(c->cfg->device_id));
}

#if NFF_BOOTSTRAP_ENABLED
/* Bootstrap (shared batch) namespace: nff/_bootstrap/{batch_id}/{hwid}/{type}. The broker confines
 * an unclaimed device to exactly this until it rolls over to a unique operational cert. */
static inline void nff_topic_bootstrap_announce(const nff_ctx_t *c, char *buf) {
    snprintf(buf, NFF_TOPIC_MAXLEN, "nff/_bootstrap/%s/%s/announce",
             NFF_TSTR(c->cfg->batch_id), NFF_TSTR(c->cfg->device_id));
}
static inline void nff_topic_bootstrap_cmd(const nff_ctx_t *c, char *buf) {
    snprintf(buf, NFF_TOPIC_MAXLEN, "nff/_bootstrap/%s/%s/cmd",
             NFF_TSTR(c->cfg->batch_id), NFF_TSTR(c->cfg->device_id));
}
static inline void nff_topic_bootstrap_response(const nff_ctx_t *c, char *buf) {
    snprintf(buf, NFF_TOPIC_MAXLEN, "nff/_bootstrap/%s/%s/response",
             NFF_TSTR(c->cfg->batch_id), NFF_TSTR(c->cfg->device_id));
}
#endif

/* ------------------------------------------------------------------ */
/* Internal module init functions                                       */
/* ------------------------------------------------------------------ */

void nff_mqtt_init(void);
void nff_mqtt_tick(void);   /* called from nff_loop/task — reconnect + loop */

void nff_heartbeat_init(void);
void nff_heartbeat_on_connect(void);
void nff_heartbeat_tick(void);

void nff_crash_init(void);  /* install panic hook, check previous boot */
void nff_crash_check_and_report(void); /* called after MQTT connect */

void nff_cmd_dispatch(const char *topic, const uint8_t *payload, size_t len);

#if NFF_BOOTSTRAP_ENABLED
/* Claim enrollment (nff_claim.c). */
void nff_claim_announce(void);
void nff_claim_handle_rollover(const char *payload, size_t len);
/* SHA-256 hex of the (base64) device_cert field in a rollover payload; used by the TBS builder so a
 * rollover_cert signature binds to the exact credentials. Returns 0 on success. */
int  nff_claim_cert_sha_hex(const char *payload, size_t plen, char out_hex[65]);
#endif

void nff_ota_check_pending_result(void); /* publish committed/rolled_back if flag set */

/* Automatic OTA rollback (nff_ota.c) */
void nff_ota_boot_check(void);  /* at nff_init: arm probation, or roll back a crash-looping image */
void nff_ota_trial_tick(void);  /* on the loop while on probation: commit when healthy, else roll back */
void nff_ota_pending_tick(void); /* drain a deferred OTA cmd (queued by nff_cmd.c) on the nff task */

#ifdef __cplusplus
}
#endif

#endif /* NFF_INTERNAL_H */
