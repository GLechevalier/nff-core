/**
 * nff_wifi_shim.cpp — WiFi -> openeth shim implementation (L2/QEMU only).
 *
 * begin() brings openeth up via the shared helper (nff_l2_openeth_up, which also sets openeth as the
 * default netif), so the customer's unchanged WiFiClientSecure/PubSubClient/HTTPClient traffic flows
 * over Ethernet. onEvent() registers a single IP_EVENT_ETH_GOT_IP handler that synthesizes the
 * ARDUINO_EVENT_WIFI_STA_GOT_IP event fleet sketches block on.
 *
 * NOTE: this file must NOT have the shim header force-included into itself (it defines, not consumes,
 * the WiFi macro) — the build force-includes the header only into the customer sketch component.
 */

#include "nff_wifi_shim.h"

#include "esp_event.h"
#include "esp_netif.h"
#include "esp_mac.h"
#include <cstdio>
#include <cstring>

NffWiFiShim nffWiFiShim;

/* C trampoline: openeth raised IP_EVENT_ETH_GOT_IP — repackage it as the Arduino STA GOT_IP event
 * and hand it to the sketch's registered handlers. */
static void nff_shim_got_ip(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)base;
    (void)id;
    auto *shim = static_cast<NffWiFiShim *>(arg);
    const ip_event_got_ip_t *ev = static_cast<const ip_event_got_ip_t *>(data);

    arduino_event_info_t info;
    std::memset(&info, 0, sizeof(info));
    std::memcpy(&info.got_ip, ev, sizeof(info.got_ip));   /* ip_event_got_ip_t */
    shim->deliverEvent(ARDUINO_EVENT_WIFI_STA_GOT_IP, info);
}

wl_status_t NffWiFiShim::begin(const char *ssid, const char *passphrase,
                               int32_t channel, const uint8_t *bssid, bool connect) {
    (void)ssid;
    (void)passphrase;
    (void)channel;
    (void)bssid;
    (void)connect;
    /* Bring up Ethernet (idempotent). nff_l2_openeth_up() blocks until DHCP, so by the time this
     * returns the link is usually already up — but the sketch's status()/onEvent() flow is honored
     * regardless. The real WiFi.begin() returns before connecting, so we return WL_DISCONNECTED. */
    nff_l2_openeth_up();
    return WL_DISCONNECTED;
}

wl_status_t NffWiFiShim::status() {
    return nff_l2_net_is_up() ? WL_CONNECTED : WL_IDLE_STATUS;
}

bool NffWiFiShim::disconnect(bool, bool) {
    return true;   /* no teardown in the mock; the link stays up for the device lifetime */
}

IPAddress NffWiFiShim::localIP() {
    uint8_t ip[4] = {0, 0, 0, 0};
    nff_l2_net_local_ip(ip);
    return IPAddress(ip[0], ip[1], ip[2], ip[3]);
}

String NffWiFiShim::macAddress() {
    /* The locally-administered MAC openeth.c assigns the emulated link. */
    char buf[18];
    std::snprintf(buf, sizeof(buf), "02:00:00:12:34:56");
    return String(buf);
}

void NffWiFiShim::onEvent(NffWiFiEventCb cb, arduino_event_id_t event) {
    (void)event;   /* the sketch typically filters inside its own callback; we deliver all events */
    handlers_.push_back(cb);
    if (!got_ip_handler_registered_) {
        got_ip_handler_registered_ = true;
        /* The default event loop is created by nff_l2_openeth_up(); register here so a sketch that
         * calls WiFi.onEvent() before WiFi.begin() still wires up. If begin() hasn't run yet the
         * loop may not exist — bring openeth up first to guarantee it. */
        nff_l2_openeth_up();
        esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_GOT_IP, &nff_shim_got_ip, this);
    }
}

void NffWiFiShim::deliverEvent(arduino_event_id_t id, const arduino_event_info_t &info) {
    for (auto &cb : handlers_) {
        if (cb) cb(id, info);
    }
}
