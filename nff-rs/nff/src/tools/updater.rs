//! Self-update for nff — Claude-Code-style background auto-update.
//!
//! Flow: after a CLI command finishes, `after_command_hook` (called next to the nudge)
//! surfaces any pending update notices and — at most once per throttle window — spawns a
//! detached `nff update --background` process. That process checks the latest GitHub
//! Release, downloads and sha256-verifies the new binary, and atomically swaps it into
//! place so the NEXT invocation runs the new version. Zero latency on the foreground
//! command; a running `nff mcp` server keeps its old image until restarted.
//!
//! Auto-update applies only to the **standalone-binary** install channel
//! (scripts/install.sh / install.ps1). Wheel installs (pip/pipx/uv — being deprecated)
//! only get a "new version available, reinstall standalone" notice; dev builds are left
//! alone. When an update attempt fails, `nff update` runs `nff doctor` for diagnostics;
//! a background failure is surfaced on the next foreground run instead.
//!
//! All mutable update state lives in `~/.nff/update.json` — deliberately NOT in
//! config.json, whose whole-file writes (nudge counter et al.) would race with the
//! background updater last-writer-wins. `~/.nff/update.lock` single-flights concurrent
//! updaters (MCP server + CLI + parallel shells).
//!
//! Mirrors the Python reference in `nff/nff/tools/updater.py`; keep the two in sync
//! (the pure-logic tests here are duplicated from `tests/test_updater.py` as the
//! behavioral parity oracle).

use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

use crate::tools::config;

/// GitHub Releases base; overridable via NFF_UPDATE_BASE_URL so tests can point at a
/// local HTTP server.
const DEFAULT_BASE_URL: &str = "https://github.com/GLechevalier/nff/releases";
/// Background check cadence.
const DEFAULT_EVERY_HOURS: i64 = 24;
/// A lock file older than this is from a dead updater and may be reclaimed.
const LOCK_STALE_SECONDS: i64 = 3600;
/// Staged downloads older than this are garbage-collected.
const STAGING_STALE_SECONDS: i64 = 24 * 3600;

const INSTALL_SH: &str = "curl -fsSL https://nanoforgeflow.com/install.sh | sh";
const INSTALL_PS1: &str = "irm https://nanoforgeflow.com/install.ps1 | iex";

/// An update attempt failed at a specific stage (check/download/checksum/verify/swap).
#[derive(Debug)]
pub struct UpdateError {
    pub stage: &'static str,
    pub detail: String,
}

impl std::fmt::Display for UpdateError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.detail)
    }
}

impl std::error::Error for UpdateError {}

fn err(stage: &'static str, detail: impl Into<String>) -> UpdateError {
    UpdateError {
        stage,
        detail: detail.into(),
    }
}

/// The install channel this nff came from. Unknown defaults to `Wheel` — the
/// notice-only channel — never to swapping.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Channel {
    Standalone,
    Wheel,
    Dev,
}

impl Channel {
    pub fn name(self) -> &'static str {
        match self {
            Channel::Standalone => "standalone",
            Channel::Wheel => "wheel",
            Channel::Dev => "dev",
        }
    }
}

// ---------------------------------------------------------------------------
// State (~/.nff/update.json)
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Debug, Clone, Default)]
pub struct UpdateState {
    /// Unix seconds of the last completed version check (throttle anchor).
    #[serde(default)]
    pub last_check_at: i64,
    /// Newest version seen upstream ("X.Y.Z"), or None if never checked.
    #[serde(default)]
    pub latest_version: Option<String>,
    /// Set by a successful background swap; cleared once the "✓ updated" notice shows.
    #[serde(default)]
    pub updated_to: Option<String>,
    /// Wheel channel: version we already nagged about (notice once per version).
    #[serde(default)]
    pub notified_version: Option<String>,
    /// The last failure; None when healthy.
    #[serde(default)]
    pub last_error: Option<UpdateErrorRecord>,
    /// Has the recorded background failure been shown on a foreground run yet?
    #[serde(default)]
    pub error_surfaced: bool,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct UpdateErrorRecord {
    pub version: Option<String>,
    pub stage: String,
    pub detail: String,
    pub at: i64,
}

// ---------------------------------------------------------------------------
// Env / config gates
// ---------------------------------------------------------------------------

/// Whether auto-update is globally disabled via NFF_NO_AUTO_UPDATE (truthy: 1/true/yes/on).
pub fn disabled() -> bool {
    match std::env::var("NFF_NO_AUTO_UPDATE") {
        Ok(v) => matches!(v.trim().to_lowercase().as_str(), "1" | "true" | "yes" | "on"),
        Err(_) => false,
    }
}

/// Throttle window in hours: NFF_UPDATE_EVERY_HOURS if it parses to a positive int.
pub fn every_hours() -> i64 {
    std::env::var("NFF_UPDATE_EVERY_HOURS")
        .ok()
        .and_then(|v| v.trim().parse::<i64>().ok())
        .filter(|&n| n > 0)
        .unwrap_or(DEFAULT_EVERY_HOURS)
}

/// Release base URL (…/releases), overridable via NFF_UPDATE_BASE_URL.
pub fn base_url() -> String {
    std::env::var("NFF_UPDATE_BASE_URL")
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| DEFAULT_BASE_URL.to_string())
        .trim_end_matches('/')
        .to_string()
}

