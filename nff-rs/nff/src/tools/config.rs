use dirs::home_dir;
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;
use thiserror::Error;

#[derive(Error, Debug)]
pub enum ConfigError {
    #[error("Could not read config: {0}")]
    Read(#[from] std::io::Error),
    #[error("Could not parse config: {0}")]
    Parse(#[from] serde_json::Error),
    #[error("{0}")]
    #[allow(dead_code)]
    Other(String),
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct Config {
    pub version: String,
    pub default_device: DeviceConfig,
    #[serde(default)]
    pub diagnosis: DiagnosisConfig,
    #[serde(default)]
    pub mcp: McpConfig,
    #[serde(default)]
    pub agent: AgentConfig,
    #[serde(default)]
    pub build: BuildConfig,
    #[serde(default)]
    pub debug: DebugConfig,
    #[serde(default)]
    pub policy: PolicyConfig,
    /// Local/offline mode: `nff init` skips the cloud sign-in and only local
    /// build/flash/monitor/debug are enabled. Sticky once set; cleared on a successful login.
    #[serde(default)]
    pub offline: bool,
    /// The most recent successful build artifact, recorded by `compile`/`flash`.
    #[serde(default)]
    pub last_build: Option<LastBuild>,
    /// Count of CLI invocations seen, used to pace the "star the repo / go Pro" nudge
    /// (shown every Nth run). Persisted so the cadence survives across separate CLI processes.
    #[serde(default)]
    pub nudge_count: u64,
    #[serde(default)]
    pub update: UpdateConfig,
}

/// The most recent successful build artifact, recorded by `compile`/`flash` so
/// `nff status` can report what's on the bench. `kind` is "elf" or "bin";
/// `built_at` is unix seconds.
#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct LastBuild {
    pub path: String,
    pub kind: String,
    pub built_at: i64,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct DeviceConfig {
    pub port: Option<String>,
    pub board: Option<String>,
    pub fqbn: Option<String>,
    #[serde(default = "default_baud")]
    pub baud: u32,
}

impl Default for DeviceConfig {
    fn default() -> Self {
        DeviceConfig {
            port: None,
            board: None,
            fqbn: None,
            baud: 9600,
        }
    }
}

fn default_baud() -> u32 {
    9600
}

fn default_server_url() -> String {
    "https://nanoforgeflow.com".into()
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct DiagnosisConfig {
    #[serde(default = "default_server_url")]
    pub server_url: String,
    #[serde(default = "default_server_url")]
    pub frontend_url: String,
    pub access_token: Option<String>,
    pub refresh_token: Option<String>,
}

impl Default for DiagnosisConfig {
    fn default() -> Self {
        DiagnosisConfig {
            server_url: default_server_url(),
            frontend_url: default_server_url(),
            access_token: None,
            refresh_token: None,
        }
    }
}

/// Opaque tokens the local MCP OAuth proxy issues to Claude Code. Decoupled from
/// the diagnosis (Supabase) JWT so the MCP session does not expire with it.
#[derive(Serialize, Deserialize, Debug, Clone, Default)]
pub struct McpConfig {
    pub access_token: Option<String>,
    pub refresh_token: Option<String>,
}

fn default_agent_server_url() -> String {
    "https://agent.nanoforgeflow.com".into()
}

fn default_local_mcp_url() -> String {
    "http://127.0.0.1:3010/mcp".into()
}

/// Cloud-agent pairing config (`nff agent`). `server_url` = the deployed
/// nff-agent-worker HTTP endpoint; `local_mcp_url` = THIS bench's `nff mcp` (so the
/// cloud agent can reach the connected hardware); `project_id` is optional (the
/// worker resolves it from the diagnosis JWT when unset). Auth reuses the diagnosis
/// tokens, so no tokens live here.
#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct AgentConfig {
    #[serde(default = "default_agent_server_url")]
    pub server_url: String,
    #[serde(default = "default_local_mcp_url")]
    pub local_mcp_url: String,
    #[serde(default)]
    pub project_id: Option<String>,
}

impl Default for AgentConfig {
    fn default() -> Self {
        AgentConfig {
            server_url: default_agent_server_url(),
            local_mcp_url: default_local_mcp_url(),
            project_id: None,
        }
    }
}

fn default_backend() -> String {
    "platformio".into()
}

/// Build backend selection. `backend` is "platformio" (default, board-universal) or
/// "arduino"; `board` carries the PlatformIO board id (e.g. "esp32dev") used when the
/// platformio backend is active — the arduino backend uses `default_device.fqbn` instead.
#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct BuildConfig {
    #[serde(default = "default_backend")]
    pub backend: String,
    #[serde(default)]
    pub board: Option<String>,
}

impl Default for BuildConfig {
    fn default() -> Self {
        BuildConfig {
            backend: default_backend(),
            board: None,
        }
    }
}

/// On-chip debug config (`nff debug` / the debug_* MCP tools). All keys are optional
/// overrides — when empty, nff auto-detects openocd_path/gdb_path from PlatformIO's package
/// cache (else PATH), openocd_config from the chip's built-in-JTAG/ST-Link cfg, and
/// interface for an external probe.
#[derive(Serialize, Deserialize, Debug, Clone, Default)]
pub struct DebugConfig {
    #[serde(default)]
    pub openocd_path: Option<String>,
    #[serde(default)]
    pub gdb_path: Option<String>,
    #[serde(default)]
    pub openocd_config: Option<String>,
    #[serde(default)]
    pub interface: Option<String>,
}

/// Local POAD-MDP policy layer (tools/policy.rs): the MCP server records every tool
/// call into ~/.nff/policy.json and advises the learned repair procedure once a faulty
/// state has `min_support` prior fixes. `NFF_POLICY=off` overrides per-run.
#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct PolicyConfig {
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default = "default_min_support")]
    pub min_support: u64,
}

