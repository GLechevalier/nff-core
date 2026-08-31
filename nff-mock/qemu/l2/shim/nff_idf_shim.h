/**
 * nff_idf_shim.h — UNIVERSAL esp_wifi/openeth shim for the native ESP-IDF L2 build.
 *
 * The native analogue of nff_wifi_shim.h (which shims arduino-esp32's WiFi object). QEMU's esp32
 * machine emulates no WiFi radio, only the OpenCores Ethernet MAC. This header is FORCE-INCLUDED
 * (clang/gcc `-include`) into a customer's ESP-IDF `main.c` for the L2 build, so an unmodified app
 * that stands a WiFi station up with the ordinary esp_wifi/esp_netif calls transparently works.
 *
 * ONE image now serves QEMU mocks AND real silicon: every shim entry point dispatches at RUNTIME on
 * nff_l2_identity_present() — true iff the QEMU supervisor spliced a per-device identity into NVS,
 * something that never exists on real hardware (see nff_l2_net.h for why namespace existence is NOT
 * the probe). QEMU → the historical openeth/NVS-identity behavior; real silicon → the genuine
 * esp_wifi/esp_netif/nff_init calls, reached as parenthesised identifiers so the macros below don't
 * recurse. Real WiFi bring-up uses the sketch's own (now genuine) WIFI_INIT_CONFIG_DEFAULT().
 *
 * Identity on real silicon: the shimmed NFF_CONFIG_INITIALIZER below carries scalars only (no DER
 * cert symbols — a stub credentials.h keeps compiling), so a real device can only authenticate from
 * the nff SDK's CLAIM STORE (nff_store.c: operational creds persisted in NVS at claim rollover,
 * surviving OTAs). An unclaimed real device cannot bootstrap from this image — same limitation the
 * QEMU-only image always had. The default broker host is NFF_SHIM_BROKER_HOST ("nff-fleet-server",
 * override with -D): it equals the fleet server cert's CN, so a WIRED (PPPoS/WiredLink) device —
 * whose router routes ANY :8883 flow to the fleet broker and resolves any hostname — passes GENUINE
 * TLS CN verification. Real WiFi-connected hardware still needs per-device provisioning; this image
 * targets QEMU mocks + wired claimed devices.
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
#include "nff_l2_net.h"    /* shared openeth bring-up + NVS identity + nff_l2_identity_present() */

#ifdef __cplusplus
extern "C" {
#endif

/* Stand-ins for the esp_wifi station bring-up. Each dispatches at runtime: QEMU (spliced identity
 * in NVS) → openeth/no-op behavior; real silicon → the real esp_wifi call. Declared here, defined
 * in nff_idf_shim.c (they must be real functions, not inlines, to call the real esp_wifi symbols
 * behind the macros). */
esp_err_t nff_l2_idf_wifi_init(const wifi_init_config_t *cfg);
esp_err_t nff_l2_idf_wifi_set_mode(wifi_mode_t mode);
esp_err_t nff_l2_idf_wifi_set_config(wifi_interface_t itf, wifi_config_t *conf);
esp_err_t nff_l2_idf_wifi_connect(void);
esp_err_t nff_l2_idf_wifi_start(void);
esp_netif_t *nff_l2_idf_netif_sta(void);

/* QEMU: set the clock (no RTC → TLS would reject every cert as "not yet valid"), load the spliced
 * NVS identity into cfg (which also flags the runtime TLS CN-check skip), then hand it to the real
 * nff_init(). Real silicon: straight to the real nff_init() — claimed devices load their
 * operational creds from the nff claim store. Stands in for the sketch's nff_init(). */
int nff_l2_idf_init_from_nvs(nff_config_t *cfg);

#ifdef __cplusplus
}
#endif

/* ---- identity: credentials.h -> NVS / claim store -----------------------------------------------
 *
 * A real-hardware app normally bakes its identity in via NFF_CONFIG_INITIALIZER, filled from the
 * per-device credentials.h that `nff provision new-device` generated. This universal image carries
 * NO baked credentials (credentials.h may be an empty stub): the initializer is rewritten to a
 * PARTIAL, scalar-only config. QEMU overwrites it from the spliced NVS identity; real claimed
 * silicon overlays the operational creds from the nff claim store (scalars inherited from this
 * base — see nff_store_load). Mirrors the scalar fields of the real initializer in nff.h.
 *
 * The sketch's g_cfg MUST keep static storage AND stay non-const: nff_init retains the pointer long
 * after app_main() returns, and the loaders write identity straight into it. */
#ifndef NFF_SHIM_BROKER_HOST
#define NFF_SHIM_BROKER_HOST "nff-fleet-server"   /* == fleet server cert CN (see module doc) */
#endif
#undef  NFF_CONFIG_INITIALIZER
#define NFF_CONFIG_INITIALIZER(id, host) {                      \
    .device_id            = (id),   /* overridden: hwid wins */ \
    .project_id           = "",     /* claimed store overrides; NEVER NULL (topic %s) */ \
    .broker_host          = NFF_SHIM_BROKER_HOST,               \
    .broker_port          = 8883,                               \
    .fw_version           = NFF_FW_VERSION,                     \
    .build_id             = NFF_BUILD_ID,                       \
    .fqbn                 = NFF_FQBN,                           \
    .heartbeat_interval_s = 30,                                 \
}

/* The sketch's nff_init(&g_cfg) lands in the dispatcher. nff_idf_shim.c reaches the real function
 * as `(nff_init)(cfg)` — a parenthesised identifier is not a macro invocation. */
#define nff_init(cfg) nff_l2_idf_init_from_nvs(cfg)

/* ---- networking: esp_wifi station -> runtime dispatch -------------------------------------------
 *
 * Rewrite the handful of esp_wifi / esp_netif entry points an ordinary station bring-up calls so
 * the unmodified app works on QEMU's emulated Ethernet MAC yet still drives the real radio on real
 * silicon. WIFI_INIT_CONFIG_DEFAULT() is left REAL (the genuine config is required on real
 * hardware; the QEMU path simply never uses it). esp_netif_init / esp_event_loop_create_default /
 * the sketch's esp_event_handler_instance_register are deliberately LEFT REAL in both modes
 * (nff_l2_openeth_up tolerates a pre-existing netif/loop, and the posted STA got-IP must reach the
 * sketch's genuinely-registered handler). */
#define esp_wifi_init(cfg)                   nff_l2_idf_wifi_init((cfg))
#define esp_wifi_set_mode(mode)              nff_l2_idf_wifi_set_mode((mode))
#define esp_wifi_set_config(itf, conf)       nff_l2_idf_wifi_set_config((itf), (conf))
#define esp_wifi_connect()                   nff_l2_idf_wifi_connect()
#define esp_wifi_start()                     nff_l2_idf_wifi_start()
#define esp_netif_create_default_wifi_sta()  nff_l2_idf_netif_sta()

#endif /* NFF_IDF_SHIM_H */
