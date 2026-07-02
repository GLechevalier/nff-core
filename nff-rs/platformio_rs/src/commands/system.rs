//! `pio system <info|prune|completion>` — port of `platformio/system/`.
//!
//! Only `info` (pure metadata) is implemented at M3; `prune` (deletes files) and
//! `completion` (shell-specific) are deferred and return `not_implemented`.

use serde_json::{json, Map, Value as Json};

use crate::cli::{SystemAction, SystemArgs};
use crate::config::options::get_core_dir;
use crate::package::manager::{get_systype, PackageManager};
use crate::output::tabulate_plain;
use crate::{CmdOutcome, PIO_CORE_VERSION};

pub fn run(args: &SystemArgs) -> CmdOutcome {
    match &args.action {
        SystemAction::Info { json_output } => info(*json_output),
        SystemAction::Prune(_) => CmdOutcome::not_implemented("system prune"),
        SystemAction::Completion(_) => CmdOutcome::not_implemented("system completion"),
    }
}

/// `system_info_cmd`. The `python_*` fields have no true Rust analogue, so they
/// carry documented placeholder/derived values (no test asserts them).
fn info(json_output: bool) -> CmdOutcome {
    let lib_nums = PackageManager::library(None).get_installed().map(|v| v.len()).unwrap_or(0);
    let platform_nums = PackageManager::platform(None).get_installed().map(|v| v.len()).unwrap_or(0);
    let tool_nums = PackageManager::tool(None).get_installed().map(|v| v.len()).unwrap_or(0);
    let exe = std::env::current_exe()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|_| "pio-rs".to_string());

    let entries: Vec<(&str, &str, Json)> = vec![
        ("core_version", "PlatformIO Core", json!(PIO_CORE_VERSION)),
        ("python_version", "Python", json!(python_version())),
        ("system", "System Type", json!(get_systype())),
        ("platform", "Platform", json!(host_platform())),
        ("filesystem_encoding", "File System Encoding", json!("utf-8")),
        ("locale_encoding", "Locale Encoding", json!("utf-8")),
        ("core_dir", "PlatformIO Core Directory", json!(get_core_dir())),
        ("platformio_exe", "PlatformIO Core Executable", json!(exe)),
        ("python_exe", "Python Executable", json!(python_exe())),
        ("global_lib_nums", "Global Libraries", json!(lib_nums)),
        ("dev_platform_nums", "Development Platforms", json!(platform_nums)),
        ("package_tool_nums", "Tools & Toolchains", json!(tool_nums)),
    ];

    if json_output {
        let mut data = Map::new();
        for (key, title, value) in &entries {
            data.insert((*key).to_string(), json!({ "title": title, "value": value }));
        }
        return CmdOutcome::ok(format!("{}\n", Json::Object(data)));
    }

    let rows: Vec<Vec<String>> =
        entries.iter().map(|(_, title, value)| vec![(*title).to_string(), scalar(value)]).collect();
    CmdOutcome::ok(format!("{}\n", tabulate_plain(&rows)))
}

fn scalar(v: &Json) -> String {
    match v {
        Json::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// No embedded Python interpreter — report "n/a" (documented deviation).
fn python_version() -> String {
    std::env::var("PYTHON_VERSION").unwrap_or_else(|_| "n/a".to_string())
}

fn python_exe() -> String {
    "n/a".to_string()
}

/// A terse host descriptor, standing in for `platform.platform(terse=True)`.
fn host_platform() -> String {
    format!("{}-{}", std::env::consts::OS, std::env::consts::ARCH)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::{SystemAction, SystemArgs};
    use crate::test_lock;

    #[test]
    fn info_json_has_expected_keys() {
        let _lk = test_lock::guard();
        std::env::set_var("PLATFORMIO_NO_ANSI", "true");
        let out = run(&SystemArgs { action: SystemAction::Info { json_output: true } });
        assert_eq!(out.code, 0);
        let parsed: Json = serde_json::from_str(out.stdout.trim()).expect("json");
        for key in ["core_version", "system", "core_dir", "dev_platform_nums"] {
            assert!(parsed.get(key).is_some(), "missing {key}");
        }
        assert_eq!(parsed["core_version"]["value"], PIO_CORE_VERSION);
        std::env::remove_var("PLATFORMIO_NO_ANSI");
    }

    #[test]
    fn info_table_lists_titles() {
        let _lk = test_lock::guard();
        std::env::set_var("PLATFORMIO_NO_ANSI", "true");
        let out = run(&SystemArgs { action: SystemAction::Info { json_output: false } });
        assert!(out.stdout.contains("PlatformIO Core"));
        assert!(out.stdout.contains("Development Platforms"));
        std::env::remove_var("PLATFORMIO_NO_ANSI");
    }

    #[test]
    fn prune_is_deferred() {
        let out = run(&SystemArgs { action: SystemAction::Prune(Default::default()) });
        assert_ne!(out.code, 0);
    }
}
