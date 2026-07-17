#!/usr/bin/env bash
# Golden-artifact parity for the M6 native ESP32 fast-path.
#
# Builds parity/fixtures/esp32-blink two ways and proves the native replay
# reproduces PlatformIO/SCons byte-for-byte:
#   A) pure SCons delegation  (`pio-rs run`)
#   B) native replay          (`pio-rs run --native`, after forcing a full rebuild)
# then diffs firmware.elf symbols (sorted `nm`) + byte-compares firmware.bin,
# bootloader.bin and partitions.bin.
#
# Requires the harness venv (Python platformio) + the installed espressif32
# platform/toolchain, exactly like run_parity.sh. Usage:
#   ./native_golden.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CRATE="$(cd "$HERE/.." && pwd)"
PY="${PYTHONEXEPATH:-$HERE/.venv-harness/Scripts/python.exe}"
BIN="${PIO_RS_BIN:-$CRATE/../target/release/pio-rs.exe}"
[ -f "$BIN" ] || BIN="$CRATE/../target/debug/pio-rs.exe"
FIX="$HERE/fixtures/esp32-blink"
BD="$FIX/.pio/build/esp32dev"
TC="$(cd ~ && pwd)/.platformio/packages/toolchain-xtensa-esp32/bin"
NM="$TC/xtensa-esp32-elf-gcc-nm.exe"
WORK="$(mktemp -d)"
export PYTHONEXEPATH="$PY"
trap 'rm -rf "$WORK"' EXIT

echo "pio-rs: $BIN"
echo "python: $PY"

echo "### A) pure SCons build"
rm -rf "$FIX/.pio"
"$BIN" run -d "$FIX" >"$WORK/a.log" 2>&1
cp "$BD/firmware.elf" "$WORK/a.elf"
for f in firmware.bin bootloader.bin partitions.bin; do cp "$BD/$f" "$WORK/a.$f"; done
"$NM" "$WORK/a.elf" | sed 's/^[0-9a-fA-F]\{8\} //' | sort >"$WORK/a.sym"

echo "### B) native replay (capture, force full rebuild, replay)"
rm -rf "$FIX/.pio"
"$BIN" run --native -d "$FIX" >"$WORK/capture.log" 2>&1   # cache miss -> SCons capture + cache plan
find "$BD" -name '*.o' -delete
rm -f "$BD"/firmware.elf "$BD"/firmware.bin "$BD"/bootloader.bin "$BD"/partitions.bin
"$BIN" run --native -d "$FIX" >"$WORK/replay.log" 2>&1     # cache hit -> native replay
grep "built natively" "$WORK/replay.log" || { echo "FAIL: native replay did not run"; cat "$WORK/replay.log"; exit 1; }
cp "$BD/firmware.elf" "$WORK/b.elf"
for f in firmware.bin bootloader.bin partitions.bin; do cp "$BD/$f" "$WORK/b.$f"; done
"$NM" "$WORK/b.elf" | sed 's/^[0-9a-fA-F]\{8\} //' | sort >"$WORK/b.sym"

echo "### RESULTS"
fail=0
if diff -q "$WORK/a.sym" "$WORK/b.sym" >/dev/null; then
  echo "  firmware.elf symbols: IDENTICAL ($(wc -l <"$WORK/a.sym") symbols)"
else
  echo "  firmware.elf symbols: DIFFER"; diff "$WORK/a.sym" "$WORK/b.sym" | head; fail=1
fi
for f in firmware.bin bootloader.bin partitions.bin; do
  if cmp -s "$WORK/a.$f" "$WORK/b.$f"; then
    echo "  $f: byte-identical ($(stat -c%s "$WORK/a.$f") bytes)"
  else
    echo "  $f: DIFFERS"; fail=1
  fi
done
rm -rf "$FIX/.pio"
[ "$fail" -eq 0 ] && echo "GOLDEN PARITY: PASS" || { echo "GOLDEN PARITY: FAIL"; exit 1; }
