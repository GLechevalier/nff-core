/**
 * nff_mqtt.c — MQTT transport wrapper.
 *
 * Creates the port MQTT handle, configures mTLS and LWT, and drives the
 * reconnect loop. All received messages are forwarded to nff_cmd_dispatch.
 */

#include "nff_internal.h"
#include <string.h>
#include <stdio.h>

/* ------------------------------------------------------------------ */
/* RX callback — called by the port's MQTT client on message arrival   */
/* ------------------------------------------------------------------ */

static void mqtt_rx_cb(const char *topic, const uint8_t *payload,
                        size_t len, void *user) {
    (void)user;
    nff_cmd_dispatch(topic, payload, len);
}

/* ------------------------------------------------------------------ */
/* nff_mqtt_after_connect — mode-aware post-(re)connect setup           */
/* ------------------------------------------------------------------ */

/* Run every time the MQTT session becomes live — the FIRST connect (init) AND every reconnect
 * (tick). A reconnect drops the broker-side subscriptions, so we must re-subscribe here; keeping
 * this in one place is what stops the bootstrap and claimed paths from diverging.
 *
 * BOOTSTRAP: the device holds only the shared batch credential, so it is confined to
 * nff/_bootstrap/{batch}/{hwid}/# — re-subscribe to the bootstrap cmd topic and RE-ANNOUNCE so a
 * WiFi blip while unclaimed doesn't leave it connected but deaf to the rollover. (Subscribing to
 * the claimed cmd topic here would be ACL-denied — there is no project cert yet.)
 * CLAIMED: re-subscribe to the operational cmd topic and fire the connect-time callbacks. */
static void nff_mqtt_after_connect(void) {
    char cmd_topic[NFF_TOPIC_MAXLEN];
#if NFF_BOOTSTRAP_ENABLED
    if (g_nff.mode == NFF_MODE_BOOTSTRAP) {
        nff_topic_bootstrap_cmd(&g_nff, cmd_topic);
        nff_port_mqtt_subscribe(g_nff.mqtt, cmd_topic, 1);
        nff_claim_announce();
        nff_log("nff: bootstrap (re)connected — re-announced for enrollment");
    } else
#endif
    {
        nff_topic_cmd(&g_nff, cmd_topic);
        nff_port_mqtt_subscribe(g_nff.mqtt, cmd_topic, 1);
        nff_heartbeat_on_connect();
        nff_crash_check_and_report();
        nff_ota_check_pending_result();
    }
    g_nff.state = NFF_STATE_CONNECTED;
    /* Reset reconnect back-off */
    g_nff.reconnect_delay_ms = 1000;
    g_nff.reconnect_next_ms  = 0;
}

/* ------------------------------------------------------------------ */
/* nff_mqtt_init — allocate handle and configure                        */
/* ------------------------------------------------------------------ */

