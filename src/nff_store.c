/* nff_store.c — see nff_store.h. Pure C over nff_port_nvs_* (string-only). */

#include "nff_store.h"
#include "nff_port.h"

#include <stdlib.h>
#include <string.h>
#include <stdio.h>

#define NVS_NS_CLAIMED   "claimed"       /* u32 flag, written last */
#define NVS_PROJECT      "op_project"    /* string */
#define CHUNK_CHARS      200             /* < POSIX mock's 256-byte NVS value cap */

/* ------------------------------------------------------------------ base64 */

static const char B64[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

int nff_b64_encode(const uint8_t *in, size_t in_len, char *out, size_t out_cap) {
    size_t need = ((in_len + 2) / 3) * 4 + 1;
    if (out_cap < need) return -1;
    size_t o = 0;
    for (size_t i = 0; i < in_len; i += 3) {
        uint32_t n = (uint32_t)in[i] << 16;
        int rem = (int)(in_len - i);
        if (rem > 1) n |= (uint32_t)in[i + 1] << 8;
        if (rem > 2) n |= (uint32_t)in[i + 2];
        out[o++] = B64[(n >> 18) & 0x3f];
        out[o++] = B64[(n >> 12) & 0x3f];
        out[o++] = rem > 1 ? B64[(n >> 6) & 0x3f] : '=';
        out[o++] = rem > 2 ? B64[n & 0x3f] : '=';
    }
    out[o] = '\0';
    return (int)o;
}

static int b64_val(char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

int nff_b64_decode(const char *in, size_t in_len, uint8_t *out, size_t out_cap) {
    uint32_t acc = 0;
    int bits = 0;
    size_t o = 0;
    for (size_t i = 0; i < in_len; i++) {
        char c = in[i];
        if (c == '=' || c == '\0') break;
        int v = b64_val(c);
        if (v < 0) continue;               /* skip whitespace/newlines */
        acc = (acc << 6) | (uint32_t)v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            if (o >= out_cap) return -1;
            out[o++] = (uint8_t)((acc >> bits) & 0xff);
        }
    }
    return (int)o;
}

/* ------------------------------------------------------------------ chunked blob store */

static int store_blob(const char *prefix, const uint8_t *data, size_t len) {
    /* base64 the blob, then split into CHUNK_CHARS-sized string chunks. */
    size_t b64cap = ((len + 2) / 3) * 4 + 1;
    char *b64 = (char *)malloc(b64cap);
    if (!b64) return NFF_ERR_NO_MEM;
    int n = nff_b64_encode(data, len, b64, b64cap);
    if (n < 0) { free(b64); return NFF_ERR_NO_MEM; }

    char key[24];
    int chunks = 0;
    for (int off = 0; off < n; off += CHUNK_CHARS) {
        char piece[CHUNK_CHARS + 1];
        int take = (n - off < CHUNK_CHARS) ? (n - off) : CHUNK_CHARS;
        memcpy(piece, b64 + off, (size_t)take);
        piece[take] = '\0';
        snprintf(key, sizeof(key), "%s.%d", prefix, chunks);
        if (nff_port_nvs_set_str(key, piece) != 0) { free(b64); return NFF_ERR_MQTT; }
        chunks++;
    }
    free(b64);
    snprintf(key, sizeof(key), "%s.n", prefix);
    return nff_port_nvs_set_u32(key, (uint32_t)chunks) == 0 ? NFF_OK : NFF_ERR_MQTT;
}

/* Load a chunked blob into a malloc'd buffer; *out is owned by the caller. */
static int load_blob(const char *prefix, uint8_t **out, size_t *out_len) {
    char key[24];
    uint32_t chunks = 0;
    snprintf(key, sizeof(key), "%s.n", prefix);
    if (nff_port_nvs_get_u32(key, &chunks) != 0 || chunks == 0) return NFF_ERR_UNINIT;

    size_t b64cap = (size_t)chunks * CHUNK_CHARS + 1;
    char *b64 = (char *)malloc(b64cap);
    if (!b64) return NFF_ERR_NO_MEM;
    size_t pos = 0;
    for (uint32_t i = 0; i < chunks; i++) {
        snprintf(key, sizeof(key), "%s.%u", prefix, i);
        char piece[CHUNK_CHARS + 1];
        if (nff_port_nvs_get_str(key, piece, sizeof(piece)) != 0) { free(b64); return NFF_ERR_UNINIT; }
        size_t plen = strlen(piece);
        if (pos + plen >= b64cap) { free(b64); return NFF_ERR_NO_MEM; }
        memcpy(b64 + pos, piece, plen);
        pos += plen;
    }
    b64[pos] = '\0';

    uint8_t *buf = (uint8_t *)malloc(pos);   /* decoded <= encoded length */
    if (!buf) { free(b64); return NFF_ERR_NO_MEM; }
    int dn = nff_b64_decode(b64, pos, buf, pos);
    free(b64);
    if (dn < 0) { free(buf); return NFF_ERR_CHECKSUM; }
    *out = buf;
    *out_len = (size_t)dn;
    return NFF_OK;
}

/* ------------------------------------------------------------------ public */

bool nff_store_is_claimed(void) {
    uint32_t v = 0;
    return nff_port_nvs_get_u32(NVS_NS_CLAIMED, &v) == 0 && v == 1;
}

int nff_store_save(const char *project_id,
                   const uint8_t *cert,  size_t cert_len,
                   const uint8_t *key,   size_t key_len,
                   const uint8_t *chain, size_t chain_len,
                   const uint8_t *ca,    size_t ca_len,
                   const uint8_t *vkey,  size_t vkey_len) {
    int rc;
    if ((rc = store_blob("op_cert",  cert,  cert_len))  != NFF_OK) return rc;
    if ((rc = store_blob("op_key",   key,   key_len))   != NFF_OK) return rc;
    if ((rc = store_blob("op_chain", chain, chain_len)) != NFF_OK) return rc;
    if ((rc = store_blob("op_ca",    ca,    ca_len))    != NFF_OK) return rc;
    if ((rc = store_blob("op_vkey",  vkey,  vkey_len))  != NFF_OK) return rc;
    if (nff_port_nvs_set_str(NVS_PROJECT, project_id) != 0) return NFF_ERR_MQTT;
    nff_port_nvs_commit();
    /* claimed flag LAST — the sole gate, so a crash before this leaves us unclaimed. */
    if (nff_port_nvs_set_u32(NVS_NS_CLAIMED, 1) != 0) return NFF_ERR_MQTT;
    nff_port_nvs_commit();
    return NFF_OK;
}

int nff_store_load(const nff_config_t *base, nff_config_t *out) {
    if (!nff_store_is_claimed()) return NFF_ERR_UNINIT;
    *out = *base;   /* inherit broker/fw/fqbn/heartbeat scalars */

    char *project = (char *)malloc(64);
    if (!project) return NFF_ERR_NO_MEM;
    if (nff_port_nvs_get_str(NVS_PROJECT, project, 64) != 0) { free(project); return NFF_ERR_UNINIT; }

    uint8_t *cert = NULL, *key = NULL, *chain = NULL, *ca = NULL, *vkey = NULL;
    size_t cert_n = 0, key_n = 0, chain_n = 0, ca_n = 0, vkey_n = 0;
    int rc = NFF_OK;
    if ((rc = load_blob("op_cert",  &cert,  &cert_n))  != NFF_OK) goto fail;
    if ((rc = load_blob("op_key",   &key,   &key_n))   != NFF_OK) goto fail;
    if ((rc = load_blob("op_chain", &chain, &chain_n)) != NFF_OK) goto fail;
    if ((rc = load_blob("op_ca",    &ca,    &ca_n))    != NFF_OK) goto fail;
    if ((rc = load_blob("op_vkey",  &vkey,  &vkey_n))  != NFF_OK) goto fail;

    out->project_id      = project;
    /* op_chain is raw leaf_DER || intermediate_DER (nff_claim). The port wraps client_cert in
     * ONE PEM block and mbedTLS parses one cert per block, so the trailing intermediate would be
     * silently dropped — the broker then can't chain leaf -> project CA -> root and aborts the
     * handshake. Split here: the leaf blob (op_cert) gives the boundary, and intermediate_cert
     * points into the chain buffer (no extra allocation; freed via client_cert). Images whose
     * NFFxICRT patch slot is empty (every OTA-delivered image) depend on this split. */
    out->client_cert     = chain; out->client_cert_len = chain_n;
    if (chain_n > cert_n && memcmp(chain, cert, cert_n) == 0) {
        out->intermediate_cert     = chain + cert_n;
        out->intermediate_cert_len = chain_n - cert_n;
    }
    out->client_key      = key;   out->client_key_len  = key_n;
    out->ca_cert         = ca;    out->ca_cert_len     = ca_n;
    out->cmd_verify_key  = vkey;  out->cmd_verify_key_len = vkey_n;
    free(cert);   /* leaf DER only needed to locate the chain split */
    return NFF_OK;

fail:
    free(project); free(cert); free(key); free(chain); free(ca); free(vkey);
    return rc;
}

void nff_store_free_loaded(nff_config_t *out) {
    free((void *)out->project_id);
    free((void *)out->client_cert);
    free((void *)out->client_key);
    free((void *)out->ca_cert);
    free((void *)out->cmd_verify_key);
    out->project_id = NULL; out->client_cert = NULL; out->client_key = NULL;
    out->ca_cert = NULL; out->cmd_verify_key = NULL;
    /* intermediate_cert may alias the freed chain buffer (set by nff_store_load's split). */
    out->intermediate_cert = NULL; out->intermediate_cert_len = 0;
}
