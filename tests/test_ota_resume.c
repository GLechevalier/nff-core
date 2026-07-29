/**
 * test_ota_resume.c — Resumable OTA download (IOT_BRIDGE.md §3 gap #6).
 *
 * Drives the full "ota" command path through nff_cmd_dispatch against the POSIX HTTPS mock,
 * which can serve a configured body, drop mid-stream once, and optionally ignore Range (200).
 *
 * Tests:
 *  1. A mid-stream drop is resumed via Range from where it stopped; the reassembled image
 *     matches the SHA-256 in the signed command, so the device arms the OTA trial + reboots.
 *  2. A server that ignores Range (returns 200 to a resume) makes the device restart the
 *     download cleanly (no double-write) and still complete with the correct SHA-256.
 */

#include <stdio.h>
#include <string.h>
#include <assert.h>

#include "nff.h"
#include "nff_port.h"
#include "nff_internal.h"

/* Mock ECDSA — always valid */
int nff_port_ecdsa_p256_verify(const uint8_t *a, const uint8_t *b, size_t c,
                                const uint8_t *d, size_t e) {
    (void)a;(void)b;(void)c;(void)d;(void)e;
    return 0;
}

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
#define NFF_FW_VERSION "2.0.0"
#define NFF_BUILD_ID   "aabbccdd11223344"
#define NFF_PROJECT_ID "test-project"

extern nff_ctx_t g_nff;
extern void nff_port_posix_reset_nvs(void);
extern void nff_port_posix_clear_published(nff_mqtt_handle_t *h);
extern const char *nff_port_posix_get_published(nff_mqtt_handle_t *h, const char *topic);
extern const char *nff_port_posix_find_published(nff_mqtt_handle_t *h, const char *topic,
                                                 const char *needle);
extern void nff_security_reset_nonce_ring(void);
extern void nff_port_posix_inject_mqtt(nff_mqtt_handle_t *, const char *, const char *);

/* HTTPS mock controls (nff_port_posix.c) */
extern void nff_port_posix_set_ota_body(const uint8_t *body, size_t len);
extern void nff_port_posix_set_ota_drop_at(size_t n);
extern void nff_port_posix_set_ota_force_200(int on);
extern void nff_port_posix_reset_ota_body(void);
extern int  nff_port_posix_reboot_called(void);
extern void nff_port_posix_reset_reboot(void);

static const char *CMD_TOPIC = "nff/test-project/devices/test-device/cmd";

static void init_connected(void) {
    nff_port_posix_reset_nvs();
    nff_security_reset_nonce_ring();
    static nff_config_t cfg = NFF_CONFIG_INITIALIZER("test-device", "localhost");
    cfg.cmd_verify_key     = NFF_CMD_VERIFY_KEY_DER;
    cfg.cmd_verify_key_len = NFF_CMD_VERIFY_KEY_LEN;
    nff_init(&cfg);
    nff_connect();
}

/* Build a deterministic body and its SHA-256 hex (using the port's own SHA-256). */
static void make_body(uint8_t *body, size_t len, char sha_hex[65]) {
    for (size_t i = 0; i < len; i++) body[i] = (uint8_t)((i * 31u + 7u) & 0xff);
    nff_sha256_ctx_t s = nff_port_sha256_new();
    nff_port_sha256_update(s, body, len);
    uint8_t digest[32];
    nff_port_sha256_finish(s, digest);
    nff_port_sha256_free(s);
    for (int i = 0; i < 32; i++) snprintf(sha_hex + i * 2, 3, "%02x", digest[i]);
}

static void send_ota_cmd(const char *version, const char *sha_hex, size_t size) {
    char json[640];
    snprintf(json, sizeof(json),
             "{\"action\":\"ota\","
             "\"version\":\"%s\","
             "\"url\":\"https://cdn.example.com/fw.bin\","
             "\"sha256\":\"%s\","
             "\"size\":%lu,"
             "\"nonce\":\"deadbeef\","
             "\"timestamp\":0,"
             "\"cmd_sig\":\"3006020101020101\"}",
             version, sha_hex, (unsigned long)size);
    nff_security_reset_nonce_ring();
    nff_port_posix_inject_mqtt(g_nff.mqtt, CMD_TOPIC, json);
    nff_loop();   /* the rx callback only parks the cmd; the loop drains + runs it */
}

