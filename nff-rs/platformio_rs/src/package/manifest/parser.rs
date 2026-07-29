//! Port of `platformio/package/manifest/parser.py`: `ManifestFileType`, the
//! shared `BaseManifestParser` behaviour, the five concrete parsers
//! (`library.json`, `module.json`, `library.properties`, `platform.json`,
//! `package.json`), and `ManifestParserFactory`.
//!
//! Manifest data is modelled as a `serde_json` object throughout, which keeps the
//! transformations close to the Python dict manipulations and lets the parity
//! tests compare with plain JSON. The behavioural spec is
//! `tests/package/test_manifest.py` (parser half), mirrored in
//! [`crate::package::manifest::tests`].

use std::collections::BTreeMap;
use std::fs;
use std::io::Read;
use std::path::Path;

use regex::Regex;
use serde_json::{json, Map, Value};

use crate::package::error::{PackageError, Result};
use crate::package::meta::urlparse;

// ---------------------------------------------------------------------------
// ManifestFileType
// ---------------------------------------------------------------------------

/// `platformio.package.manifest.parser.ManifestFileType`.
pub struct ManifestFileType;

impl ManifestFileType {
    pub const PLATFORM_JSON: &'static str = "platform.json";
    pub const LIBRARY_JSON: &'static str = "library.json";
    pub const LIBRARY_PROPERTIES: &'static str = "library.properties";
    pub const MODULE_JSON: &'static str = "module.json";
    pub const PACKAGE_JSON: &'static str = "package.json";

    /// `ManifestFileType.items()` values, **sorted** (upstream iterates the sorted
    /// values, so `library.json` wins over `package.json` in a directory).
    #[must_use]
    pub fn items() -> [&'static str; 5] {
        // Sorted: library.json < library.properties < module.json < package.json < platform.json
        [
            Self::LIBRARY_JSON,
            Self::LIBRARY_PROPERTIES,
            Self::MODULE_JSON,
            Self::PACKAGE_JSON,
            Self::PLATFORM_JSON,
        ]
    }

    /// `ManifestFileType.from_uri`.
    #[must_use]
    pub fn from_uri(uri: &str) -> Option<&'static str> {
        Self::items().into_iter().find(|t| uri.ends_with(t))
    }

    /// `ManifestFileType.from_dir`.
    #[must_use]
    pub fn from_dir(path: &Path) -> Option<&'static str> {
        Self::items().into_iter().find(|t| path.join(t).is_file())
    }
}

// ---------------------------------------------------------------------------
// ManifestParser + factory
// ---------------------------------------------------------------------------

/// A parsed manifest. `as_dict` returns the normalized data (`BaseManifestParser`
/// after `normalize_repository`, `parse_examples`, and null-field removal).
#[derive(Debug, Clone)]
pub struct ManifestParser {
    data: Map<String, Value>,
}

impl ManifestParser {
    #[must_use]
    pub fn as_dict(&self) -> Value {
        Value::Object(self.data.clone())
    }
}

/// `platformio.package.manifest.parser.ManifestParserFactory`.
pub struct ManifestParserFactory;

impl ManifestParserFactory {
    /// `ManifestParserFactory.new` — the factory dispatch method (mirrors the
    /// upstream name, so it returns a parser rather than `Self`).
    #[allow(clippy::new_ret_no_self)]
    pub fn new(
        contents: &str,
        manifest_type: &str,
        remote_url: Option<&str>,
        package_dir: Option<&Path>,
    ) -> Result<ManifestParser> {
        // BaseManifestParser.__init__ wraps any parse failure.
        let mut data = parse_by_type(contents, manifest_type, remote_url)
            .map_err(|e| PackageError::ManifestParser {
                message: format!("Could not parse manifest -> {e}"),
            })?;
        normalize_repository(&mut data);
        parse_examples(&mut data, package_dir);
        // remove None (Null) fields
        let null_keys: Vec<String> =
            data.iter().filter(|(_, v)| v.is_null()).map(|(k, _)| k.clone()).collect();
        for k in null_keys {
            data.remove(&k);
        }
        Ok(ManifestParser { data })
    }

    /// `ManifestParserFactory.new_from_dir`.
    pub fn new_from_dir(path: &Path, remote_url: Option<&str>) -> Result<ManifestParser> {
        assert!(path.is_dir(), "Invalid directory {}", path.display());
        if let Some(t) = remote_url.and_then(ManifestFileType::from_uri) {
            let file = path.join(t);
            if file.is_file() {
                return ManifestParserFactory::new(
                    &read_manifest_contents(&file)?,
                    t,
                    remote_url,
                    Some(path),
                );
            }
        }
        let t = ManifestFileType::from_dir(path).ok_or_else(|| PackageError::UnknownManifest {
            message: format!("Unknown manifest file type in {} directory", path.display()),
        })?;
        ManifestParserFactory::new(&read_manifest_contents(&path.join(t))?, t, remote_url, Some(path))
    }

