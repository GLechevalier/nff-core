//! platformio_rs — a native-Rust reimplementation of PlatformIO Core.
//!
//! This crate is built in milestones (see the plan); at M0 it is a faithful
//! command *skeleton*: every PlatformIO CLI command exists and parses, but the
//! behaviour is filled in milestone by milestone. The `pio-rs` binary wraps this
//! library so the vendored PlatformIO pytest suite can drive it exactly like the
//! upstream Python `pio` (the differential parity harness).
//!
//! The library API (not the binary) is what the `nff` crate links against for
//! in-process builds once the relevant milestones land.

pub mod cli;
pub mod config;
pub mod package;

/// The upstream PlatformIO Core version this port targets for parity.
///
/// Kept in lock-step with the vendored test suite tag (`parity/platformio-core`).
pub const PIO_CORE_VERSION: &str = "6.1.19";

/// The exact string `pio --version` prints, reproduced for parity.
#[must_use]
pub fn version_string() -> String {
    format!("PlatformIO Core, version {PIO_CORE_VERSION}")
}

/// Outcome of running a command: the process exit code plus whatever the command
/// wrote. Commands that aren't ported yet return [`CmdOutcome::not_implemented`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CmdOutcome {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

impl CmdOutcome {
    #[must_use]
    pub fn ok(stdout: impl Into<String>) -> Self {
        Self { code: 0, stdout: stdout.into(), stderr: String::new() }
    }

    /// A not-yet-ported command. Mirrors how an unimplemented subcommand should
    /// fail loudly (non-zero) so parity runs against `pio-rs` flag the gap.
    #[must_use]
    pub fn not_implemented(name: &str) -> Self {
        Self {
            code: 64, // EX_USAGE-ish; distinct from PlatformIO's own 0/1/2/3 codes
            stdout: String::new(),
            stderr: format!("pio-rs: command `{name}` is not implemented yet"),
        }
    }
}

/// Dispatch a parsed CLI invocation. At M0 every real command is a stub; the
/// only fully-honest behaviours are `--version`/`--help` (handled by clap before
/// we get here) and the not-implemented signalling.
#[must_use]
pub fn dispatch(command: &cli::Command) -> CmdOutcome {
    use cli::Command::{
        Account, Boards, Check, Ci, Debug, Device, Home, Lib, Org, Pkg, Platform, Project, Remote,
        Run, Settings, System, Team, Test, Update, Upgrade,
    };
    let name = match command {
        Account(_) => "account",
        Boards(_) => "boards",
        Check(_) => "check",
        Ci(_) => "ci",
        Debug(_) => "debug",
        Device(_) => "device",
        Home(_) => "home",
        Lib(_) => "lib",
        Org(_) => "org",
        Pkg(_) => "pkg",
        Platform(_) => "platform",
        Project(_) => "project",
        Remote(_) => "remote",
        Run(_) => "run",
        Settings(_) => "settings",
        System(_) => "system",
        Team(_) => "team",
        Test(_) => "test",
        Update(_) => "update",
        Upgrade(_) => "upgrade",
    };
    CmdOutcome::not_implemented(name)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_string_matches_platformio_format() {
        // PlatformIO prints exactly: "PlatformIO Core, version X.Y.Z"
        assert_eq!(version_string(), "PlatformIO Core, version 6.1.19");
    }

    #[test]
    fn not_implemented_is_nonzero() {
        let o = CmdOutcome::not_implemented("run");
        assert_ne!(o.code, 0);
        assert!(o.stderr.contains("run"));
    }
}
