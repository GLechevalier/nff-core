/**
 * test_claim_rollover.c — claim enrollment (DEVICE_OWNERSHIP_DESIGN.md §8). Built with
 * -DNFF_BOOTSTRAP_ENABLED=1. Covers base64, the chunked NVS store, mode selection, and the
 * rollover_cert dispatch path (verify → persist → ack → reboot).
 */
#include <stdio.h>
#include <string.h>
#include <assert.h>

#include "nff.h"
#include "nff_port.h"
#include "nff_internal.h"
#include "nff_store.h"

/* Mock ECDSA — always valid (we exercise dispatch/persist, not the crypto). */
int nff_port_ecdsa_p256_verify(const uint8_t *a, const uint8_t *b, size_t c,
                                const uint8_t *d, size_t e) {
    (void)a;(void)b;(void)c;(void)d;(void)e; return 0;
}

extern nff_ctx_t g_nff;
extern void nff_security_reset_nonce_ring(void);
void        nff_port_posix_inject_mqtt(nff_mqtt_handle_t *h, const char *topic, const char *payload);
const char *nff_port_posix_get_published(nff_mqtt_handle_t *h, const char *topic);
void        nff_port_posix_clear_published(nff_mqtt_handle_t *h);
void        nff_port_posix_force_disconnect(nff_mqtt_handle_t *h);
void        nff_port_posix_reset_nvs(void);
int         nff_port_posix_reboot_called(void);
void        nff_port_posix_reset_reboot(void);

static const uint8_t MOCK_VKEY[65] = {0x04};

/* ------------------------------------------------------------------ base64 */

static void test_base64(void) {
    const char *msg = "the quick brown fox \x01\x02\xff\x00 jumps";
    size_t n = 30;
    char enc[128];
    uint8_t dec[128];
    int el = nff_b64_encode((const uint8_t *)msg, n, enc, sizeof(enc));
    assert(el > 0);
    int dl = nff_b64_decode(enc, strlen(enc), dec, sizeof(dec));
    assert(dl == (int)n);
    assert(memcmp(dec, msg, n) == 0);
    printf("PASS: base64 round-trip\n");
}

/* ------------------------------------------------------------------ chunked store */

static void test_store_roundtrip(void) {
    nff_port_posix_reset_nvs();
    /* A >256-byte blob forces multiple NVS chunks (POSIX mock caps a value at 256). */
    uint8_t big[400];
    for (int i = 0; i < 400; i++) big[i] = (uint8_t)(i * 7 + 3);
    uint8_t small[8] = {1,2,3,4,5,6,7,8};

    assert(nff_store_is_claimed() == false);
    int rc = nff_store_save("proj-xyz", big, sizeof(big), small, sizeof(small),
                            big, sizeof(big), small, sizeof(small), MOCK_VKEY, sizeof(MOCK_VKEY));
    assert(rc == NFF_OK);
    assert(nff_store_is_claimed() == true);

    nff_config_t base = {0};
    nff_config_t out;
    rc = nff_store_load(&base, &out);
    assert(rc == NFF_OK);
    assert(strcmp(out.project_id, "proj-xyz") == 0);
    /* client_cert is the stored chain (here we stored `big` as both cert and chain). */
    assert(out.client_cert_len == sizeof(big));
    assert(memcmp(out.client_cert, big, sizeof(big)) == 0);
    assert(out.client_key_len == sizeof(small));
    assert(memcmp(out.client_key, small, sizeof(small)) == 0);
    assert(out.cmd_verify_key_len == sizeof(MOCK_VKEY));
    nff_store_free_loaded(&out);
    printf("PASS: chunked NVS store round-trip (>256B forces multi-chunk)\n");
}

/* A store claimed before op_ca provisioning (or with an empty CA) must still load: the CA comes
 * back NULL/0 and the app supplies its own trust anchor (baked root CA fallback). */
