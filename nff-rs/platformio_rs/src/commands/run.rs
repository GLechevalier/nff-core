//! `pio run` — build/upload/clean project targets (M4, Phase A).
//!
//! Port of `platformio/run/cli.py`: load `platformio.ini`, validate + select the
//! environments, and for each one resolve the platform and delegate the compile
//! to SCons (see [`crate::build`]). Output is streamed live, so this handler
//! returns a [`CmdOutcome::code_only`] carrying the aggregate exit code.

use std::io::IsTerminal;
use std::path::Path;

use anyhow::{anyhow, bail, Result};

use crate::build::{self, delegate, scons, BuildOptions};
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

fn run_impl(args: &RunArgs) -> Result<i32> {
    let project_dir = abspath(&args.project_dir);
    let ini = match &args.project_conf {
        Some(p) => abspath(p),
        None => Path::new(&project_dir).join("platformio.ini").to_string_lossy().into_owned(),
    };
    if !Path::new(&ini).is_file() {
        bail!(
            "Not a PlatformIO project. `platformio.ini` file has not been found in \
             current working directory ({project_dir}). To initialize new project \
             please use `platformio project init` command"
        );
    }

    // Match upstream `with fs.cd(project_dir)`: run relative to the project so
    // `${PROJECT_DIR}` interpolation and the relative `build_dir` resolve there.
    std::env::set_current_dir(&project_dir)
        .map_err(|e| anyhow!("cannot enter project dir {project_dir}: {e}"))?;

    let cfg = ProjectConfig::new(&ini).map_err(|e| anyhow!("{e}"))?;
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
        isatty: std::io::stdout().is_terminal(),
        program_args: args.program_arg.clone(),
        upload_port: args.upload_port.clone(),
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
                &ini,
                Path::new(&project_dir),
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

        let prepared = build::prepare_env(&cfg, env, targets, &ini, &core, &scons_py, &opts)?;
        println!("Processing {env}...");
        let code = scons::run_scons(&prepared.scons, Path::new(&project_dir));
        if code != 0 {
            overall = code;
        }
    }
    Ok(overall)
}

fn error(message: &str) -> CmdOutcome {
    CmdOutcome { code: 1, stdout: String::new(), stderr: format!("Error: {message}\n"), streamed: false }
}
