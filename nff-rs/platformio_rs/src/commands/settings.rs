//! `pio settings <get|set|reset>` — port of `platformio/commands/settings.py`.

use serde_json::Value as Json;

use crate::app;
use crate::cli::{SettingsAction, SettingsArgs};
use crate::output::{style, tabulate_simple, Color};
use crate::CmdOutcome;

pub fn run(args: &SettingsArgs) -> CmdOutcome {
    match &args.action {
        SettingsAction::Get { name } => CmdOutcome::ok(get_output(name.as_deref())),
        SettingsAction::Set { name, value } => set_cmd(name, value),
        SettingsAction::Reset => reset_cmd(),
    }
}

/// `settings.format_value`.
fn format_value(raw: &Json) -> String {
    match raw {
        Json::Bool(b) => if *b { "Yes" } else { "No" }.to_string(),
        Json::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// `settings_get` — the table of `Name / Current value [Default] / Description`.
fn get_output(name: Option<&str>) -> String {
    let mut defs = app::default_settings();
    defs.sort_by(|a, b| a.name.cmp(b.name));

    let mut rows: Vec<Vec<String>> = Vec::new();
    for def in &defs {
        if let Some(n) = name {
            if n != def.name {
                continue;
            }
        }
        let raw = app::get_setting(def.name);
        let mut formatted = format_value(&raw);
        if raw != def.default {
            let default_formatted = format_value(&def.default);
            formatted.push_str(if default_formatted.chars().count() > 10 { "\n" } else { " " });
            formatted.push_str(&format!("[{}]", style(&default_formatted, Color::Yellow)));
        }
        rows.push(vec![style(def.name, Color::Cyan), formatted, def.description.to_string()]);
    }

    format!("{}\n", tabulate_simple(&["Name", "Current value [Default]", "Description"], &rows))
}

fn set_cmd(name: &str, value: &str) -> CmdOutcome {
    match app::set_setting(name, value) {
        Ok(()) => {
            let mut out =
                format!("{}\n", style("The new value for the setting has been set!", Color::Green));
            out.push_str(&get_output(Some(name)));
            CmdOutcome::ok(out)
        }
        Err(e) => error(&e.to_string()),
    }
}

fn reset_cmd() -> CmdOutcome {
    match app::reset_settings() {
        Ok(()) => {
            let mut out = format!("{}\n", style("The settings have been reset!", Color::Green));
            out.push_str(&get_output(None));
            CmdOutcome::ok(out)
        }
        Err(e) => error(&e.to_string()),
    }
}

fn error(message: &str) -> CmdOutcome {
    CmdOutcome { code: 1, stdout: String::new(), stderr: format!("Error: {message}\n"), streamed: false }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::{SettingsAction, SettingsArgs};
    use crate::test_lock;

    struct CoreGuard {
        prev: Option<String>,
        _dir: tempfile::TempDir,
    }
    impl CoreGuard {
        fn new() -> Self {
            let dir = tempfile::tempdir().expect("tempdir");
            let prev = std::env::var("PLATFORMIO_CORE_DIR").ok();
            std::env::set_var("PLATFORMIO_CORE_DIR", dir.path());
            std::env::set_var("PLATFORMIO_NO_ANSI", "true");
            Self { prev, _dir: dir }
        }
    }
    impl Drop for CoreGuard {
        fn drop(&mut self) {
            match &self.prev {
                Some(v) => std::env::set_var("PLATFORMIO_CORE_DIR", v),
                None => std::env::remove_var("PLATFORMIO_CORE_DIR"),
            }
            std::env::remove_var("PLATFORMIO_NO_ANSI");
        }
    }

    #[test]
    fn get_lists_every_default_setting() {
        let _lk = test_lock::guard();
        let _g = CoreGuard::new();
        let out = get_output(None);
        // The parity test asserts every DEFAULT_SETTINGS key appears in output.
        for def in app::default_settings() {
            assert!(out.contains(def.name), "missing setting {}", def.name);
        }
        assert!(out.contains("Name") && out.contains("Description"));
    }

    #[test]
    fn set_then_get_reflects_change() {
        let _lk = test_lock::guard();
        let _g = CoreGuard::new();
        std::env::remove_var("PLATFORMIO_SETTING_ENABLE_TELEMETRY");
        let args = SettingsArgs {
            action: SettingsAction::Set {
                name: "enable_telemetry".into(),
                value: "no".into(),
            },
        };
        let out = run(&args);
        assert_eq!(out.code, 0);
        assert!(out.stdout.contains("has been set"));
        assert!(out.stdout.contains("No"));
    }

    #[test]
    fn set_unknown_setting_errors() {
        let _lk = test_lock::guard();
        let _g = CoreGuard::new();
        let out = set_cmd("bogus", "1");
        assert_ne!(out.code, 0);
        assert!(out.stderr.contains("Invalid setting with the name 'bogus'"));
    }
}
