/**
 * test_ota_rollback.c — Unit tests for OTA NVS pending-result flag.
 *
 * Tests:
 *  1. nff_init reads "ota_pending" NVS key and sets g_nff.pending_ota_result
 *  2. After connect, nff_ota_check_pending_result publishes "committed" or "rolled_back"
 *  3. NVS keys are cleared after the report is published
 *  4. Anti-downgrade: OTA command with version <= current is rejected
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
extern void nff_security_reset_nonce_ring(void);
extern void nff_ota_check_pending_result(void);

/* Automatic-rollback test hooks (nff_port_posix.c) */
extern int  nff_port_posix_ota_rollback_called(void);
extern int  nff_port_posix_ota_mark_valid_called(void);
extern void nff_port_posix_reset_ota_flags(void);

/* Health-gate test hooks (nff_port_posix.c) */
extern void nff_port_posix_set_min_heap(uint32_t v);
extern void nff_port_posix_reset_diag(void);

static bool health_always_false(void *u) { (void)u; return false; }
static bool health_always_true(void *u)  { (void)u; return true; }

/* Fast-forward the soak window so the next tick can commit (the soak must already be armed). */
static void elapse_soak(void) {
    g_nff.ota_health_since_ms = nff_port_millis() - (NFF_OTA_MIN_HEALTHY_MS + 1);
}

/* Initialise without resetting NVS — call after populating NVS to simulate a reboot */
static void do_init(void) {
    nff_security_reset_nonce_ring();
    static nff_config_t cfg = NFF_CONFIG_INITIALIZER("test-device", "localhost");
    cfg.cmd_verify_key     = NFF_CMD_VERIFY_KEY_DER;
    cfg.cmd_verify_key_len = NFF_CMD_VERIFY_KEY_LEN;
    nff_init(&cfg);
    nff_connect();
}

/* Full clean init — resets NVS before initialising (clean-boot scenario) */
static void clean_init(void) {
    nff_port_posix_reset_nvs();
    do_init();
}

static void test_pending_committed(void) {
    /* Reset NVS, populate keys (simulating nff_ota writing before reboot), then init */
    nff_port_posix_reset_nvs();
    nff_port_nvs_set_str("ota_pending",   "1");
    nff_port_nvs_set_str("ota_version",   "2.1.0");
    nff_port_nvs_set_str("ota_committed", "1");
    nff_port_nvs_commit();

    /* do_init() calls nff_connect() which calls nff_ota_check_pending_result() internally.
       By the time do_init() returns, the result has been published and the flag cleared. */
    do_init();

    /* Verify the result was published during connect */
    const char *resp = nff_port_posix_get_published(g_nff.mqtt,
                           "nff/test-project/devices/test-device/response");
    assert(resp != NULL);
    assert(strstr(resp, "\"ota_result\"") != NULL);
    assert(strstr(resp, "\"committed\"")  != NULL);
    assert(strstr(resp, "2.1.0")          != NULL);

    /* Flag must be false — cleared by the internal call */
    assert(g_nff.pending_ota_result == false);

    /* NVS keys were cleared during nff_init */
    char val[8] = {0};
    assert(nff_port_nvs_get_str("ota_pending", val, sizeof(val)) != 0);

    printf("PASS: pending committed OTA result published\n");
}

static void test_pending_rolled_back(void) {
    nff_port_posix_reset_nvs();
    nff_port_nvs_set_str("ota_pending",   "1");
    nff_port_nvs_set_str("ota_version",   "1.9.0");
    nff_port_nvs_set_str("ota_committed", "0");  /* "0" = rolled_back */
    nff_port_nvs_commit();

    do_init();  /* publishes ota_result=rolled_back during nff_connect */

    const char *resp = nff_port_posix_get_published(g_nff.mqtt,
                           "nff/test-project/devices/test-device/response");
    assert(resp != NULL);
    assert(strstr(resp, "\"rolled_back\"") != NULL);
    assert(g_nff.pending_ota_result == false);

    printf("PASS: pending rolled_back OTA result published\n");
}

