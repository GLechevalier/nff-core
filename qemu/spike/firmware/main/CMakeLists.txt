# Spike app component. REQUIRES the nff component (resolved via EXTRA_COMPONENT_DIRS in the
# top-level CMakeLists.txt) plus the IDF networking/eth/nvs pieces the spike's main.c touches.
idf_component_register(
    SRCS "main.c"
    REQUIRES nff esp_eth esp_netif esp_event nvs_flash
)
