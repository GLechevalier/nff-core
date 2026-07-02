//! Port of `platformio/package/meta.py`: `PackageType`, `PackageSpec` (the
//! spec-string parser), `PackageMetadata` (+ `.piopm` dump/load), `PackageItem`,
//! `PackageCompatibility`, and `PackageOutdatedResult`.
//!
//! The behavioural spec is the vendored `tests/package/test_meta.py`, mirrored as
//! Rust unit tests in [`crate::package::tests`].

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use regex::Regex;
use serde_json::{json, Value};

use crate::package::error::{PackageError, Result};
use crate::package::version::{cast_version_to_semver_default, SimpleSpec, Version};

// ---------------------------------------------------------------------------
// PackageType
// ---------------------------------------------------------------------------

/// `platformio.package.meta.PackageType` — the three package kinds.
pub struct PackageType;

impl PackageType {
    pub const LIBRARY: &'static str = "library";
    pub const PLATFORM: &'static str = "platform";
    pub const TOOL: &'static str = "tool";

    /// `PackageType.items()` — the member map, in the same (sorted-by-value)
    /// order upstream iterates when sniffing an archive.
    #[must_use]
    pub fn items() -> [&'static str; 3] {
        [Self::LIBRARY, Self::PLATFORM, Self::TOOL]
    }

    /// `PackageType.get_manifest_map()` — the manifest filenames that identify a
    /// package of each type. (`from_archive`, which sniffs a `tar.gz`, needs the
    /// archive reader and lands with M2c.)
    #[must_use]
    pub fn get_manifest_map(package_type: &str) -> &'static [&'static str] {
        match package_type {
            Self::PLATFORM => &["platform.json"],
            Self::LIBRARY => &["library.json", "library.properties", "module.json"],
            Self::TOOL => &["package.json"],
            _ => &[],
        }
    }
}

// ---------------------------------------------------------------------------
// PackageSpec
// ---------------------------------------------------------------------------

/// `platformio.package.meta.PackageSpec`.
///
/// Construct from a raw spec string with [`PackageSpec::parse`], or from explicit
/// fields with [`PackageSpec::builder`]. Equality mirrors `PackageSpec.__eq__`:
/// `owner`, `id`, `name`, `requirements`, and `uri` (never `raw` or the
/// custom-name flag).
#[derive(Debug, Clone)]
pub struct PackageSpec {
    pub owner: Option<String>,
    pub id: Option<i64>,
    pub name: Option<String>,
    pub uri: Option<String>,
    pub raw: Option<String>,
    requirements: Option<SimpleSpec>,
    name_is_custom: bool,
}

/// The three shapes `PackageSpec(requirements=...)` accepts upstream.
enum ReqInput {
    Str(String),
    Ver(Version),
    Spec(SimpleSpec),
}

/// Builder for the `PackageSpec(owner=..., name=..., requirements=..., ...)`
/// keyword-constructor form.
#[derive(Default)]
pub struct PackageSpecBuilder {
    owner: Option<String>,
    id: Option<i64>,
    name: Option<String>,
    uri: Option<String>,
    raw: Option<String>,
    requirements: Option<ReqInput>,
}

impl PackageSpecBuilder {
    #[must_use]
    pub fn owner(mut self, owner: impl Into<String>) -> Self {
        self.owner = Some(owner.into());
        self
    }
    #[must_use]
    pub fn id(mut self, id: i64) -> Self {
        self.id = Some(id);
        self
    }
    #[must_use]
    pub fn name(mut self, name: impl Into<String>) -> Self {
        self.name = Some(name.into());
        self
    }
    #[must_use]
    pub fn uri(mut self, uri: impl Into<String>) -> Self {
        self.uri = Some(uri.into());
        self
    }
    #[must_use]
    pub fn raw(mut self, raw: impl Into<String>) -> Self {
        self.raw = Some(raw.into());
        self
    }
    #[must_use]
    pub fn requirements(mut self, requirements: impl Into<String>) -> Self {
        self.requirements = Some(ReqInput::Str(requirements.into()));
        self
    }
    #[must_use]
    pub fn requirements_version(mut self, version: Version) -> Self {
        self.requirements = Some(ReqInput::Ver(version));
        self
    }
    #[must_use]
    pub fn requirements_spec(mut self, spec: SimpleSpec) -> Self {
        self.requirements = Some(ReqInput::Spec(spec));
        self
    }