static void test_no_pending_no_publish(void) {
    clean_init();  /* clean boot — no NVS keys, nothing should be published */

    /* Nothing on the response topic from OTA — connect publishes heartbeat only */
    const char *resp = nff_port_posix_get_published(g_nff.mqtt,
                           "nff/test-project/devices/test-device/response");
    assert(resp == NULL);
    printf("PASS: no pending OTA → nothing published\n");
}

static void test_ota_cmd_downgrade_rejected(void) {
    /* Send OTA command with version 1.0.0 when current is 2.0.0 */
    nff_security_reset_nonce_ring();
    nff_port_posix_clear_published(g_nff.mqtt);

    char cmd_topic[128];
    snprintf(cmd_topic, sizeof(cmd_topic), "nff/test-project/devices/test-device/cmd");

    char json[512];
    snprintf(json, sizeof(json),
             "{\"action\":\"ota\","
             "\"version\":\"1.0.0\","
             "\"url\":\"https://cdn.example.com/fw.bin\","
             "\"sha256\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\","
             "\"size\":100,"
             "\"nonce\":\"deadbeef\","
             "\"timestamp\":0,"
             "\"cmd_sig\":\"3006020101020101\"}");

    extern void nff_port_posix_inject_mqtt(nff_mqtt_handle_t *, const char *, const char *);
    nff_port_posix_inject_mqtt(g_nff.mqtt, cmd_topic, json);
    nff_loop();   /* the rx callback only parks the cmd; the loop drains + runs it */

    const char *resp = nff_port_posix_get_published(g_nff.mqtt,
                           "nff/test-project/devices/test-device/response");
    assert(resp != NULL);
    assert(strstr(resp, "\"error\"")     != NULL);
    assert(strstr(resp, "downgrade")     != NULL);
    printf("PASS: OTA downgrade rejected\n");
}

/* ---- Automatic rollback: trial / confirm / revert -------------------- */

static const char *RESP_TOPIC =
    "nff/test-project/devices/test-device/response";

/* A healthy trial image (connects, no health veto) commits and reports committed. */
static void test_trial_commit_on_connect(void) {
    nff_port_posix_reset_nvs();
    nff_port_posix_reset_ota_flags();
    nff_port_nvs_set_str("ota_trial",      "1");
    nff_port_nvs_set_str("ota_version",    "2.1.0");
    nff_port_nvs_set_u32("ota_boot_count", 0);
    nff_port_nvs_commit();

    do_init();                 /* boot_check arms probation; connect does NOT mark valid yet */
    assert(g_nff.ota_trial == true);
    assert(nff_port_posix_ota_mark_valid_called() == 0);

    nff_port_posix_clear_published(g_nff.mqtt);
    nff_loop();                /* connected + heap ok, but soak not yet elapsed -> hold */
    assert(g_nff.ota_trial == true);
    assert(nff_port_posix_ota_mark_valid_called() == 0);
    assert(g_nff.ota_health_since_ms != 0);   /* soak armed */

    elapse_soak();
    nff_loop();                /* soak elapsed -> commit */

    const char *resp = nff_port_posix_get_published(g_nff.mqtt, RESP_TOPIC);
    assert(resp != NULL);
    assert(strstr(resp, "\"ota_result\"") != NULL);
    assert(strstr(resp, "\"committed\"")  != NULL);
    assert(strstr(resp, "2.1.0")          != NULL);
    assert(nff_port_posix_ota_mark_valid_called() == 1);
    assert(nff_port_posix_ota_rollback_called()   == 0);
    assert(g_nff.ota_trial == false);

    char val[8] = {0};
    assert(nff_port_nvs_get_str("ota_trial", val, sizeof(val)) != 0);  /* erased */
    printf("PASS: healthy trial image commits after soak + reports committed\n");
}

/* A trial image that never becomes healthy within the window rolls back, and the
   reverted image then reports rolled_back on its next boot. */
