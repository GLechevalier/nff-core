/**
 * openeth.c — bring the network up over QEMU's OpenCores Ethernet MAC.
 *
 * Lifted from the proven Stage-0 spike (nff-mock/qemu/spike/firmware/main/main.c::openeth_connect),
 * factored into a reusable, idempotent helper so both the pure-IDF L2 demo and the WiFi->openeth
 * shim share one bring-up. The DP83848 PHY + short autoneg timeout + default-netif steps mirror
 * IDF's own QEMU path (protocol_examples_common/eth_connect.c).
 */

#include "nff_l2_net.h"

#include "esp_log.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_eth.h"
#include "esp_mac.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include <string.h>

static const char *TAG = "nff_l2_net";

#define ETH_GOT_IP_BIT BIT0

static EventGroupHandle_t s_eth_events;
static esp_netif_t       *s_eth_netif;
static bool               s_started;

static void eth_event_handler(void *arg, esp_event_base_t base,
                              int32_t event_id, void *data) {
    (void)arg;
    if (base == IP_EVENT && event_id == IP_EVENT_ETH_GOT_IP) {
        ip_event_got_ip_t *ev = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "openeth GOT_IP: " IPSTR, IP2STR(&ev->ip_info.ip));
        xEventGroupSetBits(s_eth_events, ETH_GOT_IP_BIT);
    } else if (base == ETH_EVENT && event_id == ETHERNET_EVENT_CONNECTED) {
        ESP_LOGI(TAG, "openeth link up");
    }
}

void nff_l2_openeth_up(void) {
    if (s_started) {
        /* Already up (or coming up): just (re)wait for the lease so callers can treat this as
         * "ensure the network is ready". */
        if (s_eth_events) {
            xEventGroupWaitBits(s_eth_events, ETH_GOT_IP_BIT, false, true, portMAX_DELAY);
        }
        return;
    }
    s_started = true;
    s_eth_events = xEventGroupCreate();

    /* Tolerate a netif/event loop already brought up by the host framework (e.g. arduino-esp32 in
     * the shim path) — re-init returns ESP_ERR_INVALID_STATE, which is not an error for us. */
    esp_err_t e = esp_netif_init();
    if (e != ESP_OK && e != ESP_ERR_INVALID_STATE) ESP_ERROR_CHECK(e);
    e = esp_event_loop_create_default();
    if (e != ESP_OK && e != ESP_ERR_INVALID_STATE) ESP_ERROR_CHECK(e);

    esp_netif_config_t netif_cfg = ESP_NETIF_DEFAULT_ETH();
    s_eth_netif = esp_netif_new(&netif_cfg);

    /* QEMU's esp32 machine emulates the OpenCores Ethernet MAC (openeth), selected with
     * CONFIG_ETH_USE_OPENETH=y; pair it with the DP83848 PHY model and a short autoneg timeout so
     * PHY bring-up doesn't stall under emulation. */
    eth_mac_config_t mac_cfg = ETH_MAC_DEFAULT_CONFIG();
    eth_phy_config_t phy_cfg = ETH_PHY_DEFAULT_CONFIG();
    phy_cfg.autonego_timeout_ms = 100;

    esp_eth_mac_t *mac = esp_eth_mac_new_openeth(&mac_cfg);
    esp_eth_phy_t *phy = esp_eth_phy_new_dp83848(&phy_cfg);

    esp_eth_config_t eth_cfg = ETH_DEFAULT_CONFIG(mac, phy);
    esp_eth_handle_t eth_handle = NULL;
    ESP_ERROR_CHECK(esp_eth_driver_install(&eth_cfg, &eth_handle));

    /* A locally-administered MAC so the emulated link has a stable address. */
    uint8_t mac_addr[6] = {0x02, 0x00, 0x00, 0x12, 0x34, 0x56};
    esp_eth_ioctl(eth_handle, ETH_CMD_S_MAC_ADDR, mac_addr);

    ESP_ERROR_CHECK(esp_netif_attach(s_eth_netif, esp_eth_new_netif_glue(eth_handle)));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_GOT_IP,
                                               &eth_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(ETH_EVENT, ESP_EVENT_ANY_ID,
                                               &eth_event_handler, NULL));
    ESP_ERROR_CHECK(esp_eth_start(eth_handle));

    /* lwIP routes sockets/TLS/MQTT over the DEFAULT netif — make it openeth so all of
     * WiFiClientSecure/PubSubClient/HTTPClient traffic flows through the emulated NIC unchanged. */
    ESP_ERROR_CHECK(esp_netif_set_default_netif(s_eth_netif));

    ESP_LOGI(TAG, "waiting for openeth DHCP lease...");
    xEventGroupWaitBits(s_eth_events, ETH_GOT_IP_BIT, false, true, portMAX_DELAY);
    ESP_LOGI(TAG, "openeth connected");
}

bool nff_l2_net_is_up(void) {
    return s_eth_events &&
           (xEventGroupGetBits(s_eth_events) & ETH_GOT_IP_BIT) != 0;
}

int nff_l2_net_local_ip(uint8_t out[4]) {
    esp_netif_ip_info_t ip;
    if (!s_eth_netif || esp_netif_get_ip_info(s_eth_netif, &ip) != ESP_OK) return -1;
    memcpy(out, &ip.ip.addr, 4);   /* esp_ip4_addr is network-order */
    return 0;
}
