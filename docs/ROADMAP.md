# nff — Roadmap

This roadmap is derived from external user feedback (an R&D engineer who installed and used
`nff` on real hardware) cross-checked against the actual state of the code. Every item below is
grounded in a specific file, so it can be picked up directly.

The goal is to close the gap between what the docs *promise* and what the binary *does*, and to
make the first-run experience friction-free for someone who just wants to compile, flash, and
monitor a board.

---

## Command status (source of truth)

Keep this table honest and in sync with `README.md`. `stable` = works on the shipped Rust binary;
`experimental` = present but rough / partial; `roadmap` = stub or not started.

| Command | State | Notes |
|---|---|---|
| `nff init` | stable | Currently forces cloud login — see P1 #4 |
| `nff compile` | stable | PlatformIO (default) + arduino backends |
| `nff flash` | stable | |
| `nff monitor` | stable | |
| `nff doctor` | stable | Pass/fail health-check; `nff status` is the read-only snapshot |
| `nff status` | stable | Snapshot: backend, board, MCP server, auth, last build |
| `nff clean` | stable | |
| `nff debug` | stable | OpenOCD + GDB on-chip debug |
| `nff install-deps` | stable | |
| `nff mcp` | stable | Bare `nff mcp` starts it; `stop` / `restart` / `logs` manage the background server |
| `nff auth` / `deauth` | stable | Browser OAuth + headless login |
| `nff repair` | stable | Cloud diagnosis (needs login) |
| `nff agent` | stable | Cloud agent over SSE (needs login) |
| `nff provision batch` | stable | Fleet batch enrollment |
| `nff pi probe` | stable | Raspberry-Pi reachability probe |
| `nff connect` | **roadmap** | Stub in **both** Rust (`commands/connect.rs`) and Python (`nff/commands/connect.py`) |
| `nff ota` | **roadmap** | Stub in **both** Rust (`commands/ota.rs`) and Python (`nff/commands/ota.py`) |

---

## P0 — Truth-in-docs & repo hygiene ✅ done (history rewrite optional)

*Hours of work, zero risk. Resolves the "between two states" and "repo cleanup" feedback.*

### 1. Resolve the Python/Rust contradiction in the README
`README.md:415` describes the Python package as **"the LIVE implementation"**, while
`README.md:36`, `README.md:451`, and `CLAUDE.md` correctly say the **Rust binary is the shipped
product** and Python is the reference/prototype. Fix `README.md:415` to match, and add the
**Command status table** (above) near the top of the README so new users immediately see what is
stable vs. experimental vs. roadmap.

### 2. Stop advertising stubs as finished features
`README.md:256` and `README.md:272-277` document `nff connect` as a working autonomous
repair loop, with no caveat. Both `nff connect` and `nff ota` are stubs everywhere.
- Mark them `🚧 roadmap — not yet implemented` in the README.
- Make the stub output point somewhere useful, e.g.
  `nff connect: not yet implemented — see docs/ROADMAP.md`.

### 3. Purge tracked binaries from the repo — **done (going-forward)**
These are no longer tracked in `HEAD` and are `.gitignore`d (`*.pdb`, `public/videos/`,
`graphify-out/`), so they can't be re-added by accident:
- `public/videos/FirstVideoGithub.mp4` (35 MB) and `public/videos/SecondVideoGithub.mp4` (37 MB)
- `nff.pdb` (3.6 MB debug symbols)
- `nff-rs/nff/graphify-out/` (committed AST cache)

The README images are kept (`public/images/tumbnail.png` banner and
`public/images/PlatformScreen.jpg`), and nothing in the tree references the videos, so there are
no broken links.

**Still optional / out-of-band:**
- **History rewrite** — the ~76 MB blobs still live in past commits. Reclaiming that space needs a
  `git filter-repo`/BFG pass + force-push and is deliberately deferred (the roadmap marked it
  optional; the working-tree fix above is what matters day-to-day).
- **Hosting the demo videos** — if the videos should be shown again, upload them as GitHub Release
  assets and link them from the README (an external step, not a repo change).

---

## P1 — Offline / local mode ✅ done (highest-value product change)

### 4. Decouple local bench tools from cloud login — **shipped**
`nff init` used to call `require_login()` before anything else and `exit(1)` on failure, blocking
anyone who only wanted to compile, flash, and monitor. That gate is gone.

- **`nff init --offline`** (or `NFF_OFFLINE=1`) skips the browser OAuth and marks the bench local;
  the choice is persisted (`config.offline`, cleared automatically on a successful `nff auth login`).
- A plain `nff init` no longer hard-requires login — a failure/timeout **falls back to local mode**
  instead of aborting. Toolchain install, MCP registration, and the background server still run.
- Local tools (`compile`/`flash`/`monitor`/`debug`) never needed a token (verified) and the MCP
  Bearer gate is off by default, so local use is fully tokenless.
- `doctor`'s login check is now a **⚠ warning**, not a ✗ failure; the Rust Claude-Desktop check was
  also downgraded to a warning (parity with Python) so `nff doctor` **exits 0** for a local-only
  bench. Signing in re-enables cloud features and flips the login check back to ✓.

