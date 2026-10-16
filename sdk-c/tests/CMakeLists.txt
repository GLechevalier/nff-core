cmake_minimum_required(VERSION 3.16)
project(nff_tests C)

set(CMAKE_C_STANDARD 99)

# Force POSIX port
add_compile_definitions(NFF_PORT_POSIX=1)

# SDK source files (excluding platform-specific ports)
set(NFF_SRCS
    ../src/nff_core.c
    ../src/nff_mqtt.c
    ../src/nff_heartbeat.c
    ../src/nff_cmd.c
    ../src/nff_ota.c
    ../src/nff_crash.c
    ../src/nff_diag.c
    ../src/nff_security.c
    ../src/nff_store.c
    ../src/nff_claim.c
    ../src/port/nff_port_posix.c
)

include_directories(../include ../src)

# Try to find OpenSSL for ECDSA verify in POSIX port
find_package(OpenSSL QUIET)
if(OpenSSL_FOUND)
    set(CRYPTO_LIB OpenSSL::SSL OpenSSL::Crypto)
    add_compile_definitions(HAVE_OPENSSL=1)
else()
    set(CRYPTO_LIB "")
endif()

# Helper: build a test executable
function(add_nff_test name)
    add_executable(${name} ${name}.c ${NFF_SRCS})
    if(WIN32)
        target_link_libraries(${name} ${CRYPTO_LIB})
    else()
        target_link_libraries(${name} ${CRYPTO_LIB} pthread)
    endif()
    add_test(NAME ${name} COMMAND ${name})
endfunction()

enable_testing()

add_nff_test(test_nonce_ring)
add_nff_test(test_cmd_dispatch)
add_nff_test(test_ota_rollback)
add_nff_test(test_ota_resume)
# Keep the retry backoff tiny so the resume test runs fast (default is 2 s/attempt).
target_compile_definitions(test_ota_resume PRIVATE NFF_OTA_DL_RETRY_BACKOFF_MS=5)
add_nff_test(test_crash_report)

# Claim enrollment test builds the SAME sources WITH the bootstrap feature flag on.
add_nff_test(test_claim_rollover)
target_compile_definitions(test_claim_rollover PRIVATE NFF_BOOTSTRAP_ENABLED=1)
