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

## P4 — Flash write speed (real ESP32, CP210x/CH340) — roadmap

*Scope is the **USB write step only** (not compile). Target hardware is the classic ESP32 over a
CP210x (VID `0x10c4`) / CH340 (VID `0x1a86`) USB-UART bridge. Goal: a typical ~500 KB app write
from ~8 s today → ~2 s, and **sub-second on the hot edit→reflash loop**.*

**Why there's easy speed on the table.** `nff flash` delegates the write opaquely to
`pio run -t upload` (default backend) / `arduino-cli upload`, and **never sets the flash baud** —
so it falls to the board default (**460800** on PlatformIO's `esp32dev`; arduino-cli would use
921600). The `--baud` flag on `nff flash` is **dead** — it only feeds the monitor
(`cli.rs:100-101` documents "not used by arduino-cli"; Python `commands/flash.py:22` never reads
it). Every flash also re-writes **all four images** (bootloader@0x1000 + partitions@0x8000 +
boot_app0@0xe000 + app@0x10000) and re-pays the ~1.5–2 s reset→ROM-sync→stub-upload handshake,
even for a one-line edit. A direct `esptool write_flash` wrapper already exists but is **dead
code** (`nff/tools/toolchain.py:611 esptool_flash`, Rust `tools/toolchain.rs:1081`
`#[allow(dead_code)]`) — the natural seam for an nff-owned uploader.

**Where the time goes.** Fixed overhead (open + auto-reset + sync + stub + flash-attach +
finalize) ≈ 1.5–2.0 s regardless of size/baud. Transfer = compressed_bytes ÷ (baud/10); firmware
compresses ~1.7–2× and esptool `-z` compression is already default. A 500 KB app ≈ 280 KB
compressed → **6.1 s @460800**, **3.0 s @921600**, **1.9 s @1.5 M**.

**Central move:** stop delegating the write to the backend. After artifacts exist, route both the
CLI and the native-replay path (`platformio_rs/src/build/native/replay.rs:143-160`, which already
isolates the four offsets) through one nff-owned `fast_upload(port, artifacts, *, baud, mode)` —
built once, land in **both** Python `nff/` and Rust `nff-rs/` per the dual-impl rule.

