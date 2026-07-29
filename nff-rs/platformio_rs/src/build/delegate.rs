//! Resolving the Phase-A delegation pieces.
//!
//! Phase A shells the actual compile out to Python SCons (see [`super`]). To do
//! that, pio-rs must locate four things:
//! 1. a **Python interpreter** that can `import platformio` (the SConscripts
//!    import the package),
//! 2. the core **`builder/main.py` sconstruct** inside that platformio install,
//! 3. **`tool-scons`** (`scons.py`), a core package, and
//! 4. the **dev-platform** package + its non-optional packages (toolchain,
//!    framework, uploader) — the platform's own `builder/main.py` becomes the
//!    `BUILD_SCRIPT` SCons variable.
//!
//! Per the M4 decision, the core builder is auto-discovered via the Python exe,
//! while `tool-scons` and the dev-platform are installed through pio-rs's own
//! Rust [`PackageManager`] (the M2 networked package layer). This couples Phase A
//! to an importable Python `platformio` — the accepted, permanent delegation
//! dependency (removed only by the native fast-path, M6).

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{anyhow, bail, Context, Result};

use crate::package::manager::PackageManager;
use crate::package::meta::PackageSpec;

/// The core-package version PlatformIO pins for `tool-scons`
/// (`platformio/package/manager/core.py` → `dependencies.py`).
pub const TOOL_SCONS_REQUIREMENTS: &str = "~4.40801.0";

/// Run a program to completion, capturing `(exit_code, stdout, stderr)`.
///
/// This is the crate's first external-process spawn; the streaming build runner
/// ([`super::scons::run_scons`]) is the only other one.
fn run_capture(program: &str, args: &[&str]) -> Result<(i32, String, String)> {
    let out = Command::new(program)
        .args(args)
        .output()
        .with_context(|| format!("failed to spawn `{program}`"))?;
    Ok((
        out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
    ))
}

/// Candidate Python interpreters, highest-priority first: an explicit
/// `PYTHONEXEPATH` override, then the usual PATH names.
fn python_candidates() -> Vec<String> {
    let mut c = Vec::new();
    if let Ok(p) = std::env::var("PYTHONEXEPATH") {
        if !p.is_empty() {
            c.push(p);
        }
    }
    c.push("python".to_string());
    c.push("python3".to_string());
    c
}

/// The core delegation handle: the Python interpreter to run SCons with, and its
/// platformio `builder/main.py` sconstruct.
#[derive(Debug, Clone)]
pub struct CoreDelegation {
    pub python_exe: String,
    /// `<python-platformio>/builder/main.py` — the `--sconstruct`.
    pub sconstruct: PathBuf,
}

/// Discover a Python that can `import platformio` and the core `builder/main.py`
/// inside it. Resolves the interpreter and sconstruct together so the returned
/// interpreter is guaranteed to have the package importable (the SConscripts
/// depend on it). Errors loudly with remediation when none is found.
pub fn resolve_core_delegation() -> Result<CoreDelegation> {
    let mut last_err = String::from("no python candidate produced a platformio path");
    for py in python_candidates() {
        match run_capture(
            &py,
            &["-c", "import platformio,os;print(os.path.dirname(platformio.__file__))"],
        ) {
            Ok((0, out, _)) => {
                let dir = out.trim();
                if !dir.is_empty() {
                    let sconstruct = Path::new(dir).join("builder").join("main.py");
                    if sconstruct.is_file() {
                        return Ok(CoreDelegation { python_exe: py, sconstruct });
                    }
                    last_err = format!("{} has no builder/main.py", dir);
                }
            }
            Ok((_, _, err)) if !err.trim().is_empty() => last_err = err.trim().to_string(),
            Ok(_) => {}
            Err(e) => last_err = e.to_string(),
        }
    }
    bail!(
        "Python PlatformIO not found. Phase A delegates the build to SCons and needs an \
         importable `platformio` package. Install it (`pip install platformio==6.1.19`) or set \
         PYTHONEXEPATH to a Python that has it. Last error: {last_err}"
    )
}

