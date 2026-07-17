/**
 * nff_idf_shim.h — transparent esp_wifi -> openeth shim for the native ESP-IDF L2 (QEMU) build ONLY.
 *
 * The native analogue of nff_wifi_shim.h (which shims arduino-esp32's WiFi object). QEMU's esp32
 * machine emulates no WiFi radio, only the OpenCores Ethernet MAC. This header is FORCE-INCLUDED
 * (clang/gcc `-include`) into a customer's ESP-IDF `main.c` for the L2 build, so an unmodified app
 * that stands a WiFi station up with the ordinary esp_wifi/esp_netif calls transparently drives
 * openeth instead. It is NOT included in a normal build for real hardware, where esp_wifi resolves to
 * the real radio driver.
 *
 * Only the thin connection-management surface is rewritten: the data path (TLS / MQTT / HTTP over
 * lwIP) already works over whatever the default netif is, and nff_l2_openeth_up() makes openeth the
 * default. The event-loop / netif init and the sketch's own event-handler registration are left REAL
 * — nff_l2_openeth_up() is idempotent, and nff_l2_idf_wifi_start() posts an IP_EVENT_STA_GOT_IP
 * through the same default loop so the sketch's own got-IP handler fires and its wait unblocks.
 *
 * IMPORTANT ordering (mirrors nff_wifi_shim.h): include the REAL esp_wifi/esp_netif headers FIRST so
 * their types + prototypes are parsed under their include guards; THEN declare the shim entry points;
 * THEN `#define` the esp_wifi_* / NFF_CONFIG_INITIALIZER tokens, so only the customer's LATER uses of
 * those tokens are rewritten.
 */
#ifndef NFF_IDF_SHIM_H
#define NFF_IDF_SHIM_H

#include "esp_err.h"
#include "esp_wifi.h"      /* real esp_wifi types/prototypes, parsed before the macros below */
#include "esp_event.h"
#include "esp_netif.h"

#include "nff.h"
#include "nff_l2_net.h"    /* shared openeth bring-up + NVS identity (same as the pure-IDF demo) */

#ifdef __cplusplus
extern "C" {
#endif

/* Bring openeth up (idempotent; blocks until the DHCP lease) then post an IP_EVENT_STA_GOT_IP so the
 * sketch's own STA got-IP handler fires and its event-group wait unblocks. Stands in for
 * esp_wifi_start(); returns ESP_OK. */
esp_err_t nff_l2_idf_wifi_start(void);

/* No-op stand-ins for the radio-config calls (QEMU has no WiFi radio). Real inline FUNCTIONS, not
 * bare-expression macros: IDF builds with -Werror=unused-value, and a discarded function-call return
 * is fine where `(ESP_OK);` as a statement is "no effect". They also consume the arg the customer
 * passes, so their locals (init/wifi config) don't become -Wunused-variable. */
static inline esp_err_t nff_l2_idf_wifi_init(const wifi_init_config_t *cfg) { (void)cfg; return ESP_OK; }
static inline esp_err_t nff_l2_idf_wifi_set_mode(wifi_mode_t mode) { (void)mode; return ESP_OK; }
static inline esp_err_t nff_l2_idf_wifi_set_config(wifi_interface_t itf, wifi_config_t *conf) {
    (void)itf; (void)conf; return ESP_OK;
}
static inline esp_err_t nff_l2_idf_wifi_connect(void) { return ESP_OK; }
static inline esp_netif_t *nff_l2_idf_netif_sta(void) { return NULL; }

/* Set the clock (QEMU has no RTC, so TLS would reject every cert as "not yet valid") + load the
 * spliced NVS identity into cfg, then hand it to the real nff_init(). Returns NFF_OK, or
 * NFF_ERR_INVALID_ARG when NVS carries no identity. Stands in for the sketch's nff_init(). */
int nff_l2_idf_init_from_nvs(nff_config_t *cfg);

#ifdef __cplusplus
}
#endif

/* ---- identity: credentials.h -> NVS -------------------------------------------------------------
 *
 * A real-hardware app bakes its identity in at compile time via NFF_CONFIG_INITIALIZER, filled from
 * the per-device credentials.h that `nff provision new-device` generated. On QEMU one image serves
 * every mock, and the supervisor splices each device's certs into the NVS partition at launch, so the
 * initializer is neutralised to {0} (nothing then references the NFF_*_DER symbols, and the sketch's
 * credentials.h may be an empty stub) and nff_init() is routed to the NVS loader above.
 *
 * The sketch's g_cfg MUST keep static storage AND stay non-const: nff_init retains the pointer long
 * after app_main() returns, and the loader writes the spliced identity straight into it. */
#undef  NFF_CONFIG_INITIALIZER
#define NFF_CONFIG_INITIALIZER(id, host) {0}

/* The sketch's nff_init(&g_cfg) lands in the NVS loader. nff_idf_shim.c reaches the real function as
 * `(nff_init)(cfg)` — a parenthesised identifier is not a macro invocation. */
#define nff_init(cfg) nff_l2_idf_init_from_nvs(cfg)

/* ---- networking: esp_wifi station -> openeth (QEMU has no WiFi radio) ---------------------------
 *
 * Rewrite the handful of esp_wifi / esp_netif entry points an ordinary station bring-up calls so the
 * unmodified app drives the emulated Ethernet MAC. WIFI_INIT_CONFIG_DEFAULT() is neutralised to {0}
 * so the sketch references no esp_wifi runtime symbol; esp_wifi_start() routes to the openeth helper.
 * esp_netif_init / esp_event_loop_create_default / the sketch's esp_event_handler_instance_register
 * are deliberately LEFT REAL (nff_l2_openeth_up tolerates a pre-existing netif/loop, and the posted
 * STA got-IP must reach the sketch's genuinely-registered handler). */
#undef  WIFI_INIT_CONFIG_DEFAULT
#define WIFI_INIT_CONFIG_DEFAULT() {0}
#define esp_wifi_init(cfg)                   nff_l2_idf_wifi_init((cfg))
#define esp_wifi_set_mode(mode)              nff_l2_idf_wifi_set_mode((mode))
#define esp_wifi_set_config(itf, conf)       nff_l2_idf_wifi_set_config((itf), (conf))
#define esp_wifi_connect()                   nff_l2_idf_wifi_connect()
#define esp_wifi_start()                     nff_l2_idf_wifi_start()
#define esp_netif_create_default_wifi_sta()  nff_l2_idf_netif_sta()

#endif /* NFF_IDF_SHIM_H */