/* A drop mid-stream is resumed from the offset; the full image hashes correctly. */
static void test_resume_after_drop(void) {
    init_connected();

    static uint8_t body[1500];
    char sha_hex[65];
    make_body(body, sizeof(body), sha_hex);

    nff_port_posix_set_ota_body(body, sizeof(body));
    nff_port_posix_set_ota_drop_at(700);   /* drop once after ~700 bytes */
    nff_port_posix_reset_reboot();

    send_ota_cmd("2.1.0", sha_hex, sizeof(body));

    /* SHA matched the reassembled image -> trial armed + reboot requested. */
    assert(nff_port_posix_reboot_called() == 1);
    char ver[32] = {0}, trial[4] = {0};
    assert(nff_port_nvs_get_str("ota_trial",   trial, sizeof(trial)) == 0 && trial[0] == '1');
    assert(nff_port_nvs_get_str("ota_version", ver,   sizeof(ver))   == 0);
    assert(strcmp(ver, "2.1.0") == 0);

    nff_port_posix_reset_ota_body();
    printf("PASS: dropped OTA download resumes via Range + verifies\n");
}

/* If the server ignores Range (200), the device restarts the download clean and still verifies. */
static void test_resume_unsupported_restarts(void) {
    init_connected();

    static uint8_t body[1500];
    char sha_hex[65];
    make_body(body, sizeof(body), sha_hex);

    nff_port_posix_set_ota_body(body, sizeof(body));
    nff_port_posix_set_ota_drop_at(700);    /* drop once -> triggers a resume attempt */
    nff_port_posix_set_ota_force_200(1);     /* ...which the server answers with 200 */
    nff_port_posix_reset_reboot();

    send_ota_cmd("2.2.0", sha_hex, sizeof(body));

    /* Clean restart (no double-write) -> SHA matches -> trial armed + reboot. */
    assert(nff_port_posix_reboot_called() == 1);
    char trial[4] = {0};
    assert(nff_port_nvs_get_str("ota_trial", trial, sizeof(trial)) == 0 && trial[0] == '1');

    nff_port_posix_reset_ota_body();
    printf("PASS: server ignoring Range triggers a clean restart + verifies\n");
}

/* The rx callback must only PARK the command (PUBACK-before-download): nothing runs
   until nff_loop drains it, and a second OTA while one is pending is refused busy. */
static void test_ota_deferred_and_busy(void) {
    init_connected();

    static uint8_t body[1500];
    char sha_hex[65];
    make_body(body, sizeof(body), sha_hex);

    nff_port_posix_set_ota_body(body, sizeof(body));
    nff_port_posix_reset_reboot();
    nff_port_posix_clear_published(g_nff.mqtt);

    /* Inject WITHOUT draining: callback returns having done nothing but park it. */
    char json[640];
    snprintf(json, sizeof(json),
             "{\"action\":\"ota\",\"version\":\"2.3.0\","
             "\"url\":\"https://cdn.example.com/fw.bin\",\"sha256\":\"%s\","
             "\"size\":%lu,\"nonce\":\"deadbeef\",\"timestamp\":0,"
             "\"cmd_sig\":\"3006020101020101\"}",
             sha_hex, (unsigned long)sizeof(body));
    nff_security_reset_nonce_ring();
    nff_port_posix_inject_mqtt(g_nff.mqtt, CMD_TOPIC, json);

    assert(g_nff.ota_cmd_pending == true);
    assert(nff_port_posix_reboot_called() == 0);
    assert(nff_port_posix_get_published(g_nff.mqtt,
           "nff/test-project/devices/test-device/response") == NULL);

    /* A second OTA arriving while one is pending is refused busy. */
    nff_security_reset_nonce_ring();
    nff_port_posix_inject_mqtt(g_nff.mqtt, CMD_TOPIC, json);
    const char *resp = nff_port_posix_get_published(g_nff.mqtt,
           "nff/test-project/devices/test-device/response");
    assert(resp != NULL && strstr(resp, "ota: busy") != NULL);

    /* Drain: the parked command runs to completion on the loop. */
    nff_port_posix_clear_published(g_nff.mqtt);
    nff_loop();

    assert(nff_port_posix_reboot_called() == 1);
    assert(g_nff.ota_cmd_pending == false);
    char trial[4] = {0};
    assert(nff_port_nvs_get_str("ota_trial", trial, sizeof(trial)) == 0 && trial[0] == '1');
    /* ota_started is no longer the last response-topic publish — the download now
       emits ota_progress stage markers after it (installing, then rebooting last). */
    resp = nff_port_posix_find_published(g_nff.mqtt,
           "nff/test-project/devices/test-device/response", "ota_started");
    assert(resp != NULL);
    resp = nff_port_posix_get_published(g_nff.mqtt,
           "nff/test-project/devices/test-device/response");
    assert(resp != NULL && strstr(resp, "ota_progress") != NULL
                        && strstr(resp, "\"rebooting\"") != NULL);

    nff_port_posix_reset_ota_body();
    printf("PASS: OTA cmd is deferred out of the rx callback + busy-guarded\n");
}

int main(void) {
    test_resume_after_drop();
    test_resume_unsupported_restarts();
    test_ota_deferred_and_busy();
    printf("All OTA resume tests passed.\n");
    return 0;
}
