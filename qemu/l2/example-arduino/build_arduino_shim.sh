#!/usr/bin/env bash
# Build an UNMODIFIED customer Arduino sketch as an arduino-as-IDF-component image with the
# WiFi->openeth shim force-included — the L2 path for arbitrary customer firmware (nff-github's
# Stage-4 server build mirrors this). Runs inside an ESP-IDF container with internet.
#
# Usage (in the espressif/idf image, repo mounted at /repo, scratch at /out):
#   SKETCH_DIR=/repo/test-nff bash build_arduino_shim.sh
#
# STATUS (2026-07-01): fully LINKED green + validated live — espressif/idf:v5.3.1 (mbedtls 3.6.0) +
# arduino-esp32 ~3.1.0 (resolves to 3.1.3) builds test-nff/blink_nff.ino to merged.bin (1.24MB raw
# flash image) + firmware.elf (11.9MB, debug_info) and xtensa-esp-elf-addr2line resolves app_main /
# start_ssl_client on the ELF. The one real blocker was NOT an mbedtls-major mismatch (the earlier
# guess): arduino-esp32's NetworkClientSecure/ssl_client.cpp gates its ENTIRE body behind
# MBEDTLS_KEY_EXCHANGE_*_PSK_ENABLED, so without PSK ciphersuites enabled the file compiles to an
# empty object -> `undefined reference to start_ssl_client(...)`. Fixed by enabling PSK modes in
# sdkconfig.defaults below (CONFIG_MBEDTLS_PSK_MODES + CONFIG_MBEDTLS_KEY_EXCHANGE_PSK).
# Note: on the *dev* snapshot `espressif/idf:release-v5.3` arduino 3.1.x also hit driver API drift
# (adc_continuous_data_t, i2c_ll_slave_init) — use the TAGGED v5.3.1, not the release-v5.3 snapshot.
set -e
PROJ=${PROJ:-/out/ardproj}
# Where the collectable artifacts (merged.bin / app.bin / firmware.elf) are deposited for the caller's
# glob. Defaults to $PROJ (standalone). nff-github sets ARTIFACT_DIR=/work (the mounted output) while
# pointing PROJ at a PERSISTENT build-cache volume — so the heavy IDF build/ survives between builds
# (ninja rebuilds only the changed sketch + nff SDK, then relinks: minutes → tens of seconds) while the
# artifacts still land where compile-source collects them.
ARTIFACT_DIR=${ARTIFACT_DIR:-$PROJ}
SHIM=/repo/nff-mock/qemu/l2/shim
PROJDIR=/repo/nff-mock/qemu/l2/project
L2NET=$PROJDIR/components/nff_l2_net
SKETCH_DIR=${SKETCH_DIR:-/repo/test-nff}
ARDUINO_VER=${ARDUINO_VER:-"~3.1.0"}   # pin to the arduino-esp32 line matching your IDF's mbedtls

# ccache: the IDF image already wires ccache into the compiler launcher, but its cache lives in the
# container and is lost with `docker run --rm`, so every build recompiles the whole arduino-esp32 core
# from scratch (~15 min). Point CCACHE_DIR at /ccache — a persistent docker volume the caller mounts
# there (see nff-github _l2_build_config) — so warm rebuilds drop to ~2-3 min. Defaults are overridable
# via `-e CCACHE_DIR=… -e CCACHE_MAXSIZE=…`; with no /ccache mount it just caches to the ephemeral
# container FS (harmless, same as before).
export CCACHE_DIR="${CCACHE_DIR:-/ccache}"
export CCACHE_MAXSIZE="${CCACHE_MAXSIZE:-5G}"
mkdir -p "$CCACHE_DIR" 2>/dev/null || true