fn default_true() -> bool {
    true
}

/// Self-update (tools/updater.rs). `auto` gates the background check-and-swap that runs
/// after CLI commands; `nff update` stays available either way. `NFF_NO_AUTO_UPDATE=1`
/// overrides per-run. Mutable update state (last check time, staged versions, errors)
/// lives in ~/.nff/update.json, not here.
#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct UpdateConfig {
    #[serde(default = "default_true")]
    pub auto: bool,
}

impl Default for UpdateConfig {
    fn default() -> Self {
        UpdateConfig { auto: true }
    }
}

fn default_min_support() -> u64 {
    3
}

impl Default for PolicyConfig {
    fn default() -> Self {
        PolicyConfig {
            enabled: true,
            min_support: 3,
        }
    }
}

impl Default for Config {
    fn default() -> Self {
        Config {
            version: "1".into(),
            default_device: DeviceConfig::default(),
            diagnosis: DiagnosisConfig::default(),
            mcp: McpConfig::default(),
            agent: AgentConfig::default(),
            build: BuildConfig::default(),
            debug: DebugConfig::default(),
            policy: PolicyConfig::default(),
            offline: false,
            last_build: None,
            nudge_count: 0,
            update: UpdateConfig::default(),
        }
    }
}

pub fn config_path() -> PathBuf {
    // NFF_CONFIG_DIR overrides the config location — used by tests for deterministic
    // isolation (HOME isn't honored by dirs::home_dir() on Windows), and handy for
    // running multiple isolated nff setups.
    if let Ok(dir) = std::env::var("NFF_CONFIG_DIR") {
        if !dir.is_empty() {
            return PathBuf::from(dir).join("config.json");
        }
    }
    home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".nff")
        .join("config.json")
}

/// The nff state directory (parent of config.json) — also home to caches like the
/// PlatformIO board-hwid index. Honors the same `NFF_CONFIG_DIR` override as `config_path`.
pub fn config_dir() -> PathBuf {
    config_path()
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."))
}

pub fn exists() -> bool {
    config_path().exists()
}

pub fn load() -> Result<Config, ConfigError> {
    let path = config_path();
    if !path.exists() {
        return Ok(Config::default());
    }
    let raw = fs::read_to_string(&path)?;
    Ok(serde_json::from_str(&raw)?)
}

pub fn save(config: &Config) -> Result<(), ConfigError> {
    let path = config_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, serde_json::to_string_pretty(config)?)?;
    fs::rename(&tmp, &path)?;
    Ok(())
}

pub fn get_default_device() -> Result<DeviceConfig, ConfigError> {
    Ok(load()?.default_device)
}

pub fn set_default_device(
    port: &str,
    board: &str,
    fqbn: &str,
    baud: u32,
) -> Result<(), ConfigError> {
    let mut config = load()?;
    config.default_device = DeviceConfig {
        port: if port.is_empty() {
            None
        } else {
            Some(port.into())
        },
        board: if board.is_empty() {
            None
        } else {
            Some(board.into())
        },
        fqbn: if fqbn.is_empty() {
            None
        } else {
            Some(fqbn.into())
        },
        baud,
    };
    save(&config)
}

