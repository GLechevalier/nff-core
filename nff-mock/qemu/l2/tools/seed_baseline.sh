#!/usr/bin/env bash
# seed_baseline.sh — build an L2 (QEMU) firmware image, stage it on the fleet volume, and print the
# build_id to point the controller at (NFF_MOCKCTL_L2_FIRMWARE_BUILD_ID).
#
# This makes the L2 baseline reproducible instead of a manual sequence. It compiles the source via
# the exact Stage-4 recipe nff-github's /internal/compile-source uses — build_espidf.sh for a native
# ESP-IDF app (a `.c`, the guided demo's firmware) or build_arduino_shim.sh for an Arduino sketch (a
# `.ino`) — pads the merged image to 4 MB, computes the nff-ota build_id contract (sha256(source)[:16])
# and bakes it in, then POSTs the bin + ELF to the fleet's /internal/store-bin + /internal/store-elf.
#
# Usage (repo root on a box with docker + the fleet reachable):
#   FLEET_URL=http://localhost:3001 FLEET_SECRET=dev-fleet-secret \
#     bash nff-mock/qemu/l2/tools/seed_baseline.sh nff-mock/qemu/l2/example-espidf/clean_l2.c
#
# Env:
#   FLEET_URL      (default http://localhost:3001)   FLEET_SECRET (default dev-fleet-secret)
#   FW_VERSION     (default 1.0.0)                    IDF_IMAGE   (default espressif/idf:v5.3.1)
#   ARDUINO_VER    (default ~3.1.0)                   # only used for a `.ino` source
set -euo pipefail

SRC="${1:?usage: seed_baseline.sh <path/to/main.c | path/to/sketch.ino>}"
# Framework is selected by extension: a native ESP-IDF app (.c) builds on build_espidf.sh; an Arduino
# sketch (.ino) on build_arduino_shim.sh. Each writes to a distinct out/<proj>/ dir on the /work mount.
case "$SRC" in
  *.c)   FRAMEWORK=espidf;  PROJ_NAME=idfproj ;;
  *.ino) FRAMEWORK=arduino; PROJ_NAME=ardproj ;;
  *)     echo "!! unknown source type: $SRC (expected .c or .ino)"; exit 2 ;;
esac
REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"          # repo root (…/nff_cli_mcp)
FLEET_URL="${FLEET_URL:-http://localhost:3001}"
FLEET_SECRET="${FLEET_SECRET:-dev-fleet-secret}"
FW_VERSION="${FW_VERSION:-1.0.0}"
IDF_IMAGE="${IDF_IMAGE:-espressif/idf:v5.3.1}"
ARDUINO_VER="${ARDUINO_VER:-~3.1.0}"
# Persistent caches — the SAME named volumes the runtime compile path mounts
# (nff-github/config.py: l2_build_cache_volume / l2_ccache_volume). Sharing them means the dev seed
# reuses the warm ninja build dir instead of a from-scratch build: cold ~15 min → warm ~1 min. Set
# either to "" to opt back into a fully ephemeral (cold) build.
BUILD_CACHE_VOLUME="${BUILD_CACHE_VOLUME:-nff-l2-builddir}"
CCACHE_VOLUME="${CCACHE_VOLUME:-nff-l2-ccache}"

# nff-ota's build_id contract: sha256 of the source string, first 16 hex chars.
BUILD_ID="$(sha256sum "$SRC" | cut -c1-16)"
echo ">> build_id=$BUILD_ID  fw=$FW_VERSION  framework=$FRAMEWORK  source=$SRC"

WORK="$(mktemp -d)"
# Stage the source where each recipe expects it: build_espidf.sh reads $SKETCH_DIR/main/main.c,
# build_arduino_shim.sh globs $SKETCH_DIR/*.ino.
if [ "$FRAMEWORK" = espidf ]; then
  mkdir -p "$WORK/main"; cp "$SRC" "$WORK/main/main.c"
else
  cp "$SRC" "$WORK/sketch.ino"
fi

