use clap::{Args, Parser, Subcommand};
use std::path::PathBuf;

#[derive(Parser)]
#[command(
    name = "nff",
    version,
    about = "nff — Claude Code IoT Bridge\n\nConnects Claude Code to physical hardware devices via USB.\nRun `nff init` to get started."
)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Commands,
}

#[derive(Subcommand)]
pub enum Commands {
    Init(InitArgs),
    Compile(CompileArgs),
    Flash(FlashArgs),
    Monitor(MonitorArgs),
    Doctor,
    Clean,
    Test,
    Connect,
    Ota,
    #[command(name = "install-deps")]
    InstallDeps(InstallDepsArgs),
    Mcp(McpArgs),
    #[command(about = "Manage authentication; bare `nff auth` signs in (browser OAuth).")]
    Auth(AuthCommand),
    #[command(about = "Sign out and clear saved credentials (alias for `auth logout`).")]
    Deauth(AuthLogoutArgs),
    Repair(RepairArgs),
    Provision(ProvisionCommand),
    Agent(AgentArgs),
    Pi(PiCommand),
    #[command(about = "Live on-chip debugging (OpenOCD + GDB); bare `nff debug` starts a session.")]
    Debug(DebugCommand),
}

#[derive(Args)]
pub struct InitArgs {
    #[arg(
        long,
        value_name = "PORT",
        help = "Serial port to use; skips auto-detection."
    )]
    pub port: Option<String>,
    #[arg(long, default_value = "9600", help = "Baud rate stored in config.")]
    pub baud: u32,
    #[arg(long, help = "Overwrite an existing config without prompting.")]
    pub force: bool,
    #[arg(
        long,
        value_name = "BACKEND",
        help = "Build backend: platformio (default, board-universal) or arduino."
    )]
    pub backend: Option<String>,
    #[arg(
        long,
        help = "Skip cloud sign-in; configure for local build/flash/monitor/debug only."
    )]
    pub offline: bool,
}

#[derive(Args)]
pub struct CompileArgs {
    #[arg(
        value_name = "FILE",
        help = "Path to .ino file or sketch directory. No board connection needed."
    )]
    pub file: PathBuf,
    #[arg(
        long,
        value_name = "FQBN",
        help = "Board FQBN, e.g. arduino:avr:uno. Falls back to config."
    )]
    pub board: Option<String>,
    #[arg(long, help = "Emit the raw JSON result instead of a human summary.")]
    pub json: bool,
}

#[derive(Args)]
pub struct FlashArgs {
    #[arg(value_name = "FILE", help = "Path to .ino file or sketch directory.")]
    pub file: PathBuf,
    #[arg(
        long,
        value_name = "FQBN",
        help = "Board FQBN, e.g. arduino:avr:uno. Falls back to config."
    )]
    pub board: Option<String>,
    #[arg(
        long,
        value_name = "PORT",
        help = "Serial port, e.g. COM3. Falls back to config."
    )]
    pub port: Option<String>,
    #[arg(long, help = "Baud rate (stored in config, not used by arduino-cli).")]
    pub baud: Option<u32>,
    #[arg(long, help = "Pause before upload — use when auto-reset is broken.")]
    pub manual_reset: bool,
}

#[derive(Args)]
pub struct MonitorArgs {
    #[arg(
        long,
        value_name = "PORT",
        help = "Serial port, e.g. COM3 or /dev/ttyUSB0. Falls back to config."
    )]
    pub port: Option<String>,
    #[arg(long, help = "Baud rate. Falls back to config default (9600).")]
    pub baud: Option<u32>,
    #[arg(
        long,
        value_name = "SECONDS",
        help = "Stop after this many seconds instead of running indefinitely."
    )]
    pub timeout: Option<f64>,
}

#[derive(Args)]
pub struct InstallDepsArgs {
    #[arg(long, help = "Reinstall even if already present.")]
    pub force: bool,
}

// ── mcp ─────────────────────────────────────────────────────────────────────

