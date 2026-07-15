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
| `nff doctor` | stable | Health-check; complements `nff status` (P2 #5) |
| `nff clean` | stable | |
| `nff debug` | stable | OpenOCD + GDB on-chip debug |
| `nff install-deps` | stable | |
| `nff mcp` | stable | Start-only today; `stop/restart/logs` in P2 #6 |
| `nff auth` / `deauth` | stable | Browser OAuth + headless login |
| `nff repair` | stable | Cloud diagnosis (needs login) |
| `nff agent` | stable | Cloud agent over SSE (needs login) |
| `nff provision batch` | stable | Fleet batch enrollment |
| `nff pi probe` | stable | Raspberry-Pi reachability probe |
| `nff connect` | **roadmap** | Stub in **both** Rust (`commands/connect.rs`) and Python (`nff/commands/connect.py`) |
| `nff ota` | **roadmap** | Stub in **both** Rust (`commands/ota.rs`) and Python (`nff/commands/ota.py`) |
| `nff status` | **roadmap** | Not yet implemented — P2 #5 |

---

## P0 — Truth-in-docs & repo hygiene

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

### 3. Purge tracked binaries from the repo
Currently tracked (≈76 MB of binary junk in a 191-file repo):
- `public/videos/FirstVideoGithub.mp4` (35 MB) and `public/videos/SecondVideoGithub.mp4` (37 MB)
- `nff.pdb` (3.6 MB debug symbols)
- `nff-rs/nff/graphify-out/` (committed AST cache)

Keep the referenced README images (`public/images/tumbnail.png` banner and
`public/images/PlatformScreen.jpg`).

Action: `git rm --cached` these, add them to `.gitignore`, and host the demo videos as GitHub
Release assets or an external link referenced from the README. (Full history rewrite is optional;
at minimum stop tracking them going forward.)

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

## P2 — Requested commands (small, high satisfaction)

### 5. `nff status`
New `Status` variant in `cli.rs` + `commands/status.rs`. Unlike `doctor` (pass/fail health-check),
`status` is a snapshot:
- active build backend — `config::active_backend()`
- detected board — `boards::list_devices()`
- MCP server up/down + URL — `daemon::is_running()`
- auth state — cloud logged-in vs. offline
- **last build artifact** — requires a small addition: persist the last ELF/bin path + timestamp in
  config on each `compile`/`flash` (nothing records this today).

### 6. `nff mcp stop | restart | logs`
Turn `McpArgs` in `cli.rs` into an `McpCommand` subcommand enum. The primitives already exist in
`tools/daemon.rs` (`is_running`, `start_background`, `log_path`):
- `stop` — kill the pid bound on `DEFAULT_PORT`
- `restart` — stop + `start_background`
- `logs` — tail `daemon::log_path()`

Low effort because the daemon layer is already built; only wiring and a stop primitive are missing.

### 7. Better missing-dependency errors
`doctor.rs` already prints actionable `→ fix` hints. Propagate that pattern into the **runtime**
paths: when `compile`/`flash` fail because `arduino-cli`, `platformio`, or `esptool` is missing,
surface the same "Run: nff install-deps" guidance instead of a raw subprocess error.

---

## P3 — CI, Windows, security (market-readiness)

### 8. Split CI from release
`.github/workflows/release.yml` is the only workflow: it triggers on **push to main only**
(no PR CI), runs **`cargo test` on ubuntu only**, has **no `pytest`** and **no clippy gate**, yet
every main push auto-publishes to PyPI.
- Add `ci.yml` on `pull_request` + push: `cargo test` **and** `cargo clippy -- -D warnings`
  (currently only a manual gate per `CLAUDE.md`) across a `{ubuntu, windows, macos}` matrix.
- Add `pytest` for the Python package as long as it is maintained.
- Keep `release.yml` for the publish path only.

### 9. Reproduce and fix the Windows install
The reviewer tried Windows first and had to fall back to Ubuntu. A Windows wheel *is* built, but
nothing in CI runs on Windows. Add a Windows CI job that does `pip install` of the built wheel,
then `nff --version` + `nff doctor` as a smoke test. `tools/installer.rs` already handles the
Windows PATH via `winreg`; the failure is most likely first-run toolchain install, which the smoke
test will surface.

### 10. Security posture (CRA-aware)
Document in `SECURITY.md`:
- the MCP server binds `127.0.0.1` only;
- the Bearer gate (`NFF_MCP_REQUIRE_AUTH`, OFF by default per `CLAUDE.md`) and when to enable it;
- an explicit "do not expose the MCP server beyond localhost without authentication" warning.

Note EU Cyber Resilience Act (CRA) relevance if `nff` is ever distributed as a market product —
a tool that talks to real hardware will eventually need a clear security contract.

---

## Suggested sequencing

1. **P0** — one afternoon, pure docs + hygiene. Removes the biggest sources of confusion.
2. **P1 #4** — the offline mode. Changes the product's first impression the most.
3. **P2 #5–7** — `nff status`, `nff mcp` subcommands, better error messages.
4. **P3** — CI split, Windows smoke test, security doc.
