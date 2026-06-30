# Shared L2 networking + NVS-identity component. Used by the pure-IDF demo (project/main) and the
# WiFi->openeth shim. REQUIRES nff for nff_config_t, the eth/netif/event stack for openeth bring-up,
# and nvs_flash for the spliced identity (DER certs/key read verbatim as blobs).
idf_component_register(
    SRCS
        "openeth.c"
        "nvs_creds.c"
    INCLUDE_DIRS
        "include"
    REQUIRES
        nff
        esp_eth
        esp_netif
        esp_event
        nvs_flash
)