    /// `ManifestParserFactory.new_from_file`.
    pub fn new_from_file(path: &Path, remote_url: Option<&str>) -> Result<ManifestParser> {
        if !path.is_file() {
            return Err(PackageError::UnknownManifest {
                message: format!("Manifest file does not exist {}", path.display()),
            });
        }
        let t = ManifestFileType::from_uri(&path.to_string_lossy()).ok_or_else(|| {
            PackageError::UnknownManifest {
                message: format!("Unknown manifest file type {}", path.display()),
            }
        })?;
        ManifestParserFactory::new(&read_manifest_contents(path)?, t, remote_url, None)
    }

    /// `ManifestParserFactory.new_from_archive` — sniff a `tar.gz` for the first
    /// (sorted) manifest member.
    pub fn new_from_archive(path: &Path) -> Result<ManifestParser> {
        assert!(path.to_string_lossy().ends_with("tar.gz"));
        let members = read_targz_members(path)?;
        for t in ManifestFileType::items() {
            for member in [t.to_string(), format!("./{t}")] {
                if let Some(content) = members.get(&member) {
                    return ManifestParserFactory::new(content, t, None, None);
                }
            }
        }
        Err(PackageError::UnknownManifest {
            message: format!("Unknown manifest file type in {} archive", path.display()),
        })
    }
}

fn parse_by_type(contents: &str, manifest_type: &str, remote_url: Option<&str>) -> Result<Map<String, Value>> {
    match manifest_type {
        ManifestFileType::LIBRARY_JSON => parse_library_json(contents),
        ManifestFileType::MODULE_JSON => parse_module_json(contents),
        ManifestFileType::LIBRARY_PROPERTIES => Ok(parse_library_properties(contents, remote_url)),
        ManifestFileType::PLATFORM_JSON => parse_platform_json(contents),
        ManifestFileType::PACKAGE_JSON => parse_package_json(contents),
        other => Err(PackageError::UnknownManifest {
            message: format!("Unknown manifest file type {other}"),
        }),
    }
}

/// `ManifestParserFactory.read_manifest_contents` — utf-8 then latin-1.
fn read_manifest_contents(path: &Path) -> Result<String> {
    let bytes = fs::read(path).map_err(|e| PackageError::UnknownManifest {
        message: format!("Manifest file does not exist {}: {e}", path.display()),
    })?;
    match String::from_utf8(bytes) {
        Ok(s) => Ok(s),
        // latin-1: every byte maps to a code point 1:1.
        Err(e) => Ok(e.into_bytes().iter().map(|&b| b as char).collect()),
    }
}