git config --global --add safe.directory /repo 2>/dev/null || true
# One-shot clean-rebuild escape hatch: wipe the persisted build state (e.g. after a toolchain bump,
# or a corrupt incremental dir). Harmless when PROJ is ephemeral. Set NFF_L2_BUILD_CLEAN=1 to force.
[ -n "$NFF_L2_BUILD_CLEAN" ] && rm -rf "$PROJ/build" "$PROJ/managed_components" "$PROJ/dependencies.lock"
mkdir -p "$PROJ"
# Refresh ONLY the sketch (main); $PROJ/build, managed_components (arduino-esp32) and the PubSubClient
# clone are deliberately left intact so a persistent PROJ rebuilds incrementally.
rm -rf "$PROJ/main"; mkdir -p "$PROJ/main" "$PROJ/components"

# ---- top-level project (NFF_QEMU project-wide so it reaches the nff SDK component) ----
cat > "$PROJ/CMakeLists.txt" <<'EOF'
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(nff_l2_arduino)
idf_build_set_property(COMPILE_DEFINITIONS "NFF_QEMU=1" APPEND)
EOF
# Firmware identity (NFF_BUILD_ID_TOKEN / NFF_FW_VERSION_TOKEN) is baked per-COMPONENT below (main +
# nff SDK), NOT project-wide. These tokens change every build (build_id = sha256(source)), so a
# project-wide COMPILE_DEFINITION would put a per-build value on all ~3100 TUs (arduino-esp32 core,
# IDF, mbedtls) and defeat ccache entirely — every object misses. Only the sketch and the nff SDK
# (nff.h #undefs the creds default and stringifies these bare tokens) actually reference them, so
# scoping the defines to those two components lets the big unchanged core hit the ccache across builds.
# Emitted after each component's idf_component_register (needs ${COMPONENT_LIB}). Without NFF_BUILD_ID
# the device's crash build_id won't match the stored ELF and backtraces won't resolve.
_fw_ident_defs() {  # $1 = path to a component CMakeLists.txt to append target_compile_definitions to
  [ -n "$NFF_BUILD_ID" ] && \
    echo "target_compile_definitions(\${COMPONENT_LIB} PRIVATE \"NFF_BUILD_ID_TOKEN=$NFF_BUILD_ID\")" >> "$1"
  [ -n "$NFF_FW_VERSION" ] && \
    echo "target_compile_definitions(\${COMPONENT_LIB} PRIVATE \"NFF_FW_VERSION_TOKEN=$NFF_FW_VERSION\")" >> "$1"
}