// ---------------------------------------------------------------------------
// Paths (resolved at call time so NFF_CONFIG_DIR works)
// ---------------------------------------------------------------------------

pub fn state_path() -> PathBuf {
    config::config_dir().join("update.json")
}

pub fn lock_path() -> PathBuf {
    config::config_dir().join("update.lock")
}

pub fn log_path() -> PathBuf {
    config::config_dir().join("update.log")
}

fn staging_dir() -> PathBuf {
    config::config_dir().join("updates")
}

pub fn marker_path() -> PathBuf {
    config::config_dir().join("install-channel")
}

// ---------------------------------------------------------------------------
// State load/save
// ---------------------------------------------------------------------------

/// Update state; a corrupt or missing file yields the defaults.
pub fn load_state() -> UpdateState {
    load_state_from(&state_path())
}

fn load_state_from(path: &Path) -> UpdateState {
    fs::read_to_string(path)
        .ok()
        .and_then(|raw| serde_json::from_str(&raw).ok())
        .unwrap_or_default()
}

/// Atomic write (tmp + rename), best-effort: update state is never worth crashing for.
pub fn save_state(state: &UpdateState) {
    save_state_to(&state_path(), state);
}

fn save_state_to(path: &Path, state: &UpdateState) {
    let Ok(json) = serde_json::to_string_pretty(state) else {
        return;
    };
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let tmp = path.with_extension("json.tmp");
    if fs::write(&tmp, json).is_ok() {
        let _ = fs::rename(&tmp, path);
    }
}

fn record_error(version: Option<&str>, e: &UpdateError) {
    let mut state = load_state();
    state.last_error = Some(UpdateErrorRecord {
        version: version.map(String::from),
        stage: e.stage.to_string(),
        detail: e.detail.clone(),
        at: config::now_unix(),
    });
    state.error_surfaced = false;
    save_state(&state);
}

// ---------------------------------------------------------------------------
// Versions
// ---------------------------------------------------------------------------

pub fn current_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// "X.Y.Z" (optional leading v) → (X, Y, Z), or None if it doesn't parse. Non-numeric
/// versions (e.g. the rolling "staging" prerelease) return None → treated as not-an-update.
pub fn parse_version(text: &str) -> Option<(u64, u64, u64)> {
    let parts: Vec<&str> = text.trim().trim_start_matches('v').split('.').collect();
    if parts.len() != 3 {
        return None;
    }
    Some((
        parts[0].parse().ok()?,
        parts[1].parse().ok()?,
        parts[2].parse().ok()?,
    ))
}

/// Extract "X.Y.Z" from a GitHub release-tag URL ending …/tag/vX.Y.Z.
pub fn parse_tag_version(location: &str) -> Option<String> {
    let tail = location.trim_end_matches('/').rsplit('/').next()?;
    parse_version(tail).map(|_| tail.trim_start_matches('v').to_string())
}

pub fn is_newer(candidate: &str, current: &str) -> bool {
    match (parse_version(candidate), parse_version(current)) {
        (Some(a), Some(b)) => a > b,
        _ => false,
    }
}

// ---------------------------------------------------------------------------
// Platform → release asset (same table as scripts/install.sh)
// ---------------------------------------------------------------------------

pub fn asset_name() -> Option<&'static str> {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("linux", "x86_64") => Some("nff-linux-x64"),
        ("linux", "aarch64") => Some("nff-linux-arm64"),
        ("macos", "aarch64") => Some("nff-macos-arm64"),
        ("macos", "x86_64") => Some("nff-macos-x64"),
        // No native Windows-arm64 build; the x64 binary runs via emulation.
        ("windows", _) => Some("nff-windows-x64.exe"),
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// Install-channel detection
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct Marker {
    pub channel: String,
    pub path: String,
    #[serde(default)]
    pub installed_at: i64,
}

pub fn default_install_dir() -> PathBuf {
    if cfg!(windows) {
        std::env::var("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|_| {
                dirs::home_dir()
                    .unwrap_or_else(|| PathBuf::from("."))
                    .join("AppData")
                    .join("Local")
            })
            .join("Programs")
            .join("nff")
    } else {
        dirs::home_dir()
            .unwrap_or_else(|| PathBuf::from("."))
            .join(".local")
            .join("bin")
    }
}