    /// `PackageSpec.__init__` — assign fields, apply the requirements setter (with
    /// the fall-back-to-custom-name rewrite when it fails), then run `_parse`.
    pub fn build(self) -> Result<PackageSpec> {
        let mut spec = PackageSpec {
            owner: self.owner,
            id: self.id,
            name: self.name,
            uri: self.uri,
            raw: self.raw,
            requirements: None,
            name_is_custom: false,
        };
        if let Some(req) = self.requirements {
            let (req_str, result) = match req {
                ReqInput::Str(s) => {
                    let r = spec.set_requirements_str(&s);
                    (s, r)
                }
                ReqInput::Ver(v) => {
                    let s = v.to_string();
                    let r = spec.set_requirements_str(&s);
                    (s, r)
                }
                ReqInput::Spec(sp) => {
                    let s = sp.to_string();
                    spec.requirements = Some(sp);
                    (s, Ok(()))
                }
            };
            if let Err(e) = result {
                // __init__ lines 208-214: only rewrite when a name exists and no
                // uri/raw was supplied; otherwise re-raise.
                if spec.name.is_none() || spec.uri.is_some() || spec.raw.is_some() {
                    return Err(e);
                }
                spec.raw = Some(format!("{}={}", spec.name.as_ref().unwrap(), req_str));
            }
        }
        if let Some(raw) = spec.raw.clone() {
            spec.parse_raw(&raw)?;
        }
        Ok(spec)
    }
}

impl PackageSpec {
    /// `PackageSpec(builder-kwargs)`.
    #[must_use]
    pub fn builder() -> PackageSpecBuilder {
        PackageSpecBuilder::default()
    }

    /// `PackageSpec("<raw string>")`.
    pub fn parse(raw: &str) -> Result<Self> {
        PackageSpec::builder().raw(raw).build()
    }

    /// `PackageSpec(<int>)` — a numeric id passed positionally.
    pub fn from_id_literal(id: i64) -> Result<Self> {
        PackageSpec::parse(&id.to_string())
    }

    #[must_use]
    pub fn requirements(&self) -> Option<&SimpleSpec> {
        self.requirements.as_ref()
    }

    #[must_use]
    pub fn external(&self) -> bool {
        self.uri.is_some()
    }

    #[must_use]
    pub fn symlink(&self) -> bool {
        self.uri.as_ref().is_some_and(|u| u.starts_with("symlink://"))
    }

    #[must_use]
    pub fn has_custom_name(&self) -> bool {
        self.name_is_custom
    }

    /// `PackageSpec.humanize`.
    #[must_use]
    pub fn humanize(&self) -> String {
        let mut result = if let Some(uri) = &self.uri {
            uri.clone()
        } else if let Some(name) = &self.name {
            match &self.owner {
                Some(owner) => format!("{owner}/{name}"),
                None => name.clone(),
            }
        } else if let Some(id) = self.id {
            format!("id:{id}")
        } else {
            String::new()
        };
        if let Some(req) = &self.requirements {
            result.push_str(&format!(" @ {req}"));
        }
        result
    }

    /// `PackageSpec.as_dict`.
    #[must_use]
    pub fn as_dict(&self) -> Value {
        json!({
            "owner": self.owner,
            "id": self.id,
            "name": self.name,
            "requirements": self.requirements.as_ref().map(ToString::to_string),
            "uri": self.uri,
        })
    }

