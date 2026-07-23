//! Build Phase B — the native ESP32 fast-path (M6).
//!
//! Phase A (see [`super`]) delegates every build to Python SCons. Phase B replaces
//! that for supported families with a native Rust engine: one **verbose SCons
//! build is captured once** into a [`plan::BuildPlan`] (compiles, archives, link,
//! image steps with fully-resolved flags), cached keyed on the build's structure,
//! and thereafter **replayed natively** — parallel compiles, incremental caching,
//! link, and `esptool` image gen — with no Python on the hot path. The SCons
//! delegation stays the permanent fallback for every unsupported platform.
//!
//! Gated behind `--native` / `PLATFORMIO_RS_NATIVE` (default off). At M6a this
//! module wires the gate + family detection + capture/cache; native *replay*
//! (skipping SCons on a cache hit) lands in M6b–M6c.

pub mod cache;
pub mod capture;
pub mod exec;
pub mod ino;
pub mod plan;
pub mod replay;

use std::io::Write;
use std::path::{Path, PathBuf};

use anyhow::{anyhow, bail, Result};

use super::delegate::{self, CoreDelegation, ResolvedPlatform};
use super::BuildOptions;
use crate::cli::RunArgs;
use crate::config::options::get_core_dir;
use crate::config::{Defaulted, ProjectConfig, Value};
use crate::package::meta::PackageSpec;
use crate::platform::board::PlatformBoardConfig;

/// ESP32 xtensa MCUs the native fast-path supports at M6. RISC-V parts
/// (`esp32c3`/`esp32c6`) and everything else fall back to SCons for now.
const SUPPORTED_MCUS: &[&str] = &["esp32", "esp32s2", "esp32s3"];
const SUPPORTED_PLATFORM: &str = "espressif32";
const SUPPORTED_FRAMEWORK: &str = "arduino";

/// Whether the native fast-path is requested: `--native` or a truthy
/// `PLATFORMIO_RS_NATIVE` (anything but empty/`0`/`false`). Default off.
#[must_use]
pub fn gate_enabled(args: &RunArgs) -> bool {
    if args.native {
        return true;
    }
    match std::env::var("PLATFORMIO_RS_NATIVE") {
        Ok(v) => {
            let v = v.trim();
            !(v.is_empty() || v == "0" || v.eq_ignore_ascii_case("false"))
        }
        Err(_) => false,
    }
}

/// Whether an environment is eligible for the native fast-path.
#[derive(Debug, Clone)]
pub struct Support {
    pub supported: bool,
    pub platform: String,
    pub framework: String,
    pub mcu: String,
    /// Why it was rejected (for the loud fallback log); `None` when supported.
    pub reason: Option<String>,
}

fn cfg_str(cfg: &ProjectConfig, section: &str, option: &str) -> Option<String> {
    match cfg.get(section, option, Defaulted::Missing) {
        Ok(Value::Str(s)) if !s.is_empty() => Some(s),
        Ok(Value::List(items)) if !items.is_empty() => Some(items[0].to_plain_string()),
        _ => None,
    }
}

fn cfg_list(cfg: &ProjectConfig, section: &str, option: &str) -> Vec<String> {
    match cfg.get(section, option, Defaulted::Missing) {
        Ok(Value::List(items)) => items.iter().map(Value::to_plain_string).collect(),
        Ok(Value::Str(s)) if !s.is_empty() => vec![s],
        _ => Vec::new(),
    }
}

/// The platform's short `name` from its `platform.json` (e.g. `"espressif32"`).
fn platform_name(dir: &Path) -> Option<String> {
    let text = std::fs::read_to_string(dir.join("platform.json")).ok()?;
    let json: serde_json::Value = serde_json::from_str(&text).ok()?;
    json.get("name").and_then(|v| v.as_str()).map(ToString::to_string)
}

/// The platform's installed `version` from its `platform.json` (part of the cache key).
fn platform_version(dir: &Path) -> String {
    std::fs::read_to_string(dir.join("platform.json"))
        .ok()
        .and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok())
        .and_then(|j| j.get("version").and_then(|v| v.as_str()).map(ToString::to_string))
        .unwrap_or_default()
}

