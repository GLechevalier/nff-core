#!/usr/bin/env bash
# Build a customer NATIVE ESP-IDF firmware (app_main, not an Arduino sketch) as an L2 (QEMU) image,
# force-including the esp_wifi->openeth shim. The native counterpart of build_arduino_shim.sh:
# nff-github's server build (_l2_espidf_build_config) routes firmware whose entry point is app_main()
# here, and Arduino setup()/loop() sketches to build_arduino_shim.sh. Runs inside an ESP-IDF container.
#
# Usage (in the espressif/idf image, repo mounted read-only at /repo, per-request source at /work):
#   SKETCH_DIR=/work bash build_espidf.sh        # reads $SKETCH_DIR/main/main.c (+ optional main/credentials.h)
#
# Reuses the maintained pure-IDF L2 project (nff-mock/qemu/l2/project) as the build vehicle: same
# top-level CMake + sdkconfig + partitions + nff_l2_net component + nff SDK component, with the
# customer's main.c dropped into main/ and the native shim force-included. No arduino-esp32 /
# PubSubClient / PSK machinery — a native app needs none of it.
#
# Output artifacts are IDENTICAL to build_arduino_shim.sh so nff-github's compile-source route collects
# them unchanged: merged.bin (4 MB, cold-boot QEMU -drive), app.bin (OTA payload), firmware.elf (debug).
set -e
PROJ=${PROJ:-/out/idfproj}
# Where the collectable artifacts (merged.bin / app.bin / firmware.elf) are deposited for the caller's
# glob. Defaults to $PROJ (standalone). nff-github sets ARTIFACT_DIR=/work while pointing PROJ at a
# PERSISTENT build-cache volume, so the heavy IDF build/ survives between builds (ninja rebuilds only
# the changed main + nff SDK, then relinks) while the artifacts still land where compile-source globs.
ARTIFACT_DIR=${ARTIFACT_DIR:-$PROJ}
SHIM=/repo/nff-mock/qemu/l2/shim
PROJDIR=/repo/nff-mock/qemu/l2/project
L2NET=$PROJDIR/components/nff_l2_net
SKETCH_DIR=${SKETCH_DIR:-/work}
MAIN_SRC=$SKETCH_DIR/main/main.c

# ccache: point CCACHE_DIR at a persistent /ccache volume the caller mounts, and enable the IDF ccache
# launcher (idf.py only routes the compiler through ccache when IDF_CCACHE_ENABLE is set). Warm builds
# then reduce to seconds. Same mechanism build_arduino_shim.sh documents at length.
export CCACHE_DIR="${CCACHE_DIR:-/ccache}"
export CCACHE_MAXSIZE="${CCACHE_MAXSIZE:-5G}"
mkdir -p "$CCACHE_DIR" 2>/dev/null || true
export IDF_CCACHE_ENABLE="${IDF_CCACHE_ENABLE:-1}"

# Write stdin to $1 only when the content differs, so an unchanged generated file keeps its old mtime
# and CMake/ninja don't treat it as touched (a `cat >` rewrites the mtime every run → a needless
# reconfigure / relink).
_write_if_changed() {
  local dest="$1" tmp; tmp="$(mktemp)"
  cat > "$tmp"
  if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then rm -f "$tmp"; else mv "$tmp" "$dest"; fi
}

git config --global --add safe.directory /repo 2>/dev/null || true
# One-shot clean-rebuild escape hatch (e.g. after a toolchain bump or a corrupt incremental dir).
[ -n "$NFF_L2_BUILD_CLEAN" ] && rm -rf "$PROJ/build" "$PROJ/managed_components" "$PROJ/dependencies.lock"
mkdir -p "$PROJ" "$PROJ/components"

# Firmware identity (NFF_BUILD_ID_TOKEN / NFF_FW_VERSION_TOKEN) is baked per-COMPONENT (main +
# nff_l2_net), NOT project-wide: these tokens change every build (build_id = sha256(source)), so a
# project-wide COMPILE_DEFINITION would put a per-build value on every TU and defeat ccache. Only the
# customer app (NFF_FW_VERSION in nff_log) and nff_l2_net/nvs_creds.c (the ONLY place that assigns
# cfg->fw_version / cfg->build_id, which the SDK publishes in every heartbeat + crash report) actually
# reference them, so scoping the defines to those two components lets the unchanged core ccache-hit.
_fw_ident_lines() {  # emit the identity target_compile_definitions on stdout
  [ -n "$NFF_BUILD_ID" ] && \
    echo "target_compile_definitions(\${COMPONENT_LIB} PRIVATE \"NFF_BUILD_ID_TOKEN=$NFF_BUILD_ID\")"
  [ -n "$NFF_FW_VERSION" ] && \
    echo "target_compile_definitions(\${COMPONENT_LIB} PRIVATE \"NFF_FW_VERSION_TOKEN=$NFF_FW_VERSION\")"
  return 0   # `set -e`: a false [ -n ] test must not abort a token-less (bare demo) build
}
_fw_ident_defs() { _fw_ident_lines >> "$1"; }

