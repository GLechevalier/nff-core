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
# ESP-IDF only routes the compiler through ccache when THIS is set — the CCACHE_DIR/MAXSIZE above are
# otherwise inert (idf.py never consults ccache), which is why a warm /ccache volume showed 0 hits and
# every cold build still recompiled the whole arduino-esp32 core (~3100 TUs) from scratch (~15 min).
# With it enabled those become cache hits: cold rebuilds drop to ~2-3 min, and a warm build's forced
# per-build-id recompiles (the NFF_BUILD_ID token below changes every build) resolve as near-instant
# hits for every TU that doesn't actually reference the token. Overridable with IDF_CCACHE_ENABLE=0.
export IDF_CCACHE_ENABLE="${IDF_CCACHE_ENABLE:-1}"

# Write stdin to $1 only when the content differs, so an unchanged generated file keeps its old mtime
# and CMake/ninja don't treat it as touched (a `cat >` rewrites the mtime every run → a needless
# reconfigure / relink). Used for the project-level CMakeLists.txt + sdkconfig.defaults below.
_write_if_changed() {
  local dest="$1" tmp; tmp="$(mktemp)"
  cat > "$tmp"
  if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then rm -f "$tmp"; else mv "$tmp" "$dest"; fi
}

git config --global --add safe.directory /repo 2>/dev/null || true
# One-shot clean-rebuild escape hatch: wipe the persisted build state (e.g. after a toolchain bump,
# or a corrupt incremental dir). Harmless when PROJ is ephemeral. Set NFF_L2_BUILD_CLEAN=1 to force.
[ -n "$NFF_L2_BUILD_CLEAN" ] && rm -rf "$PROJ/build" "$PROJ/managed_components" "$PROJ/dependencies.lock"
mkdir -p "$PROJ"
# Refresh ONLY the sketch (main); $PROJ/build, managed_components (arduino-esp32) and the PubSubClient
# clone are deliberately left intact so a persistent PROJ rebuilds incrementally.
rm -rf "$PROJ/main"; mkdir -p "$PROJ/main" "$PROJ/components"

# ---- top-level project (NFF_QEMU project-wide so it reaches the nff SDK component) ----
_write_if_changed "$PROJ/CMakeLists.txt" <<'EOF'
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(nff_l2_arduino)
idf_build_set_property(COMPILE_DEFINITIONS "NFF_QEMU=1" APPEND)
EOF
# Firmware identity (NFF_BUILD_ID_TOKEN / NFF_FW_VERSION_TOKEN) is baked per-COMPONENT below (main +
# nff SDK + nff_l2_net), NOT project-wide. These tokens change every build (build_id = sha256(source)),
# so a project-wide COMPILE_DEFINITION would put a per-build value on all ~3100 TUs (arduino-esp32 core,
# IDF, mbedtls) and defeat ccache entirely — every object misses. Only the sketch, the nff SDK and
# nff_l2_net (nff.h #undefs the creds default and stringifies these bare tokens) actually reference
# them, so scoping the defines to those components lets the big unchanged core hit the ccache.
#
# nff_l2_net is NOT optional: components/nff_l2_net/nvs_creds.c is the ONLY place that assigns
# cfg->fw_version and cfg->build_id (from NFF_FW_VERSION / NFF_BUILD_ID), and those are what the SDK
# publishes in every heartbeat and crash report. Omitting it silently falls back to that file's
# "0.0.0-l2" / "" defaults, so the device reports a STATIC non-semver version forever: OTA still
# lands, but no version-anchored recovery check (migration 0080's semver_gt, the dashboard's
# demoBrokenActive) can ever observe it, and crash backtraces lose the build_id that joins them to
# the stored ELF.
#
# Emitted after each component's idf_component_register (needs ${COMPONENT_LIB}).
_fw_ident_lines() {  # emit the identity target_compile_definitions on stdout
  [ -n "$NFF_BUILD_ID" ] && \
    echo "target_compile_definitions(\${COMPONENT_LIB} PRIVATE \"NFF_BUILD_ID_TOKEN=$NFF_BUILD_ID\")"
  [ -n "$NFF_FW_VERSION" ] && \
    echo "target_compile_definitions(\${COMPONENT_LIB} PRIVATE \"NFF_FW_VERSION_TOKEN=$NFF_FW_VERSION\")"
  return 0   # `set -e`: a false [ -n ] test must not abort a token-less (bare demo) build
}
_fw_ident_defs() {  # $1 = path to a component CMakeLists.txt to append target_compile_definitions to
  _fw_ident_lines >> "$1"
}

