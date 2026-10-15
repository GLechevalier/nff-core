# nff-sdk-c — Compilation Size Optimization Catalog

How to make the SDK take as little flash and RAM as possible — by **optimizing the existing
code** and by **making it parametrizable** so unused features compile out entirely.

This is a roadmap/analysis document. Nothing here changes default behavior; every proposed
flag defaults to today's behavior, mirroring the existing `NFF_BOOTSTRAP_ENABLED` guarantee
("operational-only builds are byte-for-byte unchanged", `include/nff.h:364`).

---

## Scope: what we're actually optimizing

A full firmware image is ~1 MB, but **nff is only a ~23 KB slice** of it — the rest is the
shared TLS (mbedTLS), WiFi/lwIP, and MQTT stacks that an already-connected device pays for
anyway. So optimization works on two fronts:

1. **nff's own ~23 KB flash / ~24 KB RAM** — shrink the code and buffers.
2. **What nff *forces into the link*** — compiling out a module also drops the heavyweight
   library it pulls in (e.g. dropping OTA removes `Update` + `HTTPClient` ≈ 26 KB). This is
   where the real KBs are.

The decisive external factor never changes: **nff's marginal cost depends on whether the
customer's firmware already links TLS + MQTT.** If it does, nff adds tens of KB. If it
doesn't, the first connected feature drags in the full stack regardless of how lean nff is.

### Flash-vs-RAM legend (read every row through this)

| Lives in | Section | Costs | Example |
|---|---|---|---|
| Code (instructions) | `.text` | **Flash** | function bodies |
| Const data / string literals / format strings | `.rodata` | **Flash** | `"nff: cmd rejected"`, JSON keys |
| Initialized globals | `.data` | **Flash + RAM** | `static const` tables |
| **Un**initialized statics / buffers | `.bss` | **RAM only** | `static char buf[1024]` |

⚠️ Common mistake: a bare `static char x[N]` (no initializer) is **RAM (`.bss`)**, *not* flash.
Most of the SDK's big buffers are RAM. Shrinking them frees RAM; flash wins come from
compiling out code (`.text`) and string literals (`.rodata`).

### Baseline — per-module footprint (ESP32 xtensa, `-Os`, `size` text+data / bss)

| Module | Flash | Static RAM | Role |
|---|--:|--:|---|
| `nff_core` | 1.0 KB | 0.3 KB | ✅ core — lifecycle, reconnect backoff |
| `nff_mqtt` | 1.0 KB | 0.3 KB | ✅ core — connection loop (glue over PubSubClient) |
| `nff_cmd` | 1.4 KB | 9.2 KB | ✅ core — command receive/verify/dispatch |
| `nff_security` | 1.7 KB | 0.2 KB | ✅ core — ECDSA verify, nonce ring |
| `nff_store` | 1.9 KB | — | bootstrap-centric (see Lever A) |
| port (`esp32_arduino`) | 5.3 KB | — | ✅ core — one per platform |
| `nff_heartbeat` | 0.6 KB | — | ◐ recommended |
| `nff_ota` | 2.5 KB | — | ○ optional |
| `nff_crash` | 5.3 KB | 6.3 KB | ○ optional |
| `nff_claim` | 2.0 KB | 8.2 KB | ○ optional (bootstrap) |
| `nff_diag` | 0.2 KB | — | ○ optional |
| **Full SDK** | **~23 KB** | **~24 KB** | |

---

## Lever A — Parametrization: compile out modules *(biggest flash wins)*

Today **all 10 modules compile unconditionally** (`CMakeLists.txt:16-41` for ESP-IDF,
`:44-62` for host). Only `NFF_BOOTSTRAP_ENABLED` (`include/nff.h:364-368`) gates anything.
A telemetry-only customer still pays for OTA, crash, claim, and the libraries they pull in.

The fix is a feature-flag scheme modeled exactly on the existing `NFF_BOOTSTRAP_ENABLED`
pattern. The abstraction already supports it: **the Zephyr build already omits `nff_store.c`
and `nff_claim.c`** (`zephyr/CMakeLists.txt:8-19`), proving the modules are separable, and
the port analysis confirms **zero abstraction leaks** — every heavy dependency is isolated to
the 4 port files, so removing a module cleanly drops its dependency.

### Proposed flags

