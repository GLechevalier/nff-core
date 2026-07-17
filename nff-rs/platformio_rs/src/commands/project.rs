//! `pio project <config|metadata|init>` — port of `platformio/project/commands/`.
//!
//! `config` (computed configuration dump) reuses the M1 `ProjectConfig` engine.
//! `metadata` (needs build-system output, M4) and `init` (heavy scaffolding) are
//! deferred and return `not_implemented`.

use std::path::Path;

use serde_json::json;

use crate::build;
use crate::cli::{ProjectAction, ProjectArgs};
use crate::config::options::abspath;
use crate::config::{ProjectConfig, Value};
use crate::output::{style, tabulate_plain, Color};
use crate::CmdOutcome;

pub fn run(args: &ProjectArgs) -> CmdOutcome {
    match &args.action {
        ProjectAction::Config { project_dir, lint, json_output } => {
            config(project_dir, *lint, *json_output)
        }
        ProjectAction::Metadata { project_dir, environment, json_output, json_output_path } => {
            metadata(project_dir, environment, *json_output, json_output_path.as_deref())
        }
        ProjectAction::Init(_) => CmdOutcome::not_implemented("project init"),
    }
}

fn config(project_dir: &str, lint: bool, json_output: bool) -> CmdOutcome {
    let ini = Path::new(project_dir).join("platformio.ini");
    if !ini.is_file() {
        // `NotPlatformIOProjectError` (exact message).
        let msg = format!(
            "Not a PlatformIO project. `platformio.ini` file has not been found in \
             current working directory ({project_dir}). To initialize new project \
             please use `platformio project init` command"
        );
        return error(&msg);
    }
    let ini_str = ini.to_string_lossy().into_owned();

    if lint {
        return lint_configuration(&ini_str, json_output);
    }

    let cfg = match ProjectConfig::new(&ini_str) {
        Ok(c) => c,
        Err(e) => return error(&e.to_string()),
    };

    if json_output {
        return match cfg.to_json() {
            Ok(j) => CmdOutcome::ok(format!("{j}\n")),
            Err(e) => error(&e.to_string()),
        };
    }

    let tuple = match cfg.as_tuple() {
        Ok(t) => t,
        Err(e) => return error(&e.to_string()),
    };
    let mut out = format!(
        "Computed project configuration for {}\n",
        style(&abspath(project_dir), Color::Cyan)
    );
    for (section, options) in &tuple {
        out.push_str(&style(section, Color::Cyan));
        out.push('\n');
        out.push_str(&"-".repeat(section.chars().count()));
        out.push('\n');
        let rows: Vec<Vec<String>> = options
            .iter()
            .map(|(name, value)| vec![name.clone(), "=".to_string(), render_value(value)])
            .collect();
        out.push_str(&tabulate_plain(&rows));
        out.push('\n');
        out.push('\n');
    }
    CmdOutcome::ok(out)
}

/// `"\n".join(value) if isinstance(value, list) else value`.
fn render_value(value: &Value) -> String {
    match value {
        Value::List(items) => {
            items.iter().map(Value::to_plain_string).collect::<Vec<_>>().join("\n")
        }
        other => other.to_plain_string(),
    }
}

fn lint_configuration(ini: &str, json_output: bool) -> CmdOutcome {
    let result = ProjectConfig::lint(ini);
    if json_output {
        let payload = json!({
            "errors": result.errors.iter().map(|e| json!({
                "type": e.type_name, "message": e.message, "lineno": e.lineno,
            })).collect::<Vec<_>>(),
            "warnings": result.warnings,
        });
        return CmdOutcome::ok(format!("{payload}\n"));
    }
    if result.errors.is_empty() && result.warnings.is_empty() {
        return CmdOutcome::ok(format!(
            "{}\n",
            style(
                "The \"platformio.ini\" configuration file is free from linting errors.",
                Color::Green
            )
        ));
    }
    let mut out = String::new();
    if !result.errors.is_empty() {
        let rows: Vec<Vec<String>> = result
            .errors
            .iter()
            .map(|e| {
                vec![
                    style(&e.type_name, Color::Red),
                    e.message.clone(),
                    e.lineno.map(|l| format!(":{l}")).unwrap_or_default(),
                ]
            })
            .collect();
        out.push_str(&tabulate_plain(&rows));
        out.push('\n');
    }
    if !result.warnings.is_empty() {
        let rows: Vec<Vec<String>> =
            result.warnings.iter().map(|w| vec![style("Warning", Color::Yellow), w.clone()]).collect();
        out.push_str(&tabulate_plain(&rows));
        out.push('\n');
    }
    CmdOutcome::ok(out)
}

/// `pio project metadata` — port of `project/commands/metadata.py`. Builds each
/// env's `idedata.json` (via the `__idedata` target) and emits an env-keyed JSON
/// map, to a file (`--json-output-path`) and/or stdout (`--json-output`).
fn metadata(
    project_dir: &str,
    environments: &[String],
    json_output: bool,
    json_output_path: Option<&str>,
) -> CmdOutcome {
    match metadata_impl(project_dir, environments, json_output, json_output_path) {
        Ok(out) => CmdOutcome::ok(out),
        Err(e) => error(&e.to_string()),
    }
}