_write_if_changed "$PROJ/sdkconfig.defaults" <<'EOF'
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
# cmp-guard: only recopy when it actually changed so its mtime (→ a reconfigure) stays put.
cmp -s "$PROJDIR/partitions.csv" "$PROJ/partitions.csv" 2>/dev/null || cp "$PROJDIR/partitions.csv" "$PROJ/partitions.csv"

# ---- main: the UNMODIFIED customer sketch (.ino -> .cpp) + its creds + the shim, force-included ----
cp "$SKETCH_DIR"/*.ino "$PROJ/main/$(basename "$SKETCH_DIR" | tr - _).cpp"
# A real-hardware sketch #includes the per-device credentials.h that `nff provision new-device`
# generated. On QEMU one image serves every mock, so nff_wifi_shim.h rewrites NFF_CONFIG/nff_init to
# read the identity from the spliced NVS partition instead — nothing references the NFF_*_DER symbols
# and a stub satisfies the #include. Copy a real one when the sketch ships it (it is then inert).
if [ -f "$SKETCH_DIR/credentials.h" ]; then
  cp "$SKETCH_DIR/credentials.h" "$PROJ/main/"
else
  _write_if_changed "$PROJ/main/credentials.h" <<'EOF'
/* stub — on L2 (QEMU) the device identity comes from NVS; see shim/nff_wifi_shim.h. */
EOF
fi
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

# ---- shared openeth + NVS-identity component (the shim calls nff_l2_openeth_up) ----
# diff-guard on the SOURCES only: leaving them untouched when unchanged keeps their mtimes and ninja
# skips recompiling openeth.c on a warm build. CMakeLists.txt is excluded because we regenerate it
# below with the per-build identity tokens, so it can never match the pristine source copy.
if ! diff -rq --exclude=CMakeLists.txt "$L2NET" "$PROJ/components/nff_l2_net" >/dev/null 2>&1; then
  rm -rf "$PROJ/components/nff_l2_net"; cp -r "$L2NET" "$PROJ/components/nff_l2_net"
fi
# nvs_creds.c assigns cfg->fw_version / cfg->build_id from these tokens (see _fw_ident_lines), so this
# component must get them too — otherwise every L2 heartbeats the "0.0.0-l2" fallback. Only the 2 TUs
# here recompile per build; the arduino/IDF core still ccache-hits. _write_if_changed keeps the mtime
# stable when the tokens are unchanged, so a re-run with the same build_id doesn't force a reconfigure.
{ cat "$L2NET/CMakeLists.txt"; _fw_ident_lines; } \
  | _write_if_changed "$PROJ/components/nff_l2_net/CMakeLists.txt"

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
# `idf.py set-target` has an implicit `fullclean` dependency, so running it unconditionally on a
# PERSISTENT $PROJ/build (the /bcache cache) is a footgun: if build/ exists but isn't a valid CMake
# build dir (a prior build was interrupted before CMake configured, or a toolchain bump corrupted it),
# fullclean *refuses* to delete it — "doesn't seem to be a CMake build directory. Refusing to
# automatically delete files" — and the whole compile dies before it starts. So only set-target when
# build/ hasn't already been configured for esp32; a valid warm cache skips it (no needless fullclean),
# and a missing/corrupt build/ (no CMakeCache.txt) falls through to a clean reconfigure via `idf.py
# build`. NFF_L2_BUILD_CLEAN=1 (above) still forces a from-scratch rebuild when you want one.
if [ ! -f "$PROJ/build/CMakeCache.txt" ] || \
   ! grep -q "^IDF_TARGET:STRING=esp32$" "$PROJ/build/CMakeCache.txt" 2>/dev/null; then
  rm -rf "$PROJ/build"   # drop any partial/corrupt dir so set-target's fullclean has nothing to refuse
  idf.py set-target esp32
fi
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