| Proposed flag | Default | Gates | nff flash saved | RAM saved | Dependency that falls away |
|---|---|---|--:|--:|---|
| `NFF_WITH_OTA` | on | `nff_ota.c` + `ota` cmd handler | ~2.5 KB | — | **`Update` + `HTTPClient` + `esp_https_ota` ≈ 26 KB** |
| `NFF_WITH_CRASH` | on | `nff_crash.c` + panic hook | ~5.3 KB | **~6.3 KB** | `esp_core_dump` |
| `NFF_WITH_DIAG` | on | `nff_diag.c` + `diag` cmd handler | ~0.2 KB | — | — |
| `NFF_WITH_HEARTBEAT` | on | `nff_heartbeat.c` | ~0.6 KB | — | — |
| `NFF_BOOTSTRAP_ENABLED` | **off (exists)** | `nff_claim.c` + `nff_store.c` | ~4 KB | **~16 KB** | downgrades MQTT buffer 8 KB→1 KB |

> ⚠️ `nff_store.c` is currently compiled in operational builds even though its base64 +
> chunked-blob logic (`src/nff_store.c:14-30`) only serves the bootstrap/claim path. It should
> move behind `NFF_BOOTSTRAP_ENABLED` (as Zephyr already does), recovering ~1.9 KB flash from
> every operational build.

### Work required per flag (real integration points)

1. **Source list** — wrap each optional `src/nff_*.c` in a CMake `option()` and add to the
   source list conditionally (`CMakeLists.txt:16-41` ESP-IDF, `:44-62` host). Pass the matching
   `-DNFF_WITH_*` via `target_compile_definitions`.
2. **Public API guards** — guard the feature's public functions in `nff.h`, e.g.
   `nff_register_health_check` (`include/nff.h:321`, impl `src/nff_core.c:223`) under
   `NFF_WITH_OTA`.
3. **Command dispatch** — guard the built-in handlers in `src/nff_cmd.c` so removing a module
   also removes its `action` branch (the `ota`/`diag`/`reboot` strcmp chain around
   `nff_cmd.c:95-99`). Otherwise the dispatcher references a compiled-out symbol.
4. **ESP-IDF component graph** — make the `REQUIRES` list conditional (`CMakeLists.txt:32-40`)
   so excluding OTA drops `esp_https_ota` + `esp_http_client` from the component build, not
   just the nff object.
5. **Init/loop hooks** — guard the module's `nff_init`/`nff_loop` calls in `src/nff_core.c`
   (the crash panic-hook install, heartbeat tick, OTA probation check).

### Resulting profiles

| Profile | Modules kept | Flash | Static RAM |
|---|---|--:|--:|
| **Full** (today) | everything | ~23 KB | ~24 KB |
| **Telemetry + commands** | core, mqtt, cmd, security, store*, heartbeat, port | ~13 KB | ~10 KB |
| **Minimal (presence-only)** | core, mqtt, security, heartbeat, port (no `nff_cmd`) | ~10 KB | ~1 KB |

*With `nff_store` gated to bootstrap, telemetry drops another ~1.9 KB.

---

## Lever B — Dependency reduction *(follows from Lever A, plus crypto knobs)*

The heavy bytes are in the libraries, not nff. Module exclusion (Lever A) is the primary way
to shed them; these knobs handle the rest.

### Module → dependency map

| Module | Heavy dependency it forces in | Marginal cost on ESP32 |
|---|---|--:|
| `nff_mqtt` | PubSubClient (~8.5 KB) + WiFiClientSecure glue | ~8.5 KB (rarely pre-present) |
| `nff_ota` | `Update` + `HTTPClient` + `esp_https_ota` (~26 KB) | ~26 KB — **drop with `NFF_WITH_OTA=0`** |
| `nff_security` | mbedTLS ECDSA/ECP/SHA-256 | ~0 (in ROM, shared with WiFi) |
| `nff_store` / all NVS | Preferences / NVS / EEPROM | ~0 if app already uses NVS |
| `nff_crash` | `esp_core_dump` | ~0 (config-gated in IDF) |

The PAL contract (`include/nff_port.h`) is the sole cross-module boundary — no module reaches
around it to a platform API, so excluding a module never leaves a dangling dependency.

### `NFF_SKIP_CMD_SIG` — drop mbedTLS ECDSA where mTLS suffices

