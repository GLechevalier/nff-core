/**
 * nff_l2_net.h — L2 (QEMU) networking + NVS-identity helpers.
 *
 * Shared by the pure-IDF nff L2 demo (project/main/main.c) and the WiFi->openeth shim
 * (l2/shim/nff_wifi_shim.cpp), so the openeth bring-up exists once. QEMU's esp32 machine emulates
 * no WiFi radio — only the OpenCores Ethernet MAC (openeth) — so the network comes up over Ethernet
 * and the firmware reads its per-device identity from NVS (spliced in at launch by the supervisor;
 * see nff-mock/qemu/nff_mock_l2/credentials.py for the key contract).
 */
#ifndef NFF_L2_NET_H
#define NFF_L2_NET_H

#include "nff.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Bring the network up over the OpenCores Ethernet MAC and make it the default lwIP netif (so all
 * socket/TLS/MQTT/OTA traffic routes through it unchanged). Blocks until a DHCP lease arrives.
 * Idempotent — safe to call from both the demo's app_main and the shim's WiFi.begin(). */
void nff_l2_openeth_up(void);

/* True once openeth has a DHCP lease (the shim maps this to WiFi.status() == WL_CONNECTED). */
bool nff_l2_net_is_up(void);

/* Copy the openeth interface IPv4 address into out (4 bytes, network order). Returns 0 on success. */
int nff_l2_net_local_ip(uint8_t out[4]);

/* Read the injected boot epoch from NVS (namespace "nff", key "boot_epoch") and settimeofday() it,
 * falling back to a compile-time floor. QEMU has no RTC, so without a sane clock mbedTLS rejects
 * every broker cert as "not yet valid" (MBEDTLS_X509_BADCERT_FUTURE). */
void nff_l2_set_clock_from_nvs(void);

/* Populate cfg from the per-device identity spliced into NVS (credentials.py key contract).
 * Cert/key/CA are stored as PEM and converted to DER here because nff_config_t takes DER; the
 * 65-byte command-verify point is read verbatim. Allocates buffers that live for the device
 * lifetime (firmware never frees them). Returns 0 on success, -1 if a mandatory key is missing. */
int nff_l2_load_config_from_nvs(nff_config_t *cfg);

#ifdef __cplusplus
}
#endif

#endif /* NFF_L2_NET_H */
