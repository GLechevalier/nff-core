//! Port of `platformio/package/manager/` — the offline (local) operations of
//! `BasePackageManager` + the install/uninstall/legacy mixins + the concrete
//! library/tool/platform managers.
//!
//! In scope (mirrors the local-only subset of `tests/package/test_manager.py`):
//! directory resolution, `find_pkg_root`/`load_manifest`/`build_metadata`/
//! `build_legacy_spec`, `get_installed`/`get_package`/`test_pkg_spec`,
//! `install_from_uri` (file:// dir + archive) with the `_install_tmp_pkg`
//! placement/detach logic, dependency resolution (with the circular-install
//! guard), and `uninstall` (with the detached-package fixup).
//!
//! Deferred (documented): `install_from_registry` + the registry/http layer
//! (network), `call_pkg_script` (Python exec — a no-op here), `PlatformFactory`
//! built-in-library detection (`is_builtin_lib` is always false), symlink
//! packages, and `update`. The in-process memory cache is omitted (recomputing
//! is always correct; it was a performance optimization).

use std::cell::RefCell;
use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

use serde_json::Value;

use crate::config::options::get_default_core_dir;
use crate::package::download::{compute_download_path, http_download, verify_checksum};
use crate::package::error::{PackageError, Result};
use crate::package::manifest::parser::ManifestParserFactory;
use crate::package::meta::{PackageItem, PackageMetadata, PackageSpec, PackageType};
use crate::package::unpack::FileUnpacker;
use crate::package::version::cast_version_to_semver_default;
use crate::registry::{RegistryClient, RegistryFileMirrorIterator};

// ---------------------------------------------------------------------------
// Directory resolution (ProjectConfig `platformio.*_dir` defaults)
// ---------------------------------------------------------------------------

fn env_path(var: &str) -> Option<PathBuf> {
    std::env::var_os(var).filter(|v| !v.is_empty()).map(PathBuf::from)
}

fn core_dir() -> PathBuf {
    env_path("PLATFORMIO_CORE_DIR").unwrap_or_else(|| PathBuf::from(get_default_core_dir()))
}

fn cache_dir() -> PathBuf {
    #[cfg(test)]
    if let Some(p) = test_cache::get() {
        return p;
    }
    env_path("PLATFORMIO_CACHE_DIR").unwrap_or_else(|| core_dir().join(".cache"))
}

/// Test-only per-thread override for the cache dir, so install tests can isolate
/// their scratch (`downloads`/`tmp`) without mutating the process-global
/// `PLATFORMIO_*` environment (which other modules' tests read).
#[cfg(test)]
pub(crate) mod test_cache {
    use std::cell::RefCell;
    use std::path::PathBuf;

    thread_local! {
        static OVERRIDE: RefCell<Option<PathBuf>> = const { RefCell::new(None) };
    }

    pub(crate) fn set(path: Option<PathBuf>) {
        OVERRIDE.with(|c| *c.borrow_mut() = path);
    }

    pub(crate) fn get() -> Option<PathBuf> {
        OVERRIDE.with(|c| c.borrow().clone())
    }
}

fn packages_dir() -> PathBuf {
    env_path("PLATFORMIO_PACKAGES_DIR").unwrap_or_else(|| core_dir().join("packages"))
}

fn platforms_dir() -> PathBuf {
    env_path("PLATFORMIO_PLATFORMS_DIR").unwrap_or_else(|| core_dir().join("platforms"))
}

fn globallib_dir() -> PathBuf {
    env_path("PLATFORMIO_GLOBALLIB_DIR").unwrap_or_else(|| core_dir().join("lib"))
}

// ---------------------------------------------------------------------------
// System-compatibility (util.get_systype / items_in_list)
// ---------------------------------------------------------------------------

/// `platformio.util.get_systype` — a stable `{system}_{arch}` string. The exact
/// value need only be self-consistent (callers compare it to itself).
#[must_use]
pub fn get_systype() -> String {
    let system = match std::env::consts::OS {
        "macos" => "darwin",
        other => other,
    };
    let arch = match (std::env::consts::OS, std::env::consts::ARCH) {
        ("windows", "x86_64") => "amd64",
        (_, arch) => arch,
    };
    format!("{system}_{arch}")
}

fn items_in_list(needle: &[String], haystack: &[String]) -> bool {
    if needle.iter().any(|x| x == "*") || haystack.iter().any(|x| x == "*") {
        return true;
    }
    needle.iter().any(|n| haystack.contains(n))
}

/// `compat.ci_strings_are_equal`.
fn ci_strings_are_equal(a: Option<&str>, b: Option<&str>) -> bool {
    match (a, b) {
        (Some(a), Some(b)) => a.trim().eq_ignore_ascii_case(b.trim()),
        (None, None) => true,
        _ => false,
    }
}

fn abspath(path: &Path) -> PathBuf {
    fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf())
}