    /// `PackageSpec.as_dependency`.
    #[must_use]
    pub fn as_dependency(&self) -> String {
        if let Some(uri) = &self.uri {
            return self.raw.clone().unwrap_or_else(|| uri.clone());
        }
        let mut result = if let Some(name) = &self.name {
            match &self.owner {
                Some(owner) => format!("{owner}/{name}"),
                None => name.clone(),
            }
        } else if let Some(id) = self.id {
            id.to_string()
        } else {
            String::new()
        };
        debug_assert!(!result.is_empty());
        if let Some(req) = &self.requirements {
            result = format!("{result}@{req}");
        }
        result
    }

    /// `PackageSpec.requirements` setter (`""`/empty → `None`; otherwise a
    /// `SimpleSpec`, whose parse error surfaces as `SemanticVersionError`).
    fn set_requirements_str(&mut self, value: &str) -> Result<()> {
        if value.is_empty() {
            self.requirements = None;
            return Ok(());
        }
        match SimpleSpec::parse(value) {
            Ok(sp) => {
                self.requirements = Some(sp);
                Ok(())
            }
            // `raise SemanticVersionError(exc)` — carry the underlying message.
            Err(e) => Err(PackageError::SemanticVersion { message: e.to_string() }),
        }
    }

    // -- the `_parse` chain -------------------------------------------------

    /// `PackageSpec._parse`.
    fn parse_raw(&mut self, raw: &str) -> Result<()> {
        let mut cur = Some(raw.trim().to_string());
        if let Some(r) = cur.take() {
            cur = Some(Self::parse_local_file(&r));
        }
        if let Some(r) = cur.take() {
            cur = self.parse_requirements(&r)?;
        }
        if let Some(r) = cur.take() {
            cur = Some(self.parse_custom_name(&r));
        }
        if let Some(r) = cur.take() {
            cur = self.parse_id(&r);
        }
        if let Some(r) = cur.take() {
            cur = self.parse_owner(&r);
        }
        if let Some(r) = cur.take() {
            cur = self.parse_uri(&r);
        }
        // Finalize: name from URI, else the leftover.
        if self.name.is_none() {
            if let Some(uri) = &self.uri {
                self.name = Some(Self::parse_name_from_uri(uri));
            } else if let Some(r) = &cur {
                self.name = Some(r.clone());
            }
        } else if let Some(r) = &cur {
            // Upstream `elif raw: self.name = raw` fires even when a name is set.
            self.name = Some(r.clone());
        }
        Ok(())
    }

    /// `_parse_local_file`.
    fn parse_local_file(raw: &str) -> String {
        if raw.starts_with("file://")
            || raw.starts_with("symlink://")
            || !(raw.contains('/') || raw.contains('\\'))
        {
            return raw.to_string();
        }
        if Path::new(raw).exists() {
            return format!("file://{raw}");
        }
        raw.to_string()
    }

    /// `_parse_requirements`.
    fn parse_requirements(&mut self, raw: &str) -> Result<Option<String>> {
        if !raw.contains('@') || raw.starts_with("file://") || raw.starts_with("symlink://") {
            return Ok(Some(raw.to_string()));
        }
        let (head, tail) = raw.rsplit_once('@').unwrap();
        if tail.contains(':') || tail.contains('/') {
            return Ok(Some(raw.to_string()));
        }
        self.set_requirements_str(tail.trim())?;
        Ok(Some(head.trim().to_string()))
    }

    /// `_parse_custom_name`.
    fn parse_custom_name(&mut self, raw: &str) -> String {
        if !raw.contains('=') || raw.starts_with("id=") {
            return raw.to_string();
        }
        let (left, right) = raw.split_once('=').unwrap();
        if left.contains('/') {
            return raw.to_string();
        }
        self.name = Some(left.trim().to_string());
        self.name_is_custom = true;
        right.trim().to_string()
    }

    /// `_parse_id`.
    fn parse_id(&mut self, raw: &str) -> Option<String> {
        if is_digits(raw) {
            self.id = raw.parse().ok();
            return None;
        }
        if let Some(rest) = raw.strip_prefix("id=") {
            return self.parse_id(rest);
        }
        Some(raw.to_string())
    }

