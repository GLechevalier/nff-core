//! Clap CLI surface mirroring PlatformIO Core's top-level commands.
//!
//! At M0 each command carries a free-form trailing-args bag so the `pio-rs`
//! binary parses any invocation the vendored pytest suite throws at it; the
//! per-command flags get modelled precisely as each milestone ports a command.

use clap::{Args, Parser, Subcommand};

/// Top-level `pio-rs` parser. Global options mirror PlatformIO's root command.
#[derive(Parser, Debug)]
#[command(
    name = "pio-rs",
    bin_name = "pio-rs",
    about = "PlatformIO Core (Rust port)",
    disable_help_subcommand = true,
    // We print PlatformIO's exact version string ourselves (see `Cli::version`),
    // so disable clap's auto `--version` and allow a bare `pio-rs --version`.
    disable_version_flag = true,
    subcommand_required = false,
    arg_required_else_help = false
)]
pub struct Cli {
    /// Print the PlatformIO Core version and exit.
    #[arg(short = 'V', long)]
    pub version: bool,

    /// Caller ID (service) — accepted for PlatformIO compatibility.
    #[arg(short = 'c', long, global = true)]
    pub caller: Option<String>,

    /// Do not print ANSI control characters.
    #[arg(long, global = true)]
    pub no_ansi: bool,

    #[command(subcommand)]
    pub command: Option<Command>,
}

/// Catch-all positional args for a not-yet-modelled command. `trailing_var_arg`
/// + `allow_hyphen_values` lets a stub accept the full upstream flag set.
#[derive(Args, Debug, Default, Clone)]
pub struct PassthroughArgs {
    #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
    pub args: Vec<String>,
}

/// Every PlatformIO top-level command (`pio <cmd>`), lazy-loaded upstream from
/// `platformio/*/cli.py` via `PlatformioCLI`.
#[derive(Subcommand, Debug)]
pub enum Command {
    /// Manage PlatformIO account.
    Account(PassthroughArgs),
    /// Board Explorer.
    Boards(BoardsArgs),
    /// Static Code Analysis.
    Check(PassthroughArgs),
    /// Continuous Integration helper.
    Ci(PassthroughArgs),
    /// Unit Debugging.
    Debug(PassthroughArgs),
    /// Device manager & serial/socket monitor.
    Device(DeviceArgs),
    /// GUI to manage PlatformIO.
    Home(PassthroughArgs),
    /// Library manager (deprecated upstream alias of `pkg`).
    Lib(PassthroughArgs),
    /// Manage organizations.
    Org(PassthroughArgs),
    /// Unified package manager.
    Pkg(PassthroughArgs),
    /// Platform manager.
    Platform(PassthroughArgs),
    /// Project manager.
    Project(ProjectArgs),
    /// Remote development.
    Remote(PassthroughArgs),
    /// Run project targets (build, upload, clean, etc.).
    Run(PassthroughArgs),
    /// Manage PlatformIO settings.
    Settings(SettingsArgs),
    /// Miscellaneous system commands.
    System(SystemArgs),
    /// Manage teams.
    Team(PassthroughArgs),
    /// Unit Testing.
    Test(PassthroughArgs),
    /// Update installed platforms, packages and libraries.
    Update(PassthroughArgs),
    /// Upgrade PlatformIO Core to the latest version.
    Upgrade(PassthroughArgs),
}

// --- M3 command surfaces ----------------------------------------------------

/// `pio boards [QUERY] [--installed] [--json-output]`.
#[derive(Args, Debug, Default, Clone)]
pub struct BoardsArgs {
    /// Filter boards by a substring of id/name/mcu/vendor/platform/frameworks.
    pub query: Option<String>,
    /// List only boards from installed platforms (skip the registry).
    #[arg(long)]
    pub installed: bool,
    /// Emit a single-line JSON array of board briefs.
    #[arg(long = "json-output")]
    pub json_output: bool,
}

/// `pio settings <get|set|reset>`.
#[derive(Args, Debug, Clone)]
pub struct SettingsArgs {
    #[command(subcommand)]
    pub action: SettingsAction,
}

#[derive(Subcommand, Debug, Clone)]
pub enum SettingsAction {
    /// Get existing setting(s).
    Get {
        /// Restrict output to a single setting.
        name: Option<String>,
    },
    /// Set a new value for a setting.
    Set { name: String, value: String },
    /// Reset settings to their defaults.
    Reset,
}

/// `pio system <info|prune|completion>`.
#[derive(Args, Debug, Clone)]
pub struct SystemArgs {
    #[command(subcommand)]
    pub action: SystemAction,
}

#[derive(Subcommand, Debug, Clone)]
pub enum SystemAction {
    /// Display system-wide information.
    Info {
        #[arg(long = "json-output")]
        json_output: bool,
    },
    /// Remove unused data (deferred to a later milestone).
    Prune(PassthroughArgs),
    /// Install/uninstall shell completion (deferred to a later milestone).
    Completion(PassthroughArgs),
}

/// `pio device <list|monitor>`.
#[derive(Args, Debug, Clone)]
pub struct DeviceArgs {
    #[command(subcommand)]
    pub action: DeviceAction,
}

#[derive(Subcommand, Debug, Clone)]
pub enum DeviceAction {
    /// List devices.
    List {
        #[arg(long)]
        serial: bool,
        #[arg(long)]
        logical: bool,
        #[arg(long)]
        mdns: bool,
        #[arg(long = "json-output")]
        json_output: bool,
    },
    /// Serial/socket monitor (deferred to a later milestone).
    Monitor(PassthroughArgs),
}

/// `pio project <config|metadata|init>`.
#[derive(Args, Debug, Clone)]
pub struct ProjectArgs {
    #[command(subcommand)]
    pub action: ProjectAction,
}

#[derive(Subcommand, Debug, Clone)]
pub enum ProjectAction {
    /// Show computed configuration.
    Config {
        #[arg(short = 'd', long = "project-dir", default_value = ".")]
        project_dir: String,
        #[arg(long)]
        lint: bool,
        #[arg(long = "json-output")]
        json_output: bool,
    },
    /// Dump project build metadata (deferred — needs the build system, M4).
    Metadata(PassthroughArgs),
    /// Initialize a new project (deferred — heavy scaffolding).
    Init(PassthroughArgs),
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::CommandFactory;

    #[test]
    fn clap_definition_is_valid() {
        Cli::command().debug_assert();
    }

    #[test]
    fn parses_run_with_passthrough_flags() {
        let cli = Cli::try_parse_from(["pio-rs", "run", "-e", "native", "-d", "/tmp/proj"])
            .expect("should parse");
        match cli.command {
            Some(Command::Run(p)) => assert_eq!(p.args, ["-e", "native", "-d", "/tmp/proj"]),
            other => panic!("expected run, got {other:?}"),
        }
    }

    #[test]
    fn bare_version_flag_parses_without_subcommand() {
        let cli = Cli::try_parse_from(["pio-rs", "--version"]).expect("should parse");
        assert!(cli.version);
        assert!(cli.command.is_none());
    }
}
