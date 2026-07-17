//! Rust port of `tests/package/test_meta.py` (the M2a parity gate).
//!
//! Every upstream test function is mirrored here 1:1 by name. These are the
//! internal-API tests that never drive `pio-rs` through the parity shim, so — as
//! with `src/config/tests.rs` and `test_config.py` — they are reimplemented as
//! Rust unit tests against the ported types.

use serde_json::json;

use crate::package::meta::{
    PackageCompatibility, PackageMetadata, PackageOutdatedResult, PackageSpec, PackageType,
    Qualifier,
};
use crate::package::version::{SimpleSpec, Version};

/// `PackageSpec("<raw>")`.
fn sp(raw: &str) -> PackageSpec {
    PackageSpec::parse(raw).expect("spec should parse")
}

#[test]
fn test_outdated_result() {
    let result = PackageOutdatedResult::new("1.2.3", Some("2.0.0"), None, false).unwrap();
    assert!(result.is_outdated(false));
    assert!(result.is_outdated(true));

    let result = PackageOutdatedResult::new("1.2.3", Some("2.0.0"), Some("1.5.4"), false).unwrap();
    assert!(result.is_outdated(false));
    assert!(result.is_outdated(true));

    let result = PackageOutdatedResult::new("1.2.3", Some("2.0.0"), Some("1.2.3"), false).unwrap();
    assert!(!result.is_outdated(false));
    assert!(result.is_outdated(true));

    let result = PackageOutdatedResult::new("1.2.3", Some("2.0.0"), None, true).unwrap();
    assert!(!result.is_outdated(false));
    assert!(!result.is_outdated(true));
}

#[test]
fn test_spec_owner() {
    assert_eq!(sp("alice/foo library"), PackageSpec::builder().owner("alice").name("foo library").build().unwrap());
    let spec = sp(" Bob / BarUpper ");
    assert_ne!(spec, PackageSpec::builder().owner("BOB").name("BARUPPER").build().unwrap());
    assert_eq!(spec.owner.as_deref(), Some("Bob"));
    assert_eq!(spec.name.as_deref(), Some("BarUpper"));
}

#[test]
fn test_spec_id() {
    assert_eq!(PackageSpec::from_id_literal(13).unwrap(), PackageSpec::builder().id(13).build().unwrap());
    assert_eq!(sp("20"), PackageSpec::builder().id(20).build().unwrap());
    let spec = sp("id=199");
    assert_eq!(spec, PackageSpec::builder().id(199).build().unwrap());
    assert_eq!(spec.id, Some(199)); // isinstance(spec.id, int)
}

#[test]
fn test_spec_name() {
    assert_eq!(sp("foo"), PackageSpec::builder().name("foo").build().unwrap());
    assert_eq!(sp(" bar-24 "), PackageSpec::builder().name("bar-24").build().unwrap());
}

#[test]
fn test_spec_requirements() {
    assert_eq!(sp("foo@1.2.3"), PackageSpec::builder().name("foo").requirements("1.2.3").build().unwrap());
    assert_eq!(
        PackageSpec::builder().name("foo").requirements_version(Version::parse("1.2.3").unwrap()).build().unwrap(),
        PackageSpec::builder().name("foo").requirements("1.2.3").build().unwrap()
    );
    assert_eq!(sp("bar @ ^1.2.3"), PackageSpec::builder().name("bar").requirements("^1.2.3").build().unwrap());
    assert_eq!(sp("13 @ ~2.0"), PackageSpec::builder().id(13).requirements("~2.0").build().unwrap());
    assert_eq!(
        PackageSpec::builder().name("hello").requirements_spec(SimpleSpec::parse("~1.2.3").unwrap()).build().unwrap(),
        PackageSpec::builder().name("hello").requirements("~1.2.3").build().unwrap()
    );
    let spec = sp("id=20 @ !=1.2.3,<2.0");
    assert!(!spec.external());
    let req = spec.requirements().expect("requirements set");
    assert!(req.contains(&Version::parse("1.3.0-beta.1").unwrap()));
    assert_eq!(spec, PackageSpec::builder().id(20).requirements("!=1.2.3,<2.0").build().unwrap());
}

#[test]
fn test_spec_local_urls() {
    assert_eq!(
        sp("file:///tmp/foo.tar.gz"),
        PackageSpec::builder().uri("file:///tmp/foo.tar.gz").name("foo").build().unwrap()
    );
    assert_eq!(
        sp("customName=file:///tmp/bar.zip"),
        PackageSpec::builder().uri("file:///tmp/bar.zip").name("customName").build().unwrap()
    );
    assert_eq!(
        sp("file:///tmp/some-lib/"),
        PackageSpec::builder().uri("file:///tmp/some-lib/").name("some-lib").build().unwrap()
    );
    assert_eq!(
        sp("symlink:///tmp/soft-link/"),
        PackageSpec::builder().uri("symlink:///tmp/soft-link/").name("soft-link").build().unwrap()
    );
    // detached package
    assert_eq!(
        sp("file:///tmp/some-lib@src-67e1043a673d2"),
        PackageSpec::builder().uri("file:///tmp/some-lib@src-67e1043a673d2").name("some-lib").build().unwrap()
    );
    // detached folder without scheme (must exist on disk to be treated as file://)
    let tmp = tempfile::tempdir().unwrap();
    let pkg_dir = tmp.path().join("detached@1.2.3");
    std::fs::create_dir(&pkg_dir).unwrap();
    let pkg_dir_str = pkg_dir.to_str().unwrap();
    assert_eq!(
        sp(pkg_dir_str),
        PackageSpec::builder().name("detached").uri(format!("file://{pkg_dir_str}")).build().unwrap()
    );
}