pub fn read_marker() -> Option<Marker> {
    let raw = fs::read_to_string(marker_path()).ok()?;
    // Tolerate a UTF-8 BOM from PowerShell-written markers.
    serde_json::from_str(raw.trim_start_matches('\u{feff}')).ok()
}

/// Record the standalone install location (written by the install scripts and re-written
/// after every successful swap, so legacy installs self-heal onto it).
pub fn write_marker(path: &Path) {
    let marker = Marker {
        channel: "standalone".into(),
        path: path.display().to_string(),
        installed_at: config::now_unix(),
    };
    if let Ok(json) = serde_json::to_string_pretty(&marker) {
        if let Some(parent) = marker_path().parent() {
            let _ = fs::create_dir_all(parent);
        }
        let _ = fs::write(marker_path(), json);
    }
}

fn current_exe_path() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    Some(exe.canonicalize().unwrap_or(exe))
}

/// Pure core of channel detection, testable without env/process state.
fn classify_channel(exe: &Path, marker: Option<&Marker>, default_dir: &Path) -> Channel {
    // Dev builds: a cargo target tree — checked first so a dev build sitting next to a
    // stale marker never self-updates.
    let parts: Vec<String> = exe
        .components()
        .map(|c| c.as_os_str().to_string_lossy().to_lowercase())
        .collect();
    if parts.iter().any(|p| p == "target")
        && parts.iter().any(|p| p == "debug" || p == "release")
    {
        return Channel::Dev;
    }
    if let Some(m) = marker {
        if m.channel == "standalone" {
            let marker_exe = PathBuf::from(&m.path);
            let marker_exe = marker_exe.canonicalize().unwrap_or(marker_exe);
            if marker_exe == exe {
                return Channel::Standalone;
            }
        }
    }
    // Legacy standalone installs that predate the marker file.
    if exe.parent() == Some(default_dir) {
        return Channel::Standalone;
    }
    // Anything else (pip/pipx/uv wheel Scripts dir, unknown locations) → notice-only.
    Channel::Wheel
}

pub fn detect_channel() -> Channel {
    match current_exe_path() {
        Some(exe) => classify_channel(&exe, read_marker().as_ref(), &default_install_dir()),
        None => Channel::Wheel,
    }
}

/// The installed binary a standalone-channel update should replace.
pub fn standalone_target() -> Option<PathBuf> {
    if let Some(m) = read_marker() {
        if m.channel == "standalone" && !m.path.is_empty() {
            return Some(PathBuf::from(m.path));
        }
    }
    let exe = current_exe_path()?;
    if exe.parent() == Some(default_install_dir().as_path()) {
        return Some(exe);
    }
    None
}

// ---------------------------------------------------------------------------
// Throttle + lock (single-flight)
// ---------------------------------------------------------------------------

pub fn should_check(state: &UpdateState, now: i64) -> bool {
    now - state.last_check_at >= every_hours() * 3600
}

fn lock_mtime_unix(path: &Path) -> Option<i64> {
    let meta = fs::metadata(path).ok()?;
    let mtime = meta.modified().ok()?;
    mtime
        .duration_since(std::time::UNIX_EPOCH)
        .ok()
        .map(|d| d.as_secs() as i64)
}

/// Take the single-flight updater lock. A stale lock (dead updater) is reclaimed.
pub fn acquire_lock() -> bool {
    acquire_lock_at(&lock_path(), config::now_unix())
}

fn acquire_lock_at(path: &Path, now: i64) -> bool {
    if let Some(parent) = path.parent() {
        if fs::create_dir_all(parent).is_err() {
            return false;
        }
    }
    for _ in 0..2 {
        match fs::OpenOptions::new().write(true).create_new(true).open(path) {
            Ok(mut fh) => {
                use std::io::Write;
                let _ = write!(fh, "{} {}", std::process::id(), now);
                return true;
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                match lock_mtime_unix(path) {
                    Some(mtime) if now - mtime > LOCK_STALE_SECONDS => {
                        let _ = fs::remove_file(path); // stale — reclaim on the retry
                        continue;
                    }
                    _ => return false,
                }
            }
            Err(_) => return false,
        }
    }
    false
}

pub fn release_lock() {
    let _ = fs::remove_file(lock_path());
}

/// A live (non-stale) lock exists — some other updater is at work.
pub fn lock_held() -> bool {
    lock_held_at(&lock_path(), config::now_unix())
}

fn lock_held_at(path: &Path, now: i64) -> bool {
    matches!(lock_mtime_unix(path), Some(mtime) if now - mtime <= LOCK_STALE_SECONDS)
}