static TMP_COUNTER: AtomicUsize = AtomicUsize::new(0);

// ---------------------------------------------------------------------------
// PackageManager
// ---------------------------------------------------------------------------

/// A unified package manager (`BasePackageManager` + concrete flavour). Build one
/// with [`PackageManager::library`], [`PackageManager::tool`], or
/// [`PackageManager::platform`].
pub struct PackageManager {
    pub pkg_type: &'static str,
    pub package_dir: PathBuf,
    install_history: RefCell<HashMap<String, PackageItem>>,
    uninstalled: RefCell<Vec<PathBuf>>,
}

impl PackageManager {
    #[must_use]
    pub fn library(package_dir: Option<PathBuf>) -> Self {
        Self::new(PackageType::LIBRARY, package_dir.unwrap_or_else(globallib_dir))
    }
    #[must_use]
    pub fn tool(package_dir: Option<PathBuf>) -> Self {
        Self::new(PackageType::TOOL, package_dir.unwrap_or_else(packages_dir))
    }
    #[must_use]
    pub fn platform(package_dir: Option<PathBuf>) -> Self {
        Self::new(PackageType::PLATFORM, package_dir.unwrap_or_else(platforms_dir))
    }

    fn new(pkg_type: &'static str, package_dir: PathBuf) -> Self {
        Self {
            pkg_type,
            package_dir,
            install_history: RefCell::new(HashMap::new()),
            uninstalled: RefCell::new(Vec::new()),
        }
    }

