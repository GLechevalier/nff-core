# Contributing to nff-sdk-c

Thank you for improving the nff device SDK. This document covers the practical path from idea to merged PR.

## Before You Start

- **Bug fix or small improvement?** Open a PR directly — no issue needed.
- **New feature or breaking change?** Open an issue first so we can align on scope before you invest time.
- **Security issue?** See [SECURITY.md](SECURITY.md) — do not open a public issue.

## Development Setup

### Prerequisites

- CMake >= 3.16
- MinGW-w64 (Windows) or GCC (Linux/macOS)
- `cmd.exe` for running tests on Windows (permission issues exist with bash on some setups)

### Build & Test

```sh
cmake -B build -G "MinGW Makefiles"   # Windows
cmake -B build                         # Linux / macOS
cmake --build build
```

Run the three host-side test binaries:

```bat
build\tests\test_nonce_ring.exe
build\tests\test_cmd_dispatch.exe
build\tests\test_ota_rollback.exe
```

All three must exit 0 before submitting a PR.

## Code Style

- C99. No C++ constructs in `.c` files.
- 4-space indent, no tabs.
- Public API lives exclusively in `include/nff.h`. Do not expose internal types there.
- Port-specific code goes under `src/port/`. A new platform = a new `nff_port_<platform>.c` file that implements every symbol declared in `include/nff_port.h`.
- No dynamic allocation (`malloc`/`free`) in the SDK core — embedded targets may not have a heap.
- Keep `nff_loop()` non-blocking. Anything that could block belongs in the port layer with a configurable timeout.

## Pull Request Checklist

- [ ] All three host tests pass (`test_nonce_ring`, `test_cmd_dispatch`, `test_ota_rollback`)
- [ ] No new compiler warnings with `-Wall -Wextra`
- [ ] New public API is documented in `include/nff.h` (brief Doxygen-style comment)
- [ ] If you added a new port, the stub in `zephyr/nff_port_zephyr_stub.c` pattern is followed
- [ ] PR description explains **why** the change is needed, not just what it does

## Port Contributions

New platform ports are very welcome. The contract is `include/nff_port.h`. Look at `src/port/nff_port_posix.c` for the reference implementation and `src/port/nff_port_esp32_idf.c` for a real embedded target.

`nff_port_ecdsa_p256_verify` may be declared `__attribute__((weak))` on platforms where tests need to override it — follow the POSIX port's pattern.

## Commit Messages

One-line summary (≤ 72 chars), imperative mood: `Fix nonce ring wraparound at boundary N`.  
Add a blank line and a longer explanation if the why is non-obvious.

## License

By submitting a PR you agree that your contribution is licensed under the [MIT License](LICENSE).
