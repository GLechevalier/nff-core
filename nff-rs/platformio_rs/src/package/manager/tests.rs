//! Rust port of the offline (local) subset of `tests/package/test_manager.py`.
//!
//! The networked tests (`test_download`, `test_install_from_registry`,
//! `test_install_lib_depndencies`, `test_install_force`, `test_registry`,
//! `test_update_*`) and `test_scripts` (Python script execution) / `test_symlink`
//! (symlink packages) are out of this milestone's scope.

use std::path::{Path, PathBuf};

use crate::package::manager::{get_systype, PackageManager};
use crate::package::meta::PackageSpec;
use crate::package::pack::PackagePacker;

/// Points the manager's cache (scratch `downloads`/`tmp`) at a fresh temp dir for
/// the duration of a test — a per-thread override, cleared on drop, so it neither
/// touches the global environment nor races other modules' tests.
struct CacheGuard {
    _tmp: tempfile::TempDir,
}

impl CacheGuard {
    fn new() -> Self {
        let tmp = tempfile::tempdir().unwrap();
        crate::package::manager::test_cache::set(Some(tmp.path().to_path_buf()));
        Self { _tmp: tmp }
    }
}

impl Drop for CacheGuard {
    fn drop(&mut self) {
        crate::package::manager::test_cache::set(None);
    }
}

fn spec(s: &str) -> PackageSpec {
    PackageSpec::parse(s).unwrap()
}

fn file_uri(p: &Path) -> String {
    format!("file://{}", p.display())
}

fn write(path: &Path, contents: &str) {
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, contents).unwrap();
}

fn real(p: &Path) -> PathBuf {
    std::fs::canonicalize(p).unwrap()
}

fn basenames(pkgs: &[crate::package::meta::PackageItem]) -> Vec<String> {
    let mut v: Vec<String> =
        pkgs.iter().map(|p| p.path.file_name().unwrap().to_string_lossy().into_owned()).collect();
    v.sort();
    v
}

#[test]
fn test_find_pkg_root() {
    // has manifest
    let tmp = tempfile::tempdir().unwrap();
    let root_dir = tmp.path().join("nested/folder");
    write(&root_dir.join("platform.json"), "");
    let pm = PackageManager::platform(Some(tmp.path().join("platforms")));
    let found = pm.find_pkg_root(tmp.path(), None).unwrap();
    assert_eq!(real(&root_dir), real(&found));

    // does not have manifest
    let tmp = tempfile::tempdir().unwrap();
    write(&tmp.path().join("nested/folder/readme.txt"), "");
    let pm = PackageManager::platform(None);
    assert!(pm.find_pkg_root(tmp.path(), None).is_err());

    // library package without manifest → find source root
    let tmp = tempfile::tempdir().unwrap();
    let root_dir = tmp.path().join("nested/folder");
    write(&root_dir.join("src/main.cpp"), "");
    write(&root_dir.join("include/main.h"), "");
    assert_eq!(real(&root_dir), real(&PackageManager::find_library_root(tmp.path())));

    // library manager should create library.json
    let lm = PackageManager::library(Some(tmp.path().join("storage")));
    let pkg_root = lm.find_pkg_root(tmp.path(), Some(&spec("custom-name@1.0.0"))).unwrap();
    assert_eq!(real(&root_dir), real(&pkg_root));
    assert!(pkg_root.join("library.json").is_file());
    let manifest = lm.load_manifest(&pkg_root).unwrap();
    assert_eq!(manifest["name"], serde_json::json!("custom-name"));
    assert!(manifest["version"].as_str().unwrap().contains("0.0.0"));
}

