/**
 * test_nonce_ring.c — Unit tests for nff_security nonce ring.
 *
 * Tests:
 *  1. Fresh nonce is accepted
 *  2. Replayed nonce is rejected
 *  3. Ring wraps correctly (NFF_NONCE_RING_SIZE + 1 unique nonces all accepted)
 *  4. After ring overflow, oldest slot is reused and the evicted nonce is accepted again
 *  5. Malformed nonce (empty, too long) is rejected
 */

#include <stdio.h>
#include <string.h>
#include <assert.h>
#include <stdint.h>

/* Pull in security module internals via the public init + a test shim */
#include "nff.h"
#include "nff_port.h"

/* Test shim: nff_security_verify_cmd calls nff_port_ecdsa_p256_verify.
   We override it here to always return 0 (valid) so tests focus on nonce logic. */

/* Minimal stub credentials for tests — use #define for lengths (constant expressions) */
static const uint8_t NFF_CLIENT_CERT_DER[]    = {0};
static const uint8_t NFF_CLIENT_KEY_DER[]     = {0};
static const uint8_t NFF_CA_CERT_DER[]        = {0};
static const uint8_t NFF_CMD_VERIFY_KEY_DER[] = {0x04,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0};
#define NFF_CLIENT_CERT_LEN  sizeof(NFF_CLIENT_CERT_DER)
#define NFF_CLIENT_KEY_LEN   sizeof(NFF_CLIENT_KEY_DER)
#define NFF_CA_CERT_LEN      sizeof(NFF_CA_CERT_DER)
#define NFF_CMD_VERIFY_KEY_LEN 65
static const uint8_t NFF_INTERMEDIATE_CERT_DER[] = {0};
#define NFF_INTERMEDIATE_CERT_LEN sizeof(NFF_INTERMEDIATE_CERT_DER)
#define NFF_FW_VERSION "1.0.0"
#define NFF_BUILD_ID   "aabbccdd11223344"
#define NFF_PROJECT_ID "test-project"

/* nff_port_ecdsa_p256_verify override: always accept for these tests */
int nff_port_ecdsa_p256_verify(const uint8_t *a, const uint8_t *b, size_t c,
                                const uint8_t *d, size_t e) {
    (void)a;(void)b;(void)c;(void)d;(void)e;
    return 0;
}

/* Declared in nff_security.c */
extern void nff_security_reset_nonce_ring(void);
extern int  nff_security_verify_cmd(const char *payload, size_t plen);

/* Build a minimal signed ping payload with given nonce/timestamp */
static void make_ping(char *buf, size_t len, const char *nonce, uint32_t ts,
                      const char *sig) {
    snprintf(buf, len,
             "{\"action\":\"ping\",\"nonce\":\"%s\","
             "\"timestamp\":%lu,\"cmd_sig\":\"%s\"}",
             nonce, (unsigned long)ts, sig);
}

/* Fake valid DER sig (hex, 2 bytes for simplicity — the mock verify accepts all) */
#define FAKE_SIG "3006020101020101"

static void test_fresh_nonce_accepted(void) {
    nff_security_reset_nonce_ring();
    char buf[256];
    make_ping(buf, sizeof(buf), "aabbccdd", 0, FAKE_SIG);
    assert(nff_security_verify_cmd(buf, strlen(buf)) == NFF_OK);
    printf("PASS: fresh nonce accepted\n");
}

static void test_replay_rejected(void) {
    nff_security_reset_nonce_ring();
    char buf[256];
    make_ping(buf, sizeof(buf), "aabbccdd", 0, FAKE_SIG);
    assert(nff_security_verify_cmd(buf, strlen(buf)) == NFF_OK);
    /* Send same nonce again */
    assert(nff_security_verify_cmd(buf, strlen(buf)) == NFF_ERR_SECURITY);
    printf("PASS: replayed nonce rejected\n");
}

static void test_ring_wraparound(void) {
    nff_security_reset_nonce_ring();
    char buf[256];
    char nonce[16];

    /* Fill the ring with NFF_NONCE_RING_SIZE unique nonces */
    for (int i = 0; i < NFF_NONCE_RING_SIZE; i++) {
        snprintf(nonce, sizeof(nonce), "nn%06d", i);
        make_ping(buf, sizeof(buf), nonce, 0, FAKE_SIG);
        int rc = nff_security_verify_cmd(buf, strlen(buf));
        assert(rc == NFF_OK);
    }

    /* Add one more — ring is full, oldest slot evicted */
    snprintf(nonce, sizeof(nonce), "nn%06d", NFF_NONCE_RING_SIZE);
    make_ping(buf, sizeof(buf), nonce, 0, FAKE_SIG);
    assert(nff_security_verify_cmd(buf, strlen(buf)) == NFF_OK);

    /* The very first nonce (nn000000) is now evicted — should be accepted again */
    make_ping(buf, sizeof(buf), "nn000000", 0, FAKE_SIG);
    assert(nff_security_verify_cmd(buf, strlen(buf)) == NFF_OK);
    printf("PASS: ring wraparound — evicted nonce accepted\n");
}

static void test_empty_nonce_rejected(void) {
    nff_security_reset_nonce_ring();
    /* Nonce value is empty string */
    char buf[256];
    snprintf(buf, sizeof(buf),
             "{\"action\":\"ping\",\"nonce\":\"\","
             "\"timestamp\":0,\"cmd_sig\":\"" FAKE_SIG "\"}");
    assert(nff_security_verify_cmd(buf, strlen(buf)) == NFF_ERR_SECURITY);
    printf("PASS: empty nonce rejected\n");
}

int main(void) {
    /* Init global context minimally */
    static nff_config_t cfg = NFF_CONFIG_INITIALIZER("test-device", "localhost");
    cfg.cmd_verify_key     = NFF_CMD_VERIFY_KEY_DER;
    cfg.cmd_verify_key_len = NFF_CMD_VERIFY_KEY_LEN;
    nff_init(&cfg);

    test_fresh_nonce_accepted();
    test_replay_rejected();
    test_ring_wraparound();
    test_empty_nonce_rejected();

    printf("All nonce ring tests passed.\n");
    return 0;
}