Implemented in both Rust (`config.rs`, `cli.rs`, `commands/{init,doctor,auth}.rs`) and Python
(`config.py`, `commands/{init,doctor,auth_cmd}.py`). This is the single change that most improves
the first-run impression.

---

## P2 — Requested commands (small, high satisfaction) ✅ done

Shipped in both Rust (`nff-rs/nff/src/`) and Python (`nff/`).

### 5. `nff status` — **shipped**
New `Status` variant in `cli.rs` + `commands/status.rs` (Python: `commands/status.py`). Unlike
`doctor` (pass/fail health-check), `status` is a read-only snapshot that always exits 0:
- active build backend — `config::active_backend()` / `toolchain.active_backend()`
- detected board — `boards::list_devices()`
- MCP server up/down + URL — `daemon::is_running()`
- auth state — signed in vs. offline (local mode) vs. signed out
- **last build artifact** — a new `last_build` config record (path + kind + unix timestamp) is now
  written by `compile`/`flash` on success and rendered here as `path (kind, N ago)`.

### 6. `nff mcp stop | restart | logs` — **shipped**
`McpArgs` now carries an optional `McpSubcommands` enum (bare `nff mcp` still starts the server;
Python uses a `@click.group(invoke_without_command=True)`). `start_background` writes the child
PID to `~/.nff/mcp.pid`; the new daemon primitives are:
- `stop` — kill the PID in the pidfile (`taskkill /F` on Windows, `kill`/`SIGTERM` on Unix), then
  remove the pidfile; a graceful message when there's no pidfile or nothing is running
- `restart` — `stop` + wait for the port to free + `start_background`
- `logs` — print `daemon::log_path()` (`--lines N` / `-n N` for the tail)

### 7. Better missing-dependency errors — **shipped**
The streaming **flash** path used to surface a bare `Executable not found: pio`. A new
`toolchain::ensure_build_tool()` pre-checks the active backend's tool and raises the same
actionable ``"<tool> not found — run `nff install-deps`"`` hint that `compile_only`/`doctor`
already give, called up front in `flash` (both languages).

---

## P3 — CI, Windows, security (market-readiness) ✅ done