cat > "$PROJ/sdkconfig.defaults" <<'EOF'
CONFIG_IDF_TARGET="esp32"
CONFIG_ETH_USE_OPENETH=y
CONFIG_AUTOSTART_ARDUINO=y
CONFIG_FREERTOS_HZ=1000
CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y
CONFIG_ESPTOOLPY_FLASHSIZE="4MB"
CONFIG_PARTITION_TABLE_CUSTOM=y
CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"
CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH=y
CONFIG_ESP_COREDUMP_DATA_FORMAT_ELF=y
CONFIG_ESP_COREDUMP_CHECKSUM_CRC32=y
CONFIG_MBEDTLS_ECP_DP_SECP256R1_ENABLED=y
CONFIG_MBEDTLS_ECDSA_C=y
# arduino-esp32's NetworkClientSecure/ssl_client.cpp wraps its ENTIRE body (start_ssl_client,
# ssl_init, send/recv, ...) in `#if defined(MBEDTLS_KEY_EXCHANGE_*_PSK_ENABLED) #else #warning`.
# Without PSK key-exchange enabled the file compiles to an empty object and the arduino link fails
# with `undefined reference to start_ssl_client(...)` (from NetworkClientSecure.cpp). IDF defaults
# these to n, so enable them here. (These two menu titles are exactly the `#warning` text.)
CONFIG_MBEDTLS_PSK_MODES=y
CONFIG_MBEDTLS_KEY_EXCHANGE_PSK=y
# OTA bulk-download tuning for the emulated openeth NIC. A sustained 4 MB HTTPS OTA download stalls and
# the connection dies at ~3 KB with the IDF defaults (lwIP TCP receive window ~5.7 KB + a 6-slot RX
# mailbox + no out-of-seq queue) — the app (flash write + SHA) drains slower than openeth delivers, the
# window fills, and the transfer resets. Enlarge the receive window/mailbox and enable OOSEQ queuing so
# the guest can absorb the stream. (Send-side bumped too; harmless.)
CONFIG_LWIP_TCP_WND_DEFAULT=32768
CONFIG_LWIP_TCP_RECVMBOX_SIZE=32
CONFIG_LWIP_TCP_SND_BUF_DEFAULT=32768
CONFIG_LWIP_TCP_QUEUE_OOSEQ=y
CONFIG_LWIP_TCP_OOSEQ_MAX_PBUFS=16
CONFIG_LWIP_TCPIP_RECVMBOX_SIZE=64
CONFIG_ESP_INT_WDT=n
CONFIG_ESP_TASK_WDT_TIMEOUT_S=30
CONFIG_ESP_MAIN_TASK_STACK_SIZE=8192
# Set a debug watchpoint at the end of each task's stack so an overflow traps IMMEDIATELY with a
# clean "Stack canary watchpoint triggered" panic + coredump — instead of silently corrupting memory
# (a tight recursion overflows in ms, well before the task WDT, and without this leaves no crash
# report). This is what makes the stack_overflow fault-taxonomy scenario reportable on QEMU.
CONFIG_FREERTOS_WATCHPOINT_END_OF_STACK=y
EOF
cp "$PROJDIR/partitions.csv" "$PROJ/partitions.csv"

