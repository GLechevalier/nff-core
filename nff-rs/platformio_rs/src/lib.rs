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

pub mod app;
pub mod build;
pub mod cli;
pub mod commands;
pub mod config;
pub mod http;
pub mod output;
pub mod package;
pub mod platform;
pub mod registry;

/// A process-wide lock shared by every test that mutates global state (the
/// current directory or environment variables), so tests across modules
/// serialize against each other rather than racing on `PLATFORMIO_*`/cwd.
#[cfg(test)]
pub(crate) mod test_lock {
    use std::sync::{Mutex, MutexGuard, PoisonError};

    static LOCK: Mutex<()> = Mutex::new(());

    pub(crate) fn guard() -> MutexGuard<'static, ()> {
        LOCK.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

/// `platformio.__registry_mirror_hosts__` — the registry mirror hosts. API
/// endpoints are `https://api.<host>`, download endpoints `https://dl.<host>`.
pub const REGISTRY_MIRROR_HOSTS: &[&str] = &["registry.platformio.org", "registry.nm1.platformio.org"];

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
    /// The command already streamed its output to the process stdout/stderr
    /// directly (e.g. a delegated build), so the binary must not re-print
    /// `stdout`/`stderr`. Only [`CmdOutcome::code_only`] sets this.
    pub streamed: bool,
}

impl CmdOutcome {
    #[must_use]
    pub fn ok(stdout: impl Into<String>) -> Self {
        Self { code: 0, stdout: stdout.into(), stderr: String::new(), streamed: false }
    }

    /// A command that already wrote its output live (streamed a subprocess) and
    /// only carries the process exit code back to the binary.
    #[must_use]
    pub fn code_only(code: i32) -> Self {
        Self { code, stdout: String::new(), stderr: String::new(), streamed: true }
    }

    /// A not-yet-ported command. Mirrors how an unimplemented subcommand should
    /// fail loudly (non-zero) so parity runs against `pio-rs` flag the gap.
    #[must_use]
    pub fn not_implemented(name: &str) -> Self {
        Self {
            code: 64, // EX_USAGE-ish; distinct from PlatformIO's own 0/1/2/3 codes
            stdout: String::new(),
            stderr: format!("pio-rs: command `{name}` is not implemented yet"),
            streamed: false,
        }
    }
}

/// Dispatch a parsed CLI invocation. The M3 command groups (`boards`, `device`,
/// `project`, `settings`, `system`) route to real handlers in [`commands`];
/// everything else is still a not-implemented stub filled in by later milestones.
#[must_use]
pub fn dispatch(command: &cli::Command) -> CmdOutcome {
    use cli::Command::{
        Account, Boards, Check, Ci, Debug, Device, Home, Lib, Org, Pkg, Platform, Project, Remote,
        Run, Settings, System, Team, Test, Update, Upgrade,
    };
    match command {
        Boards(a) => commands::boards::run(a),
        Device(a) => commands::device::run(a),
        Project(a) => commands::project::run(a),
        Settings(a) => commands::settings::run(a),
        System(a) => commands::system::run(a),
        Account(_) => CmdOutcome::not_implemented("account"),
        Check(_) => CmdOutcome::not_implemented("check"),
        Ci(_) => CmdOutcome::not_implemented("ci"),
        Debug(_) => CmdOutcome::not_implemented("debug"),
        Home(_) => CmdOutcome::not_implemented("home"),
        Lib(_) => CmdOutcome::not_implemented("lib"),
        Org(_) => CmdOutcome::not_implemented("org"),
        Pkg(_) => CmdOutcome::not_implemented("pkg"),
        Platform(_) => CmdOutcome::not_implemented("platform"),
        Remote(_) => CmdOutcome::not_implemented("remote"),
        Run(a) => commands::run::run(a),
        Team(_) => CmdOutcome::not_implemented("team"),
        Test(_) => CmdOutcome::not_implemented("test"),
        Update(_) => CmdOutcome::not_implemented("update"),
        Upgrade(_) => CmdOutcome::not_implemented("upgrade"),
    }
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
