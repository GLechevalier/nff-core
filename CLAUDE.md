# nff — Rust Architecture

> **Simulation note:** Wokwi simulation was split out of `nff` into the separate **nff-sim**
> package (`../nff-sim`). `nff` is hardware-only — compile, flash, monitor. Nothing about
> Wokwi (the `--sim` flag, `nff wokwi`, diagram.json, the wokwi config/board-chip metadata)
> remains in this repo.

## Status

> **CURRENT (2026-06): the Rust binary (`nff-rs/`) is the shipped product.** `pip install nff`
> now delivers a per-platform wheel containing the compiled Rust binary (maturin `bindings="bin"`,
> see `pyproject.toml`) — no Python runtime. The Rust port is at parity: all CLI commands, both
> build backends (PlatformIO default + arduino), and the full MCP server (HTTP + OAuth proxy +
> `/health` + background-daemon auto-start). The Python package under `nff/` remains as a
> reference/prototype kept in sync version-for-version — **land features in BOTH** so they never
> drift (prototype in Python if you like, but the shipped behavior is Rust).
>
> Parity note (2026-07-20): the local BYOT crash classifier (`nff diagnose` + the `diagnose`
> MCP tool), the platform OTA/fleet MCP tools, `nff fleet [--watch]`, and the full `nff ota`
> command set are ported — `nff-rs/nff/src/tools/{diagnose,ota_client}.rs`,
> `commands/{diagnose,fleet,ota}.rs`, plus 6 `#[tool]`s in `mcp_server.rs`. The panic-dump
> string fixtures in `tests/test_diagnose.py` are duplicated verbatim in the Rust
> `tools/diagnose.rs` test module as the behavioral parity oracle — keep both in sync.

The Rust port replaces the Python `nff` with a single compiled binary — no Python runtime for end
users, stronger types, better cross-platform packaging.

The MCP server is now native Rust (`nff-rs/nff/src/mcp_server.rs`, rmcp crate). Only
`nff test` still delegates to the Python package via subprocess.

**Adding a new MCP tool (current Python flow):** add an `async def` handler in
`nff/nff/mcp_server.py`, register it in both `_TOOLS` (with an `inputSchema`) and `_DISPATCH`.
Local hardware/toolchain logic lives in `nff/nff/tools/`. *(The Rust equivalent is a
`#[tool(...)]` method on `NffServer` in `nff-rs/nff/src/mcp_server.rs` — land tools in BOTH.)*

## Claude ↔ nff Handshake

### 1. Registration (`nff init`)

The live Python `nff init` calls `_register_mcp()` (`nff/commands/init.py`) which runs:

```
claude mcp add --scope user --transport http nff http://127.0.0.1:3010/mcp
```

This registers `nff mcp` as a user-scoped, streamable-HTTP MCP server. The transport and
URL are already included; the URL is passed positionally (there is no `--url` flag in this
form). A Bearer `--header` is **not** added here — local bench tools need no auth, and the
diagnosis tools carry their own token when they call the server.

> **Note:** the paused Rust port (`commands/init.rs`, `register_mcp_claude_code()`) was
> specced to register over stdio (`claude mcp add --scope user nff <nff_exe_path> mcp`) and
> still needs to be brought to parity with the Python HTTP form above when that work resumes.

### 2. Transport

`nff mcp` starts a **streamable HTTP MCP server** on `http://127.0.0.1:3010/mcp`
(default; override with `--host` / `--port`). All MCP messages — initialize, tools/list,
tools/call — are HTTP POST requests to that path.

### 3. Bearer authentication (opt-in, OFF by default)

**The `/mcp` Bearer gate is OFF by default.** nff is a single-user, localhost-only bench tool,
so out of the box `/mcp` is open: no token, no OAuth handshake, no "needs authentication". The
server still binds to `127.0.0.1` only, so it is not network-reachable — but any local process
can call the tools.

