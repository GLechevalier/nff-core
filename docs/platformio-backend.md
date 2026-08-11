# PlatformIO backend — board-universal builds

This document explains the migration that added **PlatformIO** as nff's build backend, why it was done, how it works, how to use and switch it, and its current scope/status.

## TL;DR

- nff can now build/flash through **PlatformIO** in addition to **arduino-cli**. The two coexist; you pick one per-run or persist a choice.
- **PlatformIO is the default** (in the Python implementation). It makes nff **board-universal** — any of PlatformIO's ~1000 board ids works, and the toolchain for a board family auto-installs on first build.
- Nothing about the existing arduino-cli path changed; opt back in with `NFF_BUILD_BACKEND=arduino` or `nff init --backend arduino`.
- **Scope:** both implementations. The PlatformIO backend has since been ported to the shipped Rust binary (`nff-rs/`) and is the default there too; this document describes the original Python migration.

## Why

The arduino-cli backend effectively limited nff to the few cores we installed (ESP32 + some AVR FQBNs). PlatformIO ships a registry of ~1000 boards and **self-installs the platform toolchain (compiler + framework + uploader) on first build**, so nff becomes truly universal — ESP32 variants (S3/C3/C6/…), RP2040/Pico, STM32, classic AVR, and far beyond — without per-board setup. PlatformIO also uses **esptool** under the hood, so the `.bin`/`.elf` artifacts stay compatible with the rest of the pipeline (OTA, crash diagnosis).

## How backend selection works

A single function, `toolchain.active_backend()`, decides the backend for every `compile`/`flash` entry point. Precedence:

1. **`NFF_BUILD_BACKEND`** env var — per-run override (`platformio`/`pio` or `arduino`/`arduino-cli`).
2. **`build.backend`** in `~/.nff/config.json` — the persisted choice (written by `nff init --backend …`).
3. **Default → `platformio`.** Only an explicit `arduino`/`arduino-cli` selects arduino-cli; anything else resolves to PlatformIO.

It is re-evaluated on every call — no restart needed. The CLI and MCP tool surface are identical regardless of backend.

### Switching

```bash
# try PlatformIO once, leaving config untouched
NFF_BUILD_BACKEND=platformio nff flash sketches/esp32_vitals --board esp32dev --port COM10

# make PlatformIO the persistent default (also saves the board id)
nff init --backend platformio          # afterwards: plain `nff flash <sketch>` — no flags

# opt back into arduino-cli
nff init --backend arduino             # or: NFF_BUILD_BACKEND=arduino <command>
```

`--board` is backend-aware: a **PlatformIO board id** (`esp32dev`) under the pio backend, an **arduino-cli FQBN** (`esp32:esp32:esp32`) under the arduino backend. The board is read from `build.board` (pio) or `default_device.fqbn` (arduino), so a saved config lets you omit `--board`.

## What's required

| Need | Detail |
|---|---|
| **PlatformIO Core** | Found on `PATH`, in `~/.platformio/penv`, or via `python -m platformio`. If missing, `nff install-deps` (pio backend active) runs `pip install platformio`. |
| **Backend selected** | Default is already `platformio`; nothing to do for a fresh setup. |
| **A board id** | A PlatformIO board id via `--board` or saved `build.board`. |
| **Network on first build** | PlatformIO downloads the platform + framework + esptool for a board family the first time only, then caches under `~/.platformio`. |
| **Connected board (flash)** | A serial port + USB-serial driver, same as before. |
| **nff SDK** | Only if the sketch does `#include <nff.h>` — auto-materialised into the project's `lib/nff/` (prefers a local `nff-sdk-c` checkout, else downloads). |

Explicitly **not** required on the pio backend: arduino-cli, manual core installs, or syncing the flattened nff Arduino library.

## How it works internally

- **Dispatcher** — `nff/nff/tools/toolchain.py` stays the arduino-cli backend *and* gained a thin dispatch layer: `compile_only`, `flash`, `stream_compile`, `stream_upload`, `resolve_sketch_dir`, and `discover_artifacts` delegate to the PlatformIO backend when it's active. `active_backend()`/`configured_board()` live here too.
- **PlatformIO backend** — `nff/nff/tools/backends/platformio.py`:
  - **Project scaffold** — writes the sketch to a native `src/main.cpp` (injecting `#include <Arduino.h>`, since a `.cpp` gets none of the `.ino` auto-preprocessing), generates a `platformio.ini`, and materialises `lib/nff/` only when the sketch uses the SDK. External Arduino libraries (e.g. PubSubClient) are added to `lib_deps` only when referenced.
  - **`platformio.ini`** — `[env:nff]` with `platform`/`board`/`framework = arduino`/`monitor_speed`, plus `build_flags = -DNFF_FQBN_TOKEN=<board>` (same token name as the arduino backend, so the firmware heartbeat reports board identity unchanged).
  - **Commands** — `pio run` (compile) and `pio run -t upload --upload-port <port>` (flash), reusing nff's existing retry/stream machinery.
  - **Artifacts** — discovered from `.pio/build/nff/firmware.{elf,bin,…}`, mapped to the same `CompileResult` shape every consumer already expects.
- **Board catalog** — `nff/nff/tools/boards.py` gained `PIO_BOARD_CATALOG` (platform per board id) and `pio_board` hints on the USB-detect map. The catalog is for defaults/auto-detect, **not** an allow-list — any board id is accepted.

## Sketch layout difference

| Backend | Layout |
|---|---|
| `arduino` | a `.ino` whose filename matches its folder |
| `platformio` | a generated `platformio.ini` + `src/main.cpp` (`#include <Arduino.h>` injected) |

A `.cpp` loses the Arduino `.ino` auto-prototype generation, so define functions before use (the nff-authored sketches already do).

## Scope, status, and caveats

- **Python only.** This lives in `nff/nff/`. `pip install nff` ships the **Rust binary** (`nff-rs/`), which is **arduino-cli only** — these changes do not reach installed users until ported. Uncommitted as of this writing.
- **Cloud onboarding still runs on arduino-cli.** `nff init --backend platformio` configures local builds and **skips** the fleet-claim onboarding step (that path is ESP32/arduino-specific for now).
- **First-build flake.** PlatformIO's package manager can hit a transient `package-manager-ioerror` on the very first platform download, leaving a partial framework (symptom: `fatal error: pins_arduino.h: No such file`). Fix: `pio system prune --force`, delete the partial package dir, and re-run.

## Verification

- Full Python test suite green (incl. a dedicated `tests/test_platformio.py` covering scaffold, `platformio.ini` generation, artifact discovery, tool discovery, and dispatch). Arduino-path tests are pinned to the arduino backend so the default flip doesn't reroute them.
- **Real hardware:** `nff compile` and `nff flash` of `sketches/esp32_vitals` under the pio backend produced `firmware.elf` + `firmware.bin`, uploaded via esptool to a real ESP32 (ESP32-D0WD-V3) on COM10, and the device booted and streamed live serial — confirming the full `compile → flash → run` loop on the PlatformIO backend.

## Files touched

- New: `nff/nff/tools/backends/{__init__.py, platformio.py}`, `tests/test_platformio.py`, this doc.
- Changed: `nff/nff/tools/toolchain.py` (dispatch + selection), `config.py` (`build` section, default `platformio`), `tools/boards.py` (catalog + hints), `tools/arduino_lib.py` (dest-parameterised SDK flatten), and the consumers `commands/{init,doctor,install_deps,compile_cmd,flash}.py` + `mcp_server.py`.