fn read_targz_members(path: &Path) -> Result<BTreeMap<String, String>> {
    let file = fs::File::open(path).map_err(|e| PackageError::Package { message: e.to_string() })?;
    let mut archive = tar::Archive::new(flate2::read::GzDecoder::new(file));
    let mut out = BTreeMap::new();
    for entry in archive.entries().map_err(|e| PackageError::Package { message: e.to_string() })? {
        let mut entry = entry.map_err(|e| PackageError::Package { message: e.to_string() })?;
        let name = entry
            .path()
            .map_err(|e| PackageError::Package { message: e.to_string() })?
            .to_string_lossy()
            .replace('\\', "/");
        let mut content = String::new();
        if entry.read_to_string(&mut content).is_ok() {
            out.insert(name, content);
        }
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Shared BaseManifestParser helpers
// ---------------------------------------------------------------------------

/// `BaseManifestParser.str_to_list` for an already-split list of items.
fn normalize_items(items: Vec<String>, lowercase: bool, unique: bool) -> Vec<String> {
    let mut result: Vec<String> = Vec::new();
    for item in items {
        let item = item.trim();
        if item.is_empty() {
            continue;
        }
        let item = if lowercase { item.to_lowercase() } else { item.to_string() };
        if unique && result.contains(&item) {
            continue;
        }
        result.push(item);
    }
    result
}

/// `BaseManifestParser.str_to_list` on a `Value` that is a string or list.
fn str_to_list(value: &Value, sep: char, lowercase: bool, unique: bool) -> Vec<String> {
    let items: Vec<String> = match value {
        Value::String(s) => s.split(sep).map(str::to_string).collect(),
        Value::Array(arr) => arr.iter().filter_map(|v| v.as_str().map(str::to_string)).collect(),
        _ => Vec::new(),
    };
    normalize_items(items, lowercase, unique)
}

fn str_list_value(items: Vec<String>) -> Value {
    Value::Array(items.into_iter().map(Value::String).collect())
}

/// `util.items_to_list`.
fn items_to_list(value: &Value) -> Vec<String> {
    match value {
        Value::Array(arr) => arr.iter().filter_map(|v| v.as_str().map(str::to_string)).collect(),
        Value::String(s) => s.split(',').map(str::trim).filter(|x| !x.is_empty()).map(str::to_string).collect(),
        _ => Vec::new(),
    }
}

/// `BaseManifestParser.cleanup_author` — normalize the email and drop null keys.
fn cleanup_author(mut author: Map<String, Value>) -> Map<String, Value> {
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| Regex::new(r"\s+[aA][tT]\s+").unwrap());
    if let Some(email) = author.get("email").and_then(Value::as_str).filter(|e| !e.is_empty()) {
        let fixed = re.replace_all(email, "@").into_owned();
        if fixed.contains('@') {
            author.insert("email".to_string(), Value::String(fixed));
        } else {
            author.insert("email".to_string(), Value::Null);
        }
    }
    let null_keys: Vec<String> =
        author.iter().filter(|(_, v)| v.is_null()).map(|(k, _)| k.clone()).collect();
    for k in null_keys {
        author.remove(&k);
    }
    author
}

/// `BaseManifestParser.parse_author_name_and_email`.
fn parse_author_name_and_email(raw: &str) -> (Option<String>, Option<String>) {
    if raw == "None" || raw.contains("://") {
        return (None, None);
    }
    let mut name = raw.to_string();
    let mut email: Option<String> = None;
    if let (Some(l), Some(r)) = (raw.find('<'), raw.find('>')) {
        name = raw[..l].to_string();
        email = Some(raw[l + 1..r].to_string());
    }
    if let Some(p) = name.find('(') {
        name = name[..p].to_string();
    }
    let name = name.trim().to_string();
    let email = email.map(|e| e.trim().to_string());
    (if name.is_empty() { None } else { Some(name) }, email)
}

/// `BaseManifestParser.normalize_repository`.
fn normalize_repository(data: &mut Map<String, Value>) {
    let Some(url) = data
        .get("repository")
        .and_then(|r| r.get("url"))
        .and_then(Value::as_str)
        .map(str::to_string)
    else {
        return;
    };
    if url.is_empty() || !url.contains("://") {
        return;
    }
    let (_scheme, netloc, path) = urlparse(&url);
    if !matches!(netloc.as_str(), "github.com" | "bitbucket.org" | "gitlab.com") {
        return;
    }
    let mut fixed = format!("https://{netloc}{path}");
    if fixed.ends_with('/') {
        fixed.pop();
    }
    if !fixed.ends_with(".git") {
        fixed.push_str(".git");
    }
    if let Some(repo) = data.get_mut("repository").and_then(Value::as_object_mut) {
        repo.insert("url".to_string(), Value::String(fixed));
    }
}

/// `BaseManifestParser.parse_examples`.
fn parse_examples(data: &mut Map<String, Value>, package_dir: Option<&Path>) {
    let valid = data
        .get("examples")
        .and_then(Value::as_array)
        .is_some_and(|arr| !arr.is_empty() && arr.iter().all(Value::is_object));
    if !valid {
        data.insert("examples".to_string(), Value::Null);
    }
    let is_empty = data.get("examples").is_none_or(|v| v.is_null() || v.as_array().is_some_and(Vec::is_empty));
    if is_empty {
        if let Some(dir) = package_dir {
            let from_dir = parse_examples_from_dir(dir).unwrap_or(Value::Null);
            data.insert("examples".to_string(), from_dir);
        }
    }
    let remove = data
        .get("examples")
        .is_some_and(|v| v.is_null() || v.as_array().is_some_and(Vec::is_empty));
    if remove {
        data.remove("examples");
    }
}

const EXAMPLE_EXTS: &[&str] = &[
    ".c", ".cc", ".cpp", ".h", ".hpp", ".asm", ".ASM", ".s", ".S", ".ino", ".pde",
];

/// `BaseManifestParser.parse_examples_from_dir`.
fn parse_examples_from_dir(package_dir: &Path) -> Option<Value> {
    let mut examples_dir = package_dir.join("examples");
    if !examples_dir.is_dir() {
        examples_dir = package_dir.join("Examples");
        if !examples_dir.is_dir() {
            return None;
        }
    }

    // Top-down DFS mirroring os.walk.
    let mut walk: Vec<std::path::PathBuf> = Vec::new();
    collect_dirs_topdown(&examples_dir, &mut walk);

    let mut result: Vec<Map<String, Value>> = Vec::new();
    // Track which result index owns a given PlatformIO-project root path.
    let mut last_pio_project: Option<(std::path::PathBuf, usize)> = None;

    for root in &walk {
        let files = visible_files(root);
        let root_hidden = root
            .file_name()
            .and_then(|s| s.to_str())
            .is_some_and(|s| s.starts_with('.'));
        if root_hidden || files.is_empty() {
            continue;
        }

        if root.join("platformio.ini").is_file() {
            let idx = result.len();
            result.push(example_entry(
                relpath(root, &examples_dir),
                relpath(root, package_dir),
                files,
            ));
            last_pio_project = Some((root.clone(), idx));
            continue;
        }
        if let Some((proj, idx)) = &last_pio_project {
            if root.starts_with(proj) {
                let extra: Vec<String> =
                    files.iter().map(|f| relpath(&root.join(f), proj)).collect();
                if let Some(Value::Array(list)) = result[*idx].get_mut("files") {
                    list.extend(extra.into_iter().map(Value::String));
                }
                continue;
            }
            last_pio_project = None;
        }

        let matched: Vec<String> =
            files.into_iter().filter(|f| EXAMPLE_EXTS.iter().any(|e| f.ends_with(e))).collect();
        if matched.is_empty() {
            continue;
        }
        let name = if root == &examples_dir { "Examples".to_string() } else { relpath(root, &examples_dir) };
        result.push(example_entry(name, relpath(root, package_dir), matched));
    }

    // normalize example names
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| Regex::new(r"(?i)[^a-z0-9\-_/]+").unwrap());
    for item in &mut result {
        if let Some(Value::String(name)) = item.get("name") {
            let unix = name.replace('\\', "/");
            let normalized = re.replace_all(&unix, "_").into_owned();
            item.insert("name".to_string(), Value::String(normalized));
        }
    }

    if result.is_empty() {
        None
    } else {
        Some(Value::Array(result.into_iter().map(Value::Object).collect()))
    }
}

