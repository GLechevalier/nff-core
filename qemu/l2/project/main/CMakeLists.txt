# nff L2 demo app component. REQUIRES the nff SDK + the shared networking/NVS helper.
idf_component_register(
    SRCS "main.c"
    REQUIRES nff nff_l2_net nvs_flash
)

# L2-only build define. Enables the SDK IDF port's QEMU concessions (the hostname-check skip in
# nff_port_esp32_idf.c, gated by #ifdef NFF_QEMU) and any WiFi->openeth shim.
target_compile_definitions(${COMPONENT_LIB} PRIVATE NFF_QEMU=1)

# Firmware identity from the build server (nff-github build.py exports these env vars for an L2
# build_command; nff.h #undefs + stringifies the bare *_TOKEN). Bare tokens, no quotes — a hex
# build_id and a semver are each a valid preprocessing token. Absent (a bare demo build), nff.h /
# nvs_creds.c fall back to defaults.
if(DEFINED ENV{NFF_BUILD_ID})
    target_compile_definitions(${COMPONENT_LIB} PRIVATE NFF_BUILD_ID_TOKEN=$ENV{NFF_BUILD_ID})
endif()
if(DEFINED ENV{NFF_FW_VERSION})
    target_compile_definitions(${COMPONENT_LIB} PRIVATE NFF_FW_VERSION_TOKEN=$ENV{NFF_FW_VERSION})
endif()