void nff_mqtt_init(void) {
    if (g_nff.mqtt) return;  /* already initialised */

    g_nff.mqtt = nff_port_mqtt_create();
    if (!g_nff.mqtt) return;

    nff_port_mqtt_set_server(g_nff.mqtt,
                              g_nff.cfg->broker_host,
                              g_nff.cfg->broker_port);

    nff_port_mqtt_set_tls(g_nff.mqtt,
                           g_nff.cfg->ca_cert,    g_nff.cfg->ca_cert_len,
                           g_nff.cfg->client_cert, g_nff.cfg->client_cert_len,
                           g_nff.cfg->client_key,  g_nff.cfg->client_key_len,
                           g_nff.cfg->intermediate_cert, g_nff.cfg->intermediate_cert_len);

    /* Runtime QEMU concession (see nff_config_t.tls_skip_cn_check) — false on real hardware. */
    nff_port_mqtt_set_skip_cn_check(g_nff.mqtt, g_nff.cfg->tls_skip_cn_check);

    nff_port_mqtt_set_rx_callback(g_nff.mqtt, mqtt_rx_cb, NULL);

    /* Register LWT — broker publishes this automatically on unclean disconnect */
    char lwt_topic[NFF_TOPIC_MAXLEN];
    nff_topic_heartbeat(&g_nff, lwt_topic);

    static char lwt_payload[128];
    snprintf(lwt_payload, sizeof(lwt_payload),
             "{\"status\":\"offline\",\"id\":\"%s\"}",
             g_nff.cfg->device_id);

    /* LWT is passed to connect — store for reconnect */
    /* nff_port_mqtt_connect wraps LWT setup in the platform layer when
       lwt_topic/lwt_payload are supplied. Pass them now via connect. */
    nff_port_mqtt_connect(g_nff.mqtt, g_nff.cfg->device_id,
                           lwt_topic, lwt_payload);

    if (nff_port_mqtt_is_connected(g_nff.mqtt)) {
        /* Mode-aware subscribe + announce/callbacks + state (sets NFF_STATE_CONNECTED so a
         * later drop is detected in nff_mqtt_tick and reconnect re-runs this). */
        nff_mqtt_after_connect();
    }
}

/* ------------------------------------------------------------------ */
/* nff_mqtt_tick — called from nff_loop every iteration                */
/* ------------------------------------------------------------------ */

void nff_mqtt_tick(void) {
    if (!g_nff.mqtt) return;

    if (nff_port_mqtt_is_connected(g_nff.mqtt)) {
        /* Service the MQTT event loop */
        nff_port_mqtt_loop(g_nff.mqtt);
        if (g_nff.state == NFF_STATE_CONNECTING) {
            /* Transitioned to live (e.g. a port whose connect completes asynchronously):
             * re-subscribe + announce/callbacks the same way as init/reconnect. */
            nff_mqtt_after_connect();
        }
    } else {
        /* Disconnected: mark state, attempt reconnect after back-off */
        if (g_nff.state == NFF_STATE_CONNECTED ||
            g_nff.state == NFF_STATE_OTA_ACTIVE) {
            g_nff.state = NFF_STATE_CONNECTING;
            nff_log("nff: MQTT disconnected, back-off %lu ms",
                    (unsigned long)g_nff.reconnect_delay_ms);
        }

        uint32_t now = nff_port_millis();
        if (now >= g_nff.reconnect_next_ms) {
            /* Attempt reconnect */
            char lwt_topic[NFF_TOPIC_MAXLEN];
            nff_topic_heartbeat(&g_nff, lwt_topic);
            static char lwt_payload[128];
            snprintf(lwt_payload, sizeof(lwt_payload),
                     "{\"status\":\"offline\",\"id\":\"%s\"}",
                     g_nff.cfg->device_id);

            nff_port_mqtt_connect(g_nff.mqtt, g_nff.cfg->device_id,
                                   lwt_topic, lwt_payload);

            if (nff_port_mqtt_is_connected(g_nff.mqtt)) {
                /* Reconnected: mode-aware re-subscribe + re-announce (bootstrap) or
                 * connect callbacks (claimed) + back-off reset. */
                nff_mqtt_after_connect();
            } else {
                /* Advance back-off */
                g_nff.reconnect_next_ms = now + g_nff.reconnect_delay_ms;
                static const uint32_t steps[] = {1000,2000,4000,8000,16000,32000,60000};
                for (int i = 0; i + 1 < (int)(sizeof(steps)/sizeof(steps[0])); i++) {
                    if (g_nff.reconnect_delay_ms == steps[i]) {
                        g_nff.reconnect_delay_ms = steps[i + 1];
                        break;
                    }
                }
                /* Saturate at 60 s if already at max */
                if (g_nff.reconnect_delay_ms > 60000) g_nff.reconnect_delay_ms = 60000;
            }
        } else {
            /* Not yet time to retry — drive the MQTT loop anyway */
            nff_port_mqtt_loop(g_nff.mqtt);
        }
    }
}