#[derive(Args)]
pub struct McpArgs {
    #[arg(
        long,
        default_value = "127.0.0.1",
        value_name = "ADDR",
        help = "Address to bind the HTTP server to."
    )]
    pub host: String,
    #[arg(
        long,
        default_value = "3010",
        value_name = "PORT",
        help = "Port to listen on."
    )]
    pub port: u16,
}

// ── auth ────────────────────────────────────────────────────────────────────

#[derive(Args)]
pub struct AuthCommand {
    /// Optional: bare `nff auth` (no subcommand) runs the login flow.
    #[command(subcommand)]
    pub sub: Option<AuthSubcommands>,
}

#[derive(Subcommand)]
pub enum AuthSubcommands {
    #[command(about = "Sign in to the nff diagnosis service.")]
    Login(AuthLoginArgs),
    #[command(about = "Sign out and clear saved credentials.")]
    Logout(AuthLogoutArgs),
    #[command(about = "Show current authentication status.")]
    Status,
}

#[derive(Args, Default)]
pub struct AuthLoginArgs {
    #[arg(
        long,
        value_name = "EMAIL",
        help = "Email address (headless/CI login; omit to use browser)."
    )]
    pub email: Option<String>,
    #[arg(
        long,
        value_name = "PASSWORD",
        help = "Password (headless/CI login; omit to use browser)."
    )]
    pub password: Option<String>,
    #[arg(
        long,
        value_name = "URL",
        help = "Diagnosis server URL. Defaults to config value (https://nanoforgeflow.com)."
    )]
    pub server: Option<String>,
}

#[derive(Args)]
pub struct AuthLogoutArgs {
    #[arg(
        long,
        value_name = "URL",
        help = "Diagnosis server URL. Defaults to config value."
    )]
    pub server: Option<String>,
}

// ── repair ──────────────────────────────────────────────────────────────────

// ── provision ─────────────────────────────────────────────────────────────

#[derive(Args)]
pub struct ProvisionCommand {
    #[command(subcommand)]
    pub sub: ProvisionSubcommands,
}

#[derive(Subcommand)]
pub enum ProvisionSubcommands {
    #[command(about = "Create one shared batch bootstrap credential and write credentials.h.")]
    Batch(BatchArgs),
}

#[derive(Args)]
pub struct BatchArgs {
    #[arg(
        long = "project",
        value_name = "UUID",
        help = "Target project id (uuid)."
    )]
    pub project: String,
    #[arg(
        long,
        value_name = "N",
        help = "Expected batch size; the server rejects enrollments beyond this as a cloned-credential anomaly. Omit for no hard quota."
    )]
    pub count: Option<u32>,
    #[arg(
        long,
        value_name = "FILE",
        default_value = "credentials.h",
        help = "Where to write the shared bootstrap credentials.h."
    )]
    pub out: PathBuf,
    #[arg(
        long = "fleet-url",
        value_name = "URL",
        help = "nff-fleet base URL (or env NFF_FLEET_URL)."
    )]
    pub fleet_url: Option<String>,
    #[arg(
        long,
        value_name = "SECRET",
        help = "X-Fleet-Secret (or env NFF_FLEET_SECRET)."
    )]
    pub secret: Option<String>,
}

// ── agent ─────────────────────────────────────────────────────────────────

#[derive(Args)]
pub struct AgentArgs {
    #[arg(value_name = "PROMPT", help = "What you want the cloud agent to do.")]
    pub prompt: String,
    #[arg(
        long,
        value_name = "UUID",
        help = "Project id override (default: resolved from your login)."
    )]
    pub project: Option<String>,
    #[arg(
        long = "agent-url",
        value_name = "URL",
        help = "Cloud agent base URL. Defaults to config agent.server_url."
    )]
    pub agent_url: Option<String>,
    #[arg(
        long = "mcp-url",
        value_name = "URL",
        help = "This bench's local nff MCP URL the agent calls back into."
    )]
    pub mcp_url: Option<String>,
    #[arg(
        long = "no-stream",
        help = "Suppress live output; print only the final reply."
    )]
    pub no_stream: bool,
}