# ---- main: the UNMODIFIED customer sketch (.ino -> .cpp) + its creds + the shim, force-included ----
cp "$SKETCH_DIR"/*.ino "$PROJ/main/$(basename "$SKETCH_DIR" | tr - _).cpp"
[ -f "$SKETCH_DIR/credentials.h" ] && cp "$SKETCH_DIR/credentials.h" "$PROJ/main/"
cp "$SHIM/nff_wifi_shim.h" "$SHIM/nff_wifi_shim.cpp" "$PROJ/main/"
cat > "$PROJ/main/CMakeLists.txt" <<'EOF'
file(GLOB SK "*.cpp")
idf_component_register(SRCS ${SK} INCLUDE_DIRS "." REQUIRES espressif__arduino-esp32 nff nff_l2_net)
# Force-include the shim into the sketch TU so the customer's WiFi.* binds to the openeth shim.
target_compile_options(${COMPONENT_LIB} PRIVATE -include "${CMAKE_CURRENT_LIST_DIR}/nff_wifi_shim.h")
EOF
_fw_ident_defs "$PROJ/main/CMakeLists.txt"   # per-build build_id/fw_version, scoped for ccache
cat > "$PROJ/main/idf_component.yml" <<EOF
dependencies:
  idf: ">=5.1"
  espressif/arduino-esp32:
    version: "$ARDUINO_VER"
EOF

# ---- shared openeth component (the shim calls nff_l2_openeth_up) ----
rm -rf "$PROJ/components/nff_l2_net"; cp -r "$L2NET" "$PROJ/components/nff_l2_net"

# ---- nff SDK as an Arduino library (arduino port .cpp + IDF REQUIRES it needs) ----
rm -rf "$PROJ/components/nff"; mkdir -p "$PROJ/components/nff"
python /repo/nff-sdk-c/tools/sync_arduino_lib.py --dest "$PROJ/components/nff"
cat > "$PROJ/components/nff/CMakeLists.txt" <<'EOF'
file(GLOB NFF_SRC "src/*.c" "src/*.cpp")
idf_component_register(
  SRCS ${NFF_SRC}
  INCLUDE_DIRS "src" "."
  REQUIRES espressif__arduino-esp32 PubSubClient mbedtls app_update esp_timer espcoredump
           freertos esp_common esp_http_client esp_https_ota nvs_flash esp_wifi esp_partition heap)
# nff Arduino-library sources built as an IDF component: downgrade -Werror format checks the real
# arduino-cli build does not apply (not the spike's global -w).
target_compile_options(${COMPONENT_LIB} PRIVATE -Wno-error=format-truncation -Wno-error=format -Wno-error=format-nonliteral)
EOF
_fw_ident_defs "$PROJ/components/nff/CMakeLists.txt"   # per-build build_id/fw_version, scoped for ccache

# ---- PubSubClient (nff Arduino-port dependency) ----
if [ ! -f "$PROJ/components/PubSubClient/PubSubClient.cpp" ]; then
  mkdir -p "$PROJ/components/PubSubClient"; rm -rf /tmp/psc
  git clone --depth 1 https://github.com/knolleary/pubsubclient /tmp/psc
  cp /tmp/psc/src/PubSubClient.cpp /tmp/psc/src/PubSubClient.h "$PROJ/components/PubSubClient/"
  cat > "$PROJ/components/PubSubClient/CMakeLists.txt" <<'EOF'
idf_component_register(SRCS "PubSubClient.cpp" INCLUDE_DIRS "." REQUIRES espressif__arduino-esp32)
EOF
fi

cd "$PROJ"
. "$IDF_PATH/export.sh" >/dev/null 2>&1
idf.py set-target esp32
idf.py build

# Stage 4: produce the artifacts nff-github's compile-source route collects — a merged flash image
# for QEMU `-drive if=mtd`, and the debug ELF copied next to it (so _elf_for_bin finds it as a
# sibling).
#
# The image MUST be padded to the full 4 MB flash size (0xFF fill, the flash-erased state): the L2
# supervisor writes it verbatim to the QEMU mtd drive, so a short image (idf.py merge-bin's default
# is just bootloader+parts+app, ~1.2 MB) leaves the flash smaller than the partition table declares
# — the OTA (0x1F0000) and coredump (0x3C0000) partitions fall off the end, so the device won't boot
# and a panic can't write a coredump (→ no resolvable backtrace). `esptool merge_bin
# --fill-flash-size 4MB @flash_args` is the proven padding path (l2/project RUNBOOK Step-equivalent).
cd "$PROJ/build"
esptool.py --chip esp32 merge_bin --fill-flash-size 4MB -o "$PROJ/merged.bin" @flash_args
cd "$PROJ"
cp "$PROJ"/build/*.elf "$PROJ/firmware.elf"

# The OTA payload is the APP image ONLY (~1.1 MB), NOT the merged 4 MB flash: an OTA flashes the app
# partition (ota_1 = 0x1D0000 ≈ 1.9 MB), so pushing the merged image fails `Update.begin(): too large`.
# Copy out the app bin (the entry at flash offset 0x20000 in flash_args) as app.bin so nff-github's
# compile-source can return it separately (merged.bin stays the cold-boot QEMU image, app.bin is OTA).
app_bin=$(awk '$1=="0x20000"{print $2}' "$PROJ/build/flash_args")
cp "$PROJ/build/$app_bin" "$PROJ/app.bin"

# Deposit the collectable artifacts where the caller globs (ARTIFACT_DIR). When PROJ is a persistent
# build-cache volume this copies merged.bin/app.bin/firmware.elf back onto the /work mount while build/
# stays cached; a no-op when ARTIFACT_DIR == PROJ (standalone).
if [ "$ARTIFACT_DIR" != "$PROJ" ]; then
  mkdir -p "$ARTIFACT_DIR"
  cp "$PROJ/merged.bin" "$PROJ/app.bin" "$PROJ/firmware.elf" "$ARTIFACT_DIR/"
fi
echo "L2 artifacts: $ARTIFACT_DIR/merged.bin (4MB, cold-boot) + $ARTIFACT_DIR/app.bin (OTA) + $ARTIFACT_DIR/firmware.elf"