Already implemented on the ESP8266 port (`src/port/nff_port_esp8266_arduino.c:~385`): when
defined, command-signature verification is bypassed and authenticity rests on the mTLS channel
alone. On a **bare/non-Espressif MCU** where mbedTLS isn't free, this avoids linking ECDSA/ECP
(~tens of KB). **Proposal:** extend the same guard to the ESP32 Arduino/IDF ports
(`nff_port_ecdsa_p256_verify` bodies) so a flash-starved non-ESP32 target can opt out.
Security trade-off must be documented — see `SECURITY.md`'s two-layer model.

### mbedTLS config trimming (bare MCUs only)

On ESP32 mbedTLS lives in ROM and is shared with WiFi — **zero marginal cost**. On a bare MCU
that statically links it, trim the `mbedtls_config.h` to the one ciphersuite + curve nff uses
(ECDHE-ECDSA, P-256, SHA-256) and disable everything else: typically **50–100 KB** reclaimed.

### MQTT buffer right-sizing (RAM)

`NFF_MQTT_BUFFER_SIZE` (`include/nff.h:375-381`) is 1 KB operational, 8 KB bootstrap, and it
drives `NFF_CMD_MAXLEN` (`:382-384`) which sizes the command parse buffer. Keep it at 1 KB for
operational builds. ⚠️ Don't undersize blindly — PubSubClient/esp-mqtt **silently drop**
PUBLISHes larger than the buffer; an OTA command carrying a pre-signed URL is ~400 B+
(`nff_cmd.c:64-72`).

---

## Lever C — Code-level optimizations *(mostly flash `.text`/`.rodata`)*

Concrete, measured findings:

| # | Site | Issue | Fix | Saves |
|---|---|---|---|--:|
| C1 | `src/nff_ota.c:31` | `sscanf(v, "%d.%d.%d", …)` in `version_le` — pulls in the libc `sscanf` formatter; **only `sscanf` site in the SDK** | Hand-roll with `strtol` + `.` split | ~200–500 B flash |
| C2 | ~17 `nff_log()` calls across `nff_cmd.c`, `nff_mqtt.c`, `nff_ota.c`, `nff_claim.c` | Debug format strings sit in `.rodata` unconditionally | Add `NFF_LOG_LEVEL`; compile verbose logs out in production | ~200–300 B flash |
| C3 | `src/nff_core.c:31` `BACKOFF_TABLE_MS` vs `src/nff_mqtt.c:131` `steps[]` | Same reconnect-backoff sequence defined twice | Single shared `static const` + accessor | small flash |
| C4 | `src/nff_mqtt.c:48` and `:109` | `static char lwt_payload[128]` built identically in two functions | Build once in a helper | dedup |

Notes:
- **No floating point** anywhere (no `%f`, no `float`/`double`) — soft-float and FP formatting
  are already avoided. ✅ Don't reintroduce them (e.g. a `%f` in a diag field would link the
  whole FP printf path).
- `snprintf` is used at ~59 sites. Swapping to a tiny-printf would save flash but is invasive
  and risky — list only as a last resort, and **measure** before committing; the ESP-IDF/newlib
  formatter is largely shared with the rest of the firmware, so the marginal saving is often
  smaller than expected.

---

## Lever D — RAM (`.bss`) tuning *(frees RAM, not flash)*

These are all **RAM** reductions. Existing knobs are `int` Kconfig symbols (`Kconfig`) — but
Kconfig only reaches **ESP-IDF**; for Arduino builds they must be set via `-D`.