// ---------------------------------------------------------------------------
// Check / download / verify
// ---------------------------------------------------------------------------

/// The newest released version "X.Y.Z", via the /releases/latest redirect — a plain web
/// redirect, so no GitHub API rate limits and no token.
pub fn check_latest() -> Result<String, UpdateError> {
    let url = format!("{}/latest", base_url());
    let client = reqwest::blocking::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(10))
        .build()
        .map_err(|e| err("check", format!("http client: {e}")))?;
    let resp = client
        .head(&url)
        .send()
        .map_err(|e| err("check", format!("could not reach {url}: {e}")))?;
    let location = resp
        .headers()
        .get(reqwest::header::LOCATION)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    parse_tag_version(location).ok_or_else(|| {
        err(
            "check",
            format!(
                "unexpected response from {url} (status {}, Location {location:?})",
                resp.status()
            ),
        )
    })
}

/// The lowercase hex digest for `asset` from a SHA256SUMS file (handles the binary-mode
/// `*asset` form), or None if not listed.
pub fn parse_sha256sums(text: &str, asset: &str) -> Option<String> {
    for line in text.lines() {
        let mut parts = line.split_whitespace();
        if let (Some(hash), Some(name)) = (parts.next(), parts.next()) {
            if name.trim_start_matches('*') == asset {
                return Some(hash.to_lowercase());
            }
        }
    }
    None
}

fn sha256_file(path: &Path) -> Result<String, UpdateError> {
    use sha2::{Digest, Sha256};
    let bytes =
        fs::read(path).map_err(|e| err("checksum", format!("could not read {}: {e}", path.display())))?;
    let mut hasher = Sha256::new();
    hasher.update(&bytes);
    Ok(format!("{:x}", hasher.finalize()))
}

/// Drop leftover partial downloads and stale staged binaries. Runs under the lock, so a
/// `.partial` on disk can only be an aborted earlier attempt.
fn gc_staging(now: i64) {
    let Ok(entries) = fs::read_dir(staging_dir()) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let is_partial = path
            .extension()
            .is_some_and(|e| e.eq_ignore_ascii_case("partial"));
        let stale = matches!(lock_mtime_unix(&path), Some(m) if now - m > STAGING_STALE_SECONDS);
        if is_partial || stale {
            let _ = fs::remove_file(path);
        }
    }
}

/// Download the release asset for `version` into the staging dir, verify its sha256
/// against the release's SHA256SUMS (mandatory), and return the staged path.
pub fn download_and_stage(version: &str) -> Result<PathBuf, UpdateError> {
    let asset = asset_name().ok_or_else(|| {
        err(
            "check",
            format!(
                "unsupported platform: {}/{}",
                std::env::consts::OS,
                std::env::consts::ARCH
            ),
        )
    })?;

    gc_staging(config::now_unix());
    let dir = staging_dir();
    fs::create_dir_all(&dir).map_err(|e| err("download", format!("could not create {}: {e}", dir.display())))?;
    let staged = dir.join(asset);
    let partial = dir.join(format!("{asset}.partial"));

    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(120))
        .build()
        .map_err(|e| err("download", format!("http client: {e}")))?;

    // Versioned URL (not latest/download): pins the check-time version so a release
    // landing mid-update can't hand us mismatched asset + checksums.
    let url = format!("{}/download/v{version}/{asset}", base_url());
    let mut resp = client
        .get(&url)
        .send()
        .and_then(|r| r.error_for_status())
        .map_err(|e| err("download", format!("download failed: {url}: {e}")))?;
    let mut file = fs::File::create(&partial)
        .map_err(|e| err("download", format!("could not write {}: {e}", partial.display())))?;
    resp.copy_to(&mut file)
        .map_err(|e| err("download", format!("download failed: {url}: {e}")))?;
    drop(file);

    let sums_url = format!("{}/download/v{version}/SHA256SUMS", base_url());
    let sums = client
        .get(&sums_url)
        .send()
        .and_then(|r| r.error_for_status())
        .and_then(|r| r.text())
        .map_err(|e| err("checksum", format!("could not fetch SHA256SUMS: {e}")))?;
    let expected = parse_sha256sums(&sums, asset)
        .ok_or_else(|| err("checksum", format!("{asset} not listed in SHA256SUMS for v{version}")))?;
    let actual = sha256_file(&partial)?;
    if actual != expected {
        let _ = fs::remove_file(&partial);
        return Err(err(
            "checksum",
            format!("checksum mismatch for {asset} (expected {expected}, got {actual})"),
        ));
    }

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&partial, fs::Permissions::from_mode(0o755));
    }
    fs::rename(&partial, &staged)
        .map_err(|e| err("download", format!("could not stage {}: {e}", staged.display())))?;
    Ok(staged)
}