fn example_entry(name: String, base: String, files: Vec<String>) -> Map<String, Value> {
    let mut m = Map::new();
    m.insert("name".to_string(), Value::String(name));
    m.insert("base".to_string(), Value::String(base));
    m.insert("files".to_string(), str_list_value(files));
    m
}

/// DFS collecting directories top-down (root first, then children), like os.walk.
pub(crate) fn collect_dirs_topdown(dir: &Path, out: &mut Vec<std::path::PathBuf>) {
    out.push(dir.to_path_buf());
    let Ok(entries) = fs::read_dir(dir) else { return };
    let mut subdirs: Vec<std::path::PathBuf> = entries
        .filter_map(std::result::Result::ok)
        .map(|e| e.path())
        .filter(|p| p.is_dir())
        .collect();
    subdirs.sort();
    for sub in subdirs {
        collect_dirs_topdown(&sub, out);
    }
}

/// Non-hidden, non-symlink file names directly in `dir`.
fn visible_files(dir: &Path) -> Vec<String> {
    let Ok(entries) = fs::read_dir(dir) else { return Vec::new() };
    let mut files: Vec<String> = Vec::new();
    for e in entries.filter_map(std::result::Result::ok) {
        let path = e.path();
        let Some(name) = path.file_name().and_then(|s| s.to_str()) else { continue };
        if name.starts_with('.') {
            continue;
        }
        let is_symlink = fs::symlink_metadata(&path).map(|m| m.file_type().is_symlink()).unwrap_or(false);
        let is_file = path.is_file();
        if is_symlink || !is_file {
            continue;
        }
        files.push(name.to_string());
    }
    files.sort();
    files
}

/// `os.path.relpath(path, base)` for a descendant `path`, joined with `/`.
fn relpath(path: &Path, base: &Path) -> String {
    path.strip_prefix(base)
        .map(|p| p.to_string_lossy().replace('\\', "/"))
        .unwrap_or_else(|_| path.to_string_lossy().into_owned())
}

// ---------------------------------------------------------------------------
// library.json
// ---------------------------------------------------------------------------

fn parse_json_object(contents: &str) -> Result<Map<String, Value>> {
    let value: Value =
        serde_json::from_str(contents).map_err(|e| PackageError::Package { message: e.to_string() })?;
    value.as_object().cloned().ok_or_else(|| PackageError::Package {
        message: "manifest is not a JSON object".to_string(),
    })
}

