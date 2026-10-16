/**
 * test_cmd_dispatch.c — Unit tests for nff_cmd dispatch.
 *
 * Tests:
 *  1. ping command returns pong JSON on response topic
 *  2. Unknown action returns error JSON
 *  3. User-registered command is dispatched correctly
 *  4. Command with invalid/missing cmd_sig is rejected (no response)
 *  5. diag command returns system info JSON
 */

#include <stdio.h>
#include <string.h>
#include <assert.h>

#include "nff.h"
#include "nff_port.h"
#include "nff_internal.h"

/* Mock ECDSA — always valid for dispatch tests */
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
#define NFF_FW_VERSION "1.0.0"
#define NFF_BUILD_ID   "aabbccdd11223344"
#define NFF_PROJECT_ID "test-project"

extern void nff_security_reset_nonce_ring(void);
/* Access the global MQTT handle */
extern nff_ctx_t g_nff;

/* Test helper declarations from nff_port_posix.c */
void        nff_port_posix_inject_mqtt(nff_mqtt_handle_t *h, const char *topic, const char *payload);
const char *nff_port_posix_get_published(nff_mqtt_handle_t *h, const char *topic);
void        nff_port_posix_clear_published(nff_mqtt_handle_t *h);

static int s_cmd_counter = 0;
static void my_custom_handler(const char *payload, char *resp, size_t resp_len, void *ctx) {
    (void)payload; (void)ctx;
    s_cmd_counter++;
    snprintf(resp, resp_len, "{\"type\":\"custom_ack\",\"count\":%d}", s_cmd_counter);
}

#define FAKE_SIG "3006020101020101"
#define TS "0"

static void send_cmd(const char *json) {
    nff_security_reset_nonce_ring();
    char topic[128];
    snprintf(topic, sizeof(topic), "nff/test-project/devices/test-device/cmd");
    nff_port_posix_inject_mqtt(g_nff.mqtt, topic, json);
}

static void test_ping(void) {
    nff_port_posix_clear_published(g_nff.mqtt);
    char json[256];
    snprintf(json, sizeof(json),
             "{\"action\":\"ping\",\"nonce\":\"aabbccdd\","
             "\"timestamp\":" TS ",\"cmd_sig\":\"" FAKE_SIG "\"}");
    send_cmd(json);

    char rtopic[128];
    snprintf(rtopic, sizeof(rtopic), "nff/test-project/devices/test-device/response");
    const char *resp = nff_port_posix_get_published(g_nff.mqtt, rtopic);
    assert(resp != NULL);
    assert(strstr(resp, "\"pong\"") != NULL);
    printf("PASS: ping → pong\n");
}

static void test_unknown_action(void) {
    nff_port_posix_clear_published(g_nff.mqtt);
    char json[256];
    snprintf(json, sizeof(json),
             "{\"action\":\"nonexistent\",\"nonce\":\"00112233\","
             "\"timestamp\":" TS ",\"cmd_sig\":\"" FAKE_SIG "\"}");
    send_cmd(json);

    char rtopic[128];
    snprintf(rtopic, sizeof(rtopic), "nff/test-project/devices/test-device/response");
    const char *resp = nff_port_posix_get_published(g_nff.mqtt, rtopic);
    assert(resp != NULL);
    assert(strstr(resp, "\"error\"") != NULL);
    assert(strstr(resp, "unknown action") != NULL);
    printf("PASS: unknown action → error response\n");
}

static void test_user_command(void) {
    s_cmd_counter = 0;
    nff_register_command("myaction", my_custom_handler, NULL);
    nff_port_posix_clear_published(g_nff.mqtt);

    char json[256];
    snprintf(json, sizeof(json),
             "{\"action\":\"myaction\",\"nonce\":\"99887766\","
             "\"timestamp\":" TS ",\"cmd_sig\":\"" FAKE_SIG "\"}");
    send_cmd(json);

    char rtopic[128];
    snprintf(rtopic, sizeof(rtopic), "nff/test-project/devices/test-device/response");
    const char *resp = nff_port_posix_get_published(g_nff.mqtt, rtopic);
    assert(resp != NULL);
    assert(strstr(resp, "\"custom_ack\"") != NULL);
    assert(s_cmd_counter == 1);
    printf("PASS: user command dispatched\n");
}

static void test_missing_sig_rejected(void) {
    nff_port_posix_clear_published(g_nff.mqtt);
    /* No cmd_sig field */
    char json[] = "{\"action\":\"ping\",\"nonce\":\"55443322\",\"timestamp\":0}";
    send_cmd(json);

    char rtopic[128];
    snprintf(rtopic, sizeof(rtopic), "nff/test-project/devices/test-device/response");
    const char *resp = nff_port_posix_get_published(g_nff.mqtt, rtopic);
    assert(resp == NULL);  /* No response published — command silently dropped */
    printf("PASS: command without sig rejected silently\n");
}

static void test_diag(void) {
    nff_port_posix_clear_published(g_nff.mqtt);
    char json[256];
    snprintf(json, sizeof(json),
             "{\"action\":\"diag\",\"nonce\":\"aabb1122\","
             "\"timestamp\":" TS ",\"cmd_sig\":\"" FAKE_SIG "\"}");
    send_cmd(json);

    char rtopic[128];
    snprintf(rtopic, sizeof(rtopic), "nff/test-project/devices/test-device/response");
    const char *resp = nff_port_posix_get_published(g_nff.mqtt, rtopic);
    assert(resp != NULL);
    assert(strstr(resp, "\"diag\"")     != NULL);
    assert(strstr(resp, "free_heap")    != NULL);
    assert(strstr(resp, "uptime_ms")    != NULL);
    printf("PASS: diag command returns system info\n");
}

int main(void) {
    static nff_config_t cfg = NFF_CONFIG_INITIALIZER("test-device", "localhost");
    cfg.cmd_verify_key     = NFF_CMD_VERIFY_KEY_DER;
    cfg.cmd_verify_key_len = NFF_CMD_VERIFY_KEY_LEN;

    nff_init(&cfg);
    nff_connect();  /* POSIX mock always connects */

    test_ping();
    test_unknown_action();
    test_user_command();
    test_missing_sig_rejected();
    test_diag();

    printf("All cmd dispatch tests passed.\n");
    return 0;
}
