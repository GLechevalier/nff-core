//! Port of `platformio/app.py` — the `DEFAULT_SETTINGS` registry and the
//! `appstate.json` key/value store under the core directory.
//!
//! In scope (what the `settings` command needs): `DEFAULT_SETTINGS`, the `State`
//! JSON store (`<core_dir>/appstate.json`, tolerant load / write-if-modified),
//! `get_setting` (env `PLATFORMIO_SETTING_<NAME>` → state → default),
//! `set_setting`/`reset_settings`, and `sanitize_setting` (with the exact
//! `InvalidSettingName`/`InvalidSettingValue` messages).
//!
//! Out of scope (documented deviation): telemetry identity (`get_cid`/
//! `get_host_id`/`get_user_agent`), session vars, and the `LockFile` — the store
//! is written without an advisory lock (single-process CLI). `projects_dir`'s
//! default is approximated as `<Documents>/PlatformIO/Projects` (upstream probes
//! the OS "Documents" dir the same way); no test asserts the value.

use std::fmt;
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value as Json};

use crate::config::options::{abspath, expanduser, get_core_dir};

/// Errors surfaced by the settings store (mirror `platformio/exception.py`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AppError {
    /// `InvalidSettingName`.
    InvalidSettingName(String),
    /// `InvalidSettingValue`.
    InvalidSettingValue { value: String, name: String },
    /// `HomeDirPermissionsError` (write failure).
    HomeDirPermissions(String),
}

impl fmt::Display for AppError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidSettingName(name) => {
                write!(f, "Invalid setting with the name '{name}'")
            }
            Self::InvalidSettingValue { value, name } => {
                write!(f, "Invalid value '{value}' for the setting '{name}'")
            }
            Self::HomeDirPermissions(dir) => write!(
                f,
                "The directory `{dir}` or its parent directory is not owned by the \
                 current user and PlatformIO can not store configuration data."
            ),
        }
    }
}

impl std::error::Error for AppError {}

type Result<T> = std::result::Result<T, AppError>;

/// One entry of `DEFAULT_SETTINGS`.
#[derive(Debug, Clone)]
pub struct SettingDef {
    pub name: &'static str,
    pub description: &'static str,
    pub default: Json,
}

/// `platformio.project.helpers.get_default_projects_dir` (approximated).
fn get_default_projects_dir() -> String {
    let docs = dirs::document_dir()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|| format!("{}/Documents", expanduser("~")));
    PathBuf::from(docs).join("PlatformIO").join("Projects").to_string_lossy().into_owned()
}

/// `platformio.app.DEFAULT_SETTINGS`, in the upstream declaration order.
#[must_use]
pub fn default_settings() -> Vec<SettingDef> {
    vec![
        SettingDef {
            name: "check_platformio_interval",
            description: "Check for the new PlatformIO Core interval (days)",
            default: json!(7),
        },
        SettingDef {
            name: "check_prune_system_threshold",
            description: "Check for pruning unnecessary data threshold (megabytes)",
            default: json!(1024),
        },
        SettingDef {
            name: "enable_cache",
            description: "Enable caching for HTTP API requests",
            default: json!(true),
        },
        SettingDef {
            name: "enable_telemetry",
            description: "Telemetry service <https://bit.ly/pio-telemetry> (Yes/No)",
            default: json!(true),
        },
        SettingDef {
            name: "force_verbose",
            description: "Force verbose output when processing environments",
            default: json!(false),
        },
        SettingDef {
            name: "projects_dir",
            description: "Default location for PlatformIO projects (PlatformIO Home)",
            default: json!(get_default_projects_dir()),
        },
        SettingDef {
            name: "enable_proxy_strict_ssl",
            description: "Verify the proxy server certificate against the list of supplied CAs",
            default: json!(true),
        },
    ]
}

fn find_default(name: &str) -> Option<SettingDef> {
    default_settings().into_iter().find(|d| d.name == name)
}

// --- appstate.json store ----------------------------------------------------

fn appstate_path() -> PathBuf {
    PathBuf::from(get_core_dir()).join("appstate.json")
}

