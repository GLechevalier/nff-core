//! `pio run` — build/upload/clean project targets (M4, Phase A).
//!
//! Port of `platformio/run/cli.py`: load `platformio.ini`, validate + select the
//! environments, and for each one resolve the platform and delegate the compile
//! to SCons (see [`crate::build`]). Output is streamed live, so this handler
//! returns a [`CmdOutcome::code_only`] carrying the aggregate exit code.
//!
//! [`run_build`] is the in-process embedding used by nff (M7): it runs the same
//! [`run_core`] with its output captured to a string and the process CWD saved/
//! restored, so the long-lived nff MCP server can drive builds without leaking the
//! chdir upstream `pio run` performs.

use std::io::IsTerminal;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use anyhow::{anyhow, bail, Result};

use crate::build::{self, delegate, scons, BuildOptions, LogSink};
use crate::cli::RunArgs;
use crate::config::options::abspath;
use crate::config::ProjectConfig;
use crate::CmdOutcome;

pub fn run(args: &RunArgs) -> CmdOutcome {
    match run_impl(args) {
        Ok(code) => CmdOutcome::code_only(code),
        Err(e) => error(&e.to_string()),
    }
}

/// Resolve the absolute project dir and `platformio.ini` path from `args`. Must be
/// called **before** any chdir, so relative `-d`/`--project-conf` resolve against
/// the caller's cwd (not the project dir we are about to enter).
fn resolve_paths(args: &RunArgs) -> (String, String) {
    let project_dir = abspath(&args.project_dir);
    let ini = match &args.project_conf {
        Some(p) => abspath(p),
        None => Path::new(&project_dir).join("platformio.ini").to_string_lossy().into_owned(),
    };
    (project_dir, ini)
}

/// Binary entry (`pio run`): chdir into the project (like upstream `with
/// fs.cd(project_dir)`) and stream the build to the terminal. The process exits
/// after, so the chdir is not restored — that is fine for a one-shot CLI.
fn run_impl(args: &RunArgs) -> Result<i32> {
    let (project_dir, ini) = resolve_paths(args);
    std::env::set_current_dir(&project_dir)
        .map_err(|e| anyhow!("cannot enter project dir {project_dir}: {e}"))?;
    run_core(args, &project_dir, &ini, None)
}

/// The build engine, assuming the current directory is already `project_dir` (so
/// `${PROJECT_DIR}` interpolation and the relative `build_dir` resolve there).
/// `project_dir`/`ini` are pre-resolved absolute paths (see [`resolve_paths`]) —
/// never re-derived here, or a relative `-d` would double against the new cwd.
/// With a `sink`, all build output is routed to it instead of the terminal.
fn run_core(args: &RunArgs, project_dir: &str, ini: &str, sink: Option<LogSink>) -> Result<i32> {
    if !Path::new(ini).is_file() {
        bail!(
            "Not a PlatformIO project. `platformio.ini` file has not been found in \
             current working directory ({project_dir}). To initialize new project \
             please use `platformio project init` command"
        );
    }

    let cfg = ProjectConfig::new(ini).map_err(|e| anyhow!("{e}"))?;
    let requested = &args.environment;
    cfg.validate(if requested.is_empty() { None } else { Some(requested.as_slice()) }, false)
        .map_err(|e| anyhow!("{e}"))?;

    let envs = build::select_envs(&cfg, requested);
    if envs.is_empty() {
        bail!("Nothing to process. Check your `platformio.ini`.");
    }

    // Resolve the shared delegation pieces once (python + core sconstruct, scons).
    let core = delegate::resolve_core_delegation()?;
    let scons_py = delegate::ensure_tool_scons()?;

    let opts = BuildOptions {
        jobs: args.jobs.unwrap_or_else(build::default_jobs),
        verbose: args.verbose,
        silent: args.silent,
        isatty: sink.is_none() && std::io::stdout().is_terminal(),
        program_args: args.program_arg.clone(),
        upload_port: args.upload_port.clone(),
        log_sink: sink,
    };

    let mut overall = 0;
    for env in &envs {
        // CLI `-t` overrides the env's configured targets.
        let targets = if args.target.is_empty() {
            build::env_targets(&cfg, env)
        } else {
            args.target.clone()
        };
        // Native fast-path (Phase B / M6): opt-in, ESP32 only. Returns `Some(code)`
        // when it handled the env; `None` means unsupported ⇒ delegate to SCons.
        if build::native::gate_enabled(args) {
            match build::native::build_env(
                &cfg,
                env,
                targets.clone(),
                ini,
                Path::new(project_dir),
                &core,
                &scons_py,
                &opts,
            )? {
                Some(code) => {
                    if code != 0 {
                        overall = code;
                    }
                    continue;
                }
                None => { /* unsupported family: fall through to SCons delegation */ }
            }
        }

        let prepared = build::prepare_env(&cfg, env, targets, ini, &core, &scons_py, &opts)?;
        // With a sink (in-process), capture the delegated build; else stream it.
        let code = if let Some(sink) = &opts.log_sink {
            sink(&format!("Processing {env}...\n"));
            let (code, output) = scons::run_scons_captured(&prepared.scons, Path::new(project_dir));
            sink(&output);
            code
        } else {
            println!("Processing {env}...");
            scons::run_scons(&prepared.scons, Path::new(project_dir))
        };
        if code != 0 {
            overall = code;
        }
    }
    Ok(overall)
}

