/**
 * test_crash_report.c — Boot-time crash report with fault backtrace (IOT_BRIDGE.md §3 gap #8).
 *
 * Simulates a crash boot (crash_simulate NVS key), stages a coredump summary via the POSIX
 * mock, and verifies nff_crash_check_and_report (called from nff_connect) publishes a retained
 * crash payload carrying pc / exception_cause / fault_addr / task_name / backtrace[] / rtc_log /
 * fw_version / build_id — the schema nff-fleet's db.ingest_crash and nff-mock expect.
 */

#include <stdio.h>
#include <string.h>
#include <assert.h>

#include "nff.h"
#include "nff_port.h"
#include "nff_internal.h"

/* Mock ECDSA — always valid (not exercised here, but the SDK links against it). */
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
extern const char *nff_port_posix_get_published(nff_mqtt_handle_t *h, const char *topic);
extern void nff_security_reset_nonce_ring(void);
extern void nff_port_posix_set_crash_info(const nff_crash_hw_info_t *info);

static const char *CRASH_TOPIC = "nff/test-project/devices/test-device/crash";

static void test_crash_report_has_backtrace(void) {
    nff_port_posix_reset_nvs();
    nff_security_reset_nonce_ring();
    nff_port_posix_set_crash_info(NULL);   /* clear any prior */

    /* Mark this boot as a crash boot (was_crash_boot reads crash_simulate on POSIX). */
    nff_port_nvs_set_str("crash_simulate", "1");
    nff_port_nvs_commit();

    /* Stage the coredump summary the SDK will read at boot. */
    nff_crash_hw_info_t hw;
    memset(&hw, 0, sizeof(hw));
    hw.pc              = 0x400d1234;
    hw.fault_addr      = 0x00000000;
    hw.exception_cause = 29;
    snprintf(hw.task_name, sizeof(hw.task_name), "%s", "loopTask");
    hw.backtrace[0]    = 0x400d1234;
    hw.backtrace[1]    = 0x400c5678;
    hw.backtrace_len   = 2;
    hw.valid           = true;
    nff_port_posix_set_crash_info(&hw);

    static nff_config_t cfg = NFF_CONFIG_INITIALIZER("test-device", "localhost");
    cfg.cmd_verify_key     = NFF_CMD_VERIFY_KEY_DER;
    cfg.cmd_verify_key_len = NFF_CMD_VERIFY_KEY_LEN;
    nff_init(&cfg);

    /* Pre-crash log lines (RTC buffer) — pushed after init so rtc_log_init doesn't wipe them. */
    nff_log("crash-test-log-A");
    nff_log("crash-test-log-B");

    nff_connect();   /* triggers nff_crash_check_and_report */

    const char *rep = nff_port_posix_get_published(g_nff.mqtt, CRASH_TOPIC);
    assert(rep != NULL);
    assert(strstr(rep, "\"type\":\"crash\"")            != NULL);
    assert(strstr(rep, "\"pc\":\"0x400d1234\"")         != NULL);
    assert(strstr(rep, "\"exception_cause\":29")        != NULL);
    assert(strstr(rep, "\"task_name\":\"loopTask\"")    != NULL);
    assert(strstr(rep, "\"backtrace\":[")               != NULL);
    assert(strstr(rep, "\"0x400d1234\"")                != NULL);
    assert(strstr(rep, "\"0x400c5678\"")                != NULL);
    assert(strstr(rep, "\"fw_version\":\"2.0.0\"")      != NULL);
    assert(strstr(rep, "\"build_id\":\"aabbccdd11223344\"") != NULL);
    assert(strstr(rep, "\"rtc_log\":[")                 != NULL);
    assert(strstr(rep, "{\"message\":\"crash-test-log-A\"}") != NULL);
    assert(strstr(rep, "\"crash_count\":1")             != NULL);

    /* crash_simulate erased so the next clean boot doesn't re-report. */
    char val[4] = {0};
    assert(nff_port_nvs_get_str("crash_simulate", val, sizeof(val)) != 0);

    printf("PASS: crash report carries pc/backtrace/rtc_log + full schema\n");
}

/* A clean boot (no crash_simulate) publishes nothing on the crash topic. */
static void test_no_crash_no_report(void) {
    nff_port_posix_reset_nvs();
    nff_security_reset_nonce_ring();
    nff_port_posix_set_crash_info(NULL);

    static nff_config_t cfg = NFF_CONFIG_INITIALIZER("test-device", "localhost");
    cfg.cmd_verify_key     = NFF_CMD_VERIFY_KEY_DER;
    cfg.cmd_verify_key_len = NFF_CMD_VERIFY_KEY_LEN;
    nff_init(&cfg);
    nff_connect();

    assert(nff_port_posix_get_published(g_nff.mqtt, CRASH_TOPIC) == NULL);
    printf("PASS: clean boot publishes no crash report\n");
}

int main(void) {
    test_crash_report_has_backtrace();
    test_no_crash_no_report();
    printf("All crash report tests passed.\n");
    return 0;
}
