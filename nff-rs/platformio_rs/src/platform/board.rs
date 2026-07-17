//! Port of `platformio/platform/board.py` — `PlatformBoardConfig`, the model for
//! a single board `*.json` manifest and its `get_brief_data()` projection (the
//! exact shape the `boards` command emits).

use std::fmt;
use std::path::Path;

use serde_json::{json, Map, Value as Json};

/// Errors mirroring `platformio/platform/exception.py` + the board field check.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BoardError {
    /// `InvalidBoardManifest` (broken JSON).
    InvalidBoardManifest(String),
    /// `UserSideException` — required `name`/`url`/`vendor` missing.
    MissingFields(String),
    /// `UnknownBoard`.
    UnknownBoard(String),
}

impl fmt::Display for BoardError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidBoardManifest(path) => {
                write!(f, "Invalid board JSON manifest '{path}'")
            }
            Self::MissingFields(path) => {
                write!(f, "Please specify name, url and vendor fields for {path}")
            }
            Self::UnknownBoard(id) => write!(f, "Unknown board ID '{id}'"),
        }
    }
}

impl std::error::Error for BoardError {}

/// Port of `PlatformBoardConfig`.
#[derive(Debug, Clone)]
pub struct PlatformBoardConfig {
    id: String,
    manifest: Json,
}

impl PlatformBoardConfig {
    /// `PlatformBoardConfig(manifest_path)` — load + validate a board manifest.
    pub fn from_file(manifest_path: &Path) -> Result<Self, BoardError> {
        let name = manifest_path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        // `os.path.basename(manifest_path)[:-5]` — strip the ".json" suffix.
        let id = name.strip_suffix(".json").unwrap_or(&name).to_string();
        let path_str = manifest_path.to_string_lossy().into_owned();
        let text = std::fs::read_to_string(manifest_path)
            .map_err(|_| BoardError::InvalidBoardManifest(path_str.clone()))?;
        let manifest: Json =
            serde_json::from_str(&text).map_err(|_| BoardError::InvalidBoardManifest(path_str.clone()))?;
        let has = |k: &str| manifest.get(k).is_some();
        if !(has("name") && has("url") && has("vendor")) {
            return Err(BoardError::MissingFields(path_str));
        }
        Ok(Self { id, manifest })
    }

    #[must_use]
    pub fn id(&self) -> &str {
        &self.id
    }

    #[must_use]
    pub fn manifest(&self) -> &Json {
        &self.manifest
    }

    /// Dotted lookup (`get("build.mcu")`) returning the raw JSON node.
    #[must_use]
    pub fn get(&self, path: &str) -> Option<&Json> {
        let mut cur = &self.manifest;
        for key in path.split('.') {
            cur = cur.get(key)?;
        }
        Some(cur)
    }

    fn get_str(&self, path: &str) -> Option<String> {
        self.get(path).and_then(|v| v.as_str().map(ToString::to_string))
    }

    /// Port of `PlatformBoardConfig.get_brief_data`.
    #[must_use]
    pub fn get_brief_data(&self) -> Json {
        let mut r = Map::new();
        r.insert("id".into(), json!(self.id));
        r.insert("name".into(), self.manifest.get("name").cloned().unwrap_or(Json::Null));
        r.insert("platform".into(), self.manifest.get("platform").cloned().unwrap_or(Json::Null));
        r.insert("mcu".into(), json!(self.get_str("build.mcu").unwrap_or_default().to_uppercase()));

        // fcpu = int("".join(digit chars of str(build.f_cpu or "0L")))
        let fcpu_src = self
            .get("build.f_cpu")
            .map(json_scalar_to_string)
            .unwrap_or_else(|| "0L".to_string());
        let digits: String = fcpu_src.chars().filter(char::is_ascii_digit).collect();
        let fcpu: i64 = digits.parse().unwrap_or(0);
        r.insert("fcpu".into(), json!(fcpu));

        r.insert("ram".into(), self.get("upload.maximum_ram_size").cloned().unwrap_or(json!(0)));
        r.insert("rom".into(), self.get("upload.maximum_size").cloned().unwrap_or(json!(0)));
        r.insert("frameworks".into(), self.manifest.get("frameworks").cloned().unwrap_or(Json::Null));
        r.insert("vendor".into(), self.manifest.get("vendor").cloned().unwrap_or(Json::Null));
        r.insert("url".into(), self.manifest.get("url").cloned().unwrap_or(Json::Null));

        if let Some(conn) = self.manifest.get("connectivity") {
            if is_truthy(conn) {
                r.insert("connectivity".into(), conn.clone());
            }
        }
        if let Some(debug) = self.get_debug_data() {
            r.insert("debug".into(), debug);
        }
        Json::Object(r)
    }

    /// Port of `PlatformBoardConfig.get_debug_data`.
    fn get_debug_data(&self) -> Option<Json> {
        let tools = self.get("debug.tools")?.as_object()?;
        if tools.is_empty() {
            return None;
        }
        let mut out = Map::new();
        for (name, options) in tools {
            let mut kept = Map::new();
            if let Some(opts) = options.as_object() {
                for (key, value) in opts {
                    if (key == "default" || key == "onboard") && is_truthy(value) {
                        kept.insert(key.clone(), value.clone());
                    }
                }
            }
            out.insert(name.clone(), Json::Object(kept));
        }
        Some(json!({ "tools": out }))
    }
}

/// `str(value)` for a scalar JSON node (used for `f_cpu`, which may be a number
/// like `16000000` or a string like `"16000000L"`).
fn json_scalar_to_string(v: &Json) -> String {
    match v {
        Json::String(s) => s.clone(),
        Json::Number(n) => n.to_string(),
        Json::Bool(b) => if *b { "True" } else { "False" }.to_string(),
        Json::Null => "0L".to_string(),
        other => other.to_string(),
    }
}

/// Python truthiness for the `connectivity`/`default`/`onboard` guards.
fn is_truthy(v: &Json) -> bool {
    match v {
        Json::Null => false,
        Json::Bool(b) => *b,
        Json::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Json::String(s) => !s.is_empty(),
        Json::Array(a) => !a.is_empty(),
        Json::Object(o) => !o.is_empty(),
    }
}
