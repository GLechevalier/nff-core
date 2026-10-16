/**
 * nff_diag.c — Diagnostics command handler (V1: basic system info).
 *
 * Handles the "diag" command: collects heap stats, uptime, WiFi RSSI, and
 * CPU count via nff_port_get_diag_info(), then publishes a JSON response.
 *
 * V2 will add FreeRTOS task-list snapshot and Core-1 stall for consistent reads.
 */

#include "nff_internal.h"
#include <stdio.h>

void nff_diag_handle_cmd(const char *payload, char *resp, size_t resp_len) {
    (void)payload;

    nff_diag_info_t di;
    nff_port_get_diag_info(&di);

    snprintf(resp, resp_len,
             "{\"type\":\"diag\","
             "\"id\":\"%s\","
             "\"fw\":\"%s\","
             "\"free_heap\":%lu,"
             "\"min_free_heap\":%lu,"
             "\"uptime_ms\":%lu,"
             "\"wifi_rssi\":%ld,"
             "\"cpu_count\":%u}",
             g_nff.cfg->device_id,
             g_nff.cfg->fw_version ? g_nff.cfg->fw_version : "",
             (unsigned long)di.free_heap,
             (unsigned long)di.min_free_heap,
             (unsigned long)di.uptime_ms,
             (long)di.wifi_rssi,
             (unsigned)di.cpu_count);
}