**Requiring auth (`NFF_MCP_REQUIRE_AUTH`):** set `NFF_MCP_REQUIRE_AUTH=1` (also accepts
`true`/`yes`/`on`) in the environment the server is launched from to turn the gate back ON. When
set, every request to `/mcp` must carry `Authorization: Bearer <token>` validated against the
opaque MCP token (`config.mcp.access_token`) — or, for back-compat, the legacy
`config.diagnosis.access_token` — in `~/.nff/config.json`. A missing or wrong token then returns
HTTP 401 and Claude surfaces an "Unauthorized" error, driving the OAuth browser login. (`/health`
is always unauthenticated, used only for liveness probing.) Gating the tools is the reason the
server is HTTP, not stdio: stdio can't gate them.

Implemented in both `bearer_auth` (Rust, `mcp_server.rs`) and `_NffASGI` (Python, `mcp_server.py`);
the server's advertised `instructions` string reflects whichever mode is active.

**One-time bootstrap order:**

```
1. nff init              # signs you in (browser login, required), detects board,
                         #   writes config, calls _register_mcp()
                         #   (claude mcp add --scope user --transport http nff http://127.0.0.1:3010/mcp),
                         #   then starts the MCP server in the background (daemon.start_background)
2. Restart Claude Code   # Claude picks up the registration and connects to the running server;
                         #   the OAuth proxy fast-path reuses the stored token (no second login)
```

> The background server is started once by `nff init` and runs until reboot. After a reboot,
> `nff mcp` (or re-running `nff init`) brings it back; `nff doctor` reports if it's down.

### 4. Tool call flow

```
Claude Code
    │
    │  HTTP POST  http://127.0.0.1:3010/mcp
    │  Authorization: Bearer <access_token>
    │  Body: MCP tools/call { "name": "...", "arguments": {...} }
    ▼
nff mcp server  (bearer_auth validates token vs ~/.nff/config.json)
    │
    ├──► local tools
    │       list_devices, compile, flash, serial_read, serial_write,
    │       reset_device, get_device_info, diagnose
    │       — operate on local hardware / toolchain; no further auth
    │       — diagnose classifies an ESP32 crash entirely locally (nff/tools/diagnose.py:
    │         parser + fault taxonomy + rule classifier) and returns STRUCTURED FACTS
    │         ONLY; the calling model writes the narrative (BYOT — zero platform LLM cost)
    │
    ├──► diagnosis tools
    │       authenticate, auth_status, auth_logout, repair
    │       — POST to config.diagnosis.server_url (/api/auth/*, /api/repair)
    │       — repair auto-refreshes the access_token on 401 using stored refresh_token;
    │         clears tokens and returns ERROR: session expired if refresh also fails
    │       — repair = platform diagnosis with server-side ELF symbolization; diagnose
    │         (above) is the free local path
    │
    └──► platform OTA / fleet tools (require login)
            ota_deploy, ota_status, ota_deployments, ota_devices, fleet_status
            — thin wrappers over nff/tools/ota_client.py → {server_url}/api/ota/*;
              blocking requests offloaded via run_in_executor; auth errors are rewritten
              to point at the `authenticate` tool
            — fleet_status returns ota_client.fleet_snapshot(): devices merged with the
              latest deployment's per-device jobs (CLI live view: `nff fleet --watch`)
```

### 5. Response conventions

| Result type | Format |
|---|---|
| Success (scalar) | `"OK: …"` string |
| Failure | `"ERROR: …"` string |
| Structured data | JSON string (`list_devices`, `get_device_info`, `compile`, `repair`) |

Claude can branch on the `OK:` / `ERROR:` prefix without parsing JSON for scalar results.

## Migration Scope

**IN (rewrite in Rust):**
- CLI entry point and all command definitions
- `nff/nff/config.py` → `tools/config.rs`
- `nff/nff/tools/serial.py` → `tools/serial.rs`
- `nff/nff/tools/boards.py` → `tools/boards.rs`
- `nff/nff/tools/toolchain.py` → `tools/toolchain.rs`
- `nff/nff/tools/installer.py` → `tools/installer.rs`
- All commands: `flash`, `init`, `monitor`, `doctor`, `clean`, `connect`, `ota`, `install-deps`
- `commands/mcp.rs` (calls `mcp_server::run()` — native Rust MCP server, no Python)

**OUT (keep in Python):**
- `nff test` command only — delegates to `python -m nff test` via subprocess

## Rust Project Layout

Create at `nff/nff-rs/` (sibling to `nff/nff/`):