### 11. High flash baud — auto-probed and persisted *(core)*
Add `config.flash.baud` and **wire the existing `--baud` flag into upload**. Auto-probe the
fastest stable rate per adapter (try 1500000 → 921600 → 460800, stop at the first that syncs +
passes esptool's stub MD5) and **persist the winner keyed on VID:PID(+serial)** in
`~/.nff/config.json` — CP210x is reliably 921600 (often 1 M/1.5 M), CH340 reliably 921600 (genuine
WCH often 1.5 M/2 M), so probe-and-remember beats a hardcode. Subprocess fallback: emit
`upload_speed = <baud>` in `write_platformio_ini` (`tools/backends/platformio.py:287`, Rust
`tools/pio.rs:360`). *Expected: transfer ~2–3× faster.*

> **How high can flash baud actually go? (why #11 probes instead of hardcoding, and why it
> stops caring past ~1.5 M.)** Three ceilings stack; the lowest wins:
> 1. **ESP32 side — not the limit.** The UART peripheral runs to ~5 Mbps and the flasher stub is
>    fine at 2–3 Mbaud. esptool SYNCs with the ROM at a fixed ~115200, then `change_baud` to the
>    `--baud` used for `write_flash` — only the *download* baud benefits from going high.
> 2. **The USB-UART bridge — the real ceiling on classic ESP32:** CP2102 ≈ 921600 (some 1 M);
>    CP2102N ≈ 3 M; CH340/CH340G = 921600 solid, 1.5 M usually, 2 M on genuine WCH; CH9102 ≈ 4 M+;
>    FT232R ≈ 3 M. It's literally *which chip is soldered on* — unknowable without trying, hence
>    probe-and-persist.
> 3. **The wall you hit first — on-device SPI-flash write throughput (~a few hundred KB/s to
>    ~1 MB/s effective).** Above ~1–1.5 Mbaud the wire stops being the bottleneck: the stub is
>    receive→inflate→program-flash bound, so the host just waits on the flash chip. 921600→1.5 M is
>    a real (~40 %) gain; 1.5 M→3 M buys almost nothing (you're flash-write-bound + hitting the
>    ~1.5–2 s fixed-overhead floor). So the useful ceiling for classic ESP32 is **~1.5 Mbaud**;
>    treat 2 M+ as a per-board bonus, not a target.
>
> **Exception — native-USB parts (S3/C3/C6, USB-Serial-JTAG / USB-CDC):** "baud" is fiction, data
> moves at USB Full-Speed (12 Mbps) regardless; the fastest flash path in the family, but out of
> scope here (P4 targets CP210x/CH340 classic ESP32). The durable wins remain **writing fewer
> bytes** (#12/#13) and **not re-handshaking** (#14), not the last increment of baud.

### 12. App-only flash *(core)*
On a normal edit, bootloader / partitions / boot_app0 are byte-identical. Hash them against a
per-device cache; when unchanged, **write only app@0x10000**, skipping the erase+write of the
other three regions. Seam: filter the four-offset set in `replay.rs:143-160`; subprocess path uses
the generalized `esptool_flash` with just `0x10000 firmware.bin`.

### 13. Delta / diff sector flash *(bigger swing)*
Cache the exact app image last flashed to a device; diff new vs cached at **4 KB flash-sector**
granularity and erase+write only changed sectors via targeted `write_flash <addr> <chunk>`.
**Safety + graceful degrade:** MD5 read-back a couple of device sectors first to confirm the
golden still matches (invalidate on mismatch / chip swap / any external flash); if the changed-byte
ratio exceeds ~60 % (e.g. an early code edit that shifts the whole layout), fall back to a full app
write. Tiny/data-only edits then collapse the transfer to near-zero.

### 14. Persistent flash session — `nff flash --watch` *(biggest swing)*
Every invocation re-pays the ~1.5–2 s reset→sync→stub handshake. A long-lived flasher that keeps
the port open with the stub resident and streams successive `write_flash` ops across edits
amortizes that to zero on the loop. Substrate: move the direct write to the **`espflash` Rust crate
in-process** (also drops ~0.4–0.5 s Python startup per flash and is the natural home for #13–14's
held connection + sector writes), keeping a `python -m esptool` fallback. Add a `--no-build`
(upload-only, reuse artifacts) entry so the write-step win is exercisable without a recompile. With
#12–14 combined, a small edit → **sub-second reflash**.

**Landmines.** Land in both languages; **do not push `nff` `main`** — it auto-publishes to PyPI +
GitHub Release (work on a branch). Baud-too-high is mitigated by probe-and-verify with step-down.
Delta correctness is gated on a read-back MD5 match with full-write as the always-safe fallback.
Native-replay wiring is ambiguous (one source says it ships v0.2.36+, the current tree shows
`pio.rs` still shelling to external `pio`) — `fast_upload` deliberately works via either the replay
seam or the subprocess path, so the win doesn't depend on resolving it.

**Verification (real hardware, COM10).** Wall-clock the write step across the matrix baud
{460800, 921600, 1.5 M} × mode {full, app-only, delta} × {cold, `--watch` hot}, confirming the
device **boots and streams the expected serial output** after each (a fast-but-wrong flash is a
regression). Confirm CH340 vs CP210x each converge to and persist their own best baud and that a
too-high rate steps down cleanly. Confirm a stale golden triggers a full write. `cargo test &&
cargo clippy -- -D warnings` + Python tests green.

---

## Suggested sequencing

1. **P0** — one afternoon, pure docs + hygiene. Removes the biggest sources of confusion.
   (Done except purging the tracked demo videos — see P0 #3.)
2. **P1 #4** ✅ — the offline mode. Changes the product's first impression the most.
3. **P2 #5–7** ✅ — `nff status`, `nff mcp` subcommands, better error messages.
4. **P3** ✅ — CI split, Windows smoke test, security doc.
5. **P4 #11–14** — flash write-step speed. Land #11 (baud) + #12 (app-only) first for the biggest
   low-risk win; #13 (delta) and #14 (`--watch` persistent session) are the bigger swings.