    fn manifest_names(&self) -> &'static [&'static str] {
        PackageType::get_manifest_map(self.pkg_type)
    }

    fn missing_manifest(&self) -> PackageError {
        PackageError::MissingPackageManifest { names: self.manifest_names().join(", ") }
    }

    fn get_download_dir(&self) -> Result<PathBuf> {
        let dir = cache_dir().join("downloads");
        fs::create_dir_all(&dir).map_err(|e| PackageError::Package { message: e.to_string() })?;
        Ok(dir)
    }

    fn get_tmp_dir(&self) -> Result<PathBuf> {
        let dir = cache_dir().join("tmp");
        fs::create_dir_all(&dir).map_err(|e| PackageError::Package { message: e.to_string() })?;
        Ok(dir)
    }

    fn mkdtemp(&self) -> Result<PathBuf> {
        let base = self.get_tmp_dir()?;
        let n = TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
        let dir = base.join(format!("pkg-installing-{}-{n}", std::process::id()));
        fs::create_dir_all(&dir).map_err(|e| PackageError::Package { message: e.to_string() })?;
        Ok(dir)
    }

    // -- manifest / metadata -------------------------------------------------

    fn get_manifest_path(&self, pkg_dir: &Path) -> Option<PathBuf> {
        if !pkg_dir.is_dir() {
            return None;
        }
        self.manifest_names().iter().map(|n| pkg_dir.join(n)).find(|p| p.is_file())
    }

    fn manifest_exists(&self, pkg_dir: &Path) -> bool {
        self.get_manifest_path(pkg_dir).is_some()
    }

    /// `BasePackageManager.find_pkg_root` (+ the library auto-manifest override).
    pub fn find_pkg_root(&self, path: &Path, spec: Option<&PackageSpec>) -> Result<PathBuf> {
        if self.manifest_exists(path) {
            return Ok(path.to_path_buf());
        }
        let mut dirs = Vec::new();
        collect_dirs_topdown(path, &mut dirs);
        for root in &dirs {
            if self.manifest_exists(root) {
                return Ok(root.clone());
            }
        }
        if self.pkg_type == PackageType::LIBRARY {
            if let Some(spec) = spec {
                let root = Self::find_library_root(path);
                let name = spec.name.clone().unwrap_or_default();
                let manifest =
                    serde_json::json!({"name": name, "version": Self::generate_rand_version()});
                let text = serde_json::to_string_pretty(&manifest)
                    .map_err(|e| PackageError::Package { message: e.to_string() })?;
                fs::write(root.join("library.json"), text)
                    .map_err(|e| PackageError::Package { message: e.to_string() })?;
                return Ok(root);
            }
        }
        Err(self.missing_manifest())
    }

    /// `LibraryPackageManager.find_library_root`.
    #[must_use]
    pub fn find_library_root(path: &Path) -> PathBuf {
        const DIR_SIGNS: &[&str] = &["include", "Include", "inc", "Inc", "src", "Src"];
        const FILE_SIGNS: &[&str] = &["conanfile.py", "CMakeLists.txt"];
        for (root, dirs, files) in walk_dirs_files(path) {
            if files.is_empty() && dirs.len() == 1 {
                continue;
            }
            if dirs.iter().any(|d| DIR_SIGNS.contains(&d.as_str())) {
                return root;
            }
            if files.iter().any(|f| FILE_SIGNS.contains(&f.as_str())) {
                return root;
            }
            if files.iter().any(|f| {
                f.ends_with(".c") || f.ends_with(".cpp") || f.ends_with(".h") || f.ends_with(".hpp") || f.ends_with(".S")
            }) {
                return root;
            }
        }
        path.to_path_buf()
    }

    /// `BasePackageManager.load_manifest`.
    pub fn load_manifest(&self, path: &Path) -> Result<Value> {
        let candidates: Vec<PathBuf> = if path.is_dir() {
            self.manifest_names().iter().map(|n| path.join(n)).collect()
        } else {
            vec![path.to_path_buf()]
        };
        for item in candidates {
            if !item.is_file() {
                continue;
            }
            if let Ok(mp) = ManifestParserFactory::new_from_file(&item, None) {
                return Ok(mp.as_dict());
            }
        }
        Err(self.missing_manifest())
    }

    /// `BasePackageManager.generate_rand_version` (a monotonic build-metadata
    /// stamp; the calendar format is simplified to a Unix-seconds counter).
    fn generate_rand_version() -> String {
        let secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let n = TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
        format!("0.0.0+{secs}{n}")
    }

    /// `BasePackageManager.build_metadata`.
    pub fn build_metadata(
        &self,
        pkg_dir: &Path,
        spec: &PackageSpec,
        vcs_revision: Option<&str>,
    ) -> Result<PackageMetadata> {
        let manifest = self.load_manifest(pkg_dir)?;
        let name = manifest.get("name").and_then(Value::as_str).unwrap_or("");
        let version = manifest.get("version").and_then(Value::as_str).unwrap_or("");

        let mut md = PackageMetadata::new(self.pkg_type, name, version, Some(spec.clone()))?;
        if md.name.is_empty() || spec.has_custom_name() {
            md.name = spec.name.clone().unwrap_or_default();
        }
        if let Some(rev) = vcs_revision {
            let base = if version.is_empty() { "0.0.0" } else { version };
            md.set_version(&format!("{base}+sha.{rev}"))?;
        }
        if md.version.is_none() {
            md.set_version(&Self::generate_rand_version())?;
        }
        Ok(md)
    }

    /// `PackageManagerLegacyMixin.build_legacy_spec`.
    pub fn build_legacy_spec(&self, pkg_dir: &Path) -> Result<PackageSpec> {
        if let Ok(entries) = fs::read_dir(pkg_dir) {
            for e in entries.filter_map(std::result::Result::ok) {
                let candidate = e.path().join(".piopkgmanager.json");
                if candidate.is_file() {
                    let text = fs::read_to_string(&candidate)
                        .map_err(|e| PackageError::Package { message: e.to_string() })?;
                    let json: Value = serde_json::from_str(&text)
                        .map_err(|e| PackageError::Package { message: e.to_string() })?;
                    let mut b = PackageSpec::builder();
                    if let Some(name) = json.get("name").and_then(Value::as_str) {
                        b = b.name(name);
                    }
                    if let Some(url) = json.get("url").and_then(Value::as_str) {
                        b = b.uri(url);
                    }
                    if let Some(req) = json.get("requirements").and_then(Value::as_str) {
                        b = b.requirements(req);
                    }
                    return b.build();
                }
            }
        }
        let manifest = self.load_manifest(pkg_dir)?;
        let name = manifest.get("name").and_then(Value::as_str).unwrap_or("");
        PackageSpec::builder().name(name).build()
    }

    // -- discovery -----------------------------------------------------------

    /// `BasePackageManager.get_installed`.
    pub fn get_installed(&self) -> Result<Vec<PackageItem>> {
        if !self.package_dir.is_dir() {
            return Ok(Vec::new());
        }
        let mut names: Vec<String> = fs::read_dir(&self.package_dir)
            .map_err(|e| PackageError::Package { message: e.to_string() })?
            .filter_map(std::result::Result::ok)
            .filter_map(|e| e.file_name().into_string().ok())
            .collect();
        names.sort();

        let mut result = Vec::new();
        for name in names {
            if name.starts_with("_tmp_installing") {
                continue;
            }
            let path = self.package_dir.join(&name);
            if !path.is_dir() {
                continue; // symlink packages (.pio-link) deferred
            }
            let mut pkg = PackageItem::new(path.clone(), None)?;
            if pkg.metadata.is_none() {
                if let Ok(spec) = self.build_legacy_spec(&path) {
                    if let Ok(md) = self.build_metadata(&path, &spec, None) {
                        pkg.metadata = Some(md);
                    }
                }
            }
            if pkg.metadata.is_none() {
                continue;
            }
            if self.pkg_type == PackageType::TOOL {
                if let Ok(manifest) = self.load_manifest(&path) {
                    let system: Vec<String> = manifest
                        .get("system")
                        .and_then(Value::as_array)
                        .map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect())
                        .unwrap_or_default();
                    if !Self::is_system_compatible(&system) {
                        continue;
                    }
                }
            }
            result.push(pkg);
        }
        Ok(result)
    }

    /// `BasePackageManager.is_system_compatible`.
    #[must_use]
    pub fn is_system_compatible(value: &[String]) -> bool {
        if value.is_empty() || value.iter().any(|v| v == "*") {
            return true;
        }
        items_in_list(value, &[get_systype()])
    }

    /// `BasePackageManager.get_package`.
    pub fn get_package(&self, spec: &PackageSpec) -> Result<Option<PackageItem>> {
        let mut best: Option<PackageItem> = None;
        for pkg in self.get_installed()? {
            if !Self::test_pkg_spec(&pkg, spec) {
                continue;
            }
            let version = pkg.metadata.as_ref().and_then(|m| m.version.clone());
            let Some(version) = version else { continue };
            if let Some(req) = spec.requirements() {
                if !req.contains(&version) {
                    continue;
                }
            }
            let better = match &best {
                None => true,
                Some(b) => {
                    let bv = b.metadata.as_ref().and_then(|m| m.version.clone());
                    bv.is_none_or(|bv| version.precedence_cmp(&bv, false) == std::cmp::Ordering::Greater)
                }
            };
            if better {
                best = Some(pkg);
            }
        }
        Ok(best)
    }

    /// `BasePackageManager.test_pkg_spec`.
    fn test_pkg_spec(pkg: &PackageItem, spec: &PackageSpec) -> bool {
        let meta = match &pkg.metadata {
            Some(m) => m,
            None => return false,
        };
        let pkg_spec = match &meta.spec {
            Some(s) => s,
            None => return false,
        };
        // id mismatch
        if let Some(id) = spec.id {
            if Some(id) != pkg_spec.id {
                return false;
            }
        }
        if spec.external() {
            let uri = spec.uri.as_deref().unwrap_or("");
            let pkg_abs = abspath(&pkg.path);
            let conds = [
                abspath(Path::new(uri)) == pkg_abs,
                uri.starts_with("file://") && abspath(Path::new(&uri[7..])) == pkg_abs,
                uri.starts_with("symlink://") && abspath(Path::new(&uri[10..])) == pkg_abs,
            ];
            if conds.iter().any(|c| *c) {
                return true;
            }
            if spec.uri != pkg_spec.uri {
                return false;
            }
        } else if (spec.owner.is_some()
            && !ci_strings_are_equal(spec.owner.as_deref(), pkg_spec.owner.as_deref()))
            // Upstream's two `elif`s (both non-external) each return false on a
            // mismatch; merged here since the bodies are identical.
            || (spec.id.is_none() && !ci_strings_are_equal(spec.name.as_deref(), Some(meta.name.as_str())))
        {
            return false;
        }
        true
    }

    fn get_pkg_dependencies(&self, pkg: &PackageItem) -> Option<Vec<Value>> {
        self.load_manifest(&pkg.path)
            .ok()
            .and_then(|m| m.get("dependencies").and_then(Value::as_array).cloned())
    }

    fn dependency_to_spec(dependency: &Value) -> Result<PackageSpec> {
        let mut b = PackageSpec::builder();
        if let Some(owner) = dependency.get("owner").and_then(Value::as_str) {
            b = b.owner(owner);
        }
        if let Some(name) = dependency.get("name").and_then(Value::as_str) {
            b = b.name(name);
        }
        if let Some(version) = dependency.get("version").and_then(Value::as_str) {
            b = b.requirements(version);
        }
        b.build()
    }

    // -- install -------------------------------------------------------------

    /// `PackageManagerInstallMixin.install`.
    pub fn install(&self, spec: &PackageSpec) -> Result<PackageItem> {
        self.install_impl(spec, false, false)
    }

    fn install_impl(&self, spec: &PackageSpec, skip_dependencies: bool, force: bool) -> Result<PackageItem> {
        let key = spec_key(spec);
        if !force {
            if let Some(pkg) = self.install_history.borrow().get(&key) {
                return Ok(pkg.clone());
            }
        }

        let existing = self.get_package(spec)?;
        if let Some(existing) = existing {
            if force {
                self.uninstall_pkg(&existing)?;
            } else {
                self.install_history.borrow_mut().insert(key, existing.clone());
                if !skip_dependencies {
                    self.install_dependencies(&existing);
                }
                return Ok(existing);
            }
        }

        let pkg = if spec.external() {
            self.install_from_uri(spec.uri.as_deref().unwrap_or(""), spec, None)?
        } else {
            self.install_from_registry(spec, None)?.ok_or_else(|| PackageError::Package {
                message: format!(
                    "Could not install package '{}' for '{}' system",
                    spec.humanize(),
                    get_systype()
                ),
            })?
        };

        // call_pkg_script(postinstall) — no-op (Python exec deferred)
        self.install_history.borrow_mut().insert(key, pkg.clone());
        if !skip_dependencies {
            self.install_dependencies(&pkg);
        }
        Ok(pkg)
    }

    fn install_dependencies(&self, pkg: &PackageItem) {
        let Some(deps) = self.get_pkg_dependencies(pkg) else { return };
        for dep in &deps {
            // library skip-builtin: is_builtin_lib is always false here, so a
            // dependency is always installed. Errors are best-effort (warn).
            if let Ok(spec) = Self::dependency_to_spec(dep) {
                let _ = self.install_impl(&spec, false, false);
            }
        }
    }

    /// `PackageManagerInstallMixin.install_from_uri` (file:// dir + archive).
    pub fn install_from_uri(&self, uri: &str, spec: &PackageSpec, checksum: Option<&str>) -> Result<PackageItem> {
        if spec.symlink() {
            return Err(PackageError::Package {
                message: "symlink:// packages are not supported yet".to_string(),
            });
        }
        let tmp_dir = self.mkdtemp()?;
        let result = (|| {
            if let Some(local) = uri.strip_prefix("file://") {
                let local = Path::new(local);
                if local.is_file() {
                    FileUnpacker::new(local)?.unpack(&tmp_dir, true)?;
                } else {
                    let _ = fs::remove_dir_all(&tmp_dir);
                    copy_dir(local, &tmp_dir)?;
                }
            } else if uri.starts_with("http://") || uri.starts_with("https://") {
                let dl = self.download(uri, checksum)?;
                FileUnpacker::new(&dl)?.unpack(&tmp_dir, true)?;
            } else {
                return Err(PackageError::Package {
                    message: format!("VCS packages are not supported yet: {uri}"),
                });
            }

            let root_dir = self.find_pkg_root(&tmp_dir, Some(spec))?;
            let metadata = self.build_metadata(&root_dir, spec, None)?;
            let pkg_item = PackageItem::new(root_dir, Some(metadata))?;
            pkg_item.dump_meta()?;
            self.install_tmp_pkg(&pkg_item)
        })();
        let _ = fs::remove_dir_all(&tmp_dir);
        result
    }

    /// `PackageManagerInstallMixin._install_tmp_pkg` — placement with the
    /// detach-existing / detach-new / overwrite logic.
    fn install_tmp_pkg(&self, tmp_pkg: &PackageItem) -> Result<PackageItem> {
        let tmp_meta = tmp_pkg.metadata.as_ref().expect("tmp pkg has metadata");
        let tmp_spec = tmp_meta.spec.as_ref().expect("tmp pkg has spec");
        let tmp_version = tmp_meta.version.clone().expect("tmp pkg has version");

        // validate version vs declared requirements
        if let Some(req) = tmp_spec.requirements() {
            if !req.contains(&tmp_version) {
                return Err(PackageError::Package {
                    message: format!(
                        "Package version {tmp_version} doesn't satisfy requirements {req}"
                    ),
                });
            }
        }

        let safe = tmp_pkg.get_safe_dirname().unwrap_or_default();
        let mut dst_path = self.package_dir.join(&safe);
        let mut action = "overwrite";

        if tmp_spec.has_custom_name() {
            action = "overwrite";
            dst_path = self.package_dir.join(tmp_spec.name.clone().unwrap_or_default());
        } else if let Ok(dst_pkg) = PackageItem::new(dst_path.clone(), None) {
            if let Some(dst_meta) = &dst_pkg.metadata {
                let dst_spec = dst_meta.spec.as_ref();
                let dst_external = dst_spec.is_some_and(PackageSpec::external);
                if dst_external {
                    if dst_spec.and_then(|s| s.uri.as_ref()) != tmp_spec.uri.as_ref() {
                        action = "detach-existing";
                    }
                } else if let Some(dst_version) = &dst_meta.version {
                    let dst_owner = dst_spec.and_then(|s| s.owner.as_ref());
                    if dst_version != &tmp_version || dst_owner != tmp_spec.owner.as_ref() {
                        action = if tmp_version.precedence_cmp(dst_version, false) == std::cmp::Ordering::Greater {
                            "detach-existing"
                        } else {
                            "detach-new"
                        };
                    }
                }
            } else {
                // no existing metadata → plain overwrite
            }
        }

        match action {
            "detach-existing" => {
                let dst_pkg = PackageItem::new(dst_path.clone(), None)?;
                let dst_meta = dst_pkg.metadata.as_ref().unwrap();
                let target = detach_dirname(&safe, dst_meta);
                let moved = self.package_dir.join(target);
                remove_dir_if_exists(&moved)?;
                copy_dir(&dst_pkg.path, &moved)?;
                remove_dir_if_exists(&dst_path)?;
                copy_dir(&tmp_pkg.path, &dst_path)?;
                PackageItem::new(dst_path, None)
            }
            "detach-new" => {
                let target = detach_dirname(&safe, tmp_meta);
                let pkg_dir = self.package_dir.join(target);
                remove_dir_if_exists(&pkg_dir)?;
                copy_dir(&tmp_pkg.path, &pkg_dir)?;
                PackageItem::new(pkg_dir, None)
            }
            _ => {
                remove_dir_if_exists(&dst_path)?;
                copy_dir(&tmp_pkg.path, &dst_path)?;
                PackageItem::new(dst_path, None)
            }
        }
    }

    // -- download + registry resolution (networked) --------------------------

    /// `PackageManagerDownloadMixin.download` — content-addressed cache + verify.
    fn download(&self, url: &str, checksum: Option<&str>) -> Result<PathBuf> {
        let dl_dir = self.get_download_dir()?;
        let dl_path = compute_download_path(&dl_dir, url, checksum);
        if dl_path.is_file() {
            return Ok(dl_path);
        }
        let tmp = dl_dir.join(format!(
            "dl-{}-{}.tmp",
            std::process::id(),
            TMP_COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        http_download(url, &tmp)?;
        if let Some(cs) = checksum {
            verify_checksum(&tmp, cs)?;
        }
        if dl_path.exists() {
            let _ = fs::remove_file(&dl_path);
        }
        fs::rename(&tmp, &dl_path).map_err(|e| PackageError::Package { message: e.to_string() })?;
        Ok(dl_path)
    }

    /// `PackageManagerRegistryMixin.install_from_registry`.
    fn install_from_registry(
        &self,
        spec: &PackageSpec,
        search_qualifiers: Option<()>,
    ) -> Result<Option<PackageItem>> {
        let unknown = || PackageError::UnknownPackage { spec: spec.humanize() };

        let (package, version): (Option<Value>, Option<Value>) =
            if spec.owner.is_some() && spec.name.is_some() && search_qualifiers.is_none() {
                let package = self.fetch_registry_package(spec)?;
                let version = pick_best_registry_version(
                    package.get("versions").and_then(Value::as_array).map(Vec::as_slice).unwrap_or(&[]),
                    spec,
                );
                (Some(package), version)
            } else if spec.id.is_some() || spec.name.is_some() {
                let packages = self.search_registry_packages(spec)?;
                if packages.is_empty() {
                    return Err(unknown());
                }
                self.find_best_registry_version(&packages, spec)?
            } else {
                (None, None)
            };

        let (Some(package), Some(version)) = (package, version) else {
            return Err(unknown());
        };

        let files = version.get("files").and_then(Value::as_array).map(Vec::as_slice).unwrap_or(&[]);
        let Some(pkgfile) = pick_compatible_pkg_file(files) else {
            if self.pkg_type == PackageType::TOOL {
                return Err(PackageError::IncompatiblePackage {
                    spec: spec.humanize(),
                    system: get_systype(),
                });
            }
            return Err(unknown());
        };

        let download_url = pkgfile.get("download_url").and_then(Value::as_str).unwrap_or("");
        let fallback_sha =
            pkgfile.get("checksum").and_then(|c| c.get("sha256")).and_then(Value::as_str).map(str::to_string);
        let install_spec = PackageSpec::builder()
            .owner(package.get("owner").and_then(|o| o.get("username")).and_then(Value::as_str).unwrap_or(""))
            .name(package.get("name").and_then(Value::as_str).unwrap_or(""))
            .id(package.get("id").and_then(Value::as_i64).unwrap_or(0))
            .build()?;

        let mut mirrors = RegistryFileMirrorIterator::new(download_url);
        while let Some((url, sha)) = mirrors.next_mirror()? {
            let checksum = sha.filter(|s| !s.is_empty()).or_else(|| fallback_sha.clone());
            match self.install_from_uri(&url, &install_spec, checksum.as_deref()) {
                Ok(pkg) => return Ok(Some(pkg)),
                // Try the next mirror on any per-mirror failure.
                Err(_) => continue,
            }
        }
        Ok(None)
    }

    fn fetch_registry_package(&self, spec: &PackageSpec) -> Result<Value> {
        let client = RegistryClient::new();
        let mut result = None;
        if let (Some(owner), Some(name)) = (&spec.owner, &spec.name) {
            result = client.get_package(self.pkg_type, owner, name, None)?;
        }
        if result.is_none() && (spec.id.is_some() || (spec.name.is_some() && spec.owner.is_none())) {
            let packages = self.search_registry_packages(spec)?;
            if let Some(first) = packages.first() {
                let owner =
                    first.get("owner").and_then(|o| o.get("username")).and_then(Value::as_str).unwrap_or("");
                let name = first.get("name").and_then(Value::as_str).unwrap_or("");
                result = client.get_package(self.pkg_type, owner, name, None)?;
            }
        }
        result.ok_or_else(|| PackageError::UnknownPackage { spec: spec.humanize() })
    }

    fn search_registry_packages(&self, spec: &PackageSpec) -> Result<Vec<Value>> {
        let owner_lc = spec.owner.as_ref().map(|o| o.to_lowercase());
        let name_lc = spec.name.as_ref().map(|n| n.to_lowercase());
        let mut qualifiers: Vec<(&str, String)> = Vec::new();
        if let Some(id) = spec.id {
            qualifiers.push(("ids", id.to_string()));
        } else {
            qualifiers.push(("types", self.pkg_type.to_string()));
            if let Some(name) = &name_lc {
                qualifiers.push(("names", name.clone()));
            }
            if let Some(owner) = &owner_lc {
                qualifiers.push(("owners", owner.clone()));
            }
        }
        let result = RegistryClient::new().list_packages(&qualifiers, None)?;
        Ok(result.get("items").and_then(Value::as_array).cloned().unwrap_or_default())
    }

    fn find_best_registry_version(
        &self,
        packages: &[Value],
        spec: &PackageSpec,
    ) -> Result<(Option<Value>, Option<Value>)> {
        for package in packages {
            // Latest version first.
            if let Some(latest) = package.get("version") {
                if let Some(version) = pick_best_registry_version(std::slice::from_ref(latest), spec) {
                    return Ok((Some(package.clone()), Some(version)));
                }
            }
            // Otherwise scan all versions of the package.
            let id = package.get("id").and_then(Value::as_i64);
            let owner =
                package.get("owner").and_then(|o| o.get("username")).and_then(Value::as_str).unwrap_or("");
            let name = package.get("name").and_then(Value::as_str).unwrap_or("");
            let mut b = PackageSpec::builder().name(name).owner(owner);
            if let Some(id) = id {
                b = b.id(id);
            }
            let full = self.fetch_registry_package(&b.build()?)?;
            let versions =
                full.get("versions").and_then(Value::as_array).map(Vec::as_slice).unwrap_or(&[]);
            if let Some(version) = pick_best_registry_version(versions, spec) {
                return Ok((Some(package.clone()), Some(version)));
            }
        }
        Ok((None, None))
    }

    // -- uninstall -----------------------------------------------------------

    /// `PackageManagerUninstallMixin.uninstall`.
    pub fn uninstall(&self, spec: &PackageSpec) -> Result<PackageItem> {
        // Upstream keeps the "already uninstalled" set in the memory cache, which
        // is reset each `_uninstall`; we clear it per top-level call so a path
        // reused after a detach-fixup isn't wrongly skipped.
        self.uninstalled.borrow_mut().clear();
        let pkg = self
            .get_package(spec)?
            .ok_or_else(|| PackageError::UnknownPackage { spec: spec.humanize() })?;
        self.uninstall_pkg(&pkg)
    }

    fn uninstall_pkg(&self, pkg: &PackageItem) -> Result<PackageItem> {
        if self.uninstalled.borrow().contains(&pkg.path) {
            return Ok(pkg.clone());
        }
        self.uninstalled.borrow_mut().push(pkg.path.clone());

        // call_pkg_script(preuninstall) — no-op
        // uninstall dependencies
        if let Some(deps) = self.get_pkg_dependencies(pkg) {
            for dep in &deps {
                if let Ok(spec) = Self::dependency_to_spec(dep) {
                    if let Ok(Some(dep_pkg)) = self.get_package(&spec) {
                        let _ = self.uninstall_pkg(&dep_pkg);
                    }
                }
            }
        }

        remove_dir_if_exists(&pkg.path)?;

        // un-fix a detached package with the same name
        let meta_name = pkg.metadata.as_ref().map(|m| m.name.clone()).unwrap_or_default();
        let name_spec = PackageSpec::builder().name(&meta_name).build()?;
        if let Some(detached) = self.get_package(&name_spec)? {
            let detached_is_versioned = detached.path.to_string_lossy().contains('@');
            let safe = detached.get_safe_dirname().unwrap_or_default();
            let canonical = self.package_dir.join(&safe);
            if detached_is_versioned && !canonical.is_dir() {
                copy_dir(&detached.path, &canonical)?;
                remove_dir_if_exists(&detached.path)?;
            }
        }
        Ok(pkg.clone())
    }
}

/// A registry version file's `system` list (empty when unspecified → universal).
fn value_system(v: &Value) -> Vec<String> {
    v.get("system")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect())
        .unwrap_or_default()
}