    /// `_parse_owner`.
    fn parse_owner(&mut self, raw: &str) -> Option<String> {
        if raw.matches('/').count() != 1 || raw.contains('@') {
            return Some(raw.to_string());
        }
        let (owner, name) = raw.split_once('/').unwrap();
        self.owner = Some(owner.trim().to_string());
        self.name = Some(name.trim().to_string());
        None
    }

    /// `_parse_uri`.
    fn parse_uri(&mut self, raw: &str) -> Option<String> {
        if !(raw.contains('@') || raw.contains(':') || raw.contains('/')) {
            return Some(raw.to_string());
        }
        let uri = raw.trim().to_string();
        self.uri = Some(uri.clone());
        let (scheme, netloc, path) = urlparse(&uri);

        // file/symlink/vcs schemes and pre-prefixed git+ URIs are left untouched.
        // (The `"symlink://"` literal reproduces the upstream tuple verbatim.)
        if scheme == "file" || scheme == "symlink://" || scheme.contains('+') || uri.starts_with("git+")
        {
            return None;
        }

        let is_archive = path.ends_with(".zip") || path.ends_with(".tar.gz") || path.ends_with(".tar.xz");
        let git = path.ends_with(".git")
            || (matches!(netloc.as_str(), "github.com" | "gitlab.com" | "bitbucket.com") && !is_archive);
        let hg = matches!(netloc.as_str(), "mbed.com" | "os.mbed.com" | "developer.mbed.org");
        if git {
            self.uri = Some(format!("git+{uri}"));
        } else if hg {
            self.uri = Some(format!("hg+{uri}"));
        }
        None
    }

    /// `_parse_name_from_uri`.
    fn parse_name_from_uri(uri: &str) -> String {
        let mut u = uri.strip_suffix('/').unwrap_or(uri).to_string();
        let mut stop_chars = vec!['#', '?'];
        if u.starts_with("file://") || u.starts_with("symlink://") {
            stop_chars.push('@');
        }
        for c in stop_chars {
            if let Some(idx) = u.find(c) {
                u.truncate(idx);
            }
        }
        let (_scheme, netloc, path) = urlparse(&u);
        if netloc == "github.com" && path.matches('/').count() > 2 {
            return path.split('/').nth(2).unwrap_or("").to_string();
        }
        let name = basename(&u);
        match name.split_once('.') {
            Some((head, _)) => head.trim().to_string(),
            None => name,
        }
    }
}

impl PartialEq for PackageSpec {
    fn eq(&self, other: &Self) -> bool {
        self.owner == other.owner
            && self.id == other.id
            && self.name == other.name
            && self.requirements == other.requirements
            && self.uri == other.uri
    }
}
impl Eq for PackageSpec {}

/// Python `str.isdigit()` for the ASCII case: non-empty, all digits.
fn is_digits(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

/// `os.path.basename` (splitting on both separators, as on Windows).
pub(crate) fn basename(s: &str) -> String {
    let idx = s.rfind(['/', '\\']);
    match idx {
        Some(i) => s[i + 1..].to_string(),
        None => s.to_string(),
    }
}

/// A minimal `urllib.parse.urlparse`, returning `(scheme, netloc, path)`. Only
/// the pieces `PackageSpec` inspects are computed; query/fragment are dropped
/// from `path`, matching upstream.
pub(crate) fn urlparse(u: &str) -> (String, String, String) {
    let (scheme, rest) = match u.find(':') {
        Some(i) => {
            let cand = &u[..i];
            let valid = !cand.is_empty()
                && cand.starts_with(|c: char| c.is_ascii_alphabetic())
                && cand.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'));
            if valid {
                (cand.to_string(), &u[i + 1..])
            } else {
                (String::new(), u)
            }
        }
        None => (String::new(), u),
    };
    let (netloc, path_part) = if let Some(stripped) = rest.strip_prefix("//") {
        let end = stripped.find(['/', '?', '#']).unwrap_or(stripped.len());
        (stripped[..end].to_string(), &stripped[end..])
    } else {
        (String::new(), rest)
    };
    let path_end = path_part.find(['?', '#']).unwrap_or(path_part.len());
    (scheme, netloc, path_part[..path_end].to_string())
}

// ---------------------------------------------------------------------------
// PackageMetadata
// ---------------------------------------------------------------------------

/// `platformio.package.meta.PackageMetadata`. Equality mirrors upstream:
/// `type`, `name`, `version`, `spec`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PackageMetadata {
    pub package_type: String,
    pub name: String,
    pub version: Option<Version>,
    pub spec: Option<PackageSpec>,
}

