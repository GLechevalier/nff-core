#!/usr/bin/env bash
# seed_baseline.sh — build an L2 (QEMU) firmware image, stage it on the fleet volume, and print the
# build_id to point the controller at (NFF_MOCKCTL_L2_FIRMWARE_BUILD_ID).
#
# This makes the L2 baseline reproducible instead of a manual sequence. It compiles a sketch via the
# Stage-4 arduino-shim recipe (the exact recipe nff-github's /internal/compile-source uses), pads the
# merged image to 4 MB, computes the nff-ota build_id contract (sha256(source)[:16]) and bakes it in,
# then POSTs the bin + ELF to the fleet's /internal/store-bin + /internal/store-elf.
#
# Usage (repo root on a box with docker + the fleet reachable):
#   FLEET_URL=http://localhost:3001 FLEET_SECRET=dev-fleet-secret \
#     bash nff-mock/qemu/l2/tools/seed_baseline.sh nff-mock/qemu/l2/example-arduino/clean_blink_l2.ino
#
# Env:
#   FLEET_URL      (default http://localhost:3001)   FLEET_SECRET (default dev-fleet-secret)
#   FW_VERSION     (default 1.0.0)                    IDF_IMAGE   (default espressif/idf:v5.3.1)
#   ARDUINO_VER    (default ~3.1.0)
set -euo pipefail

SKETCH_INO="${1:?usage: seed_baseline.sh <path/to/sketch.ino>}"
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
BUILD_ID="$(sha256sum "$SKETCH_INO" | cut -c1-16)"
echo ">> build_id=$BUILD_ID  fw=$FW_VERSION  sketch=$SKETCH_INO"

WORK="$(mktemp -d)"
cp "$SKETCH_INO" "$WORK/sketch.ino"

# PROJ lives on the persistent /bcache volume (warm ninja rebuild); ARTIFACT_DIR copies the
# collectable artifacts back onto the ephemeral /work mount so the MERGED/ELF globs below still find
# them. Empty volume names fall back to the old ephemeral PROJ under /work (a fresh cold build).
cache_mounts=""
proj="/work/out/ardproj"; artifact_env=""
if [ -n "$BUILD_CACHE_VOLUME" ]; then
  cache_mounts="-v $BUILD_CACHE_VOLUME:/bcache"; proj="/bcache/ardproj"; artifact_env="ARTIFACT_DIR=/work/out/ardproj "
fi
[ -n "$CCACHE_VOLUME" ] && cache_mounts="$cache_mounts -v $CCACHE_VOLUME:/ccache"
echo ">> compiling (arduino-shim, ${IDF_IMAGE}) — cold ~15 min, warm ~1 min (reuses ${BUILD_CACHE_VOLUME:-<none>})…"
docker run --rm -e NFF_BUILD_ID="$BUILD_ID" -e NFF_FW_VERSION="$FW_VERSION" \
  -v "$REPO":/repo:ro -v "$WORK":/work $cache_mounts "$IDF_IMAGE" \
  bash -lc "SKETCH_DIR=/work ARDUINO_VER=\"$ARDUINO_VER\" PROJ=$proj ${artifact_env}\
            bash /repo/nff-mock/qemu/l2/example-arduino/build_arduino_shim.sh"

MERGED="$WORK/out/ardproj/merged.bin"
ELF="$WORK/out/ardproj/firmware.elf"
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