fn parse_library_json(contents: &str) -> Result<Map<String, Value>> {
    let mut data = parse_json_object(contents)?;

    // _process_renamed_fields
    if let Some(url) = data.remove("url") {
        data.insert("homepage".to_string(), url);
    }
    for key in ["include", "exclude"] {
        if let Some(v) = data.remove(key) {
            let export = data.entry("export").or_insert_with(|| json!({}));
            if let Some(obj) = export.as_object_mut() {
                obj.insert(key.to_string(), v);
            }
        }
    }

    for k in ["keywords", "platforms", "frameworks"] {
        if let Some(v) = data.get(k) {
            data.insert(k.to_string(), str_list_value(str_to_list(v, ',', true, true)));
        }
    }
    if let Some(v) = data.get("headers") {
        data.insert("headers".to_string(), str_list_value(str_to_list(v, ',', false, true)));
    }
    if let Some(v) = data.get("authors").cloned() {
        data.insert("authors".to_string(), parse_authors_json(&v));
    }
    if let Some(Value::Array(items)) = data.get_mut("platforms") {
        // _fix_platforms: espressif -> espressif8266
        for item in items.iter_mut() {
            if item.as_str() == Some("espressif") {
                *item = Value::String("espressif8266".to_string());
            }
        }
        if items.is_empty() {
            data.insert("platforms".to_string(), Value::Null);
        }
    }
    if let Some(v) = data.get("export").cloned() {
        data.insert("export".to_string(), parse_export_json(&v));
    }
    if let Some(v) = data.get("dependencies").cloned() {
        data.insert("dependencies".to_string(), parse_dependencies_library_json(&v)?);
    }
    Ok(data)
}

fn parse_authors_json(raw: &Value) -> Value {
    if raw.is_null() {
        return Value::Null;
    }
    let list = match raw {
        Value::Array(arr) => arr.clone(),
        other => vec![other.clone()],
    };
    Value::Array(
        list.into_iter()
            .map(|a| {
                Value::Object(cleanup_author(a.as_object().cloned().unwrap_or_default()))
            })
            .collect(),
    )
}

fn parse_export_json(raw: &Value) -> Value {
    let Some(obj) = raw.as_object() else { return Value::Null };
    let mut result = Map::new();
    for k in ["include", "exclude"] {
        match obj.get(k) {
            Some(v) if !is_falsy(v) => {
                let list = if v.is_array() { v.clone() } else { Value::Array(vec![v.clone()]) };
                result.insert(k.to_string(), list);
            }
            _ => {}
        }
    }
    Value::Object(result)
}

fn is_falsy(v: &Value) -> bool {
    match v {
        Value::Null => true,
        Value::Bool(b) => !b,
        Value::String(s) => s.is_empty(),
        Value::Array(a) => a.is_empty(),
        Value::Object(o) => o.is_empty(),
        Value::Number(n) => n.as_f64() == Some(0.0),
    }
}

fn parse_dependencies_library_json(raw: &Value) -> Result<Value> {
    if let Some(obj) = raw.as_object() {
        // legacy single-dependency dict → wrap and fall through to list handling
        // (so authors/frameworks/platforms still get normalized).
        if obj.contains_key("name") {
            return Ok(process_dep_list(std::slice::from_ref(raw)));
        }
        // name -> version map
        let mut result = Vec::new();
        for (name, version) in obj {
            if let Some((owner, rest)) = name.split_once('/') {
                result.push(json!({"owner": owner, "name": rest, "version": version}));
            } else {
                result.push(json!({"name": name, "version": version}));
            }
        }
        return Ok(Value::Array(result));
    }
    if let Some(arr) = raw.as_array() {
        return Ok(process_dep_list(arr));
    }
    Err(PackageError::ManifestParser {
        message: "Invalid dependencies format, should be list or dictionary".to_string(),
    })
}

/// The list branch of `LibraryJsonManifestParser._parse_dependencies`: normalize
/// `platforms`/`frameworks`/`authors` on dict deps, wrap bare strings as `{name}`.
fn process_dep_list(deps: &[Value]) -> Value {
    let result: Vec<Value> = deps
        .iter()
        .map(|dep| {
            if let Some(obj) = dep.as_object() {
                let mut obj = obj.clone();
                for k in ["platforms", "frameworks", "authors"] {
                    if let Some(v) = obj.get(k) {
                        obj.insert(k.to_string(), str_list_value(items_to_list(v)));
                    }
                }
                Value::Object(obj)
            } else {
                json!({"name": dep})
            }
        })
        .collect();
    Value::Array(result)
}

// ---------------------------------------------------------------------------
// module.json
// ---------------------------------------------------------------------------

