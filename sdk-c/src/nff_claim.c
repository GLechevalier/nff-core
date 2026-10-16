/**
 * nff_claim.c — device-initiated claim enrollment (DEVICE_OWNERSHIP_DESIGN.md §8).
 *
 * While UNCLAIMED, the device boots on the shared firmware-baked batch credential, announces itself
 * on nff/_bootstrap/{batch}/{hwid}/announce, and waits. When a member accepts it, the fleet sends a
 * signed `rollover_cert` command carrying a UNIQUE per-device cert; this module verifies it (the
 * dispatcher already checked the signature against the pinned fleet rollover key), persists the new
 * credentials to NVS, acks, and reboots into CLAIMED mode.
 *
 * Wholly compiled out unless NFF_BOOTSTRAP_ENABLED.
 */

#include "nff_internal.h"

#if NFF_BOOTSTRAP_ENABLED

#include "nff_store.h"
#include "nff_json.h"
#include "nff_port.h"

#include <stdlib.h>
#include <string.h>
#include <stdio.h>

/* One large scratch buffer for extracting a base64 DER field (rollover blobs run a few KB). */
static char s_field[NFF_ROLLOVER_MQTT_BUFFER_SIZE];

nff_mode_t nff_get_mode(void) {
    return nff_store_is_claimed() ? NFF_MODE_CLAIMED : NFF_MODE_BOOTSTRAP;
}

/* Extract a base64 field and decode it into a freshly malloc'd buffer. Returns 0 on success. */
static int extract_der(const char *payload, size_t plen, const char *key,
                       uint8_t **out, size_t *out_len) {
    if (nff_json_get_str(payload, plen, key, s_field, sizeof(s_field)) != 0) return -1;
    size_t slen = strlen(s_field);
    uint8_t *buf = (uint8_t *)malloc(slen ? slen : 1);
    if (!buf) return -1;
    int n = nff_b64_decode(s_field, slen, buf, slen ? slen : 1);
    if (n < 0) { free(buf); return -1; }
    *out = buf;
    *out_len = (size_t)n;
    return 0;
}

int nff_claim_cert_sha_hex(const char *payload, size_t plen, char out_hex[65]) {
    uint8_t *der = NULL;
    size_t der_len = 0;
    if (extract_der(payload, plen, "device_cert", &der, &der_len) != 0) return -1;
    uint8_t digest[32];
    nff_sha256_ctx_t ctx = nff_port_sha256_new();
    nff_port_sha256_update(ctx, der, der_len);
    nff_port_sha256_finish(ctx, digest);
    nff_port_sha256_free(ctx);
    free(der);
    static const char hexd[] = "0123456789abcdef";
    for (int i = 0; i < 32; i++) {
        out_hex[i * 2]     = hexd[digest[i] >> 4];
        out_hex[i * 2 + 1] = hexd[digest[i] & 0x0f];
    }
    out_hex[64] = '\0';
    return 0;
}

static void publish_bootstrap_response(const char *json) {
    char topic[NFF_TOPIC_MAXLEN];
    nff_topic_bootstrap_response(&g_nff, topic);
    nff_port_mqtt_publish(g_nff.mqtt, topic, json, 1, false);
}

void nff_claim_handle_rollover(const char *payload, size_t plen) {
    /* Signature already verified by nff_cmd_dispatch (against the pinned rollover key), and the TBS
     * bound the device_cert hash — so the credentials below are authentic. */
    char project[64] = {0};
    uint8_t *cert = NULL, *key = NULL, *inter = NULL, *ca = NULL, *vkey = NULL, *chain = NULL;
    size_t cert_n = 0, key_n = 0, inter_n = 0, ca_n = 0, vkey_n = 0;

    if (nff_json_get_str(payload, plen, "project_id", project, sizeof(project)) != 0) goto err;
    if (extract_der(payload, plen, "device_cert",       &cert,  &cert_n)  != 0) goto err;
    if (extract_der(payload, plen, "device_key",        &key,   &key_n)   != 0) goto err;
    if (extract_der(payload, plen, "intermediate_cert", &inter, &inter_n) != 0) goto err;
    if (extract_der(payload, plen, "ca_cert",           &ca,    &ca_n)    != 0) goto err;
    if (ca_n == 0) goto err;   /* refuse to persist a CA-less store — stay unclaimed and retry */
    if (extract_der(payload, plen, "verify_key",        &vkey,  &vkey_n)  != 0) goto err;

    /* The presented mTLS cert is the chain leaf||intermediate (so the broker reaches root). */
    chain = (uint8_t *)malloc(cert_n + inter_n);
    if (!chain) goto err;
    memcpy(chain, cert, cert_n);
    memcpy(chain + cert_n, inter, inter_n);

    if (nff_store_save(project, cert, cert_n, key, key_n, chain, cert_n + inter_n,
                       ca, ca_n, vkey, vkey_n) != NFF_OK) goto err;

    publish_bootstrap_response("{\"type\":\"rollover_ack\",\"status\":\"applied\"}");
    nff_log("nff: rollover applied, rebooting into claimed mode");
    free(cert); free(key); free(inter); free(ca); free(vkey); free(chain);
    nff_port_delay_ms(300);
    nff_port_reboot();
    return;

err:
    nff_log("nff: rollover failed");
    publish_bootstrap_response("{\"type\":\"rollover_error\",\"msg\":\"apply failed\"}");
    free(cert); free(key); free(inter); free(ca); free(vkey); free(chain);
}

void nff_claim_announce(void) {
    nff_hw_info_t hw;
    nff_port_get_hw_info(&hw);
    /* Config device_type overrides the silicon probe (virtual builds); NULL/empty → hardware type. */
    const char *device_type = (g_nff.cfg->device_type && g_nff.cfg->device_type[0])
                              ? g_nff.cfg->device_type : hw.device_type;
    char json[256];
    snprintf(json, sizeof(json),
             "{\"status\":\"unclaimed\",\"id\":\"%s\",\"device_type\":\"%s\",\"chip\":\"%s\","
             "\"flash\":%u,\"fw\":\"%s\"}",
             g_nff.cfg->device_id, device_type, hw.chip_model,
             (unsigned)hw.flash_size, g_nff.cfg->fw_version ? g_nff.cfg->fw_version : "");
    char topic[NFF_TOPIC_MAXLEN];
    nff_topic_bootstrap_announce(&g_nff, topic);
    nff_port_mqtt_publish(g_nff.mqtt, topic, json, 1, true);  /* retained */
}

int nff_bootstrap_run(void) {
    g_nff.mode = NFF_MODE_BOOTSTRAP;
    nff_mqtt_init();          /* mode-aware: subscribes nff/_bootstrap/{batch}/{hwid}/cmd + announces */
    while (1) {
        nff_mqtt_tick();
        nff_port_delay_ms(20);
    }
    return NFF_OK;            /* unreachable: rollover reboots the device */
}

#endif /* NFF_BOOTSTRAP_ENABLED */
