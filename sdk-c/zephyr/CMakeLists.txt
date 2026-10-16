# Zephyr module CMakeLists.txt — V1 stub.
# All nff functions return NFF_ERR_NOT_SUPPORTED until the Zephyr port is implemented.
# This file allows nff-sdk-c to be included in Zephyr projects without breaking
# the build while the real Zephyr port (V2) is in development.

zephyr_library_named(nff)

zephyr_library_sources(
    ../src/nff_core.c
    ../src/nff_mqtt.c
    ../src/nff_heartbeat.c
    ../src/nff_cmd.c
    ../src/nff_ota.c
    ../src/nff_crash.c
    ../src/nff_diag.c
    ../src/nff_security.c
    # Zephyr port — stub only for V1
    nff_port_zephyr_stub.c
)

zephyr_library_include_directories(../include ../src)
