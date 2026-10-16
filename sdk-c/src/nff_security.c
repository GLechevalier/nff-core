/**
 * nff_security.c — ECDSA-P256 command verification, nonce ring, timestamp check.
 *
 * nff_security_verify_cmd() is the single entry point called by nff_cmd before
 * any command is dispatched. It rejects replayed or stale commands and verifies
 * the server's ECDSA signature over the canonical TBS string.
 */

#include "nff_internal.h"
#include "nff_json.h"
#include <string.h>
#include <stdio.h>
#include <stdint.h>

/* ------------------------------------------------------------------ */
/* Nonce ring                                                           */
/* ------------------------------------------------------------------ */

/* NFF_NONCE_RING_SIZE slots of NFF_NONCE_LEN bytes each.
   Head is the index of the next slot to write. */
static char   s_nonces[NFF_NONCE_RING_SIZE][NFF_NONCE_LEN];
static int    s_nonce_head  = 0;
static int    s_nonce_count = 0;  /* entries filled so far (saturates at ring size) */

/* Constant-time comparison to prevent timing side-channels. */
static int ct_memcmp(const void *a, const void *b, size_t n) {
    const uint8_t *pa = (const uint8_t *)a;
    const uint8_t *pb = (const uint8_t *)b;
    uint8_t diff = 0;
    for (size_t i = 0; i < n; i++) diff |= pa[i] ^ pb[i];
    return (int)diff;  /* 0 = equal */
}

/* Returns 1 if nonce has been seen before. */
static int nonce_seen(const char *nonce) {
    size_t len = strlen(nonce);
    if (len == 0 || len >= NFF_NONCE_LEN) return 1; /* reject malformed */
    int limit = (s_nonce_count < NFF_NONCE_RING_SIZE) ? s_nonce_count : NFF_NONCE_RING_SIZE;
    for (int i = 0; i < limit; i++) {
        if (ct_memcmp(s_nonces[i], nonce, NFF_NONCE_LEN) == 0) return 1;
    }
    return 0;
}

static void nonce_record(const char *nonce) {
    memset(s_nonces[s_nonce_head], 0, NFF_NONCE_LEN);
    strncpy(s_nonces[s_nonce_head], nonce, NFF_NONCE_LEN - 1);
    s_nonce_head = (s_nonce_head + 1) % NFF_NONCE_RING_SIZE;
    if (s_nonce_count < NFF_NONCE_RING_SIZE) s_nonce_count++;
}

/* ------------------------------------------------------------------ */
/* TBS construction (to-be-signed canonical string)                    */
/* ------------------------------------------------------------------ */

/**
 * Build the TBS string for a given command and write it to buf.
 *
 * Format:
 *   ping/reboot/diag/other: "{action}|{nonce}|{timestamp}"
 *   ota:                    "ota|{version}|{sha256}|{nonce}|{timestamp}"
 *
 * Returns the number of bytes written (excluding null), or -1 on error.
 */
static int build_tbs(const char *payload, size_t plen,
                      char *buf, size_t buf_len) {
    char action[32]    = {0};
    char nonce[NFF_NONCE_LEN] = {0};
    uint32_t timestamp = 0;

    if (nff_json_get_str(payload, plen, "action",    action, sizeof(action))   != 0) return -1;
    if (nff_json_get_str(payload, plen, "nonce",     nonce,  sizeof(nonce))    != 0) return -1;
    if (nff_json_get_u32(payload, plen, "timestamp", &timestamp)               != 0) return -1;

    if (strcmp(action, "ota") == 0) {
        char version[32] = {0};
        char sha256[65]  = {0};
        if (nff_json_get_str(payload, plen, "version", version, sizeof(version)) != 0) return -1;
        if (nff_json_get_str(payload, plen, "sha256",  sha256,  sizeof(sha256))  != 0) return -1;
        return snprintf(buf, buf_len, "ota|%s|%s|%s|%lu",
                        version, sha256, nonce, (unsigned long)timestamp);
    }

#if NFF_BOOTSTRAP_ENABLED
    if (strcmp(action, "rollover_cert") == 0) {
        /* rollover_cert|{project_id}|{device_id}|{sha256(device_cert_der)}|{nonce}|{timestamp}
         * Hash the leaf DER (recomputed here from the cert we received) rather than the multi-KB
         * base64 blob, so the TBS stays in this small buffer and the signature binds the exact cert. */
        char project[64] = {0};
        char devid[64]   = {0};
        char cert_sha[65] = {0};
        if (nff_json_get_str(payload, plen, "project_id", project, sizeof(project)) != 0) return -1;
        if (nff_json_get_str(payload, plen, "device_id",  devid,   sizeof(devid))   != 0) return -1;
        if (nff_claim_cert_sha_hex(payload, plen, cert_sha) != 0) return -1;
        return snprintf(buf, buf_len, "rollover_cert|%s|%s|%s|%s|%lu",
                        project, devid, cert_sha, nonce, (unsigned long)timestamp);
    }
#endif

    return snprintf(buf, buf_len, "%s|%s|%lu",
                    action, nonce, (unsigned long)timestamp);
}