# PROJ lives on the persistent /bcache volume (warm ninja rebuild); ARTIFACT_DIR copies the
# collectable artifacts back onto the ephemeral /work mount so the MERGED/ELF globs below still find
# them. Empty volume names fall back to the old ephemeral PROJ under /work (a fresh cold build).
cache_mounts=""
proj="/work/out/$PROJ_NAME"; artifact_env=""
if [ -n "$BUILD_CACHE_VOLUME" ]; then
  cache_mounts="-v $BUILD_CACHE_VOLUME:/bcache"; proj="/bcache/$PROJ_NAME"; artifact_env="ARTIFACT_DIR=/work/out/$PROJ_NAME "
fi
[ -n "$CCACHE_VOLUME" ] && cache_mounts="$cache_mounts -v $CCACHE_VOLUME:/ccache"
echo ">> compiling ($FRAMEWORK, ${IDF_IMAGE}) — cold ~15 min, warm ~1 min (reuses ${BUILD_CACHE_VOLUME:-<none>})…"
# Windows/git-bash mount-path handling. Two problems, one fix:
#  1) MSYS auto-rewrites the mount DESTINATIONS (`:/repo`, `:/work`, `:/bcache`, `:/ccache`) into
#     Windows paths, so /repo inside the container isn't the repo and every /repo/... lookup 404s
#     ("build_arduino_shim.sh: No such file or directory"). We suppress that with MSYS_NO_PATHCONV=1.
#  2) But that also stops MSYS converting the SOURCES, and $WORK=mktemp is git-bash's virtual
#     /tmp/… which Docker can't resolve (mounts /work empty → "cp: cannot stat '/work/*.ino'").
# Fix: hand Docker Windows-form source paths via cygpath, and keep conversion off for the rest.
# cygpath is absent on Linux (prod) → win_path() falls back to the path unchanged; MSYS_NO_PATHCONV
# is then just an unused env var. So this whole block is a no-op off Windows.
win_path() { cygpath -w "$1" 2>/dev/null || printf '%s' "$1"; }
REPO_MNT="$(win_path "$REPO")"
WORK_MNT="$(win_path "$WORK")"
if [ "$FRAMEWORK" = espidf ]; then
  inner="SKETCH_DIR=/work PROJ=$proj ${artifact_env}bash /repo/nff-mock/qemu/l2/example-espidf/build_espidf.sh"
else
  inner="SKETCH_DIR=/work ARDUINO_VER=\"$ARDUINO_VER\" PROJ=$proj ${artifact_env}bash /repo/nff-mock/qemu/l2/example-arduino/build_arduino_shim.sh"
fi
MSYS_NO_PATHCONV=1 docker run --rm -e NFF_BUILD_ID="$BUILD_ID" -e NFF_FW_VERSION="$FW_VERSION" \
  -v "$REPO_MNT":/repo:ro -v "$WORK_MNT":/work $cache_mounts "$IDF_IMAGE" \
  bash -lc "$inner"

MERGED="$WORK/out/$PROJ_NAME/merged.bin"
ELF="$WORK/out/$PROJ_NAME/firmware.elf"
[ "$(stat -c %s "$MERGED")" = "4194304" ] || { echo "!! merged.bin is not 4 MB"; exit 1; }

echo ">> staging on the fleet volume by build_id…"
python3 - "$MERGED" "$ELF" "$BUILD_ID" "$FLEET_URL" "$FLEET_SECRET" <<'PY'
import base64, sys, urllib.request, json
merged, elf, build_id, fleet_url, secret = sys.argv[1:6]
def post(route, field, path):
    body = json.dumps({"build_id": build_id, field: base64.b64encode(open(path, "rb").read()).decode()}).encode()
    req = urllib.request.Request(fleet_url.rstrip("/") + route, data=body,
                                 headers={"X-Fleet-Secret": secret, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        print("  ", route, r.status, r.read().decode()[:200])
post("/internal/store-bin", "bin_b64", merged)
post("/internal/store-elf", "elf_b64", elf)
PY

rm -rf "$WORK"
echo ">> DONE. Point the controller at it:"
echo "   NFF_MOCKCTL_L2_FIRMWARE_BUILD_ID=$BUILD_ID"
