# What's new in version 0.3.0 — PlatformIO backend in the shipped Rust binary (unreleased)

The board-universal **PlatformIO backend** — previously only in the Python prototype — is now ported into the shipped Rust binary (`nff-rs/`), so `pip install nff` users get it. **PlatformIO is now the default backend** in both implementations; the classic arduino-cli backend stays available via `NFF_BUILD_BACKEND=arduino` (or `build.backend` in `~/.nff/config.json`). See [Build backends](#build-backends).

### Carried over — four PlatformIO hardening fixes
- **Your own `platformio.ini` is respected.** A project that ships its own `platformio.ini` (custom partitions, PSRAM, build flags) is built as-is and never overwritten.
- **Multi-file sketches build.** A sketch folder with helper `.cpp`/`.h` files or multiple `.ino` tabs now copies every file into the build, not just the first.
- **First-build package flakes self-heal.** A transient PlatformIO `package-manager-ioerror` (or a half-installed framework surfacing as a missing `pins_arduino.h`) is classified as transient, and the broken platform is pruned + reinstalled on retry.
- **`nff clean` clears PlatformIO output too** (`nff_pio` temp root, including the heavy `.pio/build`), not just the arduino temp dir.

### Local / offline mode — sign-in is no longer mandatory
- **You don't need an nff account to compile, flash, monitor, or debug.** Those run entirely on your machine. `nff init --offline` (or `NFF_OFFLINE=1`) configures the bench with no cloud sign-in at all, and even a plain `nff init` no longer blocks on login — if the browser flow fails or times out, init continues in local mode instead of aborting. Previously init hard-exited when login failed, leaving the bench unusable.
- **`nff doctor` treats a signed-out bench as healthy.** Sign-in is now an informational warning (`optional`), not a failed check, so a local-only setup gets a clean bill of health. Only the cloud features — `nff repair`, `nff agent`, and device onboarding — actually need an account.
- **`nff auth login` lifts offline mode automatically.** Signing in re-enables cloud features and clears the local-only flag.

### New commands
- **`nff status`** — a read-only snapshot of the bench (build backend, detected board, MCP server up/down + URL, auth state, last successful build). Unlike `nff doctor`'s pass/fail health check, it just reports current state and always exits 0. `compile`/`flash` now record the last build artifact for it to display.
- **`nff mcp` is now a command group.** Bare `nff mcp` still starts the server; new `nff mcp stop` / `restart` / `logs` manage the background server that `nff init` launched (with a clear message when a running server has no pidfile because it wasn't started by nff).
- **`nff power`** — measure the energy an nff operation costs the device, in joules. Needs the **nff-power-meter** rig (an STM32 Nucleo watching a 1 Ω low-side shunt in the ESP32's ground return — no PPK2, no INA219). Subcommands: `devices` (attached? calibrated?), `calibrate` (solve the whole ground path against a multimeter reading), `selftest` (prove the shunt is wired by watching a load that moves), `set-shunt`, `measure --during "<cmd>"` (marginal joules, with `--max-joules` as a regression gate that exits 1 when over), and `monitor` (live mA). Exposed to MCP as `power_status` and `power_measure`.
  - **Honest by construction:** `power_measure` returns `ok: false` and leaves the energy fields empty rather than reporting a wrong number when samples are dropped, the accumulation window doesn't match the command, the rig isn't actually wired (a floating ADC pin otherwise reads a confident ~590 mA), or the measured command itself failed. `nff doctor` gained an optional power-meter check that never flips the exit code.

### Honest command surface
- **Stubs are no longer announced as features.** `nff connect` and `nff ota` are unimplemented; their output and the README now say so and point at the new **`docs/ROADMAP.md`**. The README gained a **Command status** table marking every command `stable` vs `🚧 roadmap`, so nothing that doesn't work looks like it does.

### Nudges
- **Periodic "star the repo / go Pro" reminder.** After a command finishes (except the long-running `mcp` server) nff occasionally prints a one-line nudge to stderr — every 5th invocation by default. Tunable with `NFF_NUDGE_EVERY=N` and fully silenced with `NFF_NO_NUDGE=1`.

### Internals & packaging
- **Centralized config.** A single `nff/config.py` module now owns `~/.nff/config.json` (default device, backend, tokens, offline flag, last build, nudge counter, power calibration), replacing scattered reads/writes.
- **Rust parity.** Offline mode, `nff status`, the `nff mcp` management subcommands, and the nudge live in the shipped Rust binary too, in lockstep with the Python reference.
- **CI/packaging.** `pyproject.toml` gained the runtime libraries (`click`, `requests`, `pyserial`, `rich`, `mcp`, …) the Python *reference* package imports under `[dev]`, so `pip install -e .[dev]` + pytest no longer aborts at collection in a clean environment. Committed build cruft was removed from the repo (a stale `graphify-out/` cache, `nff.pdb`, and the large `public/videos/*.mp4`).

### Tooling
- **`nff doctor`** shows the active backend and checks PlatformIO Core; under the PlatformIO backend a missing arduino-cli/esptool is informational, not a failure.
- **`nff install-deps`** auto-installs PlatformIO Core.
- **`nff init --backend <platformio|arduino>`** persists the backend and seeds the PlatformIO board id from the detected device.

> **Note:** Verified end-to-end against real PlatformIO + ESP32; 105 cargo tests pass and `cargo clippy -- -D warnings` is clean. (Wokwi simulation has moved to the separate `nff-sim` package.) New Python test suites cover config, offline `init`, nudges, and power (`tests/test_config.py`, `tests/test_init.py`, `tests/test_nudge.py`, `tests/test_power.py`).

---

## What's new in v0.2.20 — the "reliable install" release

This release is about making the bench loop **survive on its own**: the previous version (`0.2.19`) worked when a human was watching, but transient toolchain hiccups would surface as hard failures — fatal for an agent driving the loop unattended. It also brings the **Rust binary to full parity** so it becomes the shipped artifact, and adds first-run onboarding so a fresh machine can actually compile.

### Reliability — corrected
- **Transient failures are now retried, not fatal.** A new classifier tells a *transient* toolchain hiccup (arduino-cli `EINVAL` / "Invalid argument", a Windows build-dir file lock, a serial port re-enumerating after auto-reset, a slow build timing out) apart from a *genuine* compile error. Transient failures retry with backoff; real compile errors still fail fast. Previously **any** of these killed `compile`/`flash` outright.
- **Cold builds no longer time out.** The compile timeout was a flat 120 s — a first-time ESP32 build routinely exceeds that and died with "Command timed out". Compile now gets 600 s, upload 180 s, and a timeout is treated as retryable rather than a hard error.
- **Upload-failure misclassification fixed.** arduino-cli prints `uploading error:` on a transient port failure; the naive classifier mistook that for a compile error and refused to retry. A strong serial/upload signal (`failed uploading`, `could not open port`, `the port is busy`) now correctly wins over the bare word `error:`.
- **Serial is resilient.** `serial_read`/`serial_write`/`reset_device` retry transient port faults, and the serial monitor no longer crashes with a raw traceback when a device is unplugged mid-stream — it reports the error cleanly.
- **Stale-library guard.** "Flash to test my fix" could silently build the *old* library. `flash` and `doctor` now warn when a local `nff-sdk-c` checkout is newer than the synced Arduino library, so you never ship stale firmware unknowingly.

### Install / onboarding — added
- **`nff init` now installs the full build toolchain** (the `esp32` core, `PubSubClient`, and the `nff` Arduino library) on first run, so a freshly-set-up machine can compile a sketch that does `#include <nff.h>` without manual `arduino-cli` steps.
- **`doctor` gained an `nff lib` check** reporting the synced library version and flagging staleness.

### New capabilities
- **`nff pi probe`** — detect a directly-connected Raspberry Pi and tell you exactly which link in the chain is missing (cable/power → IP → SSH), via ARP-OUI matching, mDNS, and a TCP/22 probe (with an optional `--sweep`). Groundwork for running nff-pentester on a Pi node.

### Rust port → the shipped binary
- The Rust implementation in `nff-rs/` reached **full feature parity** with the Python package (all of the above, plus the existing CLI/MCP/OAuth surface) and is now the release artifact. Version bumped to **0.2.20** across the Rust crate and the Python package, which stay in lockstep. The Rust port is no longer "paused".

### Quality
- New automated tests across both implementations (retry classifier, serial retry, library sync/staleness, onboarding, `pi`, init). Rust passes `cargo clippy -- -D warnings` and the full `cargo test` suite, and the whole loop (compile → flash → monitor, plus the transient-retry path) was **verified on real ESP32 hardware**.

> **Upgrade note:** the on-disk library marker (`.nff_sync_meta`) gains `version`/`synced_at` fields; libraries synced by `0.2.19` will show `?` in `nff doctor` until the next `nff install-deps`/`nff init` re-syncs them. No action required.