# ---- top-level project ---------------------------------------------------------------------------
# NFF_QEMU is kept project-wide for BACKWARD COMPAT with older firmware sources that still guard on
# it. Nothing device-visible depends on it any more: the SDK's TLS CN-skip moved to a RUNTIME flag
# (nff_config_t.tls_skip_cn_check, set by the L2 NVS identity loader) and the demo firmware + shim
# dispatch QEMU-vs-real at runtime via nff_l2_identity_present() — one image serves both.
_write_if_changed "$PROJ/CMakeLists.txt" <<'EOF'
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(nff_l2_idf)
idf_build_set_property(COMPILE_DEFINITIONS "NFF_QEMU=1" APPEND)
# The universal image runs over a slow, flap-prone wired PPPoS link on real silicon: the OTA
# trial's default 60 s confirm window (nff.h) is too tight for the wired boot (WiFi-first
# fallback + PPP negotiation + mTLS-over-serial) to establish and hold MQTT before the deadline,
# so a perfectly good image gets rolled back with "health check timeout". Give the wired trial
# generous headroom. Harmless on QEMU L2 (device_type nff-qemu-esp32 simulate-adopts, never trials).
idf_build_set_property(COMPILE_DEFINITIONS "NFF_OTA_CONFIRM_TIMEOUT_S=180" APPEND)
EOF

# ---- sdkconfig.defaults: the maintained L2 project's config + OTA lwIP window bumps ----------------
# openeth (QEMU's only NIC), 4 MB flash + custom partitions, coredump-to-flash (resolvable backtrace),
# ECDSA P-256, watchdogs off (QEMU openeth trips the IWDT mid-TLS). The LWIP_* bumps are lifted from
# build_arduino_shim.sh: a sustained 4 MB HTTPS OTA over emulated openeth stalls with IDF defaults
# (tiny receive window / mailbox / no OOSEQ queue), so enlarge them. Stack-canary watchpoint so a
# task-stack overflow traps with a clean panic + coredump. PPP is enabled so the universal image's
# wired (PPPoS) fallback compiles — unused on QEMU, load-bearing on real wired hardware.
#
# ⚠ When these defaults CHANGE, the generated $PROJ/sdkconfig must be regenerated: IDF only seeds
# sdkconfig from the defaults file at generation time — an existing sdkconfig's explicit
# "# CONFIG_X is not set" lines beat a new default, so a changed default would otherwise silently
# never apply on the persistent build-cache volume. Hash-compare and delete on change (one-time
# full reconfigure; ccache keeps the rebuild short).
_defaults_hash_before=$(md5sum "$PROJ/sdkconfig.defaults" 2>/dev/null | cut -d' ' -f1)
_write_if_changed "$PROJ/sdkconfig.defaults" <<'EOF'
CONFIG_IDF_TARGET="esp32"
CONFIG_ETH_USE_OPENETH=y
CONFIG_ETH_OPENETH_DMA_RX_BUFFER_NUM=4
CONFIG_ETH_OPENETH_DMA_TX_BUFFER_NUM=1
CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y
CONFIG_ESPTOOLPY_FLASHSIZE="4MB"
# Flash mode/freq must MATCH the proven real-hardware image (DIO/80MHz — what the wired golden
# image's Arduino bootloader runs): the app header carries these and a mismatched header was the
# prime suspect when the universal image's first real-HW boot was rejected by the bootloader
# (which silently fell back to the old slot). QEMU's flash emulation is indifferent to them.
CONFIG_ESPTOOLPY_FLASHMODE_DIO=y
CONFIG_ESPTOOLPY_FLASHFREQ_80M=y
CONFIG_PARTITION_TABLE_CUSTOM=y
CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"
CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH=y
CONFIG_ESP_COREDUMP_DATA_FORMAT_ELF=y
CONFIG_ESP_COREDUMP_CHECKSUM_CRC32=y
CONFIG_MBEDTLS_ECP_DP_SECP256R1_ENABLED=y
CONFIG_MBEDTLS_ECDSA_C=y
CONFIG_ESP_INT_WDT=n
CONFIG_ESP_TASK_WDT_INIT=n
CONFIG_ESP_TASK_WDT_EN=n
CONFIG_ESP_MAIN_TASK_STACK_SIZE=8192
CONFIG_LOG_DEFAULT_LEVEL_INFO=y
CONFIG_LWIP_TCP_WND_DEFAULT=32768
CONFIG_LWIP_TCP_RECVMBOX_SIZE=32
CONFIG_LWIP_TCP_SND_BUF_DEFAULT=32768
CONFIG_LWIP_TCP_QUEUE_OOSEQ=y
CONFIG_LWIP_TCP_OOSEQ_MAX_PBUFS=16
CONFIG_LWIP_TCPIP_RECVMBOX_SIZE=64
CONFIG_FREERTOS_WATCHPOINT_END_OF_STACK=y
CONFIG_LWIP_PPP_SUPPORT=y
CONFIG_LWIP_PPP_NOTIFY_PHASE_SUPPORT=y
CONFIG_LWIP_PPP_PAP_SUPPORT=y
CONFIG_LWIP_PPP_VJ_HEADER_COMPRESSION=y
EOF
_defaults_hash_after=$(md5sum "$PROJ/sdkconfig.defaults" 2>/dev/null | cut -d' ' -f1)
if [ "$_defaults_hash_before" != "$_defaults_hash_after" ]; then
  echo "build_espidf.sh: sdkconfig.defaults changed — regenerating sdkconfig from defaults"
  rm -f "$PROJ/sdkconfig"
