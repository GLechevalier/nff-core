/**
 * nff_crash.c — Panic hook, RTC circular log, boot-time crash report.
 *
 * Three-layer crash pipeline:
 *   1. Panic hook (IRAM, synchronous): writes metadata to NVS.
 *   2. RTC memory circular log: nff_log() appends here; survives reset.
 *   3. Boot-time: detect crash via esp_reset_reason(), assemble JSON,
 *      publish retained QoS-1 to crash topic, then erase NVS keys.
 *
 * On POSIX (host tests): panic hook is a no-op, RTC log is in normal RAM,
 * reset reason is simulated via nff_port_nvs.
 */

#include "nff_internal.h"
#include <string.h>
#include <stdio.h>
#include <stdarg.h>

/* ------------------------------------------------------------------ */
/* RTC circular log                                                     */
/* ------------------------------------------------------------------ */

/*
 * On ESP32 these must be placed in RTC_NOINIT_ATTR. On POSIX and other
 * platforms they are normal static variables (no warm-boot survival, but
 * the logic is exercisable).
 *
 * Platform ports that support RTC memory should redeclare these with
 * RTC_NOINIT_ATTR and include the platform-specific header.
 */

#if defined(ESP_PLATFORM)
#  include "esp_attr.h"
#  include "esp_system.h"
#  define NFF_RTC_ATTR RTC_NOINIT_ATTR
#else
#  define NFF_RTC_ATTR
#endif

#define NFF_RTC_MAGIC 0xDEADBEEFu

NFF_RTC_ATTR static char     s_rtc_log[NFF_LOG_LINES][NFF_LOG_LINE_LEN];
NFF_RTC_ATTR static uint32_t s_rtc_head;
NFF_RTC_ATTR static uint32_t s_rtc_magic;

static void rtc_log_init(void) {
    if (s_rtc_magic != NFF_RTC_MAGIC) {
        memset(s_rtc_log, 0, sizeof(s_rtc_log));
        s_rtc_head  = 0;
        s_rtc_magic = NFF_RTC_MAGIC;
    }
}

/* Quiet mode: suppress the console echo of nff_log lines. For transports that OWN the
   console UART (the wired PPPoS image: UART0 carries PPP frames, so every echoed byte
   corrupts the link — and closing stdout instead crashes the next printf on the freed
   FILE lock, bench-proven via LoadProhibited in _vfprintf_r). The RTC ring keeps
   recording either way; nff_log_recent() is the quiet-mode read path. */
static bool s_log_quiet = false;

void nff_log_set_quiet(bool quiet) { s_log_quiet = quiet; }

/* Append a line to the RTC circular buffer. Called from nff_log(). */
void nff_crash_log(const char *line) {
    rtc_log_init();
    uint32_t slot = s_rtc_head % NFF_LOG_LINES;
    strncpy(s_rtc_log[slot], line, NFF_LOG_LINE_LEN - 1);
    s_rtc_log[slot][NFF_LOG_LINE_LEN - 1] = '\0';
    s_rtc_head++;

    if (s_log_quiet) return;

    /* Also output to UART via platform log */
#if defined(ESP_PLATFORM)
    printf("[nff] %s\n", line);
#elif defined(ARDUINO)
    Serial.println(line);
#else
    fprintf(stderr, "[nff] %s\n", line);
#endif
}

/* Copy the RTC log ring into `out` (oldest first, newline-separated, NUL-terminated).
   Returns the number of lines copied. Lets a transport-constrained image (e.g. the wired
   PPPoS sketch, where UART0 belongs to PPP) dump recent diagnostics over whatever side
   channel it does have — the wired config window, a display, a debug blink code. */
int nff_log_recent(char *out, size_t cap) {
    rtc_log_init();
    if (!out || cap == 0) return 0;
    out[0] = '\0';
    size_t used = 0;
    int copied = 0;
    uint32_t start = s_rtc_head >= NFF_LOG_LINES ? s_rtc_head - NFF_LOG_LINES : 0;
    for (uint32_t i = start; i < s_rtc_head; i++) {
        const char *line = s_rtc_log[i % NFF_LOG_LINES];
        if (!line[0]) continue;
        size_t need = strlen(line) + 1; /* + newline */
        if (used + need + 1 > cap) break;
        memcpy(out + used, line, need - 1);
        used += need - 1;
        out[used++] = '\n';
        copied++;
    }
    out[used] = '\0';
    return copied;
}