#[test]
fn test_spec_external_urls() {
    assert_eq!(
        sp("https://github.com/platformio/platformio-core/archive/develop.zip"),
        PackageSpec::builder()
            .uri("https://github.com/platformio/platformio-core/archive/develop.zip")
            .name("platformio-core")
            .build()
            .unwrap()
    );
    assert_eq!(
        sp("https://github.com/platformio/platformio-core/archive/develop.zip?param=value @ !=2"),
        PackageSpec::builder()
            .uri("https://github.com/platformio/platformio-core/archive/develop.zip?param=value")
            .name("platformio-core")
            .requirements("!=2")
            .build()
            .unwrap()
    );
    let spec =
        sp("Custom-Name=https://github.com/platformio/platformio-core/archive/develop.tar.gz@4.4.0");
    assert!(spec.external());
    assert!(spec.has_custom_name());
    assert_eq!(spec.name.as_deref(), Some("Custom-Name"));
    assert_eq!(
        spec,
        PackageSpec::builder()
            .uri("https://github.com/platformio/platformio-core/archive/develop.tar.gz")
            .name("Custom-Name")
            .requirements("4.4.0")
            .build()
            .unwrap()
    );
}

#[test]
fn test_spec_vcs_urls() {
    assert_eq!(
        sp("https://github.com/platformio/platformio-core"),
        PackageSpec::builder()
            .name("platformio-core")
            .uri("git+https://github.com/platformio/platformio-core")
            .build()
            .unwrap()
    );
    assert_eq!(
        sp("https://gitlab.com/username/reponame"),
        PackageSpec::builder().name("reponame").uri("git+https://gitlab.com/username/reponame").build().unwrap()
    );
    assert_eq!(
        sp("wolfSSL=https://os.mbed.com/users/wolfSSL/code/wolfSSL/"),
        PackageSpec::builder().name("wolfSSL").uri("hg+https://os.mbed.com/users/wolfSSL/code/wolfSSL/").build().unwrap()
    );
    assert_eq!(
        sp("https://github.com/platformio/platformio-core.git#master"),
        PackageSpec::builder()
            .name("platformio-core")
            .uri("git+https://github.com/platformio/platformio-core.git#master")
            .build()
            .unwrap()
    );
    assert_eq!(
        sp("core=git+ssh://github.com/platformio/platformio-core.git#v4.4.0@4.4.0"),
        PackageSpec::builder()
            .name("core")
            .uri("git+ssh://github.com/platformio/platformio-core.git#v4.4.0")
            .requirements("4.4.0")
            .build()
            .unwrap()
    );
    assert_eq!(
        sp("username@github.com:platformio/platformio-core.git"),
        PackageSpec::builder()
            .name("platformio-core")
            .uri("git+username@github.com:platformio/platformio-core.git")
            .build()
            .unwrap()
    );
    assert_eq!(
        sp("pkg=git+git@github.com:platformio/platformio-core.git @ ^1.2.3,!=5"),
        PackageSpec::builder()
            .name("pkg")
            .uri("git+git@github.com:platformio/platformio-core.git")
            .requirements("^1.2.3,!=5")
            .build()
            .unwrap()
    );
    // requirements that fail to parse as semver are rewritten to name=requirements
    assert_eq!(
        PackageSpec::builder()
            .owner("platformio")
            .name("external-repo")
            .requirements("https://github.com/platformio/platformio-core")
            .build()
            .unwrap(),
        PackageSpec::builder()
            .owner("platformio")
            .name("external-repo")
            .uri("git+https://github.com/platformio/platformio-core")
            .build()
            .unwrap()
    );
}

#[test]
fn test_spec_as_dict() {
    assert_eq!(
        sp("bob/foo@1.2.3").as_dict(),
        json!({"owner": "bob", "id": null, "name": "foo", "requirements": "1.2.3", "uri": null})
    );
    assert_eq!(
        sp("https://github.com/platformio/platformio-core/archive/develop.zip?param=value @ !=2").as_dict(),
        json!({
            "owner": null,
            "id": null,
            "name": "platformio-core",
            "requirements": "!=2",
            "uri": "https://github.com/platformio/platformio-core/archive/develop.zip?param=value",
        })
    );
}