fi
# cmp-guard: only recopy when it actually changed so its mtime (→ a reconfigure) stays put.
cmp -s "$PROJDIR/partitions.csv" "$PROJ/partitions.csv" 2>/dev/null || cp "$PROJDIR/partitions.csv" "$PROJ/partitions.csv"

# ---- main: the customer app_main source + creds + the native shim, force-included ----
[ -f "$MAIN_SRC" ] || { echo "build_espidf.sh: no app source at $MAIN_SRC (expected main/main.c)"; exit 2; }
rm -rf "$PROJ/main"; mkdir -p "$PROJ/main"
cp "$MAIN_SRC" "$PROJ/main/main.c"
# A real-hardware app #includes the per-device credentials.h that `nff provision new-device` wrote. On
# QEMU one image serves every mock, so nff_idf_shim.h rewrites NFF_CONFIG_INITIALIZER/nff_init to read
# the identity from the spliced NVS partition — nothing references the NFF_*_DER symbols and a stub
# satisfies the #include. Copy a real one when the source ships it (it is then inert).
if [ -f "$SKETCH_DIR/main/credentials.h" ]; then
  cp "$SKETCH_DIR/main/credentials.h" "$PROJ/main/"
else
  _write_if_changed "$PROJ/main/credentials.h" <<'EOF'
/* stub — on L2 (QEMU) the device identity comes from NVS; see shim/nff_idf_shim.h. */
EOF
fi
cp "$SHIM/nff_idf_shim.h" "$SHIM/nff_idf_shim.c" "$PROJ/main/"
# Build-injected fleet root CA (see shim nff_l2_idf_init_from_nvs): nff-github fetches the TARGET
# fleet's root CA and ships it as main/nff_injected_ca.c so real-silicon boots trust the broker they
# will actually dial. Absent (fetch failed / standalone build) → a stub TU keeps the SRCS list static
# and the shim's #ifdef path dark — exactly today's behavior (baked-CA fallback), never a build break.
if [ -f "$SKETCH_DIR/main/nff_injected_ca.c" ]; then
  cp "$SKETCH_DIR/main/nff_injected_ca.c" "$PROJ/main/nff_injected_ca.c"
  NFF_CA_DEF='target_compile_definitions(${COMPONENT_LIB} PRIVATE "NFF_INJECT_FLEET_CA=1")'
  echo "build_espidf.sh: injecting fleet root CA (env-correct broker trust anchor)"
else
  _write_if_changed "$PROJ/main/nff_injected_ca.c" <<'EOF'
/* stub — no build-injected fleet CA; the app's own trust chain applies. */
typedef int nff_injected_ca_stub_t;
EOF
  NFF_CA_DEF=''