fn metadata_impl(
    project_dir: &str,
    environments: &[String],
    json_output: bool,
    json_output_path: Option<&str>,
) -> anyhow::Result<String> {
    let project_dir = abspath(project_dir);
    let ini = Path::new(&project_dir).join("platformio.ini").to_string_lossy().into_owned();
    if !Path::new(&ini).is_file() {
        anyhow::bail!(
            "Not a PlatformIO project. `platformio.ini` file has not been found in current \
             working directory ({project_dir}). To initialize new project please use \
             `platformio project init` command"
        );
    }
    // Match `with fs.cd(project_dir)` so relative `build_dir` resolves there.
    std::env::set_current_dir(&project_dir)
        .map_err(|e| anyhow::anyhow!("cannot enter {project_dir}: {e}"))?;

    let cfg = ProjectConfig::new(&ini).map_err(|e| anyhow::anyhow!("{e}"))?;
    cfg.validate(if environments.is_empty() { None } else { Some(environments) }, false)
        .map_err(|e| anyhow::anyhow!("{e}"))?;
    let envs: Vec<String> = if environments.is_empty() { cfg.envs() } else { environments.to_vec() };

    let core = build::delegate::resolve_core_delegation()?;
    let scons_py = build::delegate::ensure_tool_scons()?;
    let opts = build::BuildOptions {
        jobs: build::default_jobs(),
        verbose: false,
        silent: true,
        isatty: false,
        program_args: Vec::new(),
        upload_port: None,
    };
    let md = build::load_build_metadata(
        Path::new(&project_dir),
        &ini,
        &cfg,
        &envs,
        &core,
        &scons_py,
        &opts,
    )?;
    let value = serde_json::Value::Object(md);

    let mut out = String::new();
    if let Some(p) = json_output_path {
        let path = if Path::new(p).is_dir() {
            Path::new(p).join("metadata.json").to_string_lossy().into_owned()
        } else {
            p.to_string()
        };
        std::fs::write(&path, serde_json::to_string(&value)?)
            .map_err(|e| anyhow::anyhow!("writing {path}: {e}"))?;
        out.push_str(&format!("{}\n", style(&format!("Saved metadata to the {path}"), Color::Green)));
    }
    if json_output {
        out.push_str(&serde_json::to_string(&value)?);
        out.push('\n');
    }
    // Human path (no JSON flags): a per-env table, like metadata.py's fallback.
    if !json_output && json_output_path.is_none() {
        if let Some(obj) = value.as_object() {
            for (envname, meta) in obj {
                out.push_str(&format!("Environment: {}\n", style(envname, Color::Cyan)));
                out.push_str(&"=".repeat(13 + envname.chars().count()));
                out.push('\n');
                let rows: Vec<Vec<String>> = meta
                    .as_object()
                    .map(|m| {
                        m.iter()
                            .map(|(name, val)| {
                                vec![
                                    name.clone(),
                                    "=".to_string(),
                                    serde_json::to_string_pretty(val).unwrap_or_default(),
                                ]
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                out.push_str(&tabulate_plain(&rows));
                out.push_str("\n\n");
            }
        }
    }
    Ok(out)
}

fn error(message: &str) -> CmdOutcome {
    CmdOutcome { code: 1, stdout: String::new(), stderr: format!("Error: {message}\n"), streamed: false }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::{ProjectAction, ProjectArgs};
    use crate::test_lock;
    use std::fs;

    fn cfg_args(dir: &str, json_output: bool) -> ProjectArgs {
        ProjectArgs {
            action: ProjectAction::Config {
                project_dir: dir.to_string(),
                lint: false,
                json_output,
            },
        }
    }

    #[test]
    fn missing_project_errors() {
        let _lk = test_lock::guard();
        let dir = tempfile::tempdir().unwrap();
        let out = run(&cfg_args(&dir.path().to_string_lossy(), false));
        assert_ne!(out.code, 0);
        assert!(out.stderr.contains("Not a PlatformIO project"));
    }

    #[test]
    fn config_json_and_human() {
        let _lk = test_lock::guard();
        std::env::set_var("PLATFORMIO_NO_ANSI", "true");
        let dir = tempfile::tempdir().unwrap();
        fs::write(
            dir.path().join("platformio.ini"),
            "[env:uno]\nplatform = atmelavr\nboard = uno\nframework = arduino\n",
        )
        .unwrap();
        let dir_s = dir.path().to_string_lossy().into_owned();

        let json_out = run(&cfg_args(&dir_s, true));
        assert_eq!(json_out.code, 0, "stderr={}", json_out.stderr);
        let parsed: serde_json::Value = serde_json::from_str(json_out.stdout.trim()).expect("json");
        assert!(parsed.is_array() || parsed.is_object());

        let human = run(&cfg_args(&dir_s, false));
        assert_eq!(human.code, 0);
        assert!(human.stdout.contains("Computed project configuration for"));
        assert!(human.stdout.contains("env:uno"));
        assert!(human.stdout.contains("board"));
        std::env::remove_var("PLATFORMIO_NO_ANSI");
    }
}