| Knob / buffer | Site | Default | Trim to | RAM saved |
|---|---|--:|--:|--:|
| `NFF_LOG_LINES` × `NFF_LOG_LINE_LEN` (RTC log) | `src/nff_crash.c:42` `s_rtc_log` | 32×128 = 4 KB | 8×64 | ~3.5 KB RTC |
| crash log JSON scratch | `src/nff_crash.c:165` `log_json` | `32×(128+16)` ≈ 4.6 KB | scales with above | ~3 KB |
| crash report scratch | `src/nff_crash.c:185` `report[2560]` | 2.5 KB | ~2 KB | ~0.5 KB |
| `NFF_NONCE_RING_SIZE` | `src/nff_security.c` ring | 16 | 8 | ~70 B |
| `NFF_MAX_USER_CMDS` | `include/nff.h:355` | 8 | 4 | ~128 B |
| cmd parse + response buffers | `src/nff_cmd.c:69` `buf`, `:91` `resp` | `NFF_MQTT_BUFFER_SIZE` (1 KB) each | — | (don't undersize — C/B caveat) |

**Proposal:** surface the crash-buffer sizes (`NFF_LOG_LINES`, `NFF_LOG_LINE_LEN`) as plain
`-D` overrides documented for Arduino too, not just Kconfig — most of the SDK's RAM is here,
and Arduino users currently can't reach it cleanly.

---

## Lever E — Build & toolchain flags *(free, no source change)*

These shrink the **whole image**, not just nff, and cost nothing but a build-config line.

```ini
# platformio.ini
[env:esp32]
platform = espressif32
framework = espidf            ; leaner than the Arduino core
build_flags =
    -Os
    -ffunction-sections -fdata-sections
    -Wl,--gc-sections         ; drop unreferenced functions/data
    -flto                     ; link-time optimization
board_build.partitions = custom.csv
```

- **`-Os` + `--gc-sections` + LTO**: ~10–20% off across the board. Pure flag change.
- **Framework choice**: ESP-IDF carries less than the Arduino core. PlatformIO doesn't compile
  smaller — *the same GCC + framework produce the same bytes* — it **exposes** the flags the
  Arduino IDE buries (`platform.local.txt`). The savings come from the flags, not the tool.
- **`sdkconfig` component stripping** (ESP-IDF): drop unused IDF components and trim mbedTLS;
  pairs with Lever B.

---

## Prioritized summary

| Lever | What | Effort | Flash | RAM | Risk |
|---|---|---|--:|--:|---|
| **A** | `NFF_WITH_OTA=0` (+ drops `Update`/`HTTPClient`) | Med | **~28 KB** | — | Low — clean module boundary |
| **A** | `NFF_WITH_CRASH=0` (+ `esp_core_dump`) | Med | ~5 KB | **~6 KB** | Low |
| **A** | Gate `nff_store` to bootstrap | Low | ~1.9 KB | — | Low — Zephyr already does |
| **E** | `-Os` + `--gc-sections` + LTO | Low | ~10–20% | — | None |
| **E** | Arduino → ESP-IDF framework | Med | image-wide | — | Med — porting |
| **B** | `NFF_SKIP_CMD_SIG` on bare MCU | Low | ~tens KB* | — | **Security trade-off** |
| **B** | mbedTLS config trim (bare MCU) | Med | ~50–100 KB* | — | Med |
| **D** | Crash buffer + ring tuning | Low | — | **~7 KB** | Low |
| **C** | `sscanf`→`strtol`, log gate, dedup | Low | ~0.5–1 KB | — | Low |

\* Bare/non-Espressif only; ~0 on ESP32 (ROM-shared).

### Recommended implementation order

1. **Lever E flags first** — free, image-wide, zero code risk. Establish the measurement baseline.
2. **Lever A feature flags** — the structural win. Formalize `NFF_WITH_OTA / _CRASH / _DIAG /
   _HEARTBEAT` on the `NFF_BOOTSTRAP_ENABLED` pattern, and gate `nff_store` to bootstrap. This
   unlocks the dependency savings (Lever B) that dominate the budget.
3. **Lever D knobs** — expose crash/ring buffer sizes to Arduino, document the RAM profile.
4. **Lever C cleanups** — opportunistic; bundle into the same PR.
5. **Lever B crypto** — only when targeting a bare MCU; carries a security trade-off that needs
   sign-off against `SECURITY.md`.

> The single biggest lever is `NFF_WITH_OTA=0`: ~28 KB flash for a device that doesn't take
> remote updates. Everything else is incremental by comparison — except on a bare MCU, where
> the TLS stack (Lever B) dwarfs all of it.

---

## Reproducing the numbers

```bash
# nff's own per-module flash/RAM (host archive):
size -t build/libnff.a
# ESP32 per-module (from a real build's object tree):
size -t examples/arduino_bootstrap/build/esp32.esp32.esp32/libraries/nff/*.o
# Dependency footprint (Arduino wrapper layer):
size -t .../libraries/PubSubClient/*.o .../libraries/Update/*.o .../libraries/HTTPClient/*.o
```

Validate module separability cheaply (host, no hardware): remove `src/nff_ota.c` from the host
source list (`CMakeLists.txt:44-62`), stub its `ota` dispatch branch in `src/nff_cmd.c`,
rebuild — a clean link proves the module is cleanly excludable.