static void test_trial_rollback_on_timeout(void) {
    nff_port_posix_reset_nvs();
    nff_port_posix_reset_ota_flags();
    nff_port_nvs_set_str("ota_trial",      "1");
    nff_port_nvs_set_str("ota_version",    "3.0.0");
    nff_port_nvs_set_u32("ota_boot_count", 0);
    nff_port_nvs_commit();

    do_init();
    assert(g_nff.ota_trial == true);

    nff_register_health_check(health_always_false, NULL);
    g_nff.ota_trial_deadline_ms = nff_port_millis() - 1;  /* window already elapsed */

    nff_loop();                /* not healthy + past deadline -> rollback */
    assert(nff_port_posix_ota_rollback_called() == 1);
    assert(g_nff.ota_trial == false);

    char committed[8] = {0}, pending[8] = {0};
    nff_port_nvs_get_str("ota_committed", committed, sizeof(committed));
    nff_port_nvs_get_str("ota_pending",   pending,   sizeof(pending));
    assert(committed[0] == '0');
    assert(pending[0]   == '1');

    /* Simulate the reverted image booting: it reports rolled_back. */
    nff_port_posix_clear_published(g_nff.mqtt);
    do_init();                 /* boot_check no-op (trial erased); pending block -> rolled_back */
    const char *resp = nff_port_posix_get_published(g_nff.mqtt, RESP_TOPIC);
    assert(resp != NULL);
    assert(strstr(resp, "\"rolled_back\"") != NULL);
    assert(strstr(resp, "3.0.0")           != NULL);
    printf("PASS: unhealthy trial image rolls back + reports rolled_back\n");
}

/* A trial image that keeps rebooting without confirming is rolled back by the
   crash-loop guard the moment the boot counter crosses the threshold. */
static void test_trial_rollback_on_bootloop(void) {
    nff_port_posix_reset_nvs();
    nff_port_posix_reset_ota_flags();
    nff_port_nvs_set_str("ota_trial",      "1");
    nff_port_nvs_set_str("ota_version",    "3.0.0");
    nff_port_nvs_set_u32("ota_boot_count", NFF_OTA_MAX_TRIAL_BOOTS - 1);
    nff_port_nvs_commit();

    /* boot_check increments to the threshold and rolls back inside nff_init;
       because the POSIX rollback doesn't actually reboot, nff_init then consumes
       the queued result and connect publishes rolled_back in the same pass. */
    nff_port_posix_clear_published(g_nff.mqtt);
    do_init();
    assert(nff_port_posix_ota_rollback_called() == 1);
    assert(g_nff.ota_trial == false);

    const char *resp = nff_port_posix_get_published(g_nff.mqtt, RESP_TOPIC);
    assert(resp != NULL);
    assert(strstr(resp, "\"rolled_back\"") != NULL);
    printf("PASS: crash-looping trial image rolls back via boot-count guard\n");
}

/* A health-check veto before the deadline keeps the image on probation — no
   premature commit and no premature rollback. */
static void test_trial_health_veto_holds(void) {
    nff_port_posix_reset_nvs();
    nff_port_posix_reset_ota_flags();
    nff_port_nvs_set_str("ota_trial",      "1");
    nff_port_nvs_set_str("ota_version",    "2.2.0");
    nff_port_nvs_set_u32("ota_boot_count", 0);
    nff_port_nvs_commit();

    do_init();
    nff_register_health_check(health_always_false, NULL);
    /* deadline left in the future (armed at boot_check) */

    nff_port_posix_clear_published(g_nff.mqtt);
    nff_loop();                /* connected but vetoed, before deadline -> hold */

    assert(g_nff.ota_trial == true);
    assert(nff_port_posix_ota_rollback_called()   == 0);
    assert(nff_port_posix_ota_mark_valid_called() == 0);
    assert(nff_port_posix_get_published(g_nff.mqtt, RESP_TOPIC) == NULL);

    char val[8] = {0};
    assert(nff_port_nvs_get_str("ota_trial", val, sizeof(val)) == 0 && val[0] == '1');

    /* Once the app reports healthy, the soak arms and then the next tick commits. */
    nff_register_health_check(health_always_true, NULL);
    nff_loop();                /* arms soak (was reset by the veto) */
    assert(g_nff.ota_trial == true);
    elapse_soak();
    nff_loop();
    assert(g_nff.ota_trial == false);
    assert(nff_port_posix_ota_mark_valid_called() == 1);
    printf("PASS: health-check veto holds probation, then commits when healthy\n");
}

