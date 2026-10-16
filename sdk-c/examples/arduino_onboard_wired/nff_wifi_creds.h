// nff_wifi_creds.h — persistent WiFi credentials in NVS, shared cred-resolution order.
//
// Resolution order everywhere (wired image, WiFi image, injected sidecar):
//   1. NVS  (namespace "nff", keys "wifi_ssid"/"wifi_pass") — written by the cut-the-cord
//      handoff or persisted from slots after a first successful connect
//   2. byte-patched slots (credentials.h WIFI_SSID/WIFI_PASS)
//   3. none -> caller falls back to the wired (PPPoS) path
//
// Uses the SDK's NVS port layer (nff_port.h) so storage matches nff's own "nff" namespace.

#pragma once
#include <stddef.h>
#include <string.h>

extern "C" {
  int nff_port_nvs_set_str(const char *key, const char *value);
  int nff_port_nvs_get_str(const char *key, char *out, size_t out_len);
}

#define NFF_WIFI_SSID_MAX 33   /* 32 + NUL */
#define NFF_WIFI_PASS_MAX 65   /* 64 + NUL */

// Load persisted creds. Returns true if an SSID is present (password may be empty — open AP).
static inline bool nff_wifi_creds_load(char *ssid, size_t ssid_len, char *pass, size_t pass_len) {
    if (nff_port_nvs_get_str("wifi_ssid", ssid, ssid_len) != 0 || ssid[0] == '\0') return false;
    if (nff_port_nvs_get_str("wifi_pass", pass, pass_len) != 0) pass[0] = '\0';
    return true;
}

// Persist creds (handoff over serial, or first successful slot-cred connect).
static inline void nff_wifi_creds_store(const char *ssid, const char *pass) {
    nff_port_nvs_set_str("wifi_ssid", ssid);
    nff_port_nvs_set_str("wifi_pass", pass ? pass : "");
}