#[allow(dead_code)]
pub fn get_diagnosis_config() -> Result<DiagnosisConfig, ConfigError> {
    Ok(load()?.diagnosis)
}

pub fn set_diagnosis_tokens(access: &str, refresh: &str) -> Result<(), ConfigError> {
    let mut config = load()?;
    config.diagnosis.access_token = Some(access.into());
    config.diagnosis.refresh_token = Some(refresh.into());
    save(&config)
}

pub fn clear_diagnosis_tokens() -> Result<(), ConfigError> {
    let mut config = load()?;
    config.diagnosis.access_token = None;
    config.diagnosis.refresh_token = None;
    save(&config)
}

#[allow(dead_code)]
pub fn set_diagnosis_server_url(url: &str) -> Result<(), ConfigError> {
    let mut config = load()?;
    config.diagnosis.server_url = url.into();
    save(&config)
}

pub fn get_mcp_tokens() -> Result<McpConfig, ConfigError> {
    Ok(load()?.mcp)
}

pub fn set_mcp_tokens(access: &str, refresh: &str) -> Result<(), ConfigError> {
    let mut config = load()?;
    config.mcp.access_token = Some(access.into());
    config.mcp.refresh_token = Some(refresh.into());
    save(&config)
}

pub fn clear_mcp_tokens() -> Result<(), ConfigError> {
    let mut config = load()?;
    config.mcp.access_token = None;
    config.mcp.refresh_token = None;
    save(&config)
}

#[allow(dead_code)]
pub fn get_build_config() -> Result<BuildConfig, ConfigError> {
    Ok(load()?.build)
}

pub fn get_debug_config() -> Result<DebugConfig, ConfigError> {
    Ok(load()?.debug)
}

/// Policy-layer config, defaulting (enabled, min_support 3) on any read error so the
/// tap stays best-effort.
pub fn get_policy_config() -> PolicyConfig {
    load().map(|c| c.policy).unwrap_or_default()
}

/// Self-update config, defaulting (auto on) on any read error so the after-command hook
/// stays best-effort.
pub fn get_update_config() -> UpdateConfig {
    load().map(|c| c.update).unwrap_or_default()
}

pub fn set_build_backend(backend: &str) -> Result<(), ConfigError> {
    let mut config = load()?;
    config.build.backend = backend.into();
    save(&config)
}

pub fn set_build_board(board: Option<&str>) -> Result<(), ConfigError> {
    let mut config = load()?;
    config.build.board = board.map(String::from);
    save(&config)
}

pub fn set_offline(offline: bool) -> Result<(), ConfigError> {
    let mut config = load()?;
    config.offline = offline;
    save(&config)
}

/// Record the most recent successful build artifact for `nff status` to report.
pub fn set_last_build(path: &str, kind: &str, built_at: i64) -> Result<(), ConfigError> {
    let mut config = load()?;
    config.last_build = Some(LastBuild {
        path: path.into(),
        kind: kind.into(),
        built_at,
    });
    save(&config)
}

pub fn get_last_build() -> Result<Option<LastBuild>, ConfigError> {
    Ok(load()?.last_build)
}

/// Increment and persist the CLI nudge counter, returning the new value. Used to pace the
/// "star the repo / go Pro" reminder (shown every Nth CLI run). Best-effort: on any config
/// read/write error it returns 0 so the caller simply skips the nudge this run.
pub fn bump_nudge_count() -> u64 {
    let mut config = match load() {
        Ok(c) => c,
        Err(_) => return 0,
    };
    config.nudge_count = config.nudge_count.saturating_add(1);
    let next = config.nudge_count;
    if save(&config).is_err() {
        return 0;
    }
    next
}

/// Current unix time in seconds (0 if the clock is somehow before the epoch).
pub fn now_unix() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Whether nff is in local/offline mode: cloud sign-in is skipped and only local
/// build/flash/monitor/debug are enabled. Precedence: `NFF_OFFLINE` env var (truthy:
/// `1`/`true`/`yes`/`on`, case-insensitive) → `offline` in config → false. Mirrors the
/// env→config→default shape of `active_backend`.
pub fn is_offline() -> bool {
    if let Ok(env) = std::env::var("NFF_OFFLINE") {
        match env.trim().to_lowercase().as_str() {
            "1" | "true" | "yes" | "on" => return true,
            "0" | "false" | "no" | "off" => return false,
            _ => {}
        }
    }
    load().map(|c| c.offline).unwrap_or(false)
}