/// Final gate before the swap: the staged binary must run and report the new version —
/// catches truncated, quarantined, or wrong-arch downloads.
pub fn verify_staged(staged: &Path, version: &str) -> Result<(), UpdateError> {
    use wait_timeout::ChildExt;
    let mut child = Command::new(staged)
        .arg("--version")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| err("verify", format!("staged binary would not run: {e}")))?;
    let status = match child.wait_timeout(Duration::from_secs(10)) {
        Ok(Some(status)) => status,
        Ok(None) => {
            let _ = child.kill();
            let _ = child.wait();
            return Err(err("verify", "staged binary timed out on --version"));
        }
        Err(e) => {
            let _ = child.kill();
            return Err(err("verify", format!("staged binary would not run: {e}")));
        }
    };
    let mut output = String::new();
    if let Some(mut out) = child.stdout.take() {
        use std::io::Read;
        let _ = out.read_to_string(&mut output);
    }
    if let Some(mut errout) = child.stderr.take() {
        use std::io::Read;
        let mut s = String::new();
        let _ = errout.read_to_string(&mut s);
        output.push(' ');
        output.push_str(&s);
    }
    if !status.success() || !output.contains(version) {
        return Err(err(
            "verify",
            format!(
                "staged binary reported {:?} (exit {:?}), expected {version}",
                output.trim(),
                status.code()
            ),
        ));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Swap
// ---------------------------------------------------------------------------

/// Replace `target` with `staged`. POSIX: same-dir copy + atomic rename (a running MCP
/// server keeps its old inode). Windows: a running exe cannot be overwritten but CAN be
/// renamed, so: copy → target.new, rename target → target.old, rename target.new →
/// target, rolling back on failure. `.old` cleanup is deferred (`cleanup_old`) because
/// an old process may still hold it. `windows` is a parameter (not cfg!) so the dance is
/// unit-testable on any OS — it's plain renames.
pub fn swap(staged: &Path, target: &Path, windows: bool) -> Result<(), UpdateError> {
    let parent = target
        .parent()
        .ok_or_else(|| err("swap", format!("no parent directory for {}", target.display())))?;
    fs::create_dir_all(parent)
        .map_err(|e| err("swap", format!("cannot access {}: {e}", parent.display())))?;

    if !windows {
        let tmp = parent.join(format!(".nff.new.{}", std::process::id()));
        let result = (|| -> std::io::Result<()> {
            fs::copy(staged, &tmp)?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                fs::set_permissions(&tmp, fs::Permissions::from_mode(0o755))?;
            }
            fs::rename(&tmp, target)
        })();
        return result.map_err(|e| {
            let _ = fs::remove_file(&tmp);
            err("swap", format!("could not install to {}: {e}", target.display()))
        });
    }

    let new = parent.join(format!("{}.new", file_name(target)));
    let old = parent.join(format!("{}.old", file_name(target)));
    fs::copy(staged, &new)
        .map_err(|e| err("swap", format!("could not write {}: {e}", new.display())))?;
    if old.exists() {
        let _ = fs::remove_file(&old); // best-effort: an old process may still hold it
    }
    let mut replaced = false;
    let result = (|| -> std::io::Result<()> {
        if target.exists() {
            fs::rename(target, &old)?;
            replaced = true;
        }
        fs::rename(&new, target)
    })();
    result.map_err(|e| {
        if replaced {
            let _ = fs::rename(&old, target); // roll the previous binary back into place
        }
        let _ = fs::remove_file(&new);
        err("swap", format!("could not install to {}: {e}", target.display()))
    })
}