fi
cat > "$PROJ/main/CMakeLists.txt" <<'EOF'
idf_component_register(
    SRCS "main.c" "nff_idf_shim.c" "nff_injected_ca.c"
    INCLUDE_DIRS "."
    REQUIRES nff nff_l2_net esp_wifi esp_event esp_netif nvs_flash esp_timer driver
)
# Force-include the shim into the customer app component so its esp_wifi.* / NFF_CONFIG_INITIALIZER /
# nff_init bind to the openeth + NVS-identity shim. (nff_idf_shim.c also #includes it explicitly and
# reaches the real nff_init as `(nff_init)` — a parenthesised identifier is not a macro invocation.)
target_compile_options(${COMPONENT_LIB} PRIVATE -include "${CMAKE_CURRENT_LIST_DIR}/nff_idf_shim.h")
EOF
[ -n "$NFF_CA_DEF" ] && echo "$NFF_CA_DEF" >> "$PROJ/main/CMakeLists.txt"
_fw_ident_defs "$PROJ/main/CMakeLists.txt"   # per-build build_id/fw_version, scoped for ccache

# ---- shared openeth + NVS-identity component (copied so we can bake identity into its CMakeLists) ----
# diff-guard on the SOURCES only: leaving them untouched when unchanged keeps their mtimes and ninja
# skips recompiling on a warm build. CMakeLists.txt is excluded because we regenerate it with the
# per-build identity tokens below, so it can never match the pristine source copy.
if ! diff -rq --exclude=CMakeLists.txt "$L2NET" "$PROJ/components/nff_l2_net" >/dev/null 2>&1; then
  rm -rf "$PROJ/components/nff_l2_net"; cp -r "$L2NET" "$PROJ/components/nff_l2_net"
fi
# nvs_creds.c assigns cfg->fw_version / cfg->build_id from these tokens, so this component MUST get
# them — otherwise every L2 heartbeats the "0.0.0-l2" fallback and no version-anchored recovery check
# (migration 0080's semver_gt) can ever observe the fix.
{ cat "$L2NET/CMakeLists.txt"; _fw_ident_lines; } \
  | _write_if_changed "$PROJ/components/nff_l2_net/CMakeLists.txt"

# ---- nff SDK as a native IDF component (its own CMakeLists auto-registers when IDF_TARGET is set) ----
# A symlink to the real SDK: the customer app + nff_l2_net reference build_id/fw_version via macros
# (baked above), but the SDK reads them from cfg->* at runtime, so it needs no per-build token and can
# stay symlinked (ccache-friendly). This is the components/nff -> nff-sdk-c convention the project uses.
rm -rf "$PROJ/components/nff"
ln -s /repo/nff-sdk-c "$PROJ/components/nff"

cd "$PROJ"
. "$IDF_PATH/export.sh" >/dev/null 2>&1
# `idf.py set-target` implies a fullclean, so run it only when build/ isn't already configured for
# esp32 — a valid warm cache skips it (no needless fullclean); a missing/corrupt build/ falls through
# to a clean reconfigure via `idf.py build`. NFF_L2_BUILD_CLEAN=1 still forces from-scratch.
if [ ! -f "$PROJ/build/CMakeCache.txt" ] || \
   ! grep -q "^IDF_TARGET:STRING=esp32$" "$PROJ/build/CMakeCache.txt" 2>/dev/null; then
  rm -rf "$PROJ/build"
  idf.py set-target esp32
fi
idf.py build

# Stage 4: produce the artifacts nff-github's compile-source route collects. The merged image MUST be
# padded to the full 4 MB flash size (0xFF fill): the L2 supervisor writes it verbatim to the QEMU mtd
# drive, so a short image leaves the OTA/coredump partitions off the end of flash.
cd "$PROJ/build"
esptool.py --chip esp32 merge_bin --fill-flash-size 4MB -o "$PROJ/merged.bin" @flash_args
cd "$PROJ"
cp "$PROJ"/build/*.elf "$PROJ/firmware.elf"

# The OTA payload is the APP image ONLY (~1 MB, fits ota_1); pushing the merged 4 MB image fails
# Update.begin "too large". The app is the entry at flash offset 0x20000 in flash_args.
app_bin=$(awk '$1=="0x20000"{print $2}' "$PROJ/build/flash_args")
cp "$PROJ/build/$app_bin" "$PROJ/app.bin"

# Deposit the collectable artifacts where the caller globs (ARTIFACT_DIR). No-op when == PROJ.
if [ "$ARTIFACT_DIR" != "$PROJ" ]; then
  mkdir -p "$ARTIFACT_DIR"
  cp "$PROJ/merged.bin" "$PROJ/app.bin" "$PROJ/firmware.elf" "$ARTIFACT_DIR/"
fi
echo "L2 (ESP-IDF) artifacts: $ARTIFACT_DIR/merged.bin (4MB, cold-boot) + $ARTIFACT_DIR/app.bin (OTA) + $ARTIFACT_DIR/firmware.elf"