fn parse_module_json(contents: &str) -> Result<Map<String, Value>> {
    let mut data = parse_json_object(contents)?;
    data.insert("frameworks".to_string(), json!(["mbed"]));
    data.insert("platforms".to_string(), json!(["*"]));
    data.insert("export".to_string(), json!({"exclude": ["tests", "test", "*.doxyfile", "*.pdf"]}));
    if let Some(author) = data.remove("author") {
        data.insert("authors".to_string(), parse_authors_module(&author));
    }
    if let Some(licenses) = data.remove("licenses") {
        data.insert("license".to_string(), parse_license_module(&licenses));
    }
    if let Some(v) = data.get("dependencies").cloned() {
        data.insert("dependencies".to_string(), parse_dependencies_module(&v)?);
    }
    if let Some(v) = data.get("keywords") {
        data.insert("keywords".to_string(), str_list_value(str_to_list(v, ',', true, true)));
    }
    Ok(data)
}

fn parse_authors_module(raw: &Value) -> Value {
    let Some(s) = raw.as_str() else { return Value::Null };
    if s.is_empty() {
        return Value::Null;
    }
    let mut result = Vec::new();
    for author in s.split(',') {
        let (name, email) = parse_author_name_and_email(author);
        let Some(name) = name else { continue };
        let mut m = Map::new();
        m.insert("name".to_string(), Value::String(name));
        m.insert("email".to_string(), email.map_or(Value::Null, Value::String));
        result.push(Value::Object(cleanup_author(m)));
    }
    Value::Array(result)
}

fn parse_license_module(raw: &Value) -> Value {
    raw.as_array()
        .and_then(|arr| arr.first())
        .and_then(|first| first.get("type").cloned())
        .unwrap_or(Value::Null)
}

fn parse_dependencies_module(raw: &Value) -> Result<Value> {
    let Some(obj) = raw.as_object() else {
        return Err(PackageError::ManifestParser {
            message: "Invalid dependencies format, should be a dictionary".to_string(),
        });
    };
    let result: Vec<Value> = obj
        .iter()
        .map(|(name, version)| json!({"name": name, "version": version, "frameworks": ["mbed"]}))
        .collect();
    Ok(Value::Array(result))
}

// ---------------------------------------------------------------------------
// library.properties
// ---------------------------------------------------------------------------

fn parse_library_properties(contents: &str, remote_url: Option<&str>) -> Map<String, Value> {
    let mut props = parse_properties(contents);

    let repository = parse_lp_repository(&props, remote_url);
    let homepage = props.get("url").cloned();
    let homepage = match (&repository, &homepage) {
        (Some(repo), Some(hp)) if repo.get("url") == Some(hp) => None,
        _ => homepage,
    };

    props.insert("frameworks".to_string(), json!(["arduino"]));
    props.insert("homepage".to_string(), homepage.unwrap_or(Value::Null));
    props.insert("repository".to_string(), repository.map_or(Value::Null, Value::Object));
    props.insert("description".to_string(), Value::String(parse_lp_description(&props)));
    let platforms = parse_lp_platforms(&props);
    props.insert("platforms".to_string(), if platforms.is_empty() { Value::Null } else { str_list_value(platforms) });
    let keywords = parse_lp_keywords(&props);
    props.insert("keywords".to_string(), if keywords.is_empty() { Value::Null } else { str_list_value(keywords) });
    props.insert("export".to_string(), parse_lp_export(remote_url).map_or(Value::Null, Value::Object));

    if let Some(includes) = props.get("includes").cloned() {
        props.insert("headers".to_string(), str_list_value(str_to_list(&includes, ',', false, true)));
    }
    if props.contains_key("author") {
        let authors = parse_lp_authors(&props);
        props.insert("authors".to_string(), authors);
        props.remove("author");
        props.remove("maintainer");
    }
    if let Some(depends) = props.get("depends").cloned() {
        if let Some(s) = depends.as_str() {
            props.insert("dependencies".to_string(), parse_lp_dependencies(s));
        }
    }
    props
}

/// `LibraryPropertiesManifestParser._parse_properties`.
fn parse_properties(contents: &str) -> Map<String, Value> {
    let mut data = Map::new();
    for line in contents.lines() {
        let line = line.trim();
        if line.is_empty() || !line.contains('=') || line.starts_with('#') {
            continue;
        }
        let (key, value) = line.split_once('=').unwrap();
        if value.trim().is_empty() {
            continue;
        }
        data.insert(key.trim().to_string(), Value::String(value.trim().to_string()));
    }
    data
}

fn prop_str<'a>(props: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    props.get(key).and_then(Value::as_str)
}