fn file_name(path: &Path) -> String {
    path.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// Best-effort removal of a leftover `<target>.old` from a previous Windows swap. Fails
/// harmlessly while an old process (e.g. a running MCP server) still holds it.
pub fn cleanup_old(target: &Path) {
    if let Some(parent) = target.parent() {
        let _ = fs::remove_file(parent.join(format!("{}.old", file_name(target))));
    }
}

// ---------------------------------------------------------------------------
// Orchestration
// ---------------------------------------------------------------------------

/// The update flow. Returns a process exit code; foreground failures come back as
/// Err (after being recorded in update.json) so the command can run `nff doctor`.
///
/// - check_only: report current vs latest; exit 0 (current) or 2 (update available).
/// - background: silent; all failures are recorded in update.json and exit 0.
/// - foreground: progress lines on stdout; wheel/dev channels get guidance + exit 1.
pub fn run_update(background: bool, check_only: bool) -> Result<i32, UpdateError> {
    let current = current_version();
    let channel = detect_channel();

    if check_only {
        let version = check_latest()?;
        let mut state = load_state();
        state.last_check_at = config::now_unix();
        state.latest_version = Some(version.clone());
        save_state(&state);
        println!("current: v{current} · latest: v{version} · channel: {}", channel.name());
        if let Some(e) = load_state().last_error {
            println!("last update attempt failed at '{}': {}", e.stage, e.detail);
        }
        if is_newer(&version, current) {
            if channel == Channel::Standalone {
                println!("update available — run 'nff update' to install");
            } else {
                println!("update available — reinstall to get it (see 'nff update')");
            }
            return Ok(2);
        }
        println!("nff is up to date");
        return Ok(0);
    }

    if !background {
        match channel {
            Channel::Dev => {
                println!("dev build — update with git pull + cargo build, not the self-updater");
                return Ok(1);
            }
            Channel::Wheel => {
                let cmd = if cfg!(windows) { INSTALL_PS1 } else { INSTALL_SH };
                println!(
                    "this nff was installed from a Python wheel (pip/pipx/uv), which the \
                     self-updater does not manage (pip distribution is being deprecated)."
                );
                println!("Reinstall standalone to enable auto-update:  {cmd}");
                return Ok(1);
            }
            Channel::Standalone => {}
        }
    }

    if !acquire_lock() {
        if background {
            return Ok(0);
        }
        println!("another update is already in progress ({})", lock_path().display());
        return Ok(1);
    }

    // Everything below runs under the lock; release it on every exit path.
    let result = run_update_locked(background, channel, current);
    release_lock();
    result
}

fn run_update_locked(
    background: bool,
    channel: Channel,
    current: &str,
) -> Result<i32, UpdateError> {
    let now = config::now_unix();
    if background && !should_check(&load_state(), now) {
        return Ok(0); // another process beat us to this window
    }

    let version = match check_latest() {
        Ok(v) => v,
        Err(e) => {
            record_error(None, &e);
            if background {
                return Ok(0);
            }
            return Err(e);
        }
    };
    let mut state = load_state();
    state.last_check_at = now;
    state.latest_version = Some(version.clone());
    save_state(&state);

    if !is_newer(&version, current) {
        if !background {
            println!("nff v{current} is up to date");
        }
        return Ok(0);
    }
    if background && channel != Channel::Standalone {
        return Ok(0); // wheel: the recorded latest_version drives the foreground notice
    }

    let Some(target) = standalone_target() else {
        let e = err(
            "swap",
            format!(
                "could not locate the installed nff binary (no {} and not in {})",
                marker_path().display(),
                default_install_dir().display()
            ),
        );
        record_error(Some(&version), &e);
        if background {
            return Ok(0);
        }
        return Err(e);
    };

    let step = (|| -> Result<(), UpdateError> {
        if !background {
            println!("Downloading {} (v{version}) …", asset_name().unwrap_or("nff"));
        }
        let staged = download_and_stage(&version)?;
        if !background {
            println!("Checksum OK");
        }
        verify_staged(&staged, &version)?;
        swap(&staged, &target, cfg!(windows))?;
        let _ = fs::remove_file(&staged);
        Ok(())
    })();
    if let Err(e) = step {
        record_error(Some(&version), &e);
        if background {
            return Ok(0);
        }
        return Err(e);
    }

    let mut state = load_state();
    state.updated_to = Some(version.clone());
    state.last_error = None;
    save_state(&state);
    write_marker(&target);
    cleanup_old(&target);
    if !background {
        println!("nff updated to v{version} — restart any running 'nff mcp' server to pick it up");
    }
    Ok(0)
}

// ---------------------------------------------------------------------------
// After-command hook (called next to the nudge)
// ---------------------------------------------------------------------------

fn notice(message: &str, warning: bool) {
    eprintln!();
    if warning {
        eprintln!("{}", console::style(message).yellow());
    } else {
        eprintln!("{}", console::style(message).cyan());
    }
}

/// Spawn a fully detached `nff update --background` (daemon.rs start_background recipe).
/// Its output goes to ~/.nff/update.log (truncated — it's a diagnostic, not a journal).
fn spawn_background() {
    let _ = fs::create_dir_all(config::config_dir());
    let Ok(log) = fs::File::create(log_path()) else {
        return;
    };
    let Ok(log_err) = log.try_clone() else {
        return;
    };
    let Ok(exe) = std::env::current_exe() else {
        return;
    };

    let mut cmd = Command::new(exe);
    cmd.args(["update", "--background"])
        .stdin(Stdio::null())
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(log_err));

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // DETACHED_PROCESS: no controlling console. CREATE_NO_WINDOW: no console-window
        // flash. Together the updater outlives the CLI invocation that spawned it.
        const DETACHED_PROCESS: u32 = 0x0000_0008;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(DETACHED_PROCESS | CREATE_NO_WINDOW);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0); // detach from the parent's session
    }

    let _ = cmd.spawn();
}