#[test]
fn test_build_legacy_spec() {
    let storage = tempfile::tempdir().unwrap();
    let pm = PackageManager::platform(Some(storage.path().to_path_buf()));

    // src manifest
    let pkg1 = storage.path().join("pkg-1");
    write(
        &pkg1.join(".pio/.piopkgmanager.json"),
        r#"{"name": "StreamSpy-0.0.1.tar", "url": "https://dl.platformio.org/e8936b7/StreamSpy-0.0.1.tar.gz", "requirements": null}"#,
    );
    assert_eq!(
        pm.build_legacy_spec(&pkg1).unwrap(),
        PackageSpec::builder()
            .name("StreamSpy-0.0.1.tar")
            .uri("https://dl.platformio.org/e8936b7/StreamSpy-0.0.1.tar.gz")
            .build()
            .unwrap()
    );

    // without src manifest
    let pkg2 = storage.path().join("pkg-2");
    write(&pkg2.join("main.cpp"), "");
    assert!(pm.build_legacy_spec(&pkg2).is_err());

    // with package manifest
    let pkg3 = storage.path().join("pkg-3");
    write(&pkg3.join("platform.json"), r#"{"name": "pkg3", "version": "1.2.0"}"#);
    assert_eq!(pm.build_legacy_spec(&pkg3).unwrap(), PackageSpec::builder().name("pkg3").build().unwrap());
}

#[test]
fn test_build_metadata() {
    let pm = PackageManager::platform(None);
    let vcs_revision = "a2ebfd7c0f";
    let tmp = tempfile::tempdir().unwrap();
    let pkg_dir = tmp.path();

    // without manifest
    assert!(pm.load_manifest(pkg_dir).is_err());
    assert!(pm.build_metadata(pkg_dir, &spec("MyLib"), None).is_err());

    // with manifest
    write(&pkg_dir.join("platform.json"), r#"{"name": "Dev-Platform", "version": "1.2.3-alpha.1"}"#);
    let metadata = pm.build_metadata(pkg_dir, &spec("owner/platform-name"), None).unwrap();
    assert_eq!(metadata.name, "Dev-Platform");
    assert_eq!(metadata.version.as_ref().unwrap().to_string(), "1.2.3-alpha.1");

    // with vcs revision
    let metadata = pm.build_metadata(pkg_dir, &spec("owner/platform-name"), Some(vcs_revision)).unwrap();
    let version = metadata.version.unwrap();
    assert_eq!(version.to_string(), format!("1.2.3-alpha.1+sha.{vcs_revision}"));
    assert_eq!(version.build[1], vcs_revision);
}

#[test]
fn test_get_installed() {
    let storage = tempfile::tempdir().unwrap();
    let s = storage.path();
    let pm = PackageManager::tool(Some(s.to_path_buf()));

    // VCS package (metadata in .git/.piopm, legacy "url")
    write(
        &s.join("pkg-vcs/.git/.piopm"),
        r#"{"name": "pkg-via-vcs", "spec": {"id": null, "name": "pkg-via-vcs", "owner": null, "requirements": null, "url": "git+https://github.com/username/repo.git"}, "type": "tool", "version": "0.0.0+sha.1ea4d5e"}"#,
    );
    // package without metadata file
    write(&s.join("foo@3.4.5/package.json"), r#"{"name": "foo", "version": "3.4.5"}"#);
    // package with metadata file
    write(&s.join("foo/package.json"), r#"{"name": "foo", "version": "3.6.0"}"#);
    write(
        &s.join("foo/.piopm"),
        r#"{"name": "foo", "spec": {"name": "foo", "owner": null, "requirements": "^3"}, "type": "tool", "version": "3.6.0"}"#,
    );
    // system compat
    write(&s.join("pkg-incompatible-system/package.json"), r#"{"name": "check-system", "version": "4.0.0", "system": ["unknown"]}"#);
    write(
        &s.join("pkg-compatible-system/package.json"),
        &format!(r#"{{"name": "check-system", "version": "3.0.0", "system": "{}"}}"#, get_systype()),
    );
    // invalid package (library.json is not a tool manifest)
    write(&s.join("invalid-package/library.json"), r#"{"name": "SomeLib", "version": "4.0.0"}"#);

    let installed = pm.get_installed().unwrap();
    assert_eq!(installed.len(), 4);
    let names: std::collections::BTreeSet<String> =
        installed.iter().map(|p| p.metadata.as_ref().unwrap().name.clone()).collect();
    assert_eq!(names, ["check-system", "foo", "pkg-via-vcs"].iter().map(ToString::to_string).collect());
    assert_eq!(pm.get_package(&spec("foo")).unwrap().unwrap().metadata.unwrap().version.unwrap().to_string(), "3.6.0");
    assert_eq!(
        pm.get_package(&spec("check-system")).unwrap().unwrap().metadata.unwrap().version.unwrap().to_string(),
        "3.0.0"
    );
}

#[test]
fn test_install_from_uri() {
    let _cache = CacheGuard::new();

    let tmp = tempfile::tempdir().unwrap();
    let storage = tempfile::tempdir().unwrap();
    let lm = PackageManager::library(Some(storage.path().to_path_buf()));

    // install from local directory
    let src_dir = tmp.path().join("local-lib-dir");
    write(&src_dir.join("main.cpp"), "");
    let s = spec(&file_uri(&src_dir));
    let pkg = lm.install(&s).unwrap();
    assert!(pkg.path.join("main.cpp").is_file());
    let manifest = lm.load_manifest(&pkg.path).unwrap();
    assert_eq!(manifest["name"], serde_json::json!("local-lib-dir"));
    assert!(manifest["version"].as_str().unwrap().starts_with("0.0.0+"));
    assert_eq!(Some(&s), pkg.metadata.as_ref().unwrap().spec.as_ref());

    // install from local archive
    let arc_src = tmp.path().join("archive-src");
    write(&arc_src.join("root/src/main.cpp"), "#include <stdio.h>");
    write(&arc_src.join("root/library.json"), r#"{"name": "manifest-lib-name", "version": "2.0.0"}"#);
    let tarball = PackagePacker::new(arc_src.clone(), None).pack(Some(tmp.path())).unwrap();
    let s = spec(&file_uri(&tarball));
    let pkg = lm.install(&s).unwrap();
    assert!(pkg.path.join("src/main.cpp").is_file());
    assert_eq!(Some(pkg.clone()), lm.get_package(&s).unwrap());
    assert_eq!(Some(&s), pkg.metadata.as_ref().unwrap().spec.as_ref());

    // install from a library.properties dir with an owner/req spec
    let reg_src = tmp.path().join("registry-1");
    write(&reg_src.join("library.properties"), "\nname = wifilib\nversion = 5.2.7\n");
    let pkg = lm.install_from_uri(&file_uri(&reg_src), &spec("company/wifilib @ ^5"), None).unwrap();
    assert_eq!(pkg.metadata.unwrap().version.unwrap().to_string(), "5.2.7");

    // folder names
    assert_eq!(basenames(&lm.get_installed().unwrap()), ["local-lib-dir", "manifest-lib-name", "wifilib"]);

}

#[test]
fn test_uninstall() {
    let _cache = CacheGuard::new();

    let tmp = tempfile::tempdir().unwrap();
    let storage = tempfile::tempdir().unwrap();
    let s = storage.path();
    let lm = PackageManager::library(Some(s.to_path_buf()));

    // foo @ 1.0.0
    let foo1 = tmp.path().join("foo");
    write(&foo1.join("library.json"), r#"{"name": "foo", "version": "1.0.0"}"#);
    let foo_1_0_0 = lm.install_from_uri(&file_uri(&foo1), &spec("foo"), None).unwrap();
    // foo @ 1.3.0
    let foo13 = tmp.path().join("foo-1.3.0");
    write(&foo13.join("library.json"), r#"{"name": "foo", "version": "1.3.0"}"#);
    lm.install_from_uri(&file_uri(&foo13), &spec("foo"), None).unwrap();
    // bar
    let bar = tmp.path().join("bar");
    write(&bar.join("library.json"), r#"{"name": "bar", "version": "1.0.0"}"#);
    let bar_pkg = lm.install(&spec(&file_uri(&bar))).unwrap();

    assert_eq!(lm.get_installed().unwrap().len(), 3);
    assert!(s.join("foo").is_dir());
    assert!(s.join("foo@1.0.0").is_dir());

    // detach on uninstall of the highest version
    lm.uninstall(&spec("FOO")).unwrap();
    assert_eq!(lm.get_installed().unwrap().len(), 2);
    assert!(s.join("foo").is_dir());
    assert!(!s.join("foo@1.0.0").is_dir());

    // uninstall the rest (by path and by item)
    lm.uninstall(&spec(&foo_1_0_0.path.to_string_lossy())).unwrap();
    lm.uninstall(&spec(&bar_pkg.path.to_string_lossy())).unwrap();
    assert!(lm.get_installed().unwrap().is_empty());

}

/// Live smoke test of the networked registry/http/download layer (the only way
/// to exercise it — `pio-rs` has no `pkg` CLI surface for the parity harness).
/// Run with `cargo test -p platformio_rs -- --ignored registry_smoke`.
#[test]
#[ignore = "requires network access to the PlatformIO registry"]
fn registry_smoke_install() {
    let _cache = CacheGuard::new();
    let storage = tempfile::tempdir().unwrap();
    let lm = PackageManager::library(Some(storage.path().to_path_buf()));
    let pkg = lm.install(&spec("OneWire")).unwrap();
    assert_eq!(pkg.metadata.as_ref().unwrap().name.to_lowercase(), "onewire");
    assert!(!lm.get_installed().unwrap().is_empty());
}

#[test]
fn test_install_circular_dependencies() {
    let _cache = CacheGuard::new();

    let tmp = tempfile::tempdir().unwrap();
    let storage = tmp.path().join("storage");
    write(&storage.join("foo/library.json"), r#"{"name": "Foo", "version": "1.0.0", "dependencies": {"Bar": "*"}}"#);
    write(&storage.join("bar/library.json"), r#"{"name": "Bar", "version": "1.0.0", "dependencies": {"Foo": "*"}}"#);

    let lm = PackageManager::library(Some(storage.clone()));
    assert_eq!(lm.get_installed().unwrap().len(), 2);

    // root library depending on both (must terminate despite the cycle)
    let root = tmp.path().join("root");
    write(&root.join("library.json"), r#"{"name": "Root", "version": "1.0.0", "dependencies": {"Foo": "^1.0.0", "Bar": "^1.0.0"}}"#);
    lm.install(&spec(&file_uri(&root))).unwrap();

}