/// The package `bin/` dirs to prepend to `PATH` when replaying the plan, so the
/// captured bare tool names resolve. Mirrors `pioplatform.py::LoadPioPlatform`,
/// which puts each installed package's `bin/` on PATH; we include every package
/// the platform declares (toolchain/uploader/framework) that has a `bin/`.
fn tool_path_dirs(platform_dir: &Path) -> Vec<String> {
    let packages_dir = PathBuf::from(get_core_dir()).join("packages");
    let names: Vec<String> = std::fs::read_to_string(platform_dir.join("platform.json"))
        .ok()
        .and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok())
        .and_then(|j| j.get("packages").and_then(|p| p.as_object()).map(|o| o.keys().cloned().collect()))
        .unwrap_or_default();
    let mut dirs = Vec::new();
    for name in names {
        let bin = packages_dir.join(&name).join("bin");
        if bin.is_dir() {
            dirs.push(bin.to_string_lossy().into_owned());
        }
    }
    dirs
}

/// Locate a board manifest by id: the dev-platform's `boards/` first, then the
/// core `boards/` dir (mirrors `PlatformBase.get_boards` search order).
fn find_board_json(platform_dir: &Path, board_id: &str) -> Option<PathBuf> {
    let candidates = [
        platform_dir.join("boards").join(format!("{board_id}.json")),
        PathBuf::from(get_core_dir()).join("boards").join(format!("{board_id}.json")),
    ];
    candidates.into_iter().find(|p| p.is_file())
}

/// Decide whether `env` is a supported native family and gather the facts the
/// cache key + logs need.
#[must_use]
pub fn detect_support(cfg: &ProjectConfig, env: &str, resolved: &ResolvedPlatform) -> Support {
    let section = format!("env:{env}");
    let platform = platform_name(&resolved.dir).unwrap_or_default();
    let frameworks = cfg_list(cfg, &section, "framework");
    let framework = frameworks.first().cloned().unwrap_or_default();

    let board_id = cfg_str(cfg, &section, "board").unwrap_or_default();
    let mcu = board_id
        .is_empty()
        .then(String::new)
        .or_else(|| {
            find_board_json(&resolved.dir, &board_id)
                .and_then(|p| PlatformBoardConfig::from_file(&p).ok())
                .and_then(|b| b.get("build.mcu").and_then(|v| v.as_str()).map(str::to_ascii_lowercase))
        })
        .unwrap_or_default();

    let mut reason = None;
    if platform != SUPPORTED_PLATFORM {
        reason = Some(format!("platform `{platform}` is not `{SUPPORTED_PLATFORM}`"));
    } else if !frameworks.iter().any(|f| f == SUPPORTED_FRAMEWORK) {
        reason = Some(format!("framework `{framework}` is not `{SUPPORTED_FRAMEWORK}`"));
    } else if board_id.is_empty() {
        reason = Some("no `board` set".to_string());
    } else if !SUPPORTED_MCUS.contains(&mcu.as_str()) {
        reason = Some(format!("mcu `{mcu}` is not a supported xtensa ESP32 part"));
    }

    Support { supported: reason.is_none(), platform, framework, mcu, reason }
}