/// CLI hook: surface pending update notices and maybe spawn the background updater.
///
/// Best-effort by design — it must never break the command the user actually ran.
/// Notices go to stderr like the nudge (not TTY-gated: Claude Code drives nff as a
/// subprocess and relays stderr).
pub fn after_command_hook(skip: bool) {
    if skip || disabled() || !config::get_update_config().auto {
        return;
    }
    let channel = detect_channel();
    if channel == Channel::Dev {
        return;
    }
    let mut state = load_state();
    let mut dirty = false;

    if let Some(version) = state.updated_to.take() {
        notice(&format!("✓ nff updated itself to v{version}"), false);
        dirty = true; // .take() cleared it
    }

    if let Some(e) = &state.last_error {
        if !state.error_surfaced {
            let version = e
                .version
                .as_ref()
                .map(|v| format!(" to v{v}"))
                .unwrap_or_default();
            notice(
                &format!(
                    "⚠ background self-update{version} failed at '{}' — run 'nff update' to retry with diagnostics",
                    e.stage
                ),
                true,
            );
            state.error_surfaced = true;
            dirty = true;
        }
    }

    if channel == Channel::Wheel {
        if let Some(latest) = state.latest_version.clone() {
            if is_newer(&latest, current_version())
                && state.notified_version.as_deref() != Some(latest.as_str())
            {
                let cmd = if cfg!(windows) { INSTALL_PS1 } else { INSTALL_SH };
                notice(
                    &format!(
                        "⬆ nff v{latest} is available (you have v{}). pip installs no longer auto-update — reinstall standalone:  {cmd}",
                        current_version()
                    ),
                    false,
                );
                state.notified_version = Some(latest);
                dirty = true;
            }
        }
    }

    if dirty {
        save_state(&state);
    }

    // Deferred deletion of the previous binary: the swapping process is itself the old
    // image (renamed to .old on Windows), so only a later run can remove it.
    if channel == Channel::Standalone {
        if let Some(target) = standalone_target() {
            cleanup_old(&target);
        }
    }

    if should_check(&state, config::now_unix()) && !lock_held() {
        spawn_background();
    }
}

