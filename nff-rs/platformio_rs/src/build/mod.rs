//! The build engine (M4).
//!
//! **Phase A — SCons delegation.** PlatformIO Core is an *orchestrator*: it
//! parses `platformio.ini`, resolves/installs packages, then hands the actual
//! compile to **SCons** running the dev-platform's Python `platform.py`. A pure
//! Rust reimplementation cannot execute arbitrary third-party `platform.py`
//! plugins, so Phase A reproduces PlatformIO's SCons invocation *exactly* and
//! shells out to it for byte-parity. The native fast-path (Phase B / M6)
//! replaces this for supported families; the delegation fallback is permanent.
//!
//! This module is split:
//! - [`scons`] — the pure-logic argument contract (`SconsBuild::argv`,
//!   `encode_scons_arg`, target classification) + the subprocess runner. Fully
//!   unit-tested without spawning anything.
//! - `delegate` — resolving the delegation pieces (the Python interpreter, the
//!   core `builder/main.py` sconstruct, `tool-scons`, and the dev-platform + its
//!   non-optional packages). Networked. Added in M4b.

pub mod delegate;
pub mod native;
pub mod scons;

use std::path::{Path, PathBuf};

use anyhow::{anyhow, bail, Result};

use crate::build::delegate::CoreDelegation;
use crate::build::scons::{SconsArg, SconsBuild};
use crate::config::{Defaulted, ProjectConfig, Value};
use crate::package::meta::PackageSpec;

/// A sink for a build's user-facing output. When set (in-process embedding, e.g.
/// nff calling `run_build`), the native path routes its progress + build log here
/// instead of the process's stdout/stderr, so the caller can capture it. `None`
/// (the `pio-rs` binary / plain `pio run`) preserves the streamed-to-terminal
/// behavior. Lines arrive already newline-terminated.
pub type LogSink = std::sync::Arc<dyn Fn(&str) + Send + Sync>;

/// Options shared across every environment in one `pio run` / `pio project
/// metadata` invocation.
#[derive(Clone)]
pub struct BuildOptions {
    pub jobs: usize,
    pub verbose: bool,
    pub silent: bool,
    pub isatty: bool,
    pub program_args: Vec<String>,
    pub upload_port: Option<String>,
    /// Optional capture sink for in-process builds; `None` streams to the terminal.
    pub log_sink: Option<LogSink>,
}

impl std::fmt::Debug for BuildOptions {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BuildOptions")
            .field("jobs", &self.jobs)
            .field("verbose", &self.verbose)
            .field("silent", &self.silent)
            .field("isatty", &self.isatty)
            .field("program_args", &self.program_args)
            .field("upload_port", &self.upload_port)
            .field("log_sink", &self.log_sink.as_ref().map(|_| "<fn>"))
            .finish()
    }
}

/// Default parallel-build job count (the CPU count, min 1).
#[must_use]
pub fn default_jobs() -> usize {
    std::thread::available_parallelism().map_or(1, std::num::NonZeroUsize::get)
}

/// Select the environments to process, exactly like `run/cli.py`: an explicit
/// `-e` list wins; otherwise `[platformio] default_envs`; otherwise all `[env:*]`.
/// Order follows `config.envs()` (declaration order).
#[must_use]
pub fn select_envs(cfg: &ProjectConfig, requested: &[String]) -> Vec<String> {
    let default_envs = cfg.default_envs();
    cfg.envs()
        .into_iter()
        .filter(|env| {
            if !requested.is_empty() {
                requested.iter().any(|r| r == env)
            } else if !default_envs.is_empty() {
                default_envs.iter().any(|d| d == env)
            } else {
                true
            }
        })
        .collect()
}

/// The env's configured `targets` option (empty when unset).
#[must_use]
pub fn env_targets(cfg: &ProjectConfig, env: &str) -> Vec<String> {
    match cfg.get(&format!("env:{env}"), "targets", Defaulted::Missing) {
        Ok(Value::List(items)) => items.iter().map(Value::to_plain_string).collect(),
        Ok(Value::Str(s)) if !s.is_empty() => vec![s],
        _ => Vec::new(),
    }
}

/// A prepared single-environment build: the resolved SCons invocation plus the
/// per-env build directory (`<build_dir>/<env>`) where artifacts/idedata land.
pub struct Prepared {
    pub scons: SconsBuild,
    pub build_dir_env: PathBuf,
}