/// `PackageManagerRegistryMixin.get_compatible_registry_versions` — filter by the
/// spec's requirements and per-file system compatibility.
fn compatible_registry_versions(versions: &[Value], spec: &PackageSpec) -> Vec<Value> {
    versions
        .iter()
        .filter(|version| {
            let name = version.get("name").and_then(Value::as_str).unwrap_or("");
            let Ok(semver) = cast_version_to_semver_default(name) else {
                return false;
            };
            if let Some(req) = spec.requirements() {
                if !req.contains(&semver) {
                    return false;
                }
            }
            let files = version.get("files").and_then(Value::as_array).map(Vec::as_slice).unwrap_or(&[]);
            files.iter().any(|f| PackageManager::is_system_compatible(&value_system(f)))
        })
        .cloned()
        .collect()
}

/// `PackageManagerRegistryMixin.pick_best_registry_version` — highest compatible.
fn pick_best_registry_version(versions: &[Value], spec: &PackageSpec) -> Option<Value> {
    let mut best: Option<(Value, crate::package::version::Version)> = None;
    for version in compatible_registry_versions(versions, spec) {
        let name = version.get("name").and_then(Value::as_str).unwrap_or("");
        let Ok(semver) = cast_version_to_semver_default(name) else { continue };
        let better = match &best {
            None => true,
            Some((_, bv)) => semver.precedence_cmp(bv, false) == std::cmp::Ordering::Greater,
        };
        if better {
            best = Some((version, semver));
        }
    }
    best.map(|(v, _)| v)
}