```
nff/nff-rs/
├── Cargo.toml              (workspace root)
└── nff/
    ├── Cargo.toml
    └── src/
        ├── main.rs
        ├── cli.rs
        ├── commands/
        │   ├── mod.rs
        │   ├── flash.rs        (replaces commands/flash.py)
        │   ├── init.rs         (replaces commands/init.py)
        │   ├── monitor.rs      (replaces commands/monitor.py)
        │   ├── doctor.rs       (replaces commands/doctor.py)
        │   ├── clean.rs        (replaces commands/clean.py)
        │   ├── connect.rs      (replaces commands/connect.py)
        │   ├── ota.rs          (replaces commands/ota.py)
        │   ├── install_deps.rs (replaces install-deps command in cli.py)
        │   └── mcp.rs          (spawns Python MCP server)
        └── tools/
            ├── mod.rs
            ├── config.rs       (replaces config.py)
            ├── serial.rs       (replaces tools/serial.py)
            ├── boards.rs       (replaces tools/boards.py)
            ├── toolchain.rs    (replaces tools/toolchain.py)
            └── installer.rs    (replaces tools/installer.py)
```

Workspace `Cargo.toml`:
```toml
[workspace]
members = ["nff"]
resolver = "2"
```

## Cargo.toml Dependencies

```toml
[package]
name = "nff"
version = "0.2.16"   # keep in sync with pyproject.toml
edition = "2021"

[[bin]]
name = "nff"
path = "src/main.rs"

[dependencies]
clap        = { version = "4", features = ["derive"] }
serialport  = "4"
rusb        = "0.9"
serde       = { version = "1", features = ["derive"] }
serde_json  = "1"
dirs        = "5"
indicatif   = "0.17"
console     = "0.15"
reqwest     = { version = "0.12", features = ["blocking"] }
zip         = "2"
flate2      = "1"
tar         = "0.4"
which       = "6"
thiserror   = "1"
anyhow      = "1"

[target.'cfg(windows)'.dependencies]
winreg      = "0.52"
```

## CLI Command Hierarchy — src/cli.rs

Use `clap` derive. Mirror every flag and option from the Python Click commands exactly.

```rust
use clap::{Parser, Subcommand, Args};

#[derive(Parser)]
#[command(name = "nff", version)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Commands,
}

#[derive(Subcommand)]
pub enum Commands {
    Init(InitArgs),
    Flash(FlashArgs),
    Monitor(MonitorArgs),
    Doctor,
    Clean,
    Test,
    Connect,
    Ota,
    #[command(name = "install-deps")]
    InstallDeps(InstallDepsArgs),
    Mcp,
}

#[derive(Args)]
pub struct InitArgs {
    #[arg(long, value_name = "PORT")]
    pub port: Option<String>,
    #[arg(long, default_value = "9600")]
    pub baud: u32,
    #[arg(long)]
    pub force: bool,
}

#[derive(Args)]
pub struct FlashArgs {
    pub file: std::path::PathBuf,   // required, must exist
    #[arg(long, value_name = "FQBN")]
    pub board: Option<String>,
    #[arg(long, value_name = "PORT")]
    pub port: Option<String>,
    #[arg(long)]
    pub baud: Option<u32>,
    #[arg(long)]
    pub manual_reset: bool,
}

#[derive(Args)]
pub struct MonitorArgs {
    #[arg(long, value_name = "PORT")]
    pub port: Option<String>,
    #[arg(long)]
    pub baud: Option<u32>,
    #[arg(long, value_name = "SECONDS")]
    pub timeout: Option<f64>,
}

#[derive(Args)]
pub struct InstallDepsArgs {
    #[arg(long)]
    pub force: bool,
}
```

## Module-by-Module Migration

### tools/config.rs (from config.py)

Config path: `dirs::home_dir().unwrap() / ".nff" / "config.json"`

```rust
#[derive(Serialize, Deserialize)]
pub struct Config {
    pub version: String,
    pub default_device: DeviceConfig,
}

#[derive(Serialize, Deserialize, Default)]
pub struct DeviceConfig {
    pub port: Option<String>,
    pub board: Option<String>,
    pub fqbn: Option<String>,
    pub baud: u32,   // default 9600
}
```