/// Build the argv that delegates a whole PlatformIO subcommand to the Python CLI:
/// `<python> -m platformio <subcommand> <forwarded...>`. Pure (no spawn) so the
/// argv mapping stays unit-testable.
#[must_use]
pub fn pio_argv(python_exe: &str, subcommand: &str, forwarded: &[String]) -> Vec<String> {
    let mut argv = Vec::with_capacity(4 + forwarded.len());
    argv.push(python_exe.to_string());
    argv.push("-m".to_string());
    argv.push("platformio".to_string());
    argv.push(subcommand.to_string());
    argv.extend(forwarded.iter().cloned());
    argv
}

/// Delegate a whole PlatformIO subcommand to the discovered Python `platformio`,
/// streaming its stdio live (inherited) with `PYTHONIOENCODING=utf-8`, and return
/// the child's exit code.
///
/// Reuses [`resolve_core_delegation`] to find an interpreter that can `import
/// platformio` — the same permanent delegation dependency Phase A relies on. This
/// is how the M5 orchestration commands (`test`, `check`, `debug`, `remote`,
/// `home`) run: they keep upstream's Python plugin/SCons execution path (each of
/// those commands re-invokes `pio run`/SCons or spawns its own external tool)
/// instead of reimplementing it natively.
pub fn run_pio_command(subcommand: &str, forwarded: &[String]) -> Result<i32> {
    let python_exe = resolve_core_delegation()?.python_exe;
    let argv = pio_argv(&python_exe, subcommand, forwarded);
    // Flush any buffered stdout first so a caller's preamble precedes child output.
    let _ = std::io::stdout().flush();
    let code = Command::new(&argv[0])
        .args(&argv[1..])
        .env("PYTHONIOENCODING", "utf-8")
        .status()
        .with_context(|| format!("failed to spawn platformio (`{}`)", argv[0]))?
        .code()
        .unwrap_or(1);
    Ok(code)
}

/// Ensure `tool-scons` is installed (via pio-rs's own package manager) and return
/// the path to its `scons.py`.
pub fn ensure_tool_scons() -> Result<PathBuf> {
    let pm = PackageManager::tool(None);
    let spec = PackageSpec::builder()
        .owner("platformio")
        .name("tool-scons")
        .requirements(TOOL_SCONS_REQUIREMENTS)
        .build()
        .map_err(|e| anyhow!("building tool-scons spec: {e}"))?;
    let item = match pm.get_package(&spec).map_err(|e| anyhow!("{e}"))? {
        Some(it) => it,
        None => pm.install(&spec).map_err(|e| anyhow!("installing tool-scons: {e}"))?,
    };
    let scons_py = item.path.join("scons.py");
    if !scons_py.is_file() {
        bail!("tool-scons at {} is missing scons.py", item.path.display());
    }
    Ok(scons_py)
}

/// A resolved dev-platform: its install dir and its `builder/main.py`
/// (the SCons `BUILD_SCRIPT`).
#[derive(Debug, Clone)]
pub struct ResolvedPlatform {
    pub dir: PathBuf,
    pub build_script: PathBuf,
}

/// Install/find the dev-platform for `spec`, install its non-optional packages,
/// and locate its build script.
pub fn resolve_platform(spec: &PackageSpec) -> Result<ResolvedPlatform> {
    let pm = PackageManager::platform(None);
    let item = match pm.get_package(spec).map_err(|e| anyhow!("{e}"))? {
        Some(it) => it,
        None => pm.install(spec).map_err(|e| anyhow!("installing platform: {e}"))?,
    };
    let dir = item.path.clone();

    let manifest = dir.join("platform.json");
    let text = std::fs::read_to_string(&manifest)
        .with_context(|| format!("reading {}", manifest.display()))?;
    let json: serde_json::Value = serde_json::from_str(&text)
        .with_context(|| format!("parsing {}", manifest.display()))?;
    install_platform_packages(&json)?;

    let build_script = dir.join("builder").join("main.py");
    if !build_script.is_file() {
        bail!("platform at {} has no builder/main.py (Phase A needs it)", dir.display());
    }
    Ok(ResolvedPlatform { dir, build_script })
}