/// `PackageManagerRegistryMixin.pick_compatible_pkg_file`.
fn pick_compatible_pkg_file(version_files: &[Value]) -> Option<&Value> {
    version_files.iter().find(|item| PackageManager::is_system_compatible(&value_system(item)))
}

/// A canonical string key for the circular-install history.
fn spec_key(spec: &PackageSpec) -> String {
    format!(
        "{:?}|{:?}|{:?}|{:?}|{:?}",
        spec.owner,
        spec.id,
        spec.name,
        spec.requirements().map(ToString::to_string),
        spec.uri,
    )
}

/// `_install_tmp_pkg` detach directory name: `name@version` (or `name@src-<md5>`
/// for external URIs).
fn detach_dirname(safe: &str, meta: &PackageMetadata) -> String {
    let external_uri = meta.spec.as_ref().and_then(|s| s.uri.as_ref());
    if let Some(uri) = external_uri {
        let mut hasher = <md5::Md5 as md5::Digest>::new();
        md5::Digest::update(&mut hasher, uri.as_bytes());
        let digest = md5::Digest::finalize(hasher);
        let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
        format!("{safe}@src-{hex}")
    } else {
        let version = meta.version.as_ref().map(ToString::to_string).unwrap_or_default();
        format!("{safe}@{version}")
    }
}