static void test_store_load_missing_ca(void) {
    nff_port_posix_reset_nvs();
    uint8_t big[400];
    for (int i = 0; i < 400; i++) big[i] = (uint8_t)(i * 7 + 3);
    uint8_t small[8] = {1,2,3,4,5,6,7,8};

    assert(nff_store_save("proj-noca", big, sizeof(big), small, sizeof(small),
                          big, sizeof(big), small, sizeof(small), MOCK_VKEY, sizeof(MOCK_VKEY)) == NFF_OK);
    /* Emulate a legacy/empty store: zero the op_ca chunk count. */
    assert(nff_port_nvs_set_u32("op_ca.n", 0) == 0);

    nff_config_t base = {0};
    nff_config_t out;
    assert(nff_store_load(&base, &out) == NFF_OK);
    assert(out.ca_cert == NULL && out.ca_cert_len == 0);
    assert(strcmp(out.project_id, "proj-noca") == 0);
    assert(out.client_cert_len == sizeof(big));
    assert(out.client_key_len == sizeof(small));
    assert(out.cmd_verify_key_len == sizeof(MOCK_VKEY));
    nff_store_free_loaded(&out);   /* NULL ca must not crash */
    printf("PASS: store load with missing/empty op_ca (legacy claim) is non-fatal\n");
}

/* A present-but-unreadable op_ca (count says 3 chunks, chunks absent) is corruption → hard fail. */
static void test_store_load_corrupt_ca(void) {
    nff_port_posix_reset_nvs();
    uint8_t small[8] = {1,2,3,4,5,6,7,8};
    assert(nff_store_save("proj-cca", small, sizeof(small), small, sizeof(small),
                          small, sizeof(small), small, sizeof(small), MOCK_VKEY, sizeof(MOCK_VKEY)) == NFF_OK);
    assert(nff_port_nvs_set_u32("op_ca.n", 3) == 0);   /* claims chunks .0-.2; only .0 exists */

    nff_config_t base = {0};
    nff_config_t out;
    assert(nff_store_load(&base, &out) != NFF_OK);
    printf("PASS: store load with corrupt op_ca still fails\n");
}

static void test_mode_selection(void) {
    nff_port_posix_reset_nvs();
    assert(nff_get_mode() == NFF_MODE_BOOTSTRAP);
    nff_store_save("p", MOCK_VKEY, 4, MOCK_VKEY, 4, MOCK_VKEY, 4, MOCK_VKEY, 4, MOCK_VKEY, 4);
    assert(nff_get_mode() == NFF_MODE_CLAIMED);
    printf("PASS: mode selection (empty NVS=BOOTSTRAP, after save=CLAIMED)\n");
}

/* ------------------------------------------------------------------ rollover dispatch */

static void b64_field(char *dst, const char *name, const uint8_t *data, size_t len) {
    char enc[256];
    nff_b64_encode(data, len, enc, sizeof(enc));
    sprintf(dst, "\"%s\":\"%s\"", name, enc);
}