### 8. Split CI from release — **shipped**
`.github/workflows/release.yml` used to be the only workflow: it triggers on **push to main only**
(no PR CI), runs **`cargo test` on ubuntu only**, had **no `pytest`** and **no clippy gate**, yet
every main push auto-publishes to PyPI.
- New `.github/workflows/ci.yml` runs on `pull_request` + push (`branches-ignore: [main]`, since
  `release.yml`'s `test` job already covers main before publishing):
  - `rust` job — `cargo test` **and** `cargo clippy --all-targets -- -D warnings` (promotes the
    manual clippy gate from `CLAUDE.md` into an enforced check) across a `{ubuntu, windows, macos}`
    matrix.
  - `python` job — `pytest` across the same 3-OS matrix (hardware-free; `tests/conftest.py` isolates
    config to `tmp_path`).
- `release.yml` is unchanged — it stays the publish-only path.

### 9. Reproduce and fix the Windows install — **shipped**
The reviewer tried Windows first and had to fall back to Ubuntu. A Windows wheel *is* built, but
nothing in CI ran on Windows. `ci.yml`'s `windows-smoke` job now builds the wheel with
`maturin-action`, `pip install`s it, and runs **`nff --version` as a hard gate** — this catches the
wheel-tag / MSVC-runtime / PATH / entry-point breakage the reviewer hit. `nff doctor` runs
`continue-on-error` (informational): it exits 1 on a bare runner (no PlatformIO/config/MCP server),
so it surfaces first-run toolchain state in the log without failing the canary.

### 10. Security posture (CRA-aware) — **shipped**
`SECURITY.md` now documents the MCP-server security contract:
- the MCP server binds `127.0.0.1` only (`/health` always unauthenticated);
- the Bearer gate (`NFF_MCP_REQUIRE_AUTH`, OFF by default per `CLAUDE.md`) and when to enable it;
- an explicit "do not expose the MCP server beyond localhost without authentication" warning;
- an EU Cyber Resilience Act (CRA) note — a tool that talks to real hardware needs a clear security
  contract if ever distributed as a market product.

---

## P4 — Build-engine performance (native replay) 🚧 roadmap

*Added 2026-07-17 from a benchmark of the PlatformIO backend: `pio+rs` (native in-process engine,
`tools/pio_native.rs` → `platformio_rs`) vs `pio normal` (legacy subprocess, `NFF_PIO_LEGACY=1`,
`tools/pio.rs`). Measured through the real `compile` MCP tool (HTTP round-trip), sketch
`sketches/esp32_vitals` on `esp32dev`, cold (cache-miss capture) + warm (plan-cache replay),
interleaved. Tokens via `tiktoken o200k_base` (proxy; char counts are exact).*

### Benchmark result (nff v0.2.36)

| arm / state | median time | payload tokens | log lines | `built_natively` |
|---|---|---|---|---|
| pio+rs · cold (capture) | ~9.3 s | **~322,600** ⚠️ | 274 | false |
| pio+rs · warm (replay) | **~2.79 s** | **319** | 3 | true |
| pio normal · cold | ~9.5 s | 1,846 | 85 | false |
| pio normal · warm | ~3.26 s | 734 | 25 | false |

Net: native replay gives a **~15% warm speedup** and a **~2.3× warm token win** (terse
`built natively` summary), but is **~equal on cold** and has a severe cold-token regression (below).

### 11. Native engine must actually ship in the release binary — **verify per release**
The first benchmark showed *zero* difference between the two arms because the shipped
`target/release/nff.exe` was **stale** (v0.2.34) and predated the native-engine wiring in
`tools/{toolchain,pio_native}.rs` + `platformio_rs/src/commands/run.rs`; the compile path silently
fell through to in-process SCons. A `cargo build --release` (→ v0.2.36) is what activated
`built natively`. **Action:** treat the native fast-path as a release-gated feature — the
`windows-smoke`/CI canaries should assert a warm `compile` emits `built natively` (i.e. the plan
cache at `.pio/build/nff/.pio-rs-native/plan.json` is written and replayed), so a stale/misbuilt
binary can never regress this silently again.

### 12. ⚠️ Cold-capture floods the MCP payload (~322k tokens) — **bug, high priority**
On a cache-miss, `native::build_env` runs a **verbose** SCons capture and pipes the entire log
(`to_out(&output)` in `platformio_rs/src/build/native/mod.rs`) straight into the `compile` tool's
`output` field — ~1.16 MB / ~322k tokens for `esp32_vitals` (vs 1,846 tokens for the legacy path).
The *first* compile of any sketch — or any compile after a structural change that invalidates the
plan cache (new source file, changed build flag, platform bump; see `plan::cache_key`) — would blow
up an agent's context. **Fix:** summarize/suppress the capture log in the returned payload the way
replay already does (keep the verbose capture on disk for debugging, return a short summary +
errors). This is the single most impactful correctness fix for the MCP/agent use case.

### 13. Make a warm compile *really* fast — bottleneck is the GNU xtensa toolchain
Phase profile of a warm build (replayed captured commands, steady-state), `esp32_vitals`:

| phase | time | share | note |
|---|---|---|---|
| **link → firmware.elf** | ~1.7 s | ~50% | GNU `ld` statically linking `libFrameworkArduino.a` (3.3 MB) + ESP-IDF libs → 6 MB ELF; single-threaded. The 9 MB `.map` is negligible (dropping `-Wl,-Map` saved nothing). |
| **compile sketch TU** | ~1.25 s | ~35% | one `g++` on `esp32_vitals.ino.cpp` — dominated by parsing `Arduino.h`/`WiFi.h`/ESP-IDF headers, not user code |
| 3× image gen (esptool) | ~0.46 s | ~13% | `python esptool.py` elf2image ×2 + `gen_esp32part.py`; ~124 ms of each is Python startup |

The orchestrator is **already** off the critical path (native replay removed SCons/Python/PIO
startup) — which is why replay only bought ~15%. What remains is irreducible compiler work. Levers,
ranked:
- **(a) PCH the Arduino headers** — highest ROI for the edit→compile loop. The ~1.25 s sketch
  compile is almost entirely header re-parsing; a precompiled header could cut it to a few hundred
  ms. PlatformIO/Arduino don't do this by default.
- **(b) Native Rust image generation** — replace the two `python esptool.py` spawns with in-process
  `elf2image` (espflash-style) in `platformio_rs`. Removes ~0.4 s of Python startup; the engine is
  already Rust.
- **(c) Faster linker (`lld`)** — the ~1.7 s link is the hard floor; every build relinks the whole
  static image (no incremental link for firmware). GNU `ld` on Xtensa is single-threaded; LLVM `lld`
  (experimental Xtensa) could be 2–5×. `mold` has no Xtensa support. Without `lld` you cannot get
  this sketch under ~1.7 s.
- **(d) Parallel compile + ccache** — only helps **cold** (47 TUs); a warm build compiles one TU, so
  `-j` is moot there. Already wired via `platformio_rs` jobs.

Rough target: PCH (−~1 s) + native image gen (−~0.4 s) → ~2.0 s; sub-1.7 s needs `lld`.

---

## Suggested sequencing

1. **P0** — one afternoon, pure docs + hygiene. Removes the biggest sources of confusion.
   (Done except purging the tracked demo videos — see P0 #3.)
2. **P1 #4** ✅ — the offline mode. Changes the product's first impression the most.
3. **P2 #5–7** ✅ — `nff status`, `nff mcp` subcommands, better error messages.
4. **P3** ✅ — CI split, Windows smoke test, security doc.
5. **P4 #11–13** 🚧 — build-engine performance. Do **#12** (cold-capture token flood) first — it's a
   correctness bug for agents — then **#11** (release-gate the native path in CI), then the
   warm-speed levers **#13** (PCH → native image gen → lld).