impl PackageMetadata {
    /// `PackageMetadata(type, name, version, spec=None)` with a string version
    /// (the common case; the setter casts via `cast_version_to_semver`).
    pub fn new(
        package_type: impl Into<String>,
        name: impl Into<String>,
        version: &str,
        spec: Option<PackageSpec>,
    ) -> Result<Self> {
        let mut md = PackageMetadata {
            package_type: package_type.into(),
            name: name.into(),
            version: None,
            spec,
        };
        md.set_version(version)?;
        Ok(md)
    }

    /// `PackageMetadata.version` setter — empty → `None`, else cast to semver.
    pub fn set_version(&mut self, value: &str) -> Result<()> {
        self.version =
            if value.is_empty() { None } else { Some(cast_version_to_semver_default(value)?) };
        Ok(())
    }

    /// `PackageMetadata.as_dict`.
    #[must_use]
    pub fn as_dict(&self) -> Value {
        json!({
            "type": self.package_type,
            "name": self.name,
            // Upstream unconditionally does `str(self.version)` (→ "None" when unset).
            "version": self.version.as_ref().map_or_else(|| "None".to_string(), ToString::to_string),
            "spec": self.spec.as_ref().map(PackageSpec::as_dict),
        })
    }

    /// `PackageMetadata.dump` — write `.piopm` JSON.
    pub fn dump(&self, path: &Path) -> Result<()> {
        let text = serde_json::to_string(&self.as_dict())
            .map_err(|e| PackageError::Package { message: e.to_string() })?;
        fs::write(path, text).map_err(|e| PackageError::Package { message: e.to_string() })
    }

    /// `PackageMetadata.load` — read `.piopm` JSON (with the Core<5.3
    /// `spec.url`→`spec.uri` shim).
    pub fn load(path: &Path) -> Result<PackageMetadata> {
        let text =
            fs::read_to_string(path).map_err(|e| PackageError::Package { message: e.to_string() })?;
        let data: Value = serde_json::from_str(&text)
            .map_err(|e| PackageError::Package { message: e.to_string() })?;

        let spec = match data.get("spec") {
            Some(spec_val) if !spec_val.is_null() => {
                let get_str = |k: &str| spec_val.get(k).and_then(Value::as_str).map(str::to_string);
                let mut builder = PackageSpec::builder();
                if let Some(owner) = get_str("owner") {
                    builder = builder.owner(owner);
                }
                if let Some(id) = spec_val.get("id").and_then(Value::as_i64) {
                    builder = builder.id(id);
                }
                if let Some(name) = get_str("name") {
                    builder = builder.name(name);
                }
                if let Some(req) = get_str("requirements") {
                    builder = builder.requirements(req);
                }
                // Legacy Core<5.3 stored the location under `url`.
                if let Some(uri) = get_str("uri").or_else(|| get_str("url")) {
                    builder = builder.uri(uri);
                }
                Some(builder.build()?)
            }
            _ => None,
        };

        let package_type = data.get("type").and_then(Value::as_str).unwrap_or("").to_string();
        let name = data.get("name").and_then(Value::as_str).unwrap_or("").to_string();
        let version = data.get("version").and_then(Value::as_str).unwrap_or("");
        PackageMetadata::new(package_type, name, version, spec)
    }
}