static void test_rollover_dispatch(void) {
    nff_port_posix_reset_nvs();
    nff_port_posix_reset_reboot();

    static nff_config_t cfg = {0};
    cfg.device_id = "";                 /* overridden with the hwid in bootstrap mode */
    cfg.project_id = "test-project";
    cfg.broker_host = "localhost";
    cfg.broker_port = 8883;
    cfg.client_cert = MOCK_VKEY; cfg.client_cert_len = 1;
    cfg.client_key  = MOCK_VKEY; cfg.client_key_len  = 1;
    cfg.ca_cert     = MOCK_VKEY; cfg.ca_cert_len     = 1;
    cfg.cmd_verify_key = MOCK_VKEY; cfg.cmd_verify_key_len = 65;  /* rollover-verify key */
    cfg.batch_id = "batch-test";

    nff_init(&cfg);
    assert(nff_get_mode() == NFF_MODE_BOOTSTRAP);
    assert(strcmp(g_nff.cfg->device_id, "deadbeef0001") == 0);  /* hwid from posix mock */
    nff_connect();

    /* Announce was published on connect. */
    const char *ann = nff_port_posix_get_published(g_nff.mqtt,
        "nff/_bootstrap/batch-test/deadbeef0001/announce");
    assert(ann != NULL && strstr(ann, "unclaimed") != NULL);

    /* Build a rollover_cert command. */
    uint8_t cert[20], key[16], inter[18], ca[12];
    for (int i = 0; i < 20; i++) cert[i]  = (uint8_t)(i + 1);
    for (int i = 0; i < 16; i++) key[i]   = (uint8_t)(i + 40);
    for (int i = 0; i < 18; i++) inter[i] = (uint8_t)(i + 80);
    for (int i = 0; i < 12; i++) ca[i]    = (uint8_t)(i + 120);

    char fcert[256], fkey[256], finter[256], fca[256], fvkey[256];
    b64_field(fcert,  "device_cert",       cert,  sizeof(cert));
    b64_field(fkey,   "device_key",        key,   sizeof(key));
    b64_field(finter, "intermediate_cert", inter, sizeof(inter));
    b64_field(fca,    "ca_cert",           ca,    sizeof(ca));
    b64_field(fvkey,  "verify_key",        MOCK_VKEY, sizeof(MOCK_VKEY));

    char json[2048];
    snprintf(json, sizeof(json),
        "{\"action\":\"rollover_cert\",\"nonce\":\"a1b2c3d4\",\"timestamp\":0,"
        "\"project_id\":\"new-proj\",\"device_id\":\"deadbeef0001\",%s,%s,%s,%s,%s,"
        "\"cmd_sig\":\"3006020101020101\"}",
        fcert, finter, fkey, fca, fvkey);

    nff_security_reset_nonce_ring();
    nff_port_posix_clear_published(g_nff.mqtt);
    nff_port_posix_inject_mqtt(g_nff.mqtt, "nff/_bootstrap/batch-test/deadbeef0001/cmd", json);

    /* Rollover applied: NVS now claimed, ack published, reboot requested. */
    assert(nff_store_is_claimed() == true);
    const char *ack = nff_port_posix_get_published(g_nff.mqtt,
        "nff/_bootstrap/batch-test/deadbeef0001/response");
    assert(ack != NULL && strstr(ack, "rollover_ack") != NULL);
    assert(nff_port_posix_reboot_called() == 1);

    /* The stored project + chain match what was rolled over. */
    nff_config_t base = {0}, loaded;
    assert(nff_store_load(&base, &loaded) == NFF_OK);
    assert(strcmp(loaded.project_id, "new-proj") == 0);
    assert(loaded.client_cert_len == sizeof(cert) + sizeof(inter));  /* leaf||intermediate */
    nff_store_free_loaded(&loaded);
    printf("PASS: rollover_cert dispatch → persist + ack + reboot\n");
}

/* A rollover whose ca_cert is empty must be rejected: the device stays unclaimed (and keeps
 * retrying) rather than persisting a CA-less store. */
static void test_rollover_empty_ca_rejected(void) {
    nff_port_posix_reset_nvs();
    nff_port_posix_reset_reboot();

    static nff_config_t cfg = {0};
    cfg.device_id = "";
    cfg.project_id = "test-project";
    cfg.broker_host = "localhost";
    cfg.broker_port = 8883;
    cfg.client_cert = MOCK_VKEY; cfg.client_cert_len = 1;
    cfg.client_key  = MOCK_VKEY; cfg.client_key_len  = 1;
    cfg.ca_cert     = MOCK_VKEY; cfg.ca_cert_len     = 1;
    cfg.cmd_verify_key = MOCK_VKEY; cfg.cmd_verify_key_len = 65;
    cfg.batch_id = "batch-test";

    nff_init(&cfg);
    assert(nff_get_mode() == NFF_MODE_BOOTSTRAP);
    nff_connect();

    uint8_t cert[20], key[16], inter[18];
    for (int i = 0; i < 20; i++) cert[i]  = (uint8_t)(i + 1);
    for (int i = 0; i < 16; i++) key[i]   = (uint8_t)(i + 40);
    for (int i = 0; i < 18; i++) inter[i] = (uint8_t)(i + 80);

    char fcert[256], fkey[256], finter[256], fvkey[256];
    b64_field(fcert,  "device_cert",       cert,  sizeof(cert));
    b64_field(fkey,   "device_key",        key,   sizeof(key));
    b64_field(finter, "intermediate_cert", inter, sizeof(inter));
    b64_field(fvkey,  "verify_key",        MOCK_VKEY, sizeof(MOCK_VKEY));

    char json[2048];
    snprintf(json, sizeof(json),
        "{\"action\":\"rollover_cert\",\"nonce\":\"e5f6a7b8\",\"timestamp\":0,"
        "\"project_id\":\"new-proj\",\"device_id\":\"deadbeef0001\",%s,%s,%s,\"ca_cert\":\"\",%s,"
        "\"cmd_sig\":\"3006020101020101\"}",
        fcert, finter, fkey, fvkey);

    nff_security_reset_nonce_ring();
    nff_port_posix_clear_published(g_nff.mqtt);
    nff_port_posix_inject_mqtt(g_nff.mqtt, "nff/_bootstrap/batch-test/deadbeef0001/cmd", json);

    assert(nff_store_is_claimed() == false);
    const char *resp = nff_port_posix_get_published(g_nff.mqtt,
        "nff/_bootstrap/batch-test/deadbeef0001/response");
    assert(resp != NULL && strstr(resp, "rollover_error") != NULL);
    assert(nff_port_posix_reboot_called() == 0);
    printf("PASS: rollover with empty ca_cert rejected (stays unclaimed)\n");
}