/* The default deeper health gate (heap floor) vetoes commit when min_free_heap is below the
   floor; the image is then rolled back when the confirm window elapses. */
static void test_trial_heap_floor_veto(void) {
    nff_port_posix_reset_nvs();
    nff_port_posix_reset_ota_flags();
    nff_port_nvs_set_str("ota_trial",      "1");
    nff_port_nvs_set_str("ota_version",    "2.3.0");
    nff_port_nvs_set_u32("ota_boot_count", 0);
    nff_port_nvs_commit();

    do_init();
    assert(g_nff.ota_trial == true);

    nff_port_posix_set_min_heap(NFF_OTA_MIN_HEAP_FLOOR - 1);  /* below the floor */
    nff_port_posix_clear_published(g_nff.mqtt);
    nff_loop();                /* connected but heap too low -> hold, soak never arms */
    assert(g_nff.ota_trial == true);
    assert(nff_port_posix_ota_mark_valid_called() == 0);
    assert(g_nff.ota_health_since_ms == 0);

    g_nff.ota_trial_deadline_ms = nff_port_millis() - 1;  /* window elapsed */
    nff_loop();                /* still unhealthy + past deadline -> rollback */
    assert(nff_port_posix_ota_rollback_called() == 1);
    assert(g_nff.ota_trial == false);

    nff_port_posix_reset_diag();
    printf("PASS: low heap vetoes commit, then rolls back at deadline\n");
}

/* Commit records the adopted version under "fw_adopted" so the L2 config loader
   (nvs_creds.c) can report the RUNNING image's real version in telemetry instead of the
   build-time-baked default — without which migration 0080's semver recovery check can
   never observe a fix on an L2/QEMU device. */
static void test_commit_records_fw_adopted(void) {
    nff_port_posix_reset_nvs();
    nff_port_posix_reset_ota_flags();
    nff_port_nvs_set_str("ota_trial",      "1");
    nff_port_nvs_set_str("ota_version",    "2.4.0");
    nff_port_nvs_set_u32("ota_boot_count", 0);
    nff_port_nvs_commit();

    do_init();
    elapse_soak();
    nff_loop();                /* soak elapsed -> commit */
    assert(g_nff.ota_trial == false);
    assert(nff_port_posix_ota_mark_valid_called() == 1);

    char fw[32] = {0};
    assert(nff_port_nvs_get_str("fw_adopted", fw, sizeof(fw)) == 0);
    assert(strcmp(fw, "2.4.0") == 0);
    printf("PASS: commit records adopted fw version to NVS\n");
}

/* Rollback must NOT clobber a previously-committed fw_adopted: it still names the image
   being reverted to. fw_adopted is written only on commit, so during a failing trial it
   holds the OLD committed version — which is exactly the image rollback restores. */
static void test_rollback_preserves_fw_adopted(void) {
    nff_port_posix_reset_nvs();
    nff_port_posix_reset_ota_flags();
    nff_port_nvs_set_str("fw_adopted",     "2.1.0");   /* the currently-committed image */
    nff_port_nvs_set_str("ota_trial",      "1");
    nff_port_nvs_set_str("ota_version",    "3.0.0");   /* the failing trial */
    nff_port_nvs_set_u32("ota_boot_count", 0);
    nff_port_nvs_commit();

    do_init();
    nff_register_health_check(health_always_false, NULL);
    g_nff.ota_trial_deadline_ms = nff_port_millis() - 1;  /* window already elapsed */
    nff_loop();                /* not healthy + past deadline -> rollback */
    assert(nff_port_posix_ota_rollback_called() == 1);

    char fw[32] = {0};
    assert(nff_port_nvs_get_str("fw_adopted", fw, sizeof(fw)) == 0);
    assert(strcmp(fw, "2.1.0") == 0);   /* untouched — still names the reverted image */
    printf("PASS: rollback preserves prior adopted fw version\n");
}

