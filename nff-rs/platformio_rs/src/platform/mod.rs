//! Platform/board metadata — the read-only slice of `platformio/platform/` and
//! `platformio/package/manager/platform.py` that the `boards` command needs.
//!
//! Ports `PlatformBase.get_boards` (the board directory search + first-wins
//! shadowing + platform filter/inject) and `PlatformPackageManager.
//! get_installed_boards` / `get_all_boards`. **No Python `platform.py` is
//! imported or executed** — only `platform.json` (for the platform's name) and
//! the board `*.json` manifests are read.
//!
//! Documented deviations: the project-level `boards_dir` is not searched (the
//! `boards`/`system` commands run globally, not inside a specific project);
//! malformed board manifests are skipped rather than aborting the listing; and
//! `get_registered_boards` (the registry `/v2/boards` fetch) is best-effort —
//! any network/parse failure yields the installed set only, exactly like
//! upstream swallowing `HTTPClientError`/`InternetConnectionError`.

pub mod board;

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::Duration;

use serde_json::Value as Json;

use crate::config::options::get_core_dir;
use board::PlatformBoardConfig;

pub use board::BoardError;

fn env_path(var: &str) -> Option<PathBuf> {
    std::env::var_os(var).filter(|v| !v.is_empty()).map(PathBuf::from)
}

fn core_dir() -> PathBuf {
    PathBuf::from(get_core_dir())
}

fn platforms_dir() -> PathBuf {
    env_path("PLATFORMIO_PLATFORMS_DIR").unwrap_or_else(|| core_dir().join("platforms"))
}

/// Installed dev-platform directories (dirs under `platforms_dir` that carry a
/// `platform.json`), sorted — approximates `PlatformPackageManager.get_installed`.
fn installed_platform_dirs() -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Ok(rd) = std::fs::read_dir(platforms_dir()) {
        let mut paths: Vec<PathBuf> = rd.filter_map(std::result::Result::ok).map(|e| e.path()).collect();
        paths.sort();
        for p in paths {
            if p.is_dir() && p.join("platform.json").is_file() {
                out.push(p);
            }
        }
    }
    out
}

/// The platform's short `name` (e.g. `"atmelavr"`) from its `platform.json`.
fn platform_name(platform_dir: &std::path::Path) -> Option<String> {
    let text = std::fs::read_to_string(platform_dir.join("platform.json")).ok()?;
    let json: Json = serde_json::from_str(&text).ok()?;
    json.get("name").and_then(|v| v.as_str()).map(ToString::to_string)
}

/// Port of `PlatformBase.get_boards` (all boards) for a single platform: search
/// `<core_dir>/boards` then `<platform_dir>/boards`, first-wins shadowing, apply
/// the `platform`/`platforms` filter, and inject the owning platform name.
fn boards_for_platform(platform_dir: &std::path::Path, name: &str) -> Vec<Json> {
    let bdirs = [core_dir().join("boards"), platform_dir.join("boards")];
    let mut cache: BTreeMap<String, PlatformBoardConfig> = BTreeMap::new();
    for bdir in &bdirs {
        let Ok(rd) = std::fs::read_dir(bdir) else { continue };
        let mut items: Vec<PathBuf> = rd.filter_map(std::result::Result::ok).map(|e| e.path()).collect();
        items.sort();
        for item in items {
            let Some(fname) = item.file_name().map(|n| n.to_string_lossy().into_owned()) else {
                continue;
            };
            let Some(id) = fname.strip_suffix(".json") else { continue };
            if cache.contains_key(id) {
                continue; // first-wins across dirs
            }
            let Ok(cfg) = PlatformBoardConfig::from_file(&item) else { continue };
            // Skip boards that declare a different owning platform.
            if let Some(p) = cfg.manifest().get("platform").and_then(Json::as_str) {
                if p != name {
                    continue;
                }
            }
            if let Some(ps) = cfg.manifest().get("platforms").and_then(Json::as_array) {
                if !ps.iter().any(|x| x.as_str() == Some(name)) {
                    continue;
                }
            }
            cache.insert(id.to_string(), cfg);
        }
    }
    cache
        .values()
        .map(|cfg| {
            let mut brief = cfg.get_brief_data();
            if let Some(obj) = brief.as_object_mut() {
                obj.insert("platform".into(), Json::String(name.to_string()));
            }
            brief
        })
        .collect()
}

/// Port of `PlatformPackageManager.get_installed_boards` — brief data for every
/// board bundled in an installed dev-platform, de-duplicated by equality.
#[must_use]
pub fn get_installed_boards() -> Vec<Json> {
    let mut boards: Vec<Json> = Vec::new();
    for platform_dir in installed_platform_dirs() {
        let Some(name) = platform_name(&platform_dir) else { continue };
        for board in boards_for_platform(&platform_dir, &name) {
            if !boards.contains(&board) {
                boards.push(board);
            }
        }
    }
    boards
}