// ---------------------------------------------------------------------------
// PackageItem
// ---------------------------------------------------------------------------

/// `platformio.package.meta.PackageItem` — an on-disk package directory and its
/// `.piopm` metadata.
#[derive(Debug, Clone)]
pub struct PackageItem {
    pub path: PathBuf,
    pub metadata: Option<PackageMetadata>,
}

impl PackageItem {
    pub const METAFILE_NAME: &'static str = ".piopm";

    /// `PackageItem(path, metadata=None)` — loads metadata from disk if present.
    pub fn new(path: impl Into<PathBuf>, metadata: Option<PackageMetadata>) -> Result<Self> {
        let path = path.into();
        let mut item = PackageItem { path, metadata };
        if item.metadata.is_none() && item.exists() {
            item.metadata = item.load_meta()?;
        }
        Ok(item)
    }

    #[must_use]
    pub fn exists(&self) -> bool {
        self.path.is_dir()
    }

    /// `PackageItem.get_safe_dirname`.
    #[must_use]
    pub fn get_safe_dirname(&self) -> Option<String> {
        static RE: OnceLock<Regex> = OnceLock::new();
        let re = RE.get_or_init(|| Regex::new(r"(?i)[^\da-z_\-. ]").unwrap());
        self.metadata.as_ref().map(|md| re.replace_all(&md.name, "_").into_owned())
    }

    /// `PackageItem.get_metafile_locations` — VCS dirs first, then the root.
    #[must_use]
    pub fn get_metafile_locations(&self) -> Vec<PathBuf> {
        vec![
            self.path.join(".git"),
            self.path.join(".hg"),
            self.path.join(".svn"),
            self.path.clone(),
        ]
    }

    /// `PackageItem.load_meta`.
    pub fn load_meta(&self) -> Result<Option<PackageMetadata>> {
        for location in self.get_metafile_locations() {
            let manifest_path = location.join(Self::METAFILE_NAME);
            if manifest_path.is_file() {
                return Ok(Some(PackageMetadata::load(&manifest_path)?));
            }
        }
        Ok(None)
    }

    /// `PackageItem.dump_meta`.
    pub fn dump_meta(&self) -> Result<()> {
        let location = self
            .get_metafile_locations()
            .into_iter()
            .find(|l| l.is_dir())
            .ok_or_else(|| PackageError::Package {
                message: "no directory to write .piopm".to_string(),
            })?;
        let md = self
            .metadata
            .as_ref()
            .ok_or_else(|| PackageError::Package { message: "no metadata to dump".to_string() })?;
        md.dump(&location.join(Self::METAFILE_NAME))
    }
}

// ---------------------------------------------------------------------------
// PackageCompatibility
// ---------------------------------------------------------------------------

/// A compatibility qualifier value: a scalar string, a list, or `None`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Qualifier {
    Str(String),
    List(Vec<String>),
    None,
}

impl Qualifier {
    #[must_use]
    pub fn str(value: impl Into<String>) -> Self {
        Qualifier::Str(value.into())
    }
    #[must_use]
    pub fn list<I, S>(values: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Qualifier::List(values.into_iter().map(Into::into).collect())
    }

    /// Python truthiness: `None`, `""`, and `[]` are falsy.
    fn is_truthy(&self) -> bool {
        match self {
            Qualifier::Str(s) => !s.is_empty(),
            Qualifier::List(l) => !l.is_empty(),
            Qualifier::None => false,
        }
    }

    fn is_list(&self) -> bool {
        matches!(self, Qualifier::List(_))
    }

    fn to_list(&self) -> Vec<String> {
        match self {
            Qualifier::Str(s) => vec![s.clone()],
            Qualifier::List(l) => l.clone(),
            Qualifier::None => Vec::new(),
        }
    }
}