/// Attempt the native fast-path for one environment.
///
/// Returns `Ok(Some(exit_code))` when the native path handled the env (supported
/// family), or `Ok(None)` when the env is unsupported — the caller then delegates
/// to SCons (a loud fallback line is already printed to stderr).
///
/// At M6a "handling" means: resolve the platform, capture a verbose SCons build
/// into a [`plan::BuildPlan`], cache it, and let SCons produce the artifacts. On a
/// cache hit the (still-delegated) build is re-run without re-capturing. M6b/M6c
/// replace the delegated execution with a native replay of the cached plan.
#[allow(clippy::too_many_arguments)]
pub fn build_env(
    cfg: &ProjectConfig,
    env: &str,
    targets: Vec<String>,
    ini: &str,
    project_dir: &Path,
    core: &CoreDelegation,
    scons_py: &Path,
    opts: &BuildOptions,
) -> Result<Option<i32>> {
    // Output routing: with a capture sink (in-process embedding) every line goes
    // to the sink; otherwise build content streams to stdout and progress to
    // stderr, exactly as before. Lines are passed newline-terminated.
    let sink = opts.log_sink.clone();
    let to_out = |s: &str| match &sink {
        Some(k) => k(s),
        None => {
            print!("{s}");
            let _ = std::io::stdout().flush();
        }
    };
    let to_err = |s: &str| match &sink {
        Some(k) => k(s),
        None => eprint!("{s}"),
    };

    let section = format!("env:{env}");
    let platform_spec = match cfg_str(cfg, &section, "platform") {
        Some(s) => s,
        None => bail!("UndefinedEnvPlatformError: Please specify platform for `{env}` environment"),
    };
    let spec = PackageSpec::parse(&platform_spec).map_err(|e| anyhow!("{e}"))?;
    let resolved = delegate::resolve_platform(&spec)?;

    let support = detect_support(cfg, env, &resolved);
    if !support.supported {
        let reason = support.reason.as_deref().unwrap_or("unsupported");
        to_err(&format!(
            "pio-rs native: fast-path unavailable for env `{env}` ({reason}); using SCons delegation\n"
        ));
        return Ok(None);
    }

    // Cache key over the build's structure (config + board + platform version).
    let ini_bytes = std::fs::read(ini).unwrap_or_default();
    let board_id = cfg_str(cfg, &section, "board").unwrap_or_default();
    let board_bytes = find_board_json(&resolved.dir, &board_id)
        .and_then(|p| std::fs::read(p).ok())
        .unwrap_or_default();
    let key = plan::cache_key(&ini_bytes, &board_bytes, &platform_version(&resolved.dir));

    let build_dir_env = super::build_dir_for(cfg, env);
    let cached = plan::load(&build_dir_env);

    // Cache hit ⇒ replay the plan natively (no SCons, no Python on this path).
    if let Some(plan) = cached.filter(|p| p.cache_key == key) {
        to_err(&format!(
            "pio-rs native: plan cache hit for `{env}` ({}, {}); replaying natively\n",
            support.platform, support.mcu
        ));
        to_out(&format!("Processing {env} (native)...\n"));
        let wants_upload = targets.iter().any(|t| t == "upload");
        match replay::replay(&plan, project_dir, opts.jobs, wants_upload, opts.upload_port.as_deref()) {
            Ok(stats) => {
                to_err(&format!(
                    "pio-rs native: `{env}` built natively — compiled {}/{}, archived {}, {} link, {} image(s){}\n",
                    stats.compiled,
                    stats.compiles_total,
                    stats.archived,
                    u8::from(stats.linked),
                    stats.images,
                    if stats.flashed { ", flashed" } else { "" }
                ));
                return Ok(Some(0));
            }
            Err(e) => {
                to_err(&format!(
                    "pio-rs native: native replay failed for `{env}` ({e}); falling back to SCons delegation\n"
                ));
                let prepared = super::prepare_env(cfg, env, targets, ini, core, scons_py, opts)?;
                // With a sink, capture the fallback build too; otherwise stream it.
                let code = if sink.is_some() {
                    let (code, output) = super::scons::run_scons_captured(&prepared.scons, project_dir);
                    to_out(&output);
                    code
                } else {
                    println!("Processing {env}...");
                    super::scons::run_scons(&prepared.scons, project_dir)
                };
                return Ok(Some(code));
            }
        }
    }

    // Cache miss ⇒ capture one verbose SCons build into a plan, then cache it. The
    // capture build itself produces the artifacts this time; subsequent runs replay.
    to_err(&format!("pio-rs native: capturing build plan for `{env}` ({}, {})…\n", support.platform, support.mcu));
    let (code, output, captured) =
        capture::capture_build(cfg, env, targets, ini, project_dir, core, scons_py, opts)?;
    to_out(&output);

    if captured.is_buildable() {
        let n = captured.compiles.len();
        let plan = plan::BuildPlan::new(env, key, captured, tool_path_dirs(&resolved.dir));
        plan::save(&build_dir_env, &plan)?;
        to_err(&format!("pio-rs native: cached build plan for `{env}` ({n} compile action(s))\n"));
    } else {
        to_err(&format!(
            "pio-rs native: capture for `{env}` produced no usable plan (final link not found); \
             not caching\n"
        ));
    }
    Ok(Some(code))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args_with_native(native: bool) -> RunArgs {
        RunArgs { native, ..RunArgs::default() }
    }

    #[test]
    fn gate_reads_flag_and_env() {
        let _g = crate::test_lock::guard();
        let prev = std::env::var("PLATFORMIO_RS_NATIVE").ok();
        std::env::remove_var("PLATFORMIO_RS_NATIVE");

        assert!(gate_enabled(&args_with_native(true)));
        assert!(!gate_enabled(&args_with_native(false)));

        std::env::set_var("PLATFORMIO_RS_NATIVE", "1");
        assert!(gate_enabled(&args_with_native(false)));
        std::env::set_var("PLATFORMIO_RS_NATIVE", "0");
        assert!(!gate_enabled(&args_with_native(false)));
        std::env::set_var("PLATFORMIO_RS_NATIVE", "false");
        assert!(!gate_enabled(&args_with_native(false)));
        std::env::set_var("PLATFORMIO_RS_NATIVE", "yes");
        assert!(gate_enabled(&args_with_native(false)));

        match prev {
            Some(v) => std::env::set_var("PLATFORMIO_RS_NATIVE", v),
            None => std::env::remove_var("PLATFORMIO_RS_NATIVE"),
        }
    }
}
