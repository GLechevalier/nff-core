//! Spawning captured tool commands for the native replay (M6b).
//!
//! Captured compile/archive/link commands use **bare** tool names
//! (`xtensa-esp32-elf-g++`), which PlatformIO resolves by putting the toolchain
//! package `bin/` dirs on `PATH`. We reproduce that: [`build_path`] prepends the
//! plan's `tool_path_dirs` to the inherited `PATH`, and [`run_captured`] spawns
//! with `cwd = project_dir` (so the relative object/source paths resolve) and
//! captures output, surfacing it only on failure.

use std::ffi::OsString;
use std::path::Path;
use std::process::Command;

use anyhow::{anyhow, bail, Context, Result};

/// `PATH` with `prepend` dirs in front of the current process's `PATH`.
#[must_use]
pub fn build_path(prepend: &[String]) -> OsString {
    let mut dirs: Vec<std::path::PathBuf> = prepend.iter().map(std::path::PathBuf::from).collect();
    if let Some(existing) = std::env::var_os("PATH") {
        dirs.extend(std::env::split_paths(&existing));
    }
    std::env::join_paths(dirs).unwrap_or_else(|_| std::env::var_os("PATH").unwrap_or_default())
}

/// Run one captured command to completion. `command[0]` is the program; the rest
/// are args. Returns an error (with captured output) on a non-zero exit.
pub fn run_captured(command: &[String], project_dir: &Path, path_prepend: &[String]) -> Result<()> {
    let program = command.first().ok_or_else(|| anyhow!("empty command"))?;
    let out = Command::new(program)
        .args(&command[1..])
        .current_dir(project_dir)
        .env("PATH", build_path(path_prepend))
        .env("PYTHONIOENCODING", "utf-8")
        .output()
        .with_context(|| format!("failed to spawn `{program}`"))?;
    if !out.status.success() {
        let stdout = String::from_utf8_lossy(&out.stdout);
        let stderr = String::from_utf8_lossy(&out.stderr);
        bail!(
            "command failed ({}, exit {}):\n{}{}",
            program,
            out.status.code().unwrap_or(-1),
            stdout,
            stderr
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_path_prepends_dirs() {
        let path = build_path(&["/toolchain/bin".to_string()]);
        let dirs: Vec<_> = std::env::split_paths(&path).collect();
        assert_eq!(dirs.first().map(|p| p.to_string_lossy().into_owned()), Some("/toolchain/bin".to_string()));
        // The inherited PATH still follows.
        assert!(!dirs.is_empty());
    }
}