/* ------------------------------------------------------------------ */
/* Panic hook                                                           */
/* ------------------------------------------------------------------ */

static void panic_handler(const char *reason) {
    /* This runs in ISR / panic context — only NVS writes are safe.
     * The crash counter is incremented at boot (see nff_crash_check_and_report) so it works on
     * ports where this hook never runs (the Arduino panic handler is not programmable); here we
     * only persist a best-effort reason + uptime as a fallback for when no coredump is stored. */
    if (reason) {
        /* Truncate to fit NVS string limit (typically 256 chars) */
        char trunc[128];
        strncpy(trunc, reason, sizeof(trunc) - 1);
        trunc[sizeof(trunc) - 1] = '\0';
        nff_port_nvs_set_str("crash_reason", trunc);
    }

    nff_port_nvs_set_u32("crash_uptime", nff_port_millis());
    nff_port_nvs_commit();
    /* The platform's default panic handler continues from here */
}

/* ------------------------------------------------------------------ */
/* nff_crash_init                                                       */
/* ------------------------------------------------------------------ */

void nff_crash_init(void) {
    rtc_log_init();
    nff_port_install_panic_hook(panic_handler);
}

/* ------------------------------------------------------------------ */
/* nff_crash_check_and_report (called after MQTT connect)              */
/* ------------------------------------------------------------------ */

/*
 * On ESP32 we query esp_reset_reason(). On POSIX we check an NVS key
 * "crash_simulate" that tests can set to trigger the code path.
 */
static int was_crash_boot(void) {
#if defined(ESP_PLATFORM)
    esp_reset_reason_t r = esp_reset_reason();
    if (r == ESP_RST_PANIC     ||
        r == ESP_RST_INT_WDT   ||
        r == ESP_RST_TASK_WDT  ||
        r == ESP_RST_WDT)
        return 1;
    /* Fall back to a stored coredump image. Under qemu-system-xtensa the L2 supervisor runs QEMU
       with -no-reboot and relaunches a FRESH qemu process on every panic, so the guest always
       boots as ESP_RST_POWERON and the panic reset-reason is lost — but the coredump written to
       the flash partition persists across the relaunch (verified: "Found core dump ... in flash").
       A valid coredump present == an unreported crash. Harmless on real hardware (a coredump only
       exists after a real crash), and nff_crash_check_and_report erases it after reporting
       (nff_port_crash_info_clear -> esp_core_dump_image_erase) so a clean boot won't re-report. */
    {
        nff_crash_hw_info_t hw;
        if (nff_port_get_crash_info(&hw) == 0 && hw.valid)
            return 1;
    }
    return 0;
#else
    char val[4] = {0};
    return (nff_port_nvs_get_str("crash_simulate", val, sizeof(val)) == 0
            && val[0] == '1');
#endif
}