/// Port of `PlatformPackageManager.get_all_boards` — installed boards plus any
/// registered (registry) boards not already known, sorted by `name`. The
/// registry fetch is best-effort and skipped on any failure (offline).
#[must_use]
pub fn get_all_boards() -> Vec<Json> {
    let mut boards = get_installed_boards();
    let key_of = |b: &Json| {
        format!(
            "{}:{}",
            b.get("platform").and_then(Json::as_str).unwrap_or(""),
            b.get("id").and_then(Json::as_str).unwrap_or("")
        )
    };
    let mut known: std::collections::HashSet<String> = boards.iter().map(&key_of).collect();
    if let Some(registered) = fetch_registered_boards() {
        for board in registered {
            let key = key_of(&board);
            if !known.contains(&key) {
                known.insert(key);
                boards.push(board);
            }
        }
    }
    boards.sort_by(|a, b| {
        let an = a.get("name").and_then(Json::as_str).unwrap_or("");
        let bn = b.get("name").and_then(Json::as_str).unwrap_or("");
        an.cmp(bn)
    });
    boards
}

/// Best-effort `GET /v2/boards` against the registry mirrors. Returns `None` on
/// any network/parse error (so the caller falls back to installed boards).
fn fetch_registered_boards() -> Option<Vec<Json>> {
    // Escape hatch to force installed-only listing (used by offline parity runs).
    if std::env::var_os("PLATFORMIO_RS_OFFLINE").is_some() {
        return None;
    }
    let client = reqwest::blocking::Client::builder().timeout(Duration::from_secs(8)).build().ok()?;
    for host in crate::REGISTRY_MIRROR_HOSTS {
        let url = format!("https://api.{host}/v2/boards");
        let Ok(resp) = client.get(&url).header("User-Agent", "platformio_rs").send() else {
            continue;
        };
        if !resp.status().is_success() {
            continue;
        }
        let Ok(body) = resp.text() else { continue };
        let Ok(json) = serde_json::from_str::<Json>(&body) else { continue };
        if let Some(arr) = json.as_array() {
            return Some(arr.clone());
        }
        if let Some(arr) = json.get("items").and_then(Json::as_array) {
            return Some(arr.clone());
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_lock;
    use std::fs;

    struct Fixture {
        prev_core: Option<String>,
        prev_platforms: Option<String>,
        _dir: tempfile::TempDir,
    }

    impl Fixture {
        /// Build a temp core dir with one platform ("myplat") bundling two boards.
        fn new() -> Self {
            let dir = tempfile::tempdir().expect("tempdir");
            let plat = dir.path().join("platforms").join("myplat");
            let boards = plat.join("boards");
            fs::create_dir_all(&boards).expect("mkdirs");
            fs::write(plat.join("platform.json"), r#"{"name":"myplat","version":"1.0.0"}"#)
                .expect("platform.json");
            fs::write(
                boards.join("uno.json"),
                r#"{"name":"Arduino Uno","url":"http://x","vendor":"Arduino",
                    "build":{"mcu":"atmega328p","f_cpu":"16000000L"},
                    "upload":{"maximum_ram_size":2048,"maximum_size":32256},
                    "frameworks":["arduino"]}"#,
            )
            .expect("uno.json");
            fs::write(
                boards.join("other.json"),
                r#"{"name":"Foreign","url":"http://y","vendor":"V","platform":"someoneelse"}"#,
            )
            .expect("other.json");

            let prev_core = std::env::var("PLATFORMIO_CORE_DIR").ok();
            let prev_platforms = std::env::var("PLATFORMIO_PLATFORMS_DIR").ok();
            std::env::set_var("PLATFORMIO_CORE_DIR", dir.path());
            std::env::remove_var("PLATFORMIO_PLATFORMS_DIR");
            Self { prev_core, prev_platforms, _dir: dir }
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            match &self.prev_core {
                Some(v) => std::env::set_var("PLATFORMIO_CORE_DIR", v),
                None => std::env::remove_var("PLATFORMIO_CORE_DIR"),
            }
            match &self.prev_platforms {
                Some(v) => std::env::set_var("PLATFORMIO_PLATFORMS_DIR", v),
                None => std::env::remove_var("PLATFORMIO_PLATFORMS_DIR"),
            }
        }
    }

    #[test]
    fn installed_boards_brief_shape_and_filter() {
        let _lk = test_lock::guard();
        let _fx = Fixture::new();
        let boards = get_installed_boards();
        // The "other" board declares a different platform -> filtered out.
        assert_eq!(boards.len(), 1);
        let b = &boards[0];
        // Exact brief-data key set the `boards` JSON output must carry.
        for key in ["fcpu", "frameworks", "id", "mcu", "name", "platform", "ram", "rom"] {
            assert!(b.get(key).is_some(), "missing key {key}");
        }
        assert_eq!(b["id"], "uno");
        assert_eq!(b["platform"], "myplat"); // injected
        assert_eq!(b["mcu"], "ATMEGA328P"); // upper-cased
        assert_eq!(b["fcpu"], 16_000_000); // digits of "16000000L"
        assert_eq!(b["ram"], 2048);
        assert_eq!(b["rom"], 32256);
    }
}