/// The outcome of an in-process [`run_build`]: the aggregate exit code and the
/// full captured build output (progress + compiler log).
pub struct BuildReport {
    pub code: i32,
    pub output: String,
}

/// Serializes in-process builds so the process-global chdir below is never
/// observed half-mutated by a concurrent caller (nff's MCP server is multi-request).
static BUILD_LOCK: Mutex<()> = Mutex::new(());

/// Restores the process CWD on drop (including on error/panic), so an in-process
/// build never leaks the chdir into `project_dir`.
struct CwdGuard(Option<PathBuf>);
impl Drop for CwdGuard {
    fn drop(&mut self) {
        if let Some(prev) = self.0.take() {
            let _ = std::env::set_current_dir(prev);
        }
    }
}

/// Run a build in-process (M7), capturing its output instead of streaming to the
/// terminal. Used by nff so `nff compile`/`nff flash` and the MCP tools call the
/// native engine directly. Serialized + CWD-guarded so the long-lived nff MCP
/// server is safe. `args.native = true` selects the native ESP32 fast-path.
pub fn run_build(args: &RunArgs) -> Result<BuildReport> {
    let _lock = BUILD_LOCK.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
    let _guard = CwdGuard(std::env::current_dir().ok());

    // Resolve paths against the caller's cwd, THEN chdir (so a relative `-d`
    // doesn't double), mirroring `run_impl`.
    let (project_dir, ini) = resolve_paths(args);
    std::env::set_current_dir(&project_dir)
        .map_err(|e| anyhow!("cannot enter project dir {project_dir}: {e}"))?;

    let buf = Arc::new(Mutex::new(String::new()));
    let sink: LogSink = {
        let b = buf.clone();
        Arc::new(move |s: &str| {
            if let Ok(mut g) = b.lock() {
                g.push_str(s);
            }
        })
    };
    let code = run_core(args, &project_dir, &ini, Some(sink))?;
    // The sink Arc lived only inside `opts`; it is dropped now, so `buf` is unique.
    let output = Arc::try_unwrap(buf)
        .map(|m| m.into_inner().unwrap_or_else(std::sync::PoisonError::into_inner))
        .unwrap_or_else(|arc| arc.lock().map(|g| g.clone()).unwrap_or_default());
    Ok(BuildReport { code, output })
}

fn error(message: &str) -> CmdOutcome {
    CmdOutcome { code: 1, stdout: String::new(), stderr: format!("Error: {message}\n"), streamed: false }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_build_restores_cwd_even_on_error() {
        let _g = crate::test_lock::guard();
        let dir = tempfile::tempdir().unwrap();
        // A project dir with no platformio.ini => run_core bails; CwdGuard must
        // still restore the original working directory.
        let before = std::env::current_dir().unwrap();
        let args = RunArgs { project_dir: dir.path().to_string_lossy().into_owned(), ..Default::default() };
        let res = run_build(&args);
        assert!(res.is_err(), "no platformio.ini should error");
        assert_eq!(std::env::current_dir().unwrap(), before, "cwd must be restored");
    }
}