/* An emulated (nff-qemu-esp32) device can't fetch the signed artifact URL, so it adopts the
   requested version IN PLACE: publishes committed immediately, persists fw_adopted, advances its
   live reported version, and never touches the download/rollback machinery. This is what makes the
   L2 self-heal demo recover for real instead of stalling until the OTA plane reaps it to timed_out. */
static void test_qemu_simulated_adopt(void) {
    nff_port_posix_reset_nvs();
    nff_security_reset_nonce_ring();

    static nff_config_t cfg = NFF_CONFIG_INITIALIZER("test-device", "localhost");
    cfg.cmd_verify_key     = NFF_CMD_VERIFY_KEY_DER;
    cfg.cmd_verify_key_len = NFF_CMD_VERIFY_KEY_LEN;
    cfg.device_type        = "nff-qemu-esp32";   /* the simulate gate */
    cfg.fw_version         = "5.1.0";
    nff_init(&cfg);
    nff_connect();
    /* Reset the OTA flags AFTER connect: a clean boot confirms the running image
       (mark_valid), which is unrelated to what the OTA command handler does below. */
    nff_port_posix_reset_ota_flags();
    nff_port_posix_clear_published(g_nff.mqtt);

    extern void nff_port_posix_inject_mqtt(nff_mqtt_handle_t *, const char *, const char *);
    const char *cmd_topic = "nff/test-project/devices/test-device/cmd";
    char json[512];
    snprintf(json, sizeof(json),
             "{\"action\":\"ota\","
             "\"version\":\"5.1.1\","
             "\"url\":\"https://cdn.example.com/fw.bin\","
             "\"sha256\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\","
             "\"size\":100,"
             "\"nonce\":\"deadbeef\","
             "\"timestamp\":0,"
             "\"cmd_sig\":\"3006020101020101\"}");
    nff_port_posix_inject_mqtt(g_nff.mqtt, cmd_topic, json);
    nff_loop();   /* the rx callback only parks the cmd; the loop drains + runs it */

    /* committed published straight away — no reboot, no download, no rollback */
    const char *resp = nff_port_posix_get_published(g_nff.mqtt, RESP_TOPIC);
    assert(resp != NULL);
    assert(strstr(resp, "\"ota_result\"") != NULL);
    assert(strstr(resp, "\"committed\"")  != NULL);
    assert(strstr(resp, "5.1.1")          != NULL);
    assert(nff_port_posix_ota_rollback_called()   == 0);
    assert(nff_port_posix_ota_mark_valid_called() == 0);

    /* fw_adopted persisted so it survives a reboot (nvs_creds.c reads it) */
    char fw[32] = {0};
    assert(nff_port_nvs_get_str("fw_adopted", fw, sizeof(fw)) == 0);
    assert(strcmp(fw, "5.1.1") == 0);

    /* live config version advanced so the heartbeat reports it immediately */
    assert(strcmp(g_nff.cfg->fw_version, "5.1.1") == 0);

    /* The non-qemu path (device_type=NULL) is NOT short-circuited: every other test in this file
       runs with the default device_type and still drives the real download/trial/commit machinery,
       so the gate is proven negative by their continued passing. */
    printf("PASS: qemu device simulate-adopts OTA in place (committed + fw_adopted, no reboot)\n");
}

int main(void) {
    test_pending_committed();
    test_pending_rolled_back();
    test_no_pending_no_publish();
    test_ota_cmd_downgrade_rejected();
    test_trial_commit_on_connect();
    test_trial_rollback_on_timeout();
    test_trial_rollback_on_bootloop();
    test_trial_health_veto_holds();
    test_trial_heap_floor_veto();
    test_commit_records_fw_adopted();
    test_rollback_preserves_fw_adopted();
    test_qemu_simulated_adopt();

    printf("All OTA rollback tests passed.\n");
    return 0;
}