/// Resolve everything needed to build one environment and assemble its
/// [`SconsBuild`]. `project_config_path` is the absolute `platformio.ini` path;
/// `core`/`scons_py` are the shared delegation pieces resolved once per run.
pub fn prepare_env(
    cfg: &ProjectConfig,
    env: &str,
    targets: Vec<String>,
    project_config_path: &str,
    core: &CoreDelegation,
    scons_py: &Path,
    opts: &BuildOptions,
) -> Result<Prepared> {
    let section = format!("env:{env}");
    let platform = match cfg.get(&section, "platform", Defaulted::Missing).map_err(|e| anyhow!("{e}"))? {
        Value::Str(s) if !s.is_empty() => s,
        _ => bail!("UndefinedEnvPlatformError: Please specify platform for `{env}` environment"),
    };
    let spec = PackageSpec::parse(&platform).map_err(|e| anyhow!("{e}"))?;
    let resolved = delegate::resolve_platform(&spec)?;

    let build_dir = match cfg.get("platformio", "build_dir", Defaulted::Missing).map_err(|e| anyhow!("{e}"))? {
        Value::Str(s) if !s.is_empty() => s,
        _ => Path::new(".pio").join("build").to_string_lossy().into_owned(),
    };
    let build_dir_env = Path::new(&build_dir).join(env);

    // Variables in PlatformIO's insertion order (build_script added last).
    let mut vars = vec![
        ("pioenv".to_string(), SconsArg::str(env)),
        ("project_config".to_string(), SconsArg::str(project_config_path)),
        ("program_args".to_string(), SconsArg::List(opts.program_args.clone())),
    ];
    if let Some(port) = &opts.upload_port {
        vars.push(("upload_port".to_string(), SconsArg::str(port)));
    }
    vars.push(("build_script".to_string(), SconsArg::str(resolved.build_script.to_string_lossy())));

    let scons = SconsBuild {
        python_exe: core.python_exe.clone(),
        scons_py: scons_py.to_string_lossy().into_owned(),
        sconstruct: core.sconstruct.to_string_lossy().into_owned(),
        jobs: opts.jobs,
        verbose: opts.verbose,
        isatty: opts.isatty,
        vars,
        targets,
    };
    Ok(Prepared { scons, build_dir_env })
}

/// The per-env build directory (`<build_dir>/<env>`), resolved from
/// `[platformio] build_dir` (default `.pio/build`). Assumes the current dir is
/// the project dir (relative `build_dir` resolves there), as `run`/`metadata` do.
fn build_dir_for(cfg: &ProjectConfig, env: &str) -> PathBuf {
    let build_dir = match cfg.get("platformio", "build_dir", Defaulted::Missing) {
        Ok(Value::Str(s)) if !s.is_empty() => s,
        _ => Path::new(".pio").join("build").to_string_lossy().into_owned(),
    };
    Path::new(&build_dir).join(env)
}

/// Port of `platformio.project.helpers.load_build_metadata`: build each env with
/// the `__idedata` target (which dumps `<build_dir>/<env>/idedata.json`), then
/// read those files into an env-keyed map. The build output is captured, not
/// streamed, so the caller's stdout carries only the metadata JSON.
///
/// Must be called with the current dir set to `project_dir` (like upstream's
/// `with fs.cd(project_dir)`), so relative `build_dir` resolves correctly.
pub fn load_build_metadata(
    project_dir: &Path,
    ini: &str,
    cfg: &ProjectConfig,
    envs: &[String],
    core: &CoreDelegation,
    scons_py: &Path,
    opts: &BuildOptions,
) -> Result<serde_json::Map<String, serde_json::Value>> {
    for env in envs {
        let prepared =
            prepare_env(cfg, env, vec!["__idedata".to_string()], ini, core, scons_py, opts)?;
        let (code, output) = scons::run_scons_captured(&prepared.scons, project_dir);
        // Upstream tolerates a ReturnErrorCode(1) as long as idedata was emitted,
        // but a build that never produced `"includes":` is a hard error.
        if code != 0 && !output.contains("\"includes\":") {
            bail!("metadata build failed for `{env}`:\n{output}");
        }
    }

    let mut result = serde_json::Map::new();
    for env in envs {
        let path = build_dir_for(cfg, env).join("idedata.json");
        if path.is_file() {
            let text = std::fs::read_to_string(&path)
                .map_err(|e| anyhow!("reading {}: {e}", path.display()))?;
            let val: serde_json::Value =
                serde_json::from_str(&text).map_err(|e| anyhow!("parsing {}: {e}", path.display()))?;
            result.insert(env.clone(), val);
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn cfg_from(ini: &str) -> (tempfile::TempDir, ProjectConfig) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("platformio.ini");
        let mut f = std::fs::File::create(&path).unwrap();
        f.write_all(ini.as_bytes()).unwrap();
        let cfg = ProjectConfig::new(&path.to_string_lossy()).unwrap();
        (dir, cfg)
    }

    #[test]
    fn select_envs_explicit_default_all() {
        let _g = crate::test_lock::guard();
        let ini = "[platformio]\ndefault_envs = b\n\n[env:a]\n[env:b]\n[env:c]\n";
        let (_d, cfg) = cfg_from(ini);
        // explicit -e wins
        assert_eq!(select_envs(&cfg, &["a".into(), "c".into()]), ["a", "c"]);
        // no -e => default_envs
        assert_eq!(select_envs(&cfg, &[]), ["b"]);
    }

    #[test]
    fn select_envs_all_when_no_default() {
        let _g = crate::test_lock::guard();
        let ini = "[env:a]\n[env:b]\n";
        let (_d, cfg) = cfg_from(ini);
        assert_eq!(select_envs(&cfg, &[]), ["a", "b"]);
    }

    #[test]
    fn env_targets_reads_option() {
        let _g = crate::test_lock::guard();
        let ini = "[env:a]\ntargets = upload, size\n[env:b]\n";
        let (_d, cfg) = cfg_from(ini);
        assert_eq!(env_targets(&cfg, "a"), ["upload", "size"]);
        assert!(env_targets(&cfg, "b").is_empty());
    }
}