fn parse_lp_description(props: &Map<String, Value>) -> String {
    let mut lines: Vec<String> = Vec::new();
    for k in ["sentence", "paragraph"] {
        if let Some(v) = prop_str(props, k) {
            if !lines.iter().any(|l| l == v) {
                lines.push(v.to_string());
            }
        }
    }
    if lines.len() == 2 {
        if !lines[0].ends_with('.') {
            lines[0].push('.');
        }
        if lines[0].len() + lines[1].len() >= 1000 {
            lines.remove(1);
        }
    }
    lines.join(" ")
}

fn parse_lp_keywords(props: &Map<String, Value>) -> Vec<String> {
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| Regex::new(r"[\s/]+").unwrap());
    let category = prop_str(props, "category").unwrap_or("");
    let items: Vec<String> = re.split(category).map(str::to_string).collect();
    normalize_items(items, true, true)
}

fn parse_lp_platforms(props: &Map<String, Value>) -> Vec<String> {
    let map: &[(&str, &str)] = &[
        ("avr", "atmelavr"),
        ("sam", "atmelsam"),
        ("samd", "atmelsam"),
        ("esp8266", "espressif8266"),
        ("esp32", "espressif32"),
        ("arc32", "intel_arc32"),
        ("stm32", "ststm32"),
        ("nrf52", "nordicnrf52"),
        ("rp2040", "raspberrypi"),
    ];
    let mut result = Vec::new();
    for arch in prop_str(props, "architectures").unwrap_or("").split(',') {
        let arch = arch.trim();
        if arch.is_empty() {
            continue;
        }
        if arch == "*" {
            return vec!["*".to_string()];
        }
        if let Some((_, mapped)) = map.iter().find(|(k, _)| *k == arch) {
            result.push((*mapped).to_string());
        }
    }
    normalize_items(result, true, true)
}

fn parse_lp_authors(props: &Map<String, Value>) -> Value {
    let mut authors: Vec<Map<String, Value>> = Vec::new();
    if let Some(author) = prop_str(props, "author") {
        for a in author.split(',') {
            let (name, email) = parse_author_name_and_email(a);
            let Some(name) = name else { continue };
            let mut m = Map::new();
            m.insert("name".to_string(), Value::String(name));
            m.insert("email".to_string(), email.map_or(Value::Null, Value::String));
            authors.push(cleanup_author(m));
        }
    }
    for a in prop_str(props, "maintainer").unwrap_or("").split(',') {
        let (name, email) = parse_author_name_and_email(a);
        let Some(name) = name else { continue };
        let mut found = false;
        for item in &mut authors {
            if item.get("name").and_then(Value::as_str).map(str::to_lowercase) != Some(name.to_lowercase()) {
                continue;
            }
            found = true;
            item.insert("maintainer".to_string(), Value::Bool(true));
            let has_email = item.get("email").and_then(Value::as_str).is_some_and(|e| !e.is_empty());
            if !has_email {
                if let Some(email) = &email {
                    if email.contains('@') {
                        item.insert("email".to_string(), Value::String(email.clone()));
                    }
                }
            }
        }
        if !found {
            let mut m = Map::new();
            m.insert("name".to_string(), Value::String(name));
            m.insert("email".to_string(), email.map_or(Value::Null, Value::String));
            m.insert("maintainer".to_string(), Value::Bool(true));
            authors.push(cleanup_author(m));
        }
    }
    Value::Array(authors.into_iter().map(Value::Object).collect())
}

fn parse_lp_repository(props: &Map<String, Value>, remote_url: Option<&str>) -> Option<Map<String, Value>> {
    if let Some(remote) = remote_url {
        let (_s, netloc, path) = urlparse(remote);
        let trimmed = path.strip_prefix('/').unwrap_or(&path);
        let mut tokens: Vec<&str> = trimmed.split('/').collect();
        tokens.pop(); // [:-1]
        if netloc.contains("github") {
            let joined = tokens.iter().take(2).copied().collect::<Vec<_>>().join("/");
            let mut m = Map::new();
            m.insert("type".to_string(), Value::String("git".to_string()));
            m.insert("url".to_string(), Value::String(format!("https://github.com/{joined}")));
            return Some(m);
        }
        if let Some(raw_idx) = tokens.iter().position(|t| *t == "raw") {
            let joined = tokens[..raw_idx].join("/");
            let mut m = Map::new();
            m.insert("type".to_string(), Value::String("git".to_string()));
            m.insert("url".to_string(), Value::String(format!("https://{netloc}/{joined}")));
            return Some(m);
        }
    }
    if prop_str(props, "url").is_some_and(|u| u.starts_with("https://github.com")) {
        let mut m = Map::new();
        m.insert("type".to_string(), Value::String("git".to_string()));
        m.insert("url".to_string(), Value::String(prop_str(props, "url").unwrap().to_string()));
        return Some(m);
    }
    None
}

