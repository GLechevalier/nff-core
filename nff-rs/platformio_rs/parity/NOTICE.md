# Vendored PlatformIO test suite — attribution

The directory `platformio-core/` contains material copied verbatim from
**PlatformIO Core**, used here as a differential parity test suite for the
`platformio_rs` Rust port.

- **Upstream:** https://github.com/platformio/platformio-core
- **Pinned tag:** `v6.1.19`
- **Pinned commit:** `15b8859aee4731fe4c5c6b06f74972535601eeb2`
- **License:** Apache License 2.0 (see `platformio-core/LICENSE`)
- **Copyright:** © 2014-present PlatformIO <contact@platformio.org>

## What is vendored

- `platformio-core/tests/` — the upstream pytest suite, **unmodified**.
- `platformio-core/LICENSE`, `tox.ini`, `Makefile` — for reference/attribution.

## What is *ours* (not PlatformIO, not Apache-2.0 upstream)

- `shim/clirunner_shim.py` — a `click.testing.CliRunner` replacement that drives
  an arbitrary `$PIO_BIN` as a subprocess.
- `conftest.py` (this directory) — swaps in the shim before the vendored conftest
  loads, so the vendored test files stay pristine.
- `run_parity.sh` — convenience runner.

## How parity works

The same vendored tests run against two binaries by swapping `$PIO_BIN`:

1. **Baseline** — `PIO_BIN=$(which pio)` must be **green** first. This proves the
   shim faithfully reproduces the in-process Click behaviour. The Python
   `platformio` package must be installed (the vendored conftest imports it, and
   the shim walks its Click tree to map command objects to argv paths).
2. **Parity** — `PIO_BIN=.../pio-rs`. A milestone is "done" when its relevant
   subset is green here too.

Tests that poke internal Python APIs (e.g. `PackageSpec`, `ProjectConfig`,
manifest parsing) cannot bind to a binary; those are reimplemented as Rust
`#[cfg(test)]` unit tests in the corresponding `platformio_rs` module instead.

When bumping the pinned tag, update `PIO_CORE_VERSION` in `src/lib.rs` to match.