/// Whether a `platform.json` package `version` string looks like a resolvable
/// semver requirement (vs a URL/VCS/branch pin we can't pass to `SimpleSpec`).
fn looks_like_version_req(v: &str) -> bool {
    !(v.contains("://") || v.contains('@') || v.contains('#') || v.contains('/'))
}

/// Parse the non-optional packages declared in a `platform.json` into specs.
/// A missing `packages` block (e.g. the `native` platform, which uses the host
/// toolchain) yields an empty list.
fn platform_package_specs(json: &serde_json::Value) -> Result<Vec<PackageSpec>> {
    let Some(pkgs) = json.get("packages").and_then(|v| v.as_object()) else {
        return Ok(Vec::new());
    };
    let mut specs = Vec::new();
    for (name, val) in pkgs {
        if val.get("optional").and_then(serde_json::Value::as_bool).unwrap_or(false) {
            continue;
        }
        let owner = val.get("owner").and_then(serde_json::Value::as_str).unwrap_or("platformio");
        let mut b = PackageSpec::builder().owner(owner).name(name.as_str());
        if let Some(ver) = val.get("version").and_then(serde_json::Value::as_str) {
            if looks_like_version_req(ver) {
                b = b.requirements(ver);
            }
        }
        specs.push(b.build().map_err(|e| anyhow!("building spec for {name}: {e}"))?);
    }
    Ok(specs)
}

/// Install every non-optional package a platform declares (best-effort: already
/// present packages are skipped).
fn install_platform_packages(json: &serde_json::Value) -> Result<()> {
    let specs = platform_package_specs(json)?;
    if specs.is_empty() {
        return Ok(());
    }
    let tm = PackageManager::tool(None);
    for spec in &specs {
        if tm.get_package(spec).map_err(|e| anyhow!("{e}"))?.is_none() {
            tm.install(spec).map_err(|e| anyhow!("installing platform package: {e}"))?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn version_req_heuristic() {
        assert!(looks_like_version_req("~1.2.3"));
        assert!(looks_like_version_req("^2.0.0"));
        assert!(!looks_like_version_req("https://github.com/x/y.git"));
        assert!(!looks_like_version_req("platformio/toolchain@~1.0.0"));
        assert!(!looks_like_version_req("owner/name"));
    }

    #[test]
    fn no_packages_block_yields_empty() {
        // The `native` platform.json has no "packages" key.
        let native = json!({ "name": "native", "version": "1.2.1" });
        assert!(platform_package_specs(&native).unwrap().is_empty());
    }

    #[test]
    fn optional_packages_are_skipped() {
        let manifest = json!({
            "name": "demo",
            "packages": {
                "toolchain-gcc": { "type": "toolchain", "owner": "platformio", "version": "~1.0.0" },
                "framework-x":    { "type": "framework", "optional": true, "version": "~2.0.0" },
                "tool-openocd":   { "type": "uploader", "optional": false }
            }
        });
        let specs = platform_package_specs(&manifest).unwrap();
        let names: Vec<&str> = specs.iter().filter_map(|s| s.name.as_deref()).collect();
        assert!(names.contains(&"toolchain-gcc"));
        assert!(names.contains(&"tool-openocd"));
        assert!(!names.contains(&"framework-x")); // optional
        assert_eq!(names.len(), 2);
    }

    // Networked / installed-state dependent — run explicitly:
    //   cargo test -p platformio_rs -- --ignored delegate
    #[test]
    #[ignore = "needs an importable Python platformio"]
    fn smoke_resolve_core_delegation() {
        let core = resolve_core_delegation().expect("core delegation");
        assert!(core.sconstruct.ends_with("builder/main.py") || core.sconstruct.ends_with("builder\\main.py"));
    }

    #[test]
    #[ignore = "installs/needs tool-scons (network on a cold cache)"]
    fn smoke_ensure_tool_scons() {
        let scons = ensure_tool_scons().expect("tool-scons");
        assert!(scons.is_file());
    }

    #[test]
    #[ignore = "needs the `native` dev-platform installed"]
    fn smoke_resolve_native_platform() {
        let spec = PackageSpec::builder().owner("platformio").name("native").build().unwrap();
        let plat = resolve_platform(&spec).expect("native platform");
        assert!(plat.build_script.is_file());
    }
}
