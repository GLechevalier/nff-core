/**
 * nff_idf_shim.c — native ESP-IDF esp_wifi -> openeth shim implementation (L2/QEMU only).
 *
 * nff_l2_idf_wifi_start() brings openeth up via the shared helper (nff_l2_openeth_up, which also sets
 * openeth as the default netif), so the customer's unchanged TLS/MQTT/HTTP traffic flows over
 * Ethernet, then synthesises the IP_EVENT_STA_GOT_IP the sketch's own handler is waiting for.
 *
 * NOTE: this file DOES include the shim header (for its own prototypes + the token overrides), so it
 * reaches the real nff_init as `(nff_init)(cfg)`. It defines the shim entry points the header
 * declares; it does not call any of the shimmed esp_wifi_* tokens.
 */

#include "nff_idf_shim.h"

#include "esp_event.h"
#include "esp_netif.h"
#include "freertos/FreeRTOS.h"

esp_err_t nff_l2_idf_wifi_start(void) {
    /* Bring up Ethernet (idempotent; blocks until DHCP), then post the STA got-IP the sketch waits on.
     * openeth itself raises IP_EVENT_ETH_GOT_IP (a different event), so the sketch's
     * IP_EVENT_STA_GOT_IP handler would never fire without this synthesised post. Data is NULL: the
     * demo firmware's handler only sets its event-group bit and does not dereference the payload. */
    nff_l2_openeth_up();
    return esp_event_post(IP_EVENT, IP_EVENT_STA_GOT_IP, NULL, 0, portMAX_DELAY);
}

int nff_l2_idf_init_from_nvs(nff_config_t *cfg) {
    if (!cfg) return NFF_ERR_INVALID_ARG;
    /* The clock has to be set BEFORE any TLS handshake: QEMU has no RTC, so without it mbedTLS rejects
     * every broker cert as "not yet valid" (MBEDTLS_X509_BADCERT_FUTURE). On real hardware this line
     * doesn't exist, which is exactly why it lives in the shim and not in the firmware. */
    nff_l2_set_clock_from_nvs();
    if (nff_l2_load_config_from_nvs(cfg) != 0) return NFF_ERR_INVALID_ARG;
    return (nff_init)(cfg);   /* parenthesised: the real function, not the macro above */
}