Atomic write pattern:
```rust
let tmp = config_path.with_extension("json.tmp");
fs::write(&tmp, serde_json::to_string_pretty(&config)?)?;
fs::rename(tmp, config_path)?;
```

Public API to implement (mirrors config.py):
- `load() -> Result<Config>`
- `save(config: &Config) -> Result<()>`
- `get_default_device() -> Result<DeviceConfig>`
- `set_default_device(port, board, fqbn, baud) -> Result<()>`
- `exists() -> bool`

### tools/boards.rs (from tools/boards.py)

`serialport::available_ports()` returns `Vec<SerialPortInfo>` where each entry has an optional
`SerialPortType::UsbPort(UsbPortInfo { vid: u16, pid: u16, .. })`.

```rust
pub const BOARD_MAP: &[(u16, u16, &str, &str)] = &[
    (0x2341, 0x0043, "Arduino Uno",       "arduino:avr:uno"),
    (0x2341, 0x0010, "Arduino Mega 2560", "arduino:avr:mega"),
    (0x2341, 0x0036, "Arduino Leonardo",  "arduino:avr:leonardo"),
    (0x2341, 0x0058, "Arduino Nano",      "arduino:avr:nano"),
    (0x10c4, 0xea60, "ESP32 (CP210x)",    "esp32:esp32:esp32"),
    (0x1a86, 0x7523, "ESP32 (CH340)",     "esp32:esp32:esp32"),
    (0x0403, 0x6001, "ESP8266 (FTDI)",    "esp8266:esp8266:generic"),
];

pub struct DetectedDevice {
    pub port: String,
    pub board: String,
    pub fqbn: String,
    pub vendor_id: String,   // 4-char zero-padded hex, lowercase
    pub product_id: String,
}

pub fn list_devices() -> Vec<DetectedDevice> { ... }
pub fn find_device(port: Option<&str>) -> Option<DetectedDevice> { ... }
```

### tools/serial.rs (from tools/serial.py)

```rust
use serialport::SerialPort;
use std::time::{Duration, Instant};
use std::io::{BufRead, BufReader, Write};

// Open with 100ms read timeout (check deadline per iteration, like Python)
fn open(port: &str, baud: u32) -> Result<Box<dyn SerialPort>, SerialError> {
    serialport::new(port, baud)
        .timeout(Duration::from_millis(100))
        .open()
        .map_err(SerialError::Open)
}

pub fn serial_read(duration_ms: u64, port: Option<&str>, baud: Option<u32>) -> String { ... }
// Reads until deadline, returns captured UTF-8 string or "ERROR: ..."

pub fn serial_write(data: &str, port: Option<&str>, baud: Option<u32>) -> String { ... }
// Appends \n if missing, returns "OK: wrote N byte(s) to <port>" or "ERROR: ..."

pub fn reset_device(port: Option<&str>) -> String { ... }
// port.write_data_terminal_ready(false) + sleep 50ms + write_data_terminal_ready(true)
// Returns "OK: reset <port> via DTR toggle" or "ERROR: ..."

pub fn stream_lines(port: Option<&str>, baud: Option<u32>, timeout_s: Option<f64>)
    -> impl Iterator<Item = String> { ... }
// BufReader::new(serial_port).lines(), deadline-terminated

fn resolve_port(opt: Option<&str>) -> Result<String, SerialError> { ... }
fn resolve_baud(opt: Option<u32>) -> Result<u32, SerialError> { ... }
```

### tools/toolchain.rs (from tools/toolchain.py)

Sketch directory: `std::env::temp_dir() / "nff_sketch"`

