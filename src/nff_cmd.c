/**
 * nff_cmd.c — Command receive, parse, verify, dispatch.
 *
 * Called by nff_mqtt when a message arrives on the cmd topic.
 * Verifies the ECDSA signature, then dispatches to built-in or
 * user-registered handlers, and publishes the result on the response topic.
 */

#include "nff_internal.h"
#include "nff_json.h"
#include <string.h>
#include <stdio.h>

/* Forward declaration from nff_security.c */
int nff_security_verify_cmd(const char *payload, size_t plen);

/* ------------------------------------------------------------------ */
/* Built-in handlers                                                    */
/* ------------------------------------------------------------------ */

static void handle_ping(const char *payload, char *resp, size_t resp_len, void *ctx) {
    (void)payload; (void)ctx;
    snprintf(resp, resp_len,
             "{\"type\":\"pong\",\"id\":\"%s\",\"fw\":\"%s\"}",
             g_nff.cfg->device_id,
             g_nff.cfg->fw_version ? g_nff.cfg->fw_version : "");
}

static void handle_reboot(const char *payload, char *resp, size_t resp_len, void *ctx) {
    (void)payload; (void)ctx;
    snprintf(resp, resp_len,
             "{\"type\":\"rebooting\",\"id\":\"%s\"}",
             g_nff.cfg->device_id);
    /* Publish the acknowledgement before rebooting */
    char topic[NFF_TOPIC_MAXLEN];
    nff_topic_response(&g_nff, topic);
    nff_port_mqtt_publish(g_nff.mqtt, topic, resp, 1, false);
    nff_port_delay_ms(200);
    nff_port_reboot();
}

/* ------------------------------------------------------------------ */
/* Dispatch                                                             */
/* ------------------------------------------------------------------ */

void nff_cmd_dispatch(const char *topic, const uint8_t *payload, size_t len) {
    if (!payload || len == 0) return;

    /* Only our own command topic is actionable. The broker is supposed to scope delivery to
       nff/{project}/devices/{id}/#, but defend in depth: a stray heartbeat / announce / another
       device's message routed here must NOT be parsed as a command. Each such message otherwise
       runs a full ECDSA verify (tens of ms) that fails on the missing cmd_sig and spams
       "cmd rejected (security)" — a flood that starves the MQTT keepalive and knocks the device
       offline. A cheap topic compare drops foreign traffic before any crypto. */
    char own_cmd[NFF_TOPIC_MAXLEN];
#if NFF_BOOTSTRAP_ENABLED
    if (g_nff.mode == NFF_MODE_BOOTSTRAP) nff_topic_bootstrap_cmd(&g_nff, own_cmd);
    else                                  nff_topic_cmd(&g_nff, own_cmd);
#else
    nff_topic_cmd(&g_nff, own_cmd);
#endif
    if (!topic || strcmp(topic, own_cmd) != 0) return;  /* not addressed to this device's cmd topic */

    /* Work on a null-terminated copy (payload may not be null-terminated).
       Buffer must hold the largest signed command (OTA cmds carry a pre-signed
       URL and run ~600B+). Reject — rather than silently truncate — anything
       too large, since a truncated copy would always fail signature verify and
       surface as a misleading "security" rejection. */
    static char buf[NFF_CMD_MAXLEN];
    if (len >= sizeof(buf)) {
        nff_log("nff: cmd too large (%u >= %u), dropped", (unsigned)len, (unsigned)sizeof(buf));
        return;
    }
    memcpy(buf, payload, len);
    buf[len] = '\0';

    /* Verify signature first — reject unsigned or replayed commands */
    if (nff_security_verify_cmd(buf, len) != NFF_OK) {
        nff_log("nff: cmd rejected (security)");
        return;
    }

    /* Extract action field */
    char action[32] = {0};
    if (nff_json_get_str(buf, len, "action", action, sizeof(action)) != 0) {
        nff_log("nff: cmd missing action field");
        return;
    }

    /* Response buffer */
    static char resp[NFF_RESPONSE_MAXLEN];
    resp[0] = '\0';

    /* Built-in handlers */
    if (strcmp(action, "ping") == 0) {
        handle_ping(buf, resp, sizeof(resp), NULL);
    } else if (strcmp(action, "reboot") == 0) {
        handle_reboot(buf, resp, sizeof(resp), NULL);
        return; /* handle_reboot publishes and reboots — never reaches here */
    } else if (strcmp(action, "ota") == 0) {
        /* Defer to the nff task: running the download here would block this rx
           callback (esp-mqtt task on IDF) for minutes — no PUBACK, no keepalive,
           broker drops the session mid-download. Park the verified payload;
           nff_ota_pending_tick() runs it from nff_loop. */
        nff_port_mutex_lock(g_nff.lock);
        if (g_nff.ota_cmd_pending || g_nff.state == NFF_STATE_OTA_ACTIVE) {
            nff_port_mutex_unlock(g_nff.lock);
            snprintf(resp, sizeof(resp), "{\"type\":\"error\",\"msg\":\"ota: busy\"}");
        } else {
            memcpy(g_nff.ota_cmd_buf, buf, len + 1);   /* len < NFF_CMD_MAXLEN, NUL included */
            g_nff.ota_cmd_pending = true;
            nff_port_mutex_unlock(g_nff.lock);
            /* resp left empty: skip the generic response — ota_started, published
               from the nff task moments later, stays the first response message. */
        }
    } else if (strcmp(action, "diag") == 0) {
        extern void nff_diag_handle_cmd(const char *payload, char *resp, size_t resp_len);
        nff_diag_handle_cmd(buf, resp, sizeof(resp));
#if NFF_BOOTSTRAP_ENABLED
    } else if (strcmp(action, "rollover_cert") == 0) {
        /* Claim rollover: persist the per-device cert to NVS, ack, reboot. Publishes its own
         * response on the bootstrap channel, so return without the generic publish below. */
        nff_claim_handle_rollover(buf, len);
        return;
#endif
    } else {
        /* User-registered commands */
        int found = 0;
        nff_port_mutex_lock(g_nff.lock);
        for (int i = 0; i < g_nff.num_cmds; i++) {
            if (strcmp(g_nff.cmds[i].name, action) == 0) {
                nff_cmd_handler_t h = g_nff.cmds[i].handler;
                void *ctx            = g_nff.cmds[i].user_ctx;
                nff_port_mutex_unlock(g_nff.lock);
                h(buf, resp, sizeof(resp), ctx);
                found = 1;
                break;
            }
        }
        if (!found) {
            nff_port_mutex_unlock(g_nff.lock);
            snprintf(resp, sizeof(resp),
                     "{\"type\":\"error\",\"msg\":\"unknown action\",\"action\":\"%s\"}",
                     action);
        }
    }

    /* Publish response (skip if handler left resp empty) */
    if (resp[0] != '\0') {
        char rtopic[NFF_TOPIC_MAXLEN];
        nff_topic_response(&g_nff, rtopic);
        nff_port_mqtt_publish(g_nff.mqtt, rtopic, resp, 1, false);
    }
}