// ---------------------------------------------------------------------------
// Tests — pure-logic cases mirrored from tests/test_updater.py (parity oracle)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("nff_updater_{name}_{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn parse_version_cases() {
        assert_eq!(parse_version("0.2.37"), Some((0, 2, 37)));
        assert_eq!(parse_version("v1.10.2"), Some((1, 10, 2)));
        assert_eq!(parse_version("staging"), None);
        assert_eq!(parse_version("1.2"), None);
        assert_eq!(parse_version("1.2.x"), None);
        assert_eq!(parse_version(""), None);
    }

    #[test]
    fn parse_tag_version_cases() {
        assert_eq!(
            parse_tag_version("https://github.com/GLechevalier/nff/releases/tag/v0.2.40"),
            Some("0.2.40".into())
        );
        assert_eq!(parse_tag_version(".../tag/v0.2.40/"), Some("0.2.40".into()));
        assert_eq!(parse_tag_version(".../tag/staging"), None);
        assert_eq!(parse_tag_version(""), None);
    }

    #[test]
    fn is_newer_cases() {
        assert!(is_newer("0.2.38", "0.2.37"));
        assert!(is_newer("0.3.0", "0.2.99"));
        assert!(!is_newer("0.2.37", "0.2.37"));
        assert!(!is_newer("0.2.36", "0.2.37"));
        assert!(!is_newer("staging", "0.2.37"));
    }

    #[test]
    fn parse_sha256sums_cases() {
        let text = "aaaa  nff-linux-x64\nbbbb  *nff-windows-x64.exe\nCCCC  nff-macos-arm64\nnot-a-valid-line\n";
        assert_eq!(parse_sha256sums(text, "nff-linux-x64"), Some("aaaa".into()));
        // binary-mode `*` prefix
        assert_eq!(parse_sha256sums(text, "nff-windows-x64.exe"), Some("bbbb".into()));
        // lowercased
        assert_eq!(parse_sha256sums(text, "nff-macos-arm64"), Some("cccc".into()));
        assert_eq!(parse_sha256sums(text, "nff-macos-x64"), None);
    }

    #[test]
    fn state_roundtrip_and_defaults() {
        let dir = tmp_dir("state");
        let path = dir.join("update.json");
        let state = load_state_from(&path);
        assert_eq!(state.last_check_at, 0);
        assert!(state.latest_version.is_none());
        let mut state = state;
        state.latest_version = Some("0.9.9".into());
        state.last_check_at = 123;
        save_state_to(&path, &state);
        let again = load_state_from(&path);
        assert_eq!(again.latest_version.as_deref(), Some("0.9.9"));
        assert_eq!(again.last_check_at, 123);
        assert!(!again.error_surfaced);
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn state_survives_corrupt_file() {
        let dir = tmp_dir("corrupt");
        let path = dir.join("update.json");
        fs::write(&path, "{not json").unwrap();
        assert!(load_state_from(&path).latest_version.is_none());
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn state_ignores_unknown_and_missing_fields() {
        // A legacy/foreign update.json must still parse (serde defaults).
        let state: UpdateState = serde_json::from_str(r#"{"latest_version": "1.2.3"}"#).unwrap();
        assert_eq!(state.latest_version.as_deref(), Some("1.2.3"));
        assert_eq!(state.last_check_at, 0);
    }

    #[test]
    fn lock_exclusive_stale_reclaim() {
        let dir = tmp_dir("lock");
        let path = dir.join("update.lock");
        let now = 1_000_000_000_i64;
        // Fresh acquire succeeds; second holder is rejected while the lock is live.
        assert!(acquire_lock_at(&path, now));
        // (the file's real mtime is "now" on disk; use the real clock for held checks)
        let real_now = config::now_unix();
        assert!(lock_held_at(&path, real_now));
        assert!(!acquire_lock_at(&path, real_now));
        // A holder far in the future sees the real-mtime lock as stale and reclaims it.
        assert!(acquire_lock_at(&path, real_now + LOCK_STALE_SECONDS + 60));
        let _ = fs::remove_file(&path);
        assert!(!lock_held_at(&path, real_now));
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn should_check_throttle() {
        // Default cadence (env untouched in tests): 24h.
        let now = 1_000_000_000_i64;
        let state = |last: i64| UpdateState {
            last_check_at: last,
            ..Default::default()
        };
        assert!(should_check(&state(0), now));
        assert!(should_check(&state(now - 25 * 3600), now));
        assert!(!should_check(&state(now - 3600), now));
    }

    #[test]
    fn classify_channel_cases() {
        let default_dir = PathBuf::from("/home/u/.local/bin");

        // Cargo target tree → dev, even with a matching marker.
        let dev_exe = PathBuf::from("/repo/nff-rs/target/release/nff");
        let marker = Marker {
            channel: "standalone".into(),
            path: dev_exe.display().to_string(),
            installed_at: 0,
        };
        assert_eq!(
            classify_channel(&dev_exe, Some(&marker), &default_dir),
            Channel::Dev
        );

        // Marker pointing at this exe → standalone (paths that don't exist on disk
        // canonicalize to themselves).
        let exe = PathBuf::from("/opt/custom/nff");
        let marker = Marker {
            channel: "standalone".into(),
            path: exe.display().to_string(),
            installed_at: 0,
        };
        assert_eq!(
            classify_channel(&exe, Some(&marker), &default_dir),
            Channel::Standalone
        );

        // Marker for a different binary → not this install → wheel.
        let other = Marker {
            channel: "standalone".into(),
            path: "/somewhere/else/nff".into(),
            installed_at: 0,
        };
        assert_eq!(
            classify_channel(&exe, Some(&other), &default_dir),
            Channel::Wheel
        );

        // No marker but sitting in the default install dir → legacy standalone.
        let legacy = default_dir.join("nff");
        assert_eq!(
            classify_channel(&legacy, None, &default_dir),
            Channel::Standalone
        );

        // Anywhere else with no marker → wheel (notice-only).
        assert_eq!(classify_channel(&exe, None, &default_dir), Channel::Wheel);
    }

    #[test]
    fn swap_posix_replaces_target() {
        let dir = tmp_dir("swap_posix");
        let staged = dir.join("staged");
        fs::write(&staged, b"NEW").unwrap();
        let target = dir.join("bin").join("nff");
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        fs::write(&target, b"OLD").unwrap();
        swap(&staged, &target, false).unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"NEW");
        assert!(staged.exists()); // swap copies; caller removes the staged file
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn swap_windows_dance_keeps_old_for_deferred_cleanup() {
        let dir = tmp_dir("swap_win");
        let staged = dir.join("staged.exe");
        fs::write(&staged, b"NEW").unwrap();
        let target = dir.join("bin").join("nff.exe");
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        fs::write(&target, b"OLD").unwrap();
        swap(&staged, &target, true).unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"NEW");
        let old = target.parent().unwrap().join("nff.exe.old");
        assert_eq!(fs::read(&old).unwrap(), b"OLD");
        cleanup_old(&target);
        assert!(!old.exists());
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn swap_windows_without_preexisting_target() {
        let dir = tmp_dir("swap_fresh");
        let staged = dir.join("staged.exe");
        fs::write(&staged, b"NEW").unwrap();
        let target = dir.join("bin").join("nff.exe");
        swap(&staged, &target, true).unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"NEW");
        let _ = fs::remove_dir_all(dir);
    }
}