```rust
pub struct RunResult {
    pub success: bool,
    pub stdout: String,
    pub stderr: String,
    pub returncode: i32,
}
impl RunResult {
    pub fn output(&self) -> String { /* concat stdout + stderr, trimmed */ }
}

// Tool discovery
pub fn find_arduino_cli() -> Option<PathBuf> {
    // 1. which::which("arduino-cli")
    // 2. Windows: %LOCALAPPDATA%\Programs\arduino-cli\arduino-cli.exe
    // 3. Unix: ~/.local/bin/arduino-cli
}

pub fn find_esptool() -> Option<PathBuf> {
    // which::which("esptool.py") then which::which("esptool")
}

// Sketch management
pub fn write_sketch(code: &str, sketch_dir: Option<&Path>) -> Result<PathBuf, ToolchainError> {
    // Writes code to <sketch_dir>/<sketch_dir.name>.ino
}

pub fn elf_path_for(sketch_dir: &Path, fqbn: &str) -> PathBuf {
    // sketch_dir/build/<fqbn.replace(':','.')>/<sketch_name>.elf
}

// Subprocess wrappers — all use std::process::Command with 120s timeout
pub fn compile_sketch(sketch_dir: &Path, fqbn: &str) -> Result<RunResult, ToolchainError> {
    // arduino-cli compile --fqbn <fqbn> --build-path <build_path> <sketch_dir>
}

pub fn upload_sketch(sketch_dir: &Path, fqbn: &str, port: &str) -> Result<RunResult, ToolchainError> {
    // arduino-cli upload --fqbn <fqbn> --port <port> <sketch_dir>
}

// Streaming variants for progress display
pub struct ProcessStream { cmd: Vec<String>, pub returncode: Option<i32> }
impl Iterator for ProcessStream { type Item = String; ... }
// Uses Command::stdout(Stdio::piped()) + BufReader::lines()

pub fn stream_compile(sketch_dir: &Path, fqbn: &str) -> ProcessStream { ... }
pub fn stream_upload(sketch_dir: &Path, fqbn: &str, port: &str) -> ProcessStream { ... }

// Combined flash: write_sketch → compile → upload
pub fn flash(code: &str, fqbn: &str, port: &str) -> String {
    // Returns "OK: flash complete\n---compile---\n...\n---upload---\n..."
    // or "ERROR: ..."
}

// esptool / espflash wrapper
pub fn esptool_flash(port: &str, bin_path: &Path, baud: u32, address: &str) -> String {
    // Prefers subprocess espflash/esptool, falls back to "python -m esptool"
    // Default address "0x0"
}
```

### tools/installer.rs (from tools/installer.py)

Arduino CLI download base: `https://downloads.arduino.cc/arduino-cli/arduino-cli_latest`

```rust
fn asset_url() -> (&'static str, &'static str) {
    // Returns (url, extension) based on std::env::consts::{OS, ARCH}
    // Windows x86_64 → "_Windows_64bit.zip"
    // Windows other  → "_Windows_32bit.zip"
    // macOS arm64    → "_macOS_ARM64.tar.gz"
    // macOS other    → "_macOS_64bit.tar.gz"
    // Linux x86_64   → "_Linux_64bit.tar.gz"
    // Linux aarch64  → "_Linux_ARM64.tar.gz"
    // Linux arm      → "_Linux_ARMv7.tar.gz"
}

fn install_dir() -> PathBuf {
    // Windows: %LOCALAPPDATA%\Programs\arduino-cli
    // Unix: ~/.local/bin
}

pub fn install(force: bool) -> Result<PathBuf> {
    // 1. reqwest::blocking::get(url)?.bytes()? → write to tempfile
    // 2. Extract binary: zip (ZipArchive) or tar.gz (flate2 GzDecoder + tar::Archive)
    // 3. Unix: set executable bit (std::os::unix::fs::PermissionsExt)
    // 4. ensure_on_path(install_dir)
    // Returns path to extracted executable
}

fn ensure_on_path(dir: &Path) {
    // Windows: winreg read/write HKCU\Environment PATH (semicolon-separated)
    // Unix: append 'export PATH="$PATH:/dir"' to first of ~/.bashrc, ~/.zshrc, ~/.profile
    // Both: update std::env::var("PATH") for current process
}

pub fn verify(exe: &Path) -> bool {
    // Command::new(exe).arg("version").output().map(|o| o.status.success()).unwrap_or(false)
}
```

### mcp_server.rs

`nff mcp` starts the MCP server natively in Rust via the `rmcp` crate on stdio. No Python needed.

```rust
pub async fn run() -> anyhow::Result<()> {
    let service = NffServer.serve(stdio()).await?;
    service.waiting().await?;
    Ok(())
}
```

Add new tools as `async fn` methods on `NffServer` with `#[tool(description = "...")]`.

### commands/flash.rs (from commands/flash.py)

