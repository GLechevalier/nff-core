# platformio_rs

A native-Rust reimplementation of [PlatformIO Core](https://github.com/platformio/platformio-core),
built so `nff`/`nff-rs` (and the agents that drive them) get a fast, single-binary,
dependency-free firmware build path.

> Status: **M0–M5 done; M6 native ESP32 fast-path proven on real hardware.**
> Config/package/registry (M1–M2), platform+board metadata and the `boards`/`device`/
> `settings`/`system`/`project` commands (M3), SCons build delegation for `run` (M4),
> the `test`/`check`/`debug`/`remote`/`home` orchestration commands (M5), and the
> **native ESP32 fast-path** (M6a–d): `run --native` captures one verbose SCons build
> into a cached plan, then replays it natively (parallel compiles + `-MMD`
> incremental cache + link + `esptool` images + flash) — `firmware.elf`/`firmware.bin`/
> `bootloader.bin`/`partitions.bin` come out **byte-identical** to PlatformIO
> (`parity/native_golden.sh`), and a natively-built+flashed image **boots on a real
> ESP32** (`run --native -t upload`). Remaining: M6e (CI wiring + `.ino`→`.cpp` for
> nff). See the roadmap (`~/.claude/plans/zazzy-cuddling-pillow.md`).

## What's here

```
platformio_rs/
  src/
    lib.rs            # library API (linked in-process by `nff`) + dispatch
    cli.rs            # clap surface mirroring PlatformIO's commands
    bin/pio_rs.rs     # `pio-rs` — drop-in CLI for the parity harness
  parity/             # differential test harness (see parity/NOTICE.md)
    platformio-core/  # vendored upstream pytest suite @ v6.1.19 (Apache-2.0)
    shim/             # CliRunner -> subprocess driver keyed on $PIO_BIN
    conftest.py       # swaps in the shim; vendored tests stay pristine
    run_parity.sh
```

## Two binaries, one library

- The **`pio-rs` binary** is a PlatformIO-compatible CLI. It exists primarily so the
  vendored pytest suite can drive the Rust port exactly like the Python `pio`.
- The **`platformio_rs` library** is what `nff-rs` links against for in-process builds
  (no subprocess) once the build milestones land.

## Developing

```bash
# Rust unit tests
cargo test -p platformio_rs
cargo clippy -p platformio_rs -- -D warnings
```

### Parity harness

The harness deliberately depends on a working Python `platformio` install: the
vendored conftest imports it, and the shim walks its Click command tree to map
command objects to argv paths. Parity is measured *against* the reference, so
this is by design.

> **Use the dedicated harness venv.** Installing `platformio==6.1.19` pins older
> `starlette`/`uvicorn` that conflict with other nff Python services — never put
> it in the workspace's shared `.venv`. `run_parity.sh` auto-detects
> `parity/.venv-harness/` and uses it.

One-time setup:

```bash
cd platformio_rs/parity
python -m venv .venv-harness
.venv-harness/Scripts/pip install platformio==6.1.19 jsondiff pytest   # (bin/ on Unix)
```

Run it:

```bash
# Baseline — MUST be green before parity. Defaults to the harness venv's pio.
PARITY_SCOPE=offline ./run_parity.sh baseline   # curated no-network subset (the M0 gate)
PARITY_SCOPE=full    ./run_parity.sh baseline   # everything (needs network, slow)

# Parity — same tests against the Rust binary:
cargo build --release -p platformio_rs
PARITY_SCOPE=offline ./run_parity.sh rust
```

`SHIM_INCOMPATIBLE` in `run_parity.sh` lists tests that monkeypatch in-process
Python state or hardcode the `pio` binary name; those can't run through a
subprocess shim and are reimplemented as Rust `#[cfg(test)]` tests instead.

## Milestones

| # | Scope |
|---|-------|
| M0 | Scaffold crate + clap skeleton + parity harness (baseline green) |
| M1 | `platformio.ini` config parse/validate/interpolate |
| M2 | Package meta + registry client + download/extract/cache |
| M3 | Platform/board metadata + `boards`/`device`/`settings`/`system`/`project` |
| M4 | Build Phase A — SCons delegation (`run`), byte-parity anchor |
| M5 | `test`, `check`, `debug`, `remote`, `home` |
| M6 | Build Phase B — native ESP32 fast-path (bypass SCons). a–d done (byte-identical `.elf`/`.bin` + real-HW boot); e (CI wiring + `.ino`) pending |
| M7 | nff integration + MCP tools |
| M8 | Port remaining pure-logic tests to Rust |