/// `platformio.package.meta.PackageCompatibility`.
#[derive(Debug, Clone, Default)]
pub struct PackageCompatibility {
    qualifiers: BTreeMap<String, Qualifier>,
}

impl PackageCompatibility {
    pub const KNOWN_QUALIFIERS: [&'static str; 6] =
        ["owner", "name", "version", "platforms", "frameworks", "authors"];

    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Set a qualifier, validating the key against `KNOWN_QUALIFIERS` (the
    /// `ValueError` upstream raises for unknown keys).
    pub fn set(mut self, key: &str, value: Qualifier) -> Result<Self> {
        if !Self::KNOWN_QUALIFIERS.contains(&key) {
            return Err(PackageError::UnknownCompatibilityQualifier { qualifier: key.to_string() });
        }
        self.qualifiers.insert(key.to_string(), value);
        Ok(self)
    }

    /// `PackageCompatibility.is_compatible`.
    #[must_use]
    pub fn is_compatible(&self, other: &PackageCompatibility) -> bool {
        for (key, current) in &self.qualifiers {
            let other_value = other.qualifiers.get(key);
            let other_truthy = other_value.is_some_and(Qualifier::is_truthy);
            if !current.is_truthy() || !other_truthy {
                continue;
            }
            let other_value = other_value.unwrap();
            if current.is_list() || other_value.is_list() {
                if !items_in_list(current, other_value) {
                    return false;
                }
                continue;
            }
            if key == "version" {
                if !Self::compare_versions(current, other_value) {
                    return false;
                }
                continue;
            }
            if current != other_value {
                return false;
            }
        }
        true
    }

    /// `PackageCompatibility._compare_versions`.
    fn compare_versions(current: &Qualifier, other: &Qualifier) -> bool {
        if current == other {
            return true;
        }
        let (Qualifier::Str(spec), Qualifier::Str(version)) = (current, other) else {
            return false;
        };
        let Ok(v) = cast_version_to_semver_default(version) else {
            return false;
        };
        SimpleSpec::parse(spec).is_ok_and(|s| s.contains(&v))
    }
}

/// `platformio.util.items_in_list` for two truthy qualifiers.
fn items_in_list(needle: &Qualifier, haystack: &Qualifier) -> bool {
    let needle = needle.to_list();
    let haystack = haystack.to_list();
    if needle.iter().any(|x| x == "*") || haystack.iter().any(|x| x == "*") {
        return true;
    }
    needle.iter().any(|n| haystack.contains(n))
}

// ---------------------------------------------------------------------------
// PackageOutdatedResult
// ---------------------------------------------------------------------------

/// `platformio.package.meta.PackageOutdatedResult`.
#[derive(Debug, Clone)]
pub struct PackageOutdatedResult {
    pub current: Option<Version>,
    pub latest: Option<Version>,
    pub wanted: Option<Version>,
    pub detached: bool,
}

impl PackageOutdatedResult {
    /// `PackageOutdatedResult(current, latest=None, wanted=None, detached=False)`
    /// — the `__setattr__` casts each non-empty version string via
    /// `cast_version_to_semver`.
    pub fn new(
        current: &str,
        latest: Option<&str>,
        wanted: Option<&str>,
        detached: bool,
    ) -> Result<Self> {
        let cast = |v: Option<&str>| -> Result<Option<Version>> {
            match v {
                Some(s) if !s.is_empty() => Ok(Some(cast_version_to_semver_default(s)?)),
                _ => Ok(None),
            }
        };
        Ok(Self {
            current: cast(Some(current))?,
            latest: cast(latest)?,
            wanted: cast(wanted)?,
            detached,
        })
    }

    /// `PackageOutdatedResult.is_outdated`.
    #[must_use]
    pub fn is_outdated(&self, allow_incompatible: bool) -> bool {
        if self.detached || self.latest.is_none() || self.current == self.latest {
            return false;
        }
        if allow_incompatible {
            return self.current != self.latest;
        }
        if self.wanted.is_some() {
            return self.current != self.wanted;
        }
        true
    }
}