```
1. Resolve FQBN: --board arg → config::get_default_device().fqbn → error
2. Resolve port: --port arg → boards::find_device() → config default → error
3. If --manual-reset: print prompt, wait for Enter
4. toolchain::stream_compile(sketch_dir, fqbn) → print each line (indicatif spinner)
5. toolchain::stream_upload(sketch_dir, fqbn, port) → print each line
6. Check ProcessStream.returncode; exit non-zero on failure
```

### commands/init.rs (from commands/init.py)

```
1. boards::list_devices() — list connected devices
2. User picks port (or auto-detect if only one)
3. config::set_default_device(port, board, fqbn, baud)
4. installer::install(force=false) if arduino-cli not found
5. Register MCP with Claude Code:
   Command::new("claude").args(["mcp", "add", "--scope", "user",
       "nff", current_exe_path, "mcp"])
6. Print success summary
```

### commands/monitor.rs (from commands/monitor.py)

```rust
pub fn run(args: &MonitorArgs) -> anyhow::Result<()> {
    let port = serial::resolve_port(args.port.as_deref())?;
    let baud = serial::resolve_baud(args.baud)?;
    for line in serial::stream_lines(Some(&port), Some(baud), args.timeout) {
        println!("{line}");
    }
    Ok(())
}
```

## Error Handling Convention

Domain errors use `thiserror`:
```rust
#[derive(thiserror::Error, Debug)]
pub enum SerialError { ... }

#[derive(thiserror::Error, Debug)]
pub enum ToolchainError { ... }

#[derive(thiserror::Error, Debug)]
pub enum ConfigError { ... }
```

Command-level functions return `anyhow::Result<()>` and use `?` for propagation.

MCP-facing tool functions (called via Python MCP server through subprocess) must return
`"OK: ..."` or `"ERROR: ..."` strings — same protocol as Python, so the existing
`mcp_server.py` can call the Rust binary without changes.

## Completed Migration Phases

All phases complete. Run `cargo test && cargo clippy -- -D warnings` to verify.

1. ~~Bootstrap~~ — workspace Cargo.toml, all deps, builds clean
2. ~~config.rs~~ — `~/.nff/config.json` read/write round-trip
3. ~~boards.rs~~ — USB enumeration via `serialport`
4. ~~serial.rs~~ — read/write/reset via `serialport`
5. ~~toolchain.rs~~ — arduino-cli subprocess wrappers
6. ~~installer.rs~~ — download + extract (zip on Windows, tar.gz on Unix)
7. ~~cli.rs + main.rs~~ — clap, all commands wired
8. ~~commands~~ — `init`, `flash`, `monitor`, `doctor`, `clean`, `install_deps`, `mcp`, `connect`, `ota`
9. ~~mcp_server.rs~~ — native Rust MCP server via `rmcp` (no Python subprocess)

## Verification

`cargo test && cargo clippy -- -D warnings`

End-to-end smoke test:
- [ ] `./nff --version` prints version string matching `Cargo.toml`
- [ ] `./nff init` creates `~/.nff/config.json` with correct schema
- [ ] `./nff flash <sketch.ino>` compiles and uploads
- [ ] `./nff monitor --timeout 5` streams serial for 5 s then exits cleanly
- [ ] `./nff install-deps` downloads and installs arduino-cli
- [ ] `./nff mcp` starts native Rust MCP server on stdio; `list_devices` tool returns data from Claude Code
- [ ] `cargo build --release` produces a single binary that runs on a clean machine without Python

## Key Source Files (reference during migration)

| Rust target | Python source |
|---|---|
| `tools/config.rs` | `nff/nff/config.py` |
| `tools/boards.rs` | `nff/nff/tools/boards.py` |
| `tools/serial.rs` | `nff/nff/tools/serial.py` |
| `tools/toolchain.rs` | `nff/nff/tools/toolchain.py` |
| `tools/installer.rs` | `nff/nff/tools/installer.py` |
| `commands/flash.rs` | `nff/nff/commands/flash.py` |
| `commands/init.rs` | `nff/nff/commands/init.py` |
| `commands/monitor.rs` | `nff/nff/commands/monitor.py` |
| `cli.rs` | `nff/nff/cli.py` |
| `mcp_server.rs` | Native Rust MCP server (rmcp) — supersedes `nff/nff/mcp_server.py` |