/// `State.__enter__` — load the store; any error yields an empty dict.
fn load_state() -> Map<String, Json> {
    let path = appstate_path();
    std::fs::read_to_string(&path)
        .ok()
        .and_then(|s| serde_json::from_str::<Json>(&s).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default()
}

/// `State.__exit__` — write the store (creating the core dir as needed).
fn save_state(state: &Map<String, Json>) -> Result<()> {
    let path = appstate_path();
    if let Some(dir) = path.parent() {
        if !dir.is_dir() {
            std::fs::create_dir_all(dir)
                .map_err(|_| AppError::HomeDirPermissions(dir.to_string_lossy().into_owned()))?;
        }
    }
    std::fs::write(&path, Json::Object(state.clone()).to_string()).map_err(|_| {
        AppError::HomeDirPermissions(
            path.parent().unwrap_or(Path::new("")).to_string_lossy().into_owned(),
        )
    })
}

/// The "Python-ish" string form used by `sanitize_setting`'s bool/int coercion.
fn as_input_string(value: &Json) -> String {
    match value {
        Json::String(s) => s.clone(),
        Json::Bool(b) => if *b { "True" } else { "False" }.to_string(),
        other => other.to_string(),
    }
}

/// `platformio.app.sanitize_setting`.
pub fn sanitize_setting(name: &str, value: Json) -> Result<Json> {
    let def = find_default(name).ok_or_else(|| AppError::InvalidSettingName(name.to_string()))?;
    let raw = as_input_string(&value);

    // `projects_dir` is the only setting with a validator (isdir + abspath).
    if name == "projects_dir" {
        if !Path::new(&raw).is_dir() {
            return Err(AppError::InvalidSettingValue { value: raw, name: name.to_string() });
        }
        return Ok(Json::String(abspath(&raw)));
    }

    match &def.default {
        Json::Bool(_) => {
            if let Json::Bool(_) = value {
                Ok(value)
            } else {
                let truthy = matches!(raw.to_lowercase().as_str(), "true" | "yes" | "y" | "1");
                Ok(Json::Bool(truthy))
            }
        }
        Json::Number(_) => raw
            .trim()
            .parse::<i64>()
            .map(|n| json!(n))
            .map_err(|_| AppError::InvalidSettingValue { value: raw, name: name.to_string() }),
        _ => Ok(value),
    }
}

/// `platformio.app.get_setting` — env → state → default precedence.
#[must_use]
pub fn get_setting(name: &str) -> Json {
    let env_name = format!("PLATFORMIO_SETTING_{}", name.to_uppercase());
    if let Ok(v) = std::env::var(&env_name) {
        return sanitize_setting(name, Json::String(v.clone())).unwrap_or(Json::String(v));
    }
    let state = load_state();
    if let Some(v) = state.get("settings").and_then(Json::as_object).and_then(|s| s.get(name)) {
        return v.clone();
    }
    find_default(name).map(|d| d.default).unwrap_or(Json::Null)
}

/// `platformio.app.set_setting`.
pub fn set_setting(name: &str, value: &str) -> Result<()> {
    let sanitized = sanitize_setting(name, Json::String(value.to_string()))?;
    let mut state = load_state();
    let settings = state
        .entry("settings".to_string())
        .or_insert_with(|| Json::Object(Map::new()));
    if let Some(obj) = settings.as_object_mut() {
        obj.insert(name.to_string(), sanitized);
    }
    save_state(&state)
}

/// `platformio.app.reset_settings`.
pub fn reset_settings() -> Result<()> {
    let mut state = load_state();
    if state.remove("settings").is_some() {
        return save_state(&state);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_lock;

    /// Point the store at an isolated temp core dir for the duration of a test.
    struct CoreGuard {
        prev: Option<String>,
        _dir: tempfile::TempDir,
    }
    impl CoreGuard {
        fn new() -> Self {
            let dir = tempfile::tempdir().expect("tempdir");
            let prev = std::env::var("PLATFORMIO_CORE_DIR").ok();
            std::env::set_var("PLATFORMIO_CORE_DIR", dir.path());
            Self { prev, _dir: dir }
        }
    }
    impl Drop for CoreGuard {
        fn drop(&mut self) {
            match &self.prev {
                Some(v) => std::env::set_var("PLATFORMIO_CORE_DIR", v),
                None => std::env::remove_var("PLATFORMIO_CORE_DIR"),
            }
        }
    }

    #[test]
    fn defaults_have_seven_keys() {
        let names: Vec<&str> = default_settings().iter().map(|d| d.name).collect();
        assert_eq!(names.len(), 7);
        assert!(names.contains(&"enable_cache"));
        assert!(names.contains(&"projects_dir"));
    }

    #[test]
    fn get_setting_returns_default_when_unset() {
        let _lk = test_lock::guard();
        let _g = CoreGuard::new();
        std::env::remove_var("PLATFORMIO_SETTING_ENABLE_CACHE");
        assert_eq!(get_setting("enable_cache"), json!(true));
        assert_eq!(get_setting("check_platformio_interval"), json!(7));
    }

    #[test]
    fn set_get_reset_roundtrip() {
        let _lk = test_lock::guard();
        let _g = CoreGuard::new();
        std::env::remove_var("PLATFORMIO_SETTING_ENABLE_CACHE");
        set_setting("enable_cache", "no").expect("set");
        assert_eq!(get_setting("enable_cache"), json!(false));
        reset_settings().expect("reset");
        assert_eq!(get_setting("enable_cache"), json!(true));
    }

    #[test]
    fn env_override_wins() {
        let _lk = test_lock::guard();
        let _g = CoreGuard::new();
        std::env::set_var("PLATFORMIO_SETTING_ENABLE_CACHE", "false");
        assert_eq!(get_setting("enable_cache"), json!(false));
        std::env::remove_var("PLATFORMIO_SETTING_ENABLE_CACHE");
    }

    #[test]
    fn sanitize_rejects_unknown_name() {
        let err = sanitize_setting("nope", json!("x")).unwrap_err();
        assert_eq!(err.to_string(), "Invalid setting with the name 'nope'");
    }

    #[test]
    fn sanitize_int_and_bool() {
        assert_eq!(sanitize_setting("check_platformio_interval", json!("14")).unwrap(), json!(14));
        assert_eq!(sanitize_setting("enable_telemetry", json!("yes")).unwrap(), json!(true));
        assert_eq!(sanitize_setting("enable_telemetry", json!("0")).unwrap(), json!(false));
        assert!(sanitize_setting("check_platformio_interval", json!("abc")).is_err());
    }
}
