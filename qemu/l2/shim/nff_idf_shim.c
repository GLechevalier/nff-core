/**
 * nff_idf_shim.c — universal esp_wifi/openeth shim implementation (runtime QEMU/real dispatch).
 *
 * Every entry point branches on nff_l2_identity_present(): a supervisor-spliced NVS identity means
 * QEMU (openeth, no radio); its absence means real silicon, where the REAL esp_wifi/esp_netif/
 * nff_init are invoked. This file includes the shim header (for its own prototypes + the token
 * overrides), so it reaches the real functions as parenthesised identifiers — `(esp_wifi_init)(cfg)`
 * is a call to the genuine symbol, not the macro.
 *
 * QEMU path: nff_l2_idf_wifi_start() brings openeth up via the shared helper (nff_l2_openeth_up,
 * which also sets openeth as the default netif), so the customer's unchanged TLS/MQTT/HTTP traffic
 * flows over Ethernet, then synthesises the IP_EVENT_STA_GOT_IP the sketch's own handler waits for.
 */

#include "nff_idf_shim.h"

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "freertos/FreeRTOS.h"

#ifdef NFF_INJECT_FLEET_CA
/* Build-injected fleet root CA (generated main/nff_injected_ca.c, fetched from the TARGET fleet's
 * /internal/fleet-ca at build time by nff-github). Pipeline-built universal images previously fell
 * back to whatever FLEET_ROOT_CA_DER snapshot the customer source happened to bake — a DIFFERENT
 * fleet's root fails TLS verify (-0x2700) against this fleet's broker, so the OTA trial can never
 * connect and always rolls back. The injected anchor is env-correct by construction. */
extern const unsigned char nff_injected_fleet_ca[];
extern const unsigned int  nff_injected_fleet_ca_len;
#endif

esp_err_t nff_l2_idf_wifi_init(const wifi_init_config_t *cfg) {
    if (nff_l2_identity_present()) { (void)cfg; return ESP_OK; }   /* QEMU: no radio to init */
    /* esp_wifi_init takes a non-const pointer; the sketch's local is writable. */
    return (esp_wifi_init)((wifi_init_config_t *)cfg);
}

esp_err_t nff_l2_idf_wifi_set_mode(wifi_mode_t mode) {
    if (nff_l2_identity_present()) { (void)mode; return ESP_OK; }
    return (esp_wifi_set_mode)(mode);
}

esp_err_t nff_l2_idf_wifi_set_config(wifi_interface_t itf, wifi_config_t *conf) {
    if (nff_l2_identity_present()) { (void)itf; (void)conf; return ESP_OK; }
    return (esp_wifi_set_config)(itf, conf);
}

esp_err_t nff_l2_idf_wifi_connect(void) {
    if (nff_l2_identity_present()) return ESP_OK;
    return (esp_wifi_connect)();
}

esp_netif_t *nff_l2_idf_netif_sta(void) {
    if (nff_l2_identity_present()) return NULL;   /* QEMU: openeth creates its own netif */
    return (esp_netif_create_default_wifi_sta)();
}

esp_err_t nff_l2_idf_wifi_start(void) {
    if (!nff_l2_identity_present()) return (esp_wifi_start)();
    /* QEMU: bring up Ethernet (idempotent; blocks until DHCP), then post the STA got-IP the sketch
     * waits on. openeth itself raises IP_EVENT_ETH_GOT_IP (a different event), so the sketch's
     * IP_EVENT_STA_GOT_IP handler would never fire without this synthesised post. Data is NULL: the
     * demo firmware's handler only sets its event-group bit and does not dereference the payload. */
    nff_l2_openeth_up();
    return esp_event_post(IP_EVENT, IP_EVENT_STA_GOT_IP, NULL, 0, portMAX_DELAY);
}

int nff_l2_idf_init_from_nvs(nff_config_t *cfg) {
    if (!cfg) return NFF_ERR_INVALID_ARG;
    if (!nff_l2_identity_present()) {
        /* Real silicon: no spliced identity — the real nff_init() resolves the device's operational
         * creds from the nff claim store (claimed) or fails cleanly (unclaimed: this credential-less
         * universal image cannot bootstrap). The RTC/SNTP story is the platform's own. */
#ifdef NFF_INJECT_FLEET_CA
        /* Broker trust anchor: the build-injected CA WINS over whatever the app placed here (claim
         * store op_ca or its baked fallback). The image is deployed by the same stack that built
         * it, so the build-time fleet CA is correct for the broker it will dial — and a stale
         * op_ca / stale bake can never re-arm the -0x2700 rollback loop. CA rotation still works:
         * each new build re-fetches the live root. */
        cfg->ca_cert = (const uint8_t *)nff_injected_fleet_ca;
        cfg->ca_cert_len = nff_injected_fleet_ca_len;
        ESP_LOGI("nff_shim", "using build-injected fleet root CA (%u B)", nff_injected_fleet_ca_len);
#endif
        return (nff_init)(cfg);
    }
    /* QEMU: the clock has to be set BEFORE any TLS handshake — no RTC, so without it mbedTLS rejects
     * every broker cert as "not yet valid" (MBEDTLS_X509_BADCERT_FUTURE). The NVS loader also sets
     * cfg->tls_skip_cn_check (the runtime replacement for the old compile-time NFF_QEMU gate). */
    nff_l2_set_clock_from_nvs();
    if (nff_l2_load_config_from_nvs(cfg) != 0) return NFF_ERR_INVALID_ARG;
    return (nff_init)(cfg);   /* parenthesised: the real function, not the macro above */
}