void nff_crash_check_and_report(void) {
    if (!was_crash_boot()) return;

    /* Bump the cumulative crash counter here, at boot — single source of truth that works on
       every port, including the Arduino one where the panic hook never runs. */
    uint32_t count = 0;
    nff_port_nvs_get_u32("crash_count", &count);
    count++;
    nff_port_nvs_set_u32("crash_count", count);

    /* Best-effort metadata persisted by the panic hook (may be empty on ports without one). */
    char reason[128]  = {0};
    uint32_t uptime   = 0;
    nff_port_nvs_get_str("crash_reason",  reason,  sizeof(reason));
    nff_port_nvs_get_u32("crash_uptime", &uptime);

    /* On-device fault detail from the coredump-to-flash summary (esp_core_dump_get_summary).
       Available even on the Arduino port because it's read at boot, not in panic context. */
    nff_crash_hw_info_t hw;
    nff_port_get_crash_info(&hw);

    /* If the panic hook left no reason, synthesise one from the fault detail. */
    if (reason[0] == '\0' && hw.valid) {
        snprintf(reason, sizeof(reason), "cause=%d pc=0x%08lx",
                 hw.exception_cause, (unsigned long)hw.pc);
    }

    /* Build the backtrace JSON array (["0x...","0x..."], or []). */
    char bt_json[NFF_CRASH_BT_MAX * 13 + 4];
    int bp = 0;
    bp += snprintf(bt_json + bp, sizeof(bt_json) - (size_t)bp, "[");
    for (uint8_t i = 0; hw.valid && i < hw.backtrace_len && i < NFF_CRASH_BT_MAX; i++) {
        bp += snprintf(bt_json + bp, sizeof(bt_json) - (size_t)bp,
                       "%s\"0x%08lx\"", (i == 0) ? "" : ",",
                       (unsigned long)hw.backtrace[i]);
    }
    snprintf(bt_json + bp, sizeof(bt_json) - (size_t)bp, "]");

    /* Build the rtc_log JSON array as objects [{"message":"..."}, ...] so nff-fleet's
       db.ingest_crash populates rtc_log_lines (it reads entry.get("message")). */
    static char log_json[NFF_LOG_LINES * (NFF_LOG_LINE_LEN + 16)];
    int pos = 0;
    pos += snprintf(log_json + pos, sizeof(log_json) - (size_t)pos, "[");
    uint32_t total = (s_rtc_head < NFF_LOG_LINES) ? s_rtc_head : NFF_LOG_LINES;
    uint32_t start = (s_rtc_head >= NFF_LOG_LINES) ? s_rtc_head - NFF_LOG_LINES : 0;
    bool first = true;
    for (uint32_t i = 0; i < total && pos < (int)sizeof(log_json) - 32; i++) {
        uint32_t slot = (start + i) % NFF_LOG_LINES;
        if (s_rtc_log[slot][0] == '\0') continue;
        pos += snprintf(log_json + pos, sizeof(log_json) - (size_t)pos,
                        "%s{\"message\":\"%s\"}",
                        first ? "" : ",",
                        s_rtc_log[slot]);
        first = false;
    }
    pos += snprintf(log_json + pos, sizeof(log_json) - (size_t)pos, "]");

    /* Assemble crash report JSON. Field names match nff-fleet's db.ingest_crash and nff-mock so
       crash_reports + backtrace_frames + rtc_log_lines all populate; fw/build kept for any
       legacy consumer of the retained payload. exception_cause is null when unknown. */
    static char report[2560];
    char cause_buf[16];
    if (hw.valid && hw.exception_cause >= 0)
        snprintf(cause_buf, sizeof(cause_buf), "%d", hw.exception_cause);
    else
        snprintf(cause_buf, sizeof(cause_buf), "null");

    snprintf(report, sizeof(report),
             "{\"type\":\"crash\","
             "\"id\":\"%s\","
             "\"fw\":\"%s\","
             "\"fw_version\":\"%s\","
             "\"build\":\"%s\","
             "\"build_id\":\"%s\","
             "\"reason\":\"%s\","
             "\"exception_cause\":%s,"
             "\"pc\":\"0x%08lx\","
             "\"fault_addr\":\"0x%08lx\","
             "\"task_name\":\"%s\","
             "\"crash_count\":%lu,"
             "\"uptime_ms\":%lu,"
             "\"backtrace\":%s,"
             "\"rtc_log\":%s}",
             g_nff.cfg->device_id,
             g_nff.cfg->fw_version ? g_nff.cfg->fw_version : "",
             g_nff.cfg->fw_version ? g_nff.cfg->fw_version : "",
             g_nff.cfg->build_id   ? g_nff.cfg->build_id   : "",
             g_nff.cfg->build_id   ? g_nff.cfg->build_id   : "",
             reason,
             cause_buf,
             (unsigned long)(hw.valid ? hw.pc : 0),
             (unsigned long)(hw.valid ? hw.fault_addr : 0),
             hw.valid ? hw.task_name : "",
             (unsigned long)count,
             (unsigned long)uptime,
             bt_json,
             log_json);

    /* Publish retained QoS-1 — broker holds this indefinitely */
    char topic[NFF_TOPIC_MAXLEN];
    nff_topic_crash(&g_nff, topic);
    nff_port_mqtt_publish(g_nff.mqtt, topic, report, 1, true);

    /* Erase the stored fault image + NVS crash keys so the next clean boot doesn't re-report */
    nff_port_crash_info_clear();
    nff_port_nvs_erase_key("crash_reason");
    nff_port_nvs_erase_key("crash_uptime");
    nff_port_nvs_erase_key("crash_simulate");
    /* Keep crash_count — cumulative counter across all boots */
    nff_port_nvs_commit();

    /* Reset RTC log head so next boot starts fresh */
    s_rtc_head = 0;
}