/* ------------------------------------------------------------------ reconnect re-announce */

/* Regression: a WiFi/MQTT drop while UNCLAIMED must reconnect, re-subscribe to the bootstrap
 * cmd topic, and RE-ANNOUNCE — not silently fall back to the claimed cmd/heartbeat path (which
 * is ACL-denied for a device that holds only the shared batch credential). */
static void test_bootstrap_reconnect_reannounce(void) {
    nff_port_posix_reset_nvs();
    nff_port_posix_reset_reboot();

    static nff_config_t cfg = {0};
    cfg.device_id = "";
    cfg.project_id = "test-project";
    cfg.broker_host = "localhost";
    cfg.broker_port = 8883;
    cfg.client_cert = MOCK_VKEY; cfg.client_cert_len = 1;
    cfg.client_key  = MOCK_VKEY; cfg.client_key_len  = 1;
    cfg.ca_cert     = MOCK_VKEY; cfg.ca_cert_len     = 1;
    cfg.cmd_verify_key = MOCK_VKEY; cfg.cmd_verify_key_len = 65;
    cfg.batch_id = "batch-test";

    nff_init(&cfg);
    assert(nff_get_mode() == NFF_MODE_BOOTSTRAP);
    nff_connect();

    /* Initial announce landed on the bootstrap topic. */
    const char *ann = nff_port_posix_get_published(g_nff.mqtt,
        "nff/_bootstrap/batch-test/deadbeef0001/announce");
    assert(ann != NULL && strstr(ann, "unclaimed") != NULL);

    /* Simulate a WiFi blip, then one tick: the mock reconnects on the next connect. */
    nff_port_posix_clear_published(g_nff.mqtt);
    nff_port_posix_force_disconnect(g_nff.mqtt);
    nff_mqtt_tick();

    /* Fixed: reconnect RE-ANNOUNCED on the bootstrap topic ... */
    const char *reann = nff_port_posix_get_published(g_nff.mqtt,
        "nff/_bootstrap/batch-test/deadbeef0001/announce");
    assert(reann != NULL && strstr(reann, "unclaimed") != NULL);

    /* ... and did NOT publish to the claimed heartbeat topic (the old bug). */
    const char *hb = nff_port_posix_get_published(g_nff.mqtt,
        "nff/test-project/devices/deadbeef0001/heartbeat");
    assert(hb == NULL);
    printf("PASS: bootstrap reconnect re-announces (not claimed cmd/heartbeat)\n");
}

int main(void) {
    test_base64();
    test_store_roundtrip();
    test_store_load_missing_ca();
    test_store_load_corrupt_ca();
    test_mode_selection();
    test_rollover_dispatch();
    test_rollover_empty_ca_rejected();
    test_bootstrap_reconnect_reannounce();
    printf("All claim rollover tests passed.\n");
    return 0;
}