fn parse_lp_export(remote_url: Option<&str>) -> Option<Map<String, Value>> {
    let remote = remote_url?;
    let (_s, netloc, path) = urlparse(remote);
    let trimmed = path.strip_prefix('/').unwrap_or(&path);
    let mut tokens: Vec<&str> = trimmed.split('/').collect();
    tokens.pop(); // [:-1]
    let include = if netloc.contains("github") {
        let joined = tokens.iter().skip(3).copied().collect::<Vec<_>>().join("/");
        if joined.is_empty() { None } else { Some(joined) }
    } else if let Some(raw_idx) = tokens.iter().position(|t| *t == "raw") {
        let joined = tokens.iter().skip(raw_idx + 2).copied().collect::<Vec<_>>().join("/");
        if joined.is_empty() { None } else { Some(joined) }
    } else {
        None
    };
    include.map(|inc| {
        let mut m = Map::new();
        m.insert("include".to_string(), json!([inc]));
        m
    })
}

fn parse_lp_dependencies(raw: &str) -> Value {
    let mut result = Vec::new();
    for item in raw.split(',') {
        let item = item.trim();
        if item.is_empty() {
            continue;
        }
        if item.ends_with(')') && item.contains('(') {
            let (name, version) = item.split_once('(').unwrap();
            let version = version.strip_suffix(')').unwrap_or(version);
            result.push(json!({"name": name.trim(), "version": version.trim(), "frameworks": ["arduino"]}));
        } else {
            result.push(json!({"name": item, "frameworks": ["arduino"]}));
        }
    }
    Value::Array(result)
}

// ---------------------------------------------------------------------------
// platform.json
// ---------------------------------------------------------------------------

fn parse_platform_json(contents: &str) -> Result<Map<String, Value>> {
    let mut data = parse_json_object(contents)?;
    if let Some(v) = data.get("keywords") {
        data.insert("keywords".to_string(), str_list_value(str_to_list(v, ',', true, true)));
    }
    if let Some(fw) = data.get("frameworks").cloned() {
        let value = if let Some(obj) = fw.as_object() {
            let keys: Vec<String> = obj.keys().cloned().collect();
            str_list_value(normalize_items(keys, true, true))
        } else {
            Value::Null
        };
        data.insert("frameworks".to_string(), value);
    }
    if let Some(packages) = data.get("packages").cloned() {
        data.insert("dependencies".to_string(), parse_dependencies_platform(&packages));
    }
    Ok(data)
}

fn parse_dependencies_platform(raw: &Value) -> Value {
    let Some(obj) = raw.as_object() else { return json!([]) };
    let mut result = Vec::new();
    for (name, opts) in obj {
        let mut item = Map::new();
        item.insert("name".to_string(), Value::String(name.clone()));
        for k in ["owner", "version"] {
            if let Some(v) = opts.get(k) {
                item.insert(k.to_string(), v.clone());
            }
        }
        result.push(Value::Object(item));
    }
    Value::Array(result)
}

// ---------------------------------------------------------------------------
// package.json
// ---------------------------------------------------------------------------

fn parse_package_json(contents: &str) -> Result<Map<String, Value>> {
    let mut data = parse_json_object(contents)?;
    if let Some(v) = data.get("keywords") {
        data.insert("keywords".to_string(), str_list_value(str_to_list(v, ',', true, true)));
    }
    // _parse_system
    if let Some(system) = data.get("system").cloned() {
        let drop = matches!(system.as_str(), Some("*") | Some("all"))
            || system.as_array().is_some_and(|a| a.len() == 1 && a[0].as_str() == Some("*"));
        if drop {
            data.remove("system");
        } else {
            data.insert("system".to_string(), str_list_value(str_to_list(&system, ',', true, true)));
        }
    }
    // _parse_homepage
    if let Some(url) = data.remove("url") {
        data.insert("homepage".to_string(), url);
    }
    // _parse_repository
    if let Some(repo) = data.get("repository").cloned() {
        if !repo.is_object() {
            let mut url = repo.as_str().map_or_else(|| repo.to_string(), str::to_string);
            for prefix in ["github", "gitlab", "bitbucket"] {
                if let Some(rest) = url.strip_prefix(&format!("{prefix}:")) {
                    url = format!("https://{prefix}.com/{rest}");
                    break;
                }
            }
            data.insert("repository".to_string(), json!({"type": "git", "url": url}));
        }
    }
    Ok(data)
}
