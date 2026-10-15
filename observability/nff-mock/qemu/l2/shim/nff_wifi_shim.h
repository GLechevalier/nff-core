/**
 * nff_wifi_shim.h — transparent WiFi -> openeth shim for the L2 (QEMU) build ONLY.
 *
 * QEMU's esp32 machine emulates no WiFi radio, only the OpenCores Ethernet MAC. This header is
 * FORCE-INCLUDED (clang/gcc `-include`) into a customer's Arduino sketch component for the
 * arduino-as-IDF-component L2 build, so unmodified firmware that calls WiFi.begin()/status()/
 * localIP()/onEvent() transparently drives openeth instead. It is NOT included in a normal
 * arduino-cli build for real hardware, where <WiFi.h> resolves to the real radio driver.
 *
 * Only the thin connection-management API is shimmed: the data path (WiFiClientSecure / PubSubClient
 * / HTTPClient over lwIP) already works over whatever the default netif is, and nff_l2_openeth_up()
 * makes openeth the default. See nff-mock/qemu/l2/README.md for the (honest) transparency limits.
 *
 * IMPORTANT ordering (Risk 1): include the REAL <WiFi.h> FIRST so the genuine `extern WiFiClass
 * WiFi;` and all WiFi* types are parsed under their include guards; THEN declare the shim; THEN
 * `#define WiFi` so only the customer's later `WiFi.` token uses are rewritten to the shim.
 */
#ifndef NFF_WIFI_SHIM_H
#define NFF_WIFI_SHIM_H

#include <WiFi.h>          /* real WiFiClass + WiFi* types, parsed before the macro below */
#include <IPAddress.h>
#include <functional>
#include <vector>

#include "nff_l2_net.h"    /* shared openeth bring-up (same helper the pure-IDF demo uses) */

/* Arduino-esp32 event callback shape (std::function form). The exact onEvent() overloads vary by
 * core version; this covers the common case fleet sketches use. */
typedef std::function<void(arduino_event_id_t, arduino_event_info_t)> NffWiFiEventCb;

/**
 * NffWiFiShim — a minimal stand-in for arduino-esp32's WiFiClass that drives openeth. Implements the
 * handful of methods real fleet firmware actually calls; the rest are synthetic/no-op stubs.
 */
class NffWiFiShim {
public:
    /* --- connection management (the load-bearing surface) --- */
    wl_status_t begin(const char *ssid = nullptr, const char *passphrase = nullptr,
                      int32_t channel = 0, const uint8_t *bssid = nullptr, bool connect = true);
    wl_status_t status();
    bool disconnect(bool wifioff = false, bool eraseap = false);
    IPAddress localIP();

    /* Translate IP_EVENT_ETH_GOT_IP into ARDUINO_EVENT_WIFI_STA_GOT_IP for the sketch's handlers. */
    void onEvent(NffWiFiEventCb cb, arduino_event_id_t event = ARDUINO_EVENT_MAX);

    /* --- synthetic / no-op stubs (radio concepts QEMU has no analogue for) --- */
    int8_t  RSSI() { return -55; }
    String  macAddress();
    String  SSID() { return String(""); }
    int32_t channel() { return 1; }
    bool    setSleep(bool) { return true; }
    bool    mode(wifi_mode_t) { return true; }
    int16_t scanNetworks(bool = false, bool = false) { return 0; }

    /* Internal: invoke stored handlers with a synthesized arduino event (called from the openeth
     * GOT_IP handler). Public so the C event trampoline in the .cpp can reach it. */
    void deliverEvent(arduino_event_id_t id, const arduino_event_info_t &info);

private:
    std::vector<NffWiFiEventCb> handlers_;
    bool got_ip_handler_registered_ = false;
};

extern NffWiFiShim nffWiFiShim;

/* From here on, the customer's `WiFi.begin(...)` etc. bind to the shim. Other WiFi* tokens
 * (WiFiClass, WiFiClient, WiFiClientSecure, WiFiGeneric) are distinct identifiers and untouched. */
#define WiFi nffWiFiShim

/* ---- identity: credentials.h -> NVS -------------------------------------------------------
 *
 * A real-hardware sketch bakes its identity in at compile time: NFF_CONFIG() expands to a
 * nff_config_t literal filled from the per-device credentials.h that `nff provision new-device`
 * generated, and nff_init() takes that struct verbatim (it never reads NVS — see
 * nff-sdk-c/src/nff_core.c).
 *
 * On QEMU there is no per-device image: ONE build serves every mock, and the supervisor splices
 * each device's certs into the NVS partition at launch (nff-mock/qemu/nff_mock_l2/credentials.py).
 * So the two tokens that would bake in an identity are rewritten here to read it from NVS instead,
 * and the sketch — which must stay byte-identical to what you would flash on a real ESP32 — is
 * none the wiser.
 *
 * Because NFF_CONFIG no longer expands to the initializer, nothing references the NFF_*_DER symbols
 * and the sketch's credentials.h may be an empty stub (build_arduino_shim.sh writes one when the
 * sketch dir has none).
 *
 * `var` MUST keep static storage: nff_init() retains the pointer long after setup() returns, so a
 * stack local would dangle and corrupt the heartbeat-topic format. */
#undef NFF_CONFIG
#define NFF_CONFIG(var, id, host) static nff_config_t var

#ifdef __cplusplus
extern "C" {
#endif
/* Fill cfg from the spliced NVS partition (and set the clock, which QEMU has no RTC for), then hand
 * it to the real nff_init(). Returns NFF_OK, or NFF_ERR_INVALID_ARG when NVS carries no identity. */
int nff_l2_init_from_nvs(nff_config_t *cfg);
#ifdef __cplusplus
}
#endif

/* The sketch's `nff_init(&g_cfg)` routes through the wrapper above. nff_wifi_shim.cpp reaches the
 * real function as `(nff_init)(cfg)` — a parenthesised identifier is not a macro invocation. */
#define nff_init(cfg) nff_l2_init_from_nvs(cfg)

#endif /* NFF_WIFI_SHIM_H */
