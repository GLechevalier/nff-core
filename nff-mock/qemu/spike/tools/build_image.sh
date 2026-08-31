#!/usr/bin/env bash
# build_image.sh — build the Stage-0 spike firmware into a single merged.bin (toolchain box only).
#
# Prereqs (NOT available in the dev session — run this on a box with the toolchain):
#   - ESP-IDF installed and exported:   . $IDF_PATH/export.sh
#   - A provisioned credentials.h copied into firmware/main/ :
#       nff-fleet provision new-device --id spike-01 --project <uuid>
#       cp credentials.h ../firmware/main/credentials.h
#
# Output: firmware/build/nff_spike.merged.bin  (flash to QEMU's MTD via `nff-mock-l2 spike --image`)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fw="$here/../firmware"
sdk="$here/../../../../nff-sdk-c"   # nff-mock/qemu/spike/tools -> repo root -> nff-sdk-c

if [[ -z "${IDF_PATH:-}" ]]; then
  echo "IDF_PATH unset — source your ESP-IDF export first (. \$IDF_PATH/export.sh)" >&2
  exit 1
fi
if [[ ! -f "$fw/main/credentials.h" ]]; then
  echo "missing $fw/main/credentials.h — provision a device and copy it in first (see RUNBOOK.md)" >&2
  exit 1
fi

# The SDK's documented IDF convention is `components/nff`. Create that symlink so the component
# name is `nff` (matching `REQUIRES nff`), pointing at the unmodified nff-sdk-c repo.
mkdir -p "$fw/components"
if [[ ! -e "$fw/components/nff" ]]; then
  ln -s "$(cd "$sdk" && pwd)" "$fw/components/nff"
fi

# Pin a sane boot epoch (NOW) so the broker cert's validity window is satisfied even if NVS has no
# boot_epoch key. Passed as a bare define the spike's main.c reads as a fallback.
BOOT_EPOCH="$(date +%s)"

cd "$fw"
idf.py set-target esp32
idf.py build -DNFF_SPIKE_BOOT_EPOCH="$BOOT_EPOCH"

# Merge bootloader + partition table + app into one raw image for QEMU's MTD flash.
esptool.py --chip esp32 merge_bin --fill-flash-size 4MB \
  -o build/nff_spike.merged.bin \
  @build/flash_args

echo "built: $fw/build/nff_spike.merged.bin (boot_epoch fallback=$BOOT_EPOCH)"
echo "next: nff-mock-l2 spike --image $fw/build/nff_spike.merged.bin"
