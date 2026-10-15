cmake_minimum_required(VERSION 3.16)

# ESP-IDF component registration — place nff-sdk-c/ under components/nff/
# and list "nff" in REQUIRES in your own CMakeLists.txt.

if(DEFINED IDF_TARGET)
    # ---- ESP-IDF build -------------------------------------------------------
    if("${IDF_TARGET}" STREQUAL "esp32")
        set(NFF_PORT_FILE src/port/nff_port_esp32_idf.c)
    elseif("${IDF_TARGET}" STREQUAL "esp8266")
        set(NFF_PORT_FILE src/port/nff_port_esp8266_arduino.c)
    else()
        set(NFF_PORT_FILE src/port/nff_port_esp32_idf.c)
    endif()

    idf_component_register(
        SRCS
            src/nff_core.c
            src/nff_mqtt.c
            src/nff_heartbeat.c
            src/nff_cmd.c
            src/nff_ota.c
            src/nff_crash.c
            src/nff_diag.c
            src/nff_security.c
            src/nff_store.c
            src/nff_claim.c
            ${NFF_PORT_FILE}
        INCLUDE_DIRS
            include
            src          # for nff_internal.h and nff_json.h
        REQUIRES
            esp_timer
            freertos
            nvs_flash
            mqtt            # IDF 5.x renamed the "esp-mqtt" component to "mqtt"
            esp_wifi
            esp_http_client
            esp_https_ota
            app_update      # esp_ota_ops.h (OTA partition flip / rollback)
            espcoredump     # esp_core_dump.h (coredump-to-flash crash summary)
            mbedtls
    )

    # uint32_t's underlying type (unsigned int vs long unsigned int) varies by toolchain, so the
    # SDK's %u/%lu printf specifiers warn under IDF's -Werror. Suppress format warnings for THIS
    # component only (not a global -w): the eventual cleanup is a PRIxNN/<inttypes.h> sweep over src/.
    target_compile_options(${COMPONENT_LIB} PRIVATE -Wno-format)

else()
    # ---- Host / POSIX build (unit tests) -------------------------------------
    project(nff C)
    set(CMAKE_C_STANDARD 99)
    add_compile_definitions(NFF_PORT_POSIX=1)

    add_library(nff STATIC
        src/nff_core.c
        src/nff_mqtt.c
        src/nff_heartbeat.c
        src/nff_cmd.c
        src/nff_ota.c
        src/nff_crash.c
        src/nff_diag.c
        src/nff_security.c
        src/nff_store.c
        src/nff_claim.c
        src/port/nff_port_posix.c
    )
    target_include_directories(nff PUBLIC include src)

    find_package(OpenSSL QUIET)
    if(WIN32)
        if(OpenSSL_FOUND)
            target_link_libraries(nff OpenSSL::SSL OpenSSL::Crypto)
        endif()
    else()
        if(OpenSSL_FOUND)
            target_link_libraries(nff OpenSSL::SSL OpenSSL::Crypto pthread)
        else()
            target_link_libraries(nff pthread)
        endif()
    endif()

    # Build tests if this is the top-level project
    if(CMAKE_SOURCE_DIR STREQUAL PROJECT_SOURCE_DIR)
        add_subdirectory(tests)
    endif()

endif()