// ── pi ──────────────────────────────────────────────────────────────────────

#[derive(Args)]
pub struct PiCommand {
    #[command(subcommand)]
    pub sub: PiSubcommands,
}

#[derive(Subcommand)]
pub enum PiSubcommands {
    #[command(about = "Test whether a Raspberry Pi is connected and SSH-ready.")]
    Probe(PiProbeArgs),
}

#[derive(Args)]
pub struct PiProbeArgs {
    #[arg(
        long,
        value_name = "IP",
        help = "Probe a specific IP/hostname directly."
    )]
    pub host: Option<String>,
    #[arg(
        long,
        help = "Also TCP/22-sweep direct-link /24 subnets (e.g. ICS 192.168.137.x)."
    )]
    pub sweep: bool,
    #[arg(long, help = "Emit machine-readable JSON.")]
    pub json: bool,
}

// ── debug ─────────────────────────────────────────────────────────────────

#[derive(Args)]
pub struct DebugCommand {
    /// Optional: bare `nff debug` (no subcommand) starts a session.
    #[command(subcommand)]
    pub sub: Option<DebugSubcommands>,
    #[arg(long, value_name = "PATH", help = "Path to a built .elf (defaults to the last build).")]
    pub elf: Option<String>,
    #[arg(long, value_name = "FQBN", help = "Board id/FQBN to derive the chip family.")]
    pub board: Option<String>,
    #[arg(long, value_name = "CFG", help = "OpenOCD interface cfg for an external JTAG probe.")]
    pub interface: Option<String>,
}

#[derive(Subcommand)]
pub enum DebugSubcommands {
    #[command(about = "Report the OpenOCD/GDB binaries, chip, and ELF nff would use — no hardware.")]
    Check(DebugCheckArgs),
    #[command(about = "Start a debug session and enter an interactive prompt.")]
    Start(DebugStartArgs),
}

#[derive(Args, Default)]
pub struct DebugCheckArgs {
    #[arg(long, value_name = "FQBN", help = "Board id/FQBN to derive the chip family.")]
    pub board: Option<String>,
}

#[derive(Args, Default)]
pub struct DebugStartArgs {
    #[arg(long, value_name = "PATH", help = "Path to a built .elf (defaults to the last build).")]
    pub elf: Option<String>,
    #[arg(long, value_name = "FQBN", help = "Board id/FQBN to derive the chip family.")]
    pub board: Option<String>,
    #[arg(long, value_name = "CFG", help = "OpenOCD interface cfg for an external JTAG probe.")]
    pub interface: Option<String>,
}

#[derive(Args)]
pub struct RepairArgs {
    #[arg(
        long,
        value_name = "TEXT",
        help = "Raw serial/crash output to diagnose. Reads from device if omitted."
    )]
    pub serial: Option<String>,
    #[arg(
        long,
        value_name = "MS",
        help = "How long to capture serial output from the device (default: 5000 ms)."
    )]
    pub capture_ms: Option<u32>,
    #[arg(
        long,
        value_name = "PORT",
        help = "Serial port to read from. Falls back to config."
    )]
    pub port: Option<String>,
    #[arg(long, help = "Baud rate for serial capture. Falls back to config.")]
    pub baud: Option<u32>,
    #[arg(
        long,
        value_name = "ID",
        help = "Firmware build ID (hex hash of ELF). Enables symbol resolution when provided."
    )]
    pub build_id: Option<String>,
    #[arg(
        long,
        value_name = "FQBN",
        help = "Board FQBN hint for the diagnosis server."
    )]
    pub board: Option<String>,
    #[arg(
        long,
        value_name = "URL",
        help = "Diagnosis server URL. Defaults to config value."
    )]
    pub server: Option<String>,
}