#[test]
fn test_spec_as_dependency() {
    assert_eq!(sp("owner/pkgname").as_dependency(), "owner/pkgname");
    assert_eq!(
        PackageSpec::builder().owner("owner").name("pkgname").build().unwrap().as_dependency(),
        "owner/pkgname"
    );
    assert_eq!(sp("bob/foo @ ^1.2.3").as_dependency(), "bob/foo@^1.2.3");
    assert_eq!(
        sp("https://github.com/o/r/a/develop.zip?param=value @ !=2").as_dependency(),
        "https://github.com/o/r/a/develop.zip?param=value @ !=2"
    );
    assert_eq!(
        sp("wolfSSL=https://os.mbed.com/users/wolfSSL/code/wolfSSL/").as_dependency(),
        "wolfSSL=https://os.mbed.com/users/wolfSSL/code/wolfSSL/"
    );
}

#[test]
fn test_metadata_as_dict() {
    let mut metadata = PackageMetadata::new(PackageType::LIBRARY, "foo", "1.2.3", None).unwrap();
    // test setter
    metadata.set_version("0.1.2+12345").unwrap();
    assert_eq!(metadata.version, Some(Version::parse("0.1.2+12345").unwrap()));
    assert_eq!(
        metadata.as_dict(),
        json!({"type": PackageType::LIBRARY, "name": "foo", "version": "0.1.2+12345", "spec": null})
    );

    let metadata = PackageMetadata::new(
        PackageType::TOOL,
        "toolchain",
        "2.0.5",
        Some(sp("platformio/toolchain@~2.0.0")),
    )
    .unwrap();
    assert_eq!(
        metadata.as_dict(),
        json!({
            "type": PackageType::TOOL,
            "name": "toolchain",
            "version": "2.0.5",
            "spec": {
                "owner": "platformio",
                "id": null,
                "name": "toolchain",
                "requirements": "~2.0.0",
                "uri": null,
            },
        })
    );
}

#[test]
fn test_metadata_dump() {
    let pkg_dir = tempfile::tempdir().unwrap();
    let metadata = PackageMetadata::new(
        PackageType::TOOL,
        "toolchain",
        "2.0.5",
        Some(sp("platformio/toolchain@~2.0.0")),
    )
    .unwrap();
    let dst = pkg_dir.path().join(".piopm");
    metadata.dump(&dst).unwrap();
    assert!(dst.is_file());
    let contents = std::fs::read_to_string(&dst).unwrap();
    assert!(contents.contains("null"));
    assert!(contents.contains("\"~2.0.0\""));
}

#[test]
fn test_metadata_load() {
    let contents = r#"
{
  "name": "foo",
  "spec": {
    "name": "foo",
    "owner": "username",
    "requirements": "!=3.4.5"
  },
  "type": "platform",
  "version": "0.1.3"
}
"#;
    let pkg_dir = tempfile::tempdir().unwrap();
    let dst = pkg_dir.path().join(".piopm");
    std::fs::write(&dst, contents).unwrap();
    let metadata = PackageMetadata::load(&dst).unwrap();
    assert_eq!(metadata.version, Some(Version::parse("0.1.3").unwrap()));
    assert_eq!(
        metadata,
        PackageMetadata::new(
            PackageType::PLATFORM,
            "foo",
            "0.1.3",
            Some(PackageSpec::builder().owner("username").name("foo").requirements("!=3.4.5").build().unwrap()),
        )
        .unwrap()
    );

    let piopm_path = pkg_dir.path().join(".piopm");
    let metadata =
        PackageMetadata::new(PackageType::LIBRARY, "mylib", "1.2.3", Some(sp("mylib"))).unwrap();
    metadata.dump(&piopm_path).unwrap();
    let restored_metadata = PackageMetadata::load(&piopm_path).unwrap();
    assert_eq!(metadata, restored_metadata);
}

#[test]
fn test_compatibility() {
    let pc = PackageCompatibility::new;

    assert!(pc().is_compatible(&pc()));
    assert!(pc().is_compatible(&pc().set("platforms", Qualifier::list(["espressif32"])).unwrap()));
    assert!(pc()
        .set("frameworks", Qualifier::list(["arduino"]))
        .unwrap()
        .is_compatible(&pc().set("platforms", Qualifier::list(["espressif32"])).unwrap()));
    assert!(pc()
        .set("platforms", Qualifier::str("espressif32"))
        .unwrap()
        .is_compatible(&pc().set("platforms", Qualifier::list(["espressif32"])).unwrap()));
    assert!(pc()
        .set("platforms", Qualifier::str("espressif32"))
        .unwrap()
        .set("frameworks", Qualifier::list(["arduino"]))
        .unwrap()
        .is_compatible(&pc().set("platforms", Qualifier::None).unwrap()));
    assert!(pc()
        .set("platforms", Qualifier::str("espressif32"))
        .unwrap()
        .set("frameworks", Qualifier::list(["arduino"]))
        .unwrap()
        .is_compatible(&pc().set("platforms", Qualifier::list(["*"])).unwrap()));
    assert!(!pc()
        .set("platforms", Qualifier::str("espressif32"))
        .unwrap()
        .set("frameworks", Qualifier::list(["arduino"]))
        .unwrap()
        .is_compatible(&pc().set("platforms", Qualifier::list(["atmelavr"])).unwrap()));
}
