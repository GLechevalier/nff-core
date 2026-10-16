# L2 arduino-as-IDF-component build — the WiFi→openeth shim path (Part 3)

Builds an **unmodified** customer Arduino sketch as an arduino-as-IDF-component image with the
WiFi→openeth shim force-included, so the sketch's `WiFi.*` transparently drives openeth in QEMU.
`nff-github`'s Stage-4 server build mirrors this recipe. `build_arduino_shim.sh` assembles + builds it.

## Validated 2026-06-30 (compile)

Ran against `test-nff/blink_nff.ino` — an unmodified sketch (`#include <WiFi.h>`,
`WiFi.begin("maxime's iPhone", "12345678")`, `WiFi.status()`, `WiFi.localIP()`, `nff_init/connect/loop`,
compiled-in `credentials.h`). **The shim's `#define WiFi nffWiFiShim` substitution, every shimmed
method, the customer sketch, and the nff SDK Arduino port all COMPILE clean** against real
arduino-esp32 — the box-gated risk for the shim (Part 3's compile-time transparency) is retired. The
whole arduino-esp32 core also compiles, and the openeth bring-up was made idempotent so it coexists
with arduino's netif/event init (`l2/project/components/nff_l2_net/openeth.c`).

## Assembly (what the script does)

1. **main/** — the customer `.ino` → `.cpp` + its `credentials.h` + the shim (`l2/shim/`), with the
   shim header **force-included** into the sketch TU (`-include`). `NFF_QEMU` is set **project-wide**
   (top `CMakeLists.txt`, `idf_build_set_property`) so it reaches the **nff** SDK component (the
   broker hostname-check skip), not just `main`.
2. **components/nff** — the nff SDK synced as an Arduino library (`sync_arduino_lib.py`, the `.cpp`
   Arduino port). Its sources live under `src/`; REQUIRES `espressif__arduino-esp32 PubSubClient`
   plus the IDF components the Arduino port pulls in (`app_update` for `esp_ota_ops.h`, `esp_timer`,
   `espcoredump`, `mbedtls`, …). Format `-Werror`s the arduino-cli build doesn't apply are scoped-off.
3. **components/PubSubClient** — `knolleary/pubsubclient` (the Arduino port's MQTT dependency).
4. **arduino-esp32** — pulled by the component manager (`main/idf_component.yml`); `AUTOSTART_ARDUINO`
   so the core calls the sketch's `setup()/loop()`.

## Toolchain caveat — a fully-LINKED binary needs a matched arduino-esp32 ↔ IDF pair

A clean link was **not** achieved in the dev `espressif/idf:release-v5.3` image because it is a dev
snapshot (`v5.3.5-784-…`) that no released arduino-esp32 cleanly matches:
- **latest arduino-esp32 3.x** compiles everything but its `ssl_client.cpp` gates `start_ssl_client`
  on `MBEDTLS_VERSION_MAJOR >= 4` → undefined references (this IDF has mbedtls **3**).
- **arduino-esp32 3.1.x** (the IDF-5.3 / mbedtls-3 line) fails to *compile* against this dev snapshot
  (driver API drift: `adc_continuous_data_t`, `i2c_ll_slave_init`).

This is an arduino-esp32 ↔ IDF version-matching issue, independent of the shim/nff. For a fully-linked
image use a **tagged** IDF release whose mbedtls major matches the arduino-esp32 release, e.g.
`espressif/idf:v5.3.1` + `arduino-esp32 ~3.1.0`:

```bash
docker run --rm -v <repo>:/repo:ro -v <scratch>:/out espressif/idf:v5.3.1 \
  bash -lc 'SKETCH_DIR=/repo/test-nff ARDUINO_VER="~3.1.0" bash /repo/nff-mock/qemu/l2/example-arduino/build_arduino_shim.sh'
```