/* ------------------------------------------------------------------ */
/* Public verify entry point                                            */
/* ------------------------------------------------------------------ */

/**
 * Verify a raw command payload. Returns:
 *   0                — signature valid, nonce fresh, timestamp in window
 *   NFF_ERR_SECURITY — signature invalid, replayed nonce, or stale timestamp
 */
int nff_security_verify_cmd(const char *payload, size_t plen) {
    char nonce[NFF_NONCE_LEN]       = {0};
    uint32_t timestamp              = 0;
    char sig_hex[145]               = {0}; /* DER ECDSA-P256 max 72 bytes → 144 hex chars + NUL = 145.
                                              MUST be 145, not 144: a 72-byte sig (both r,s sign-padded,
                                              ~26% of signatures) is 144 hex chars and overflowed a 144
                                              buffer, making nff_json_get_str fail → spurious cmd rejects. */

    if (nff_json_get_str(payload, plen, "nonce",     nonce,   sizeof(nonce))   != 0) return NFF_ERR_SECURITY;
    if (nff_json_get_u32(payload, plen, "timestamp", &timestamp)               != 0) return NFF_ERR_SECURITY;
    if (nff_json_get_str(payload, plen, "cmd_sig",   sig_hex, sizeof(sig_hex)) != 0) return NFF_ERR_SECURITY;

    /* 1. Timestamp window check */
    uint32_t now = nff_port_millis() / 1000;  /* approximate wall time — requires SNTP */
    /* Allow a generous window: timestamp is unix epoch, now is uptime — skip window
       check in V1 if the device hasn't run SNTP (now < 1e9 means not synced). */
    if (now > 1000000000u) {
        uint32_t diff = (timestamp > now) ? (timestamp - now) : (now - timestamp);
        if (diff > NFF_TIMESTAMP_WINDOW_S) return NFF_ERR_SECURITY;
    }

    /* 2. Nonce replay check — before ECDSA (cheap short-circuit) */
    if (nonce_seen(nonce)) return NFF_ERR_SECURITY;

    /* 3. Build TBS */
    char tbs[256] = {0};
    if (build_tbs(payload, plen, tbs, sizeof(tbs)) < 0) return NFF_ERR_SECURITY;

    /* 4. Decode hex signature to DER bytes */
    size_t hex_len = strlen(sig_hex);
    if (hex_len == 0 || (hex_len & 1) != 0 || hex_len > 144) return NFF_ERR_SECURITY;
    uint8_t sig_der[72];
    size_t sig_len = hex_len / 2;
    for (size_t i = 0; i < sig_len; i++) {
        uint8_t hi, lo;
        char c = sig_hex[i * 2];
        hi = (uint8_t)((c >= 'a') ? c - 'a' + 10 : (c >= 'A') ? c - 'A' + 10 : c - '0');
        c = sig_hex[i * 2 + 1];
        lo = (uint8_t)((c >= 'a') ? c - 'a' + 10 : (c >= 'A') ? c - 'A' + 10 : c - '0');
        sig_der[i] = (uint8_t)((hi << 4) | lo);
    }

    /* 5. ECDSA verify via port */
    int rc = nff_port_ecdsa_p256_verify(
        g_nff.cfg->cmd_verify_key,
        (const uint8_t *)tbs, strlen(tbs),
        sig_der, sig_len);
    if (rc != 0) return NFF_ERR_SECURITY;

    /* 6. Record nonce only after successful verify */
    nonce_record(nonce);
    return NFF_OK;
}

/* Reset nonce ring (used by host unit tests between test cases) */
void nff_security_reset_nonce_ring(void) {
    memset(s_nonces, 0, sizeof(s_nonces));
    s_nonce_head  = 0;
    s_nonce_count = 0;
}
