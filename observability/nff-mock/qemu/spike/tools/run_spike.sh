#!/usr/bin/env bash
# Boot the spike firmware in qemu-system-xtensa (bundled in the idf image) on the nff network,
# bridging the broker via SLIRP guestfwd -> ncat nff-fleet 8883. Captures UART, then prints markers.
set -e
cd /project
. "$IDF_PATH/export.sh" >/dev/null 2>&1

echo "=== merge to a single 4 MB flash image for QEMU MTD (padded) ==="
( cd build && esptool.py --chip esp32 merge_bin --fill-flash-size 4MB -o merged.bin @flash_args )
ls -la build/merged.bin

echo "=== ensure ncat (the guestfwd forwarder) is present ==="
if ! command -v ncat >/dev/null; then
  apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq ncat >/dev/null 2>&1 || true
fi
command -v ncat || { echo "ncat missing"; exit 1; }

QEMU="$(command -v qemu-system-xtensa)"
echo "=== qemu: $QEMU ==="
echo "=== booting (90s cap); broker 10.13.0.10:8883 -> ncat nff-fleet 8883 ==="
# net=10.13.0.0/24 puts the broker's fixed guest IP inside the SLIRP subnet so the guestfwd rule
# is routable (default SLIRP net is 10.0.2.0/24, which would reject 10.13.0.10).
timeout 95 "$QEMU" -nographic -machine esp32 -m 4M \
  -drive file=build/merged.bin,if=mtd,format=raw \
  -nic "user,model=open_eth,id=net0,net=10.13.0.0/24,guestfwd=tcp:10.13.0.10:8883-cmd:ncat nff-fleet 8883" \
  -serial mon:stdio > /tmp/serial.log 2>&1 || true

echo "=== serial markers (openeth / clock / mqtt / SPIKE / errors) ==="
grep -nE "openeth|GOT_IP|eth |clock|boot_epoch|SPIKE:|nff|mqtt|MQTT|TLS|MBEDTLS|handshake|connect|rst:|Guru|panic|abort|Backtrace|E \(|W \(" /tmp/serial.log | head -80
echo "=== tail of serial ==="
tail -25 /tmp/serial.log
echo "=== verdict ==="
if grep -q "SPIKE: heartbeat published" /tmp/serial.log; then echo "FIRMWARE_REPORTS_HEARTBEAT_PUBLISHED"; else echo "NO_HEARTBEAT_MARKER"; fi