fn remove_dir_if_exists(path: &Path) -> Result<()> {
    if path.is_dir() {
        fs::remove_dir_all(path).map_err(|e| PackageError::Package { message: e.to_string() })?;
    }
    Ok(())
}

/// Recursive directory copy (`shutil.copytree`); symlinks are followed.
fn copy_dir(src: &Path, dst: &Path) -> Result<()> {
    fs::create_dir_all(dst).map_err(|e| PackageError::Package { message: e.to_string() })?;
    for entry in fs::read_dir(src).map_err(|e| PackageError::Package { message: e.to_string() })? {
        let entry = entry.map_err(|e| PackageError::Package { message: e.to_string() })?;
        let from = entry.path();
        let to = dst.join(entry.file_name());
        if from.is_dir() {
            copy_dir(&from, &to)?;
        } else {
            fs::copy(&from, &to).map_err(|e| PackageError::Package { message: e.to_string() })?;
        }
    }
    Ok(())
}

/// DFS collecting directories top-down, like `os.walk` (root first).
fn collect_dirs_topdown(dir: &Path, out: &mut Vec<PathBuf>) {
    out.push(dir.to_path_buf());
    let Ok(entries) = fs::read_dir(dir) else { return };
    let mut subdirs: Vec<PathBuf> =
        entries.filter_map(std::result::Result::ok).map(|e| e.path()).filter(|p| p.is_dir()).collect();
    subdirs.sort();
    for sub in subdirs {
        collect_dirs_topdown(&sub, out);
    }
}

/// `os.walk`-style traversal yielding `(root, subdir_names, file_names)` top-down.
fn walk_dirs_files(dir: &Path) -> Vec<(PathBuf, Vec<String>, Vec<String>)> {
    let mut out = Vec::new();
    walk_dirs_files_into(dir, &mut out);
    out
}

fn walk_dirs_files_into(dir: &Path, out: &mut Vec<(PathBuf, Vec<String>, Vec<String>)>) {
    let Ok(entries) = fs::read_dir(dir) else { return };
    let mut dirs = Vec::new();
    let mut files = Vec::new();
    let mut subpaths = Vec::new();
    for e in entries.filter_map(std::result::Result::ok) {
        let path = e.path();
        let name = e.file_name().to_string_lossy().into_owned();
        if path.is_dir() {
            dirs.push(name);
            subpaths.push(path);
        } else {
            files.push(name);
        }
    }
    dirs.sort();
    files.sort();
    subpaths.sort();
    out.push((dir.to_path_buf(), dirs, files));
    for sub in subpaths {
        walk_dirs_files_into(&sub, out);
    }
}

#[cfg(test)]
mod tests;