/// Normalize a backend name to "arduino" or "platformio". "pio" aliases platformio;
/// only an explicit "arduino"/"arduino-cli" selects the arduino-cli backend.
fn normalize_backend(name: &str) -> String {
    match name.trim().to_lowercase().as_str() {
        "arduino" | "arduino-cli" => "arduino".into(),
        _ => "platformio".into(),
    }
}

/// The active build backend: "platformio" (default) or "arduino". Precedence:
/// `NFF_BUILD_BACKEND` env var → `build.backend` in config → "platformio".
pub fn active_backend() -> String {
    if let Ok(env) = std::env::var("NFF_BUILD_BACKEND") {
        if !env.trim().is_empty() {
            return normalize_backend(&env);
        }
    }
    let name = load()
        .map(|c| c.build.backend)
        .unwrap_or_else(|_| default_backend());
    normalize_backend(&name)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_config_round_trips() {
        let config = Config::default();
        let json = serde_json::to_string_pretty(&config).unwrap();
        let parsed: Config = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.version, "1");
        assert_eq!(parsed.default_device.baud, 9600);
    }

    #[test]
    fn build_config_defaults_to_platformio() {
        assert_eq!(BuildConfig::default().backend, "platformio");
        assert!(BuildConfig::default().board.is_none());
    }

    #[test]
    fn build_section_is_optional_in_legacy_config() {
        // A config.json written before the build section existed must still parse.
        let legacy = r#"{
            "version": "1",
            "default_device": {"port": null, "board": null, "fqbn": null, "baud": 9600}
        }"#;
        let parsed: Config = serde_json::from_str(legacy).unwrap();
        assert_eq!(parsed.build.backend, "platformio");
    }

    #[test]
    fn offline_defaults_false_and_round_trips() {
        let mut config = Config::default();
        assert!(!config.offline);
        config.offline = true;
        let json = serde_json::to_string_pretty(&config).unwrap();
        let parsed: Config = serde_json::from_str(&json).unwrap();
        assert!(parsed.offline);
    }

    #[test]
    fn offline_absent_in_legacy_config() {
        // A config.json written before the offline field existed must still parse.
        let legacy = r#"{
            "version": "1",
            "default_device": {"port": null, "board": null, "fqbn": null, "baud": 9600}
        }"#;
        let parsed: Config = serde_json::from_str(legacy).unwrap();
        assert!(!parsed.offline);
    }

    #[test]
    fn nudge_count_defaults_zero_and_round_trips() {
        let mut config = Config::default();
        assert_eq!(config.nudge_count, 0);
        config.nudge_count = 42;
        let json = serde_json::to_string_pretty(&config).unwrap();
        let parsed: Config = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.nudge_count, 42);
    }

    #[test]
    fn nudge_count_absent_in_legacy_config() {
        // A config.json written before the nudge_count field existed must still parse.
        let legacy = r#"{
            "version": "1",
            "default_device": {"port": null, "board": null, "fqbn": null, "baud": 9600}
        }"#;
        let parsed: Config = serde_json::from_str(legacy).unwrap();
        assert_eq!(parsed.nudge_count, 0);
    }

    #[test]
    fn update_section_defaults_auto_on_and_is_optional_in_legacy_config() {
        assert!(UpdateConfig::default().auto);
        // A config.json written before the update section existed must still parse.
        let legacy = r#"{
            "version": "1",
            "default_device": {"port": null, "board": null, "fqbn": null, "baud": 9600}
        }"#;
        let parsed: Config = serde_json::from_str(legacy).unwrap();
        assert!(parsed.update.auto);
    }

    #[test]
    fn normalize_backend_aliases() {
        assert_eq!(normalize_backend("pio"), "platformio");
        assert_eq!(normalize_backend("platformio"), "platformio");
        assert_eq!(normalize_backend("PlatformIO"), "platformio");
        assert_eq!(normalize_backend("arduino"), "arduino");
        assert_eq!(normalize_backend("arduino-cli"), "arduino");
        assert_eq!(normalize_backend("  ARDUINO  "), "arduino");
        assert_eq!(normalize_backend("nonsense"), "platformio");
    }
}
