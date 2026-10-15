#!/usr/bin/env python3
"""Spike-only patch of the CONTAINER copy of nff_port_esp32_idf.c so it builds + does mTLS on
IDF 5.3. The real nff-sdk-c is untouched. Idempotent. Surfaces real SDK-port gaps:
  1. set_tls had the stale 6-arg signature (no project-intermediate) -> present leaf+intermediate
     PEM chain to esp-mqtt (required so the broker can build the chain to root).
  2. panic hook used non-public IDF APIs -> stub it (the network spike doesn't crash).
"""
import sys, io

PATH = "/project/components/nff/src/port/nff_port_esp32_idf.c"
src = io.open(PATH, encoding="utf-8").read()

if "SPIKE_PORT_PATCHED" not in src:
    # --- includes: base64 for DER->PEM ---
    src = src.replace(
        '#include "mqtt_client.h"',
        '#include "mqtt_client.h"\n#include "mbedtls/base64.h"  /* SPIKE_PORT_PATCHED */',
        1,
    )

    # --- struct: store the intermediate + the built PEM buffers ---
    old_struct = (
        "    const uint8_t           *ca;    size_t ca_len;\n"
        "    const uint8_t           *cert;  size_t cert_len;\n"
        "    const uint8_t           *key;   size_t key_len;\n"
    )
    new_struct = (
        "    const uint8_t           *ca;    size_t ca_len;\n"
        "    const uint8_t           *cert;  size_t cert_len;\n"
        "    const uint8_t           *key;   size_t key_len;\n"
        "    const uint8_t           *inter; size_t inter_len;  /* project intermediate CA (spike) */\n"
        "    char                    *ca_pem; char *cert_pem; char *key_pem;  /* built at connect */\n"
    )
    assert old_struct in src, "struct block not found"
    src = src.replace(old_struct, new_struct, 1)

    # --- set_tls: 8-arg, storing the intermediate (matches nff_port.h + the Arduino port) ---
    old_set_tls = (
        "void nff_port_mqtt_set_tls(nff_mqtt_handle_t *h,\n"
        "                            const uint8_t *ca, size_t ca_len,\n"
        "                            const uint8_t *cert, size_t cert_len,\n"
        "                            const uint8_t *key, size_t key_len) {\n"
        "    struct nff_mqtt_handle *mh = (struct nff_mqtt_handle *)h;\n"
        "    mh->ca       = ca;   mh->ca_len   = ca_len;\n"
        "    mh->cert     = cert; mh->cert_len = cert_len;\n"
        "    mh->key      = key;  mh->key_len  = key_len;\n"
        "}"
    )
    new_set_tls = (
        "/* SPIKE: DER->PEM (base64, 64-col) so a leaf+intermediate CHAIN can be presented. */\n"
        "static char *der_to_pem(const char *begin, const char *end,\n"
        "                        const uint8_t *der, size_t der_len) {\n"
        "    if (!der || !der_len) return NULL;\n"
        "    size_t need = 0;\n"
        "    mbedtls_base64_encode(NULL, 0, &need, der, der_len);\n"
        "    char *b64 = malloc(need + 1);\n"
        "    if (!b64) return NULL;\n"
        "    size_t olen = 0;\n"
        "    if (mbedtls_base64_encode((unsigned char *)b64, need + 1, &olen, der, der_len) != 0) {\n"
        "        free(b64); return NULL;\n"
        "    }\n"
        "    size_t lines = (olen + 63) / 64;\n"
        "    size_t cap = strlen(begin) + strlen(end) + olen + lines + 8;\n"
        "    char *pem = malloc(cap);\n"
        "    if (!pem) { free(b64); return NULL; }\n"
        "    char *p = pem;\n"
        "    p += sprintf(p, \"%s\\n\", begin);\n"
        "    for (size_t i = 0; i < olen; i += 64) {\n"
        "        size_t n = (olen - i < 64) ? olen - i : 64;\n"
        "        memcpy(p, b64 + i, n); p += n; *p++ = '\\n';\n"
        "    }\n"
        "    p += sprintf(p, \"%s\\n\", end);\n"
        "    *p = 0;\n"
        "    free(b64);\n"
        "    return pem;\n"
        "}\n\n"
        "void nff_port_mqtt_set_tls(nff_mqtt_handle_t *h,\n"
        "                            const uint8_t *ca, size_t ca_len,\n"
        "                            const uint8_t *cert, size_t cert_len,\n"
        "                            const uint8_t *key, size_t key_len,\n"
        "                            const uint8_t *inter, size_t inter_len) {\n"
        "    struct nff_mqtt_handle *mh = (struct nff_mqtt_handle *)h;\n"
        "    mh->ca       = ca;    mh->ca_len    = ca_len;\n"
        "    mh->cert     = cert;  mh->cert_len  = cert_len;\n"
        "    mh->key      = key;   mh->key_len   = key_len;\n"
        "    mh->inter    = inter; mh->inter_len = inter_len;\n"
        "}"
    )
    assert old_set_tls in src, "set_tls block not found"
    src = src.replace(old_set_tls, new_set_tls, 1)

    # --- connect: build the PEM chain just before the cfg literal ---
    anchor = '    char uri[320];\n    snprintf(uri, sizeof(uri), "mqtts://%s:%u", mh->host, (unsigned)mh->port);\n'
    build_pem = (
        anchor +
        "\n    /* SPIKE: build PEM (esp-mqtt wants PEM for a multi-cert client chain). */\n"
        "    free(mh->ca_pem); free(mh->cert_pem); free(mh->key_pem);\n"
        "    mh->ca_pem = der_to_pem(\"-----BEGIN CERTIFICATE-----\", \"-----END CERTIFICATE-----\",\n"
        "                            mh->ca, mh->ca_len);\n"
        "    char *leaf = der_to_pem(\"-----BEGIN CERTIFICATE-----\", \"-----END CERTIFICATE-----\",\n"
        "                            mh->cert, mh->cert_len);\n"
        "    char *intr = der_to_pem(\"-----BEGIN CERTIFICATE-----\", \"-----END CERTIFICATE-----\",\n"
        "                            mh->inter, mh->inter_len);\n"
        "    if (leaf && intr) {\n"
        "        mh->cert_pem = malloc(strlen(leaf) + strlen(intr) + 1);\n"
        "        if (mh->cert_pem) { strcpy(mh->cert_pem, leaf); strcat(mh->cert_pem, intr); }\n"
        "        free(leaf); free(intr);\n"
        "    } else { mh->cert_pem = leaf; free(intr); }\n"
        "    mh->key_pem = der_to_pem(\"-----BEGIN PRIVATE KEY-----\", \"-----END PRIVATE KEY-----\",\n"
        "                             mh->key, mh->key_len);\n"
    )
    assert anchor in src, "connect uri anchor not found"
    src = src.replace(anchor, build_pem, 1)

    # --- connect cfg: use the PEM buffers (+1 lengths so esp-tls treats them as PEM) ---
    old_cfg = (
        "            .verification.certificate     = (const char *)mh->ca,\n"
        "            .verification.certificate_len = mh->ca_len,\n"
    )
    new_cfg = (
        "            .verification.certificate     = mh->ca_pem,\n"
        "            .verification.certificate_len = mh->ca_pem ? strlen(mh->ca_pem) + 1 : 0,\n"
    )
    assert old_cfg in src, "cfg verification block not found"
    src = src.replace(old_cfg, new_cfg, 1)

    old_auth = (
        "                .certificate     = (const char *)mh->cert,\n"
        "                .certificate_len = mh->cert_len,\n"
        "                .key             = (const char *)mh->key,\n"
        "                .key_len         = mh->key_len,\n"
    )
    new_auth = (
        "                .certificate     = mh->cert_pem,\n"
        "                .certificate_len = mh->cert_pem ? strlen(mh->cert_pem) + 1 : 0,\n"
        "                .key             = mh->key_pem,\n"
        "                .key_len         = mh->key_pem ? strlen(mh->key_pem) + 1 : 0,\n"
    )
    assert old_auth in src, "cfg authentication block not found"
    src = src.replace(old_auth, new_auth, 1)

    # --- panic hook: stub it (esp_set_panic_handler / esp_panic_info_t are not public in 5.3) ---
    old_panic = (
        'static void (*s_panic_hook)(const char *) = NULL;\n'
        '\n'
        'static void IRAM_ATTR nff_esp_panic_handler(const esp_panic_info_t *info) {\n'
        '    if (s_panic_hook) {\n'
        '        char reason[64];\n'
        '        snprintf(reason, sizeof(reason), "cause=%d pc=0x%08x",\n'
        '                 (int)info->exception_cause,\n'
        '                 (uint32_t)(uintptr_t)info->exception_addr);\n'
        '        s_panic_hook(reason);\n'
        '    }\n'
        '    esp_default_panic_handler(info);\n'
        '}\n'
        '\n'
        'void nff_port_install_panic_hook(void (*fn)(const char *)) {\n'
        '    s_panic_hook = fn;\n'
        '    esp_set_panic_handler(nff_esp_panic_handler);\n'
        '}'
    )
    new_panic = (
        '/* SPIKE: panic-hook customization uses non-public IDF APIs (esp_set_panic_handler /\n'
        ' * esp_panic_info_t); stub it for the network spike, which does not crash. */\n'
        'void nff_port_install_panic_hook(void (*fn)(const char *)) {\n'
        '    (void)fn;\n'
        '}'
    )
    assert old_panic in src, "panic hook block not found"
    src = src.replace(old_panic, new_panic, 1)

    io.open(PATH, "w", encoding="utf-8").write(src)
    print("patched nff_port_esp32_idf.c (SPIKE_PORT_PATCHED)")
else:
    print("already patched")
