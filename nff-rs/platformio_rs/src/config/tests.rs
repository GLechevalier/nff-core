//! Rust port of `tests/project/test_config.py` (the M1 parity gate).
//!
//! `test_config.py` pokes `ProjectConfig` internals directly (it never drives the
//! CLI), so it can only be reproduced as Rust unit tests. Every upstream test
//! function is mirrored here. Tests mutate process-global cwd / env vars, so they
//! are serialized by [`TEST_LOCK`].

use std::path::{Path, PathBuf, MAIN_SEPARATOR};
use std::sync::MutexGuard;

use super::{Defaulted, ProjectConfig, SetValue, Value};
use super::error::ConfigError;

// ---------------------------------------------------------------------------
// Fixtures (verbatim from test_config.py)
// ---------------------------------------------------------------------------

const BASE_CONFIG: &str = "
[platformio]
env_default = base, extra_2
src_dir = ${custom.src_dir}
extra_configs =
  extra_envs.ini
  extra_debug.ini

# global options per [env:*]
[env]
monitor_speed = 9600  ; inline comment
custom_monitor_speed = 115200
lib_deps =
    Lib1 ; inline comment in multi-line value
    Lib2
lib_ignore = ${custom.lib_ignore}
custom_builtin_option = ${env.build_type}

[strict_ldf]
lib_ldf_mode = chain+
lib_compat_mode = strict

[monitor_custom]
monitor_speed = ${env.custom_monitor_speed}

[strict_settings]
extends = strict_ldf, monitor_custom
build_flags = -D RELEASE

[custom]
src_dir = source
debug_flags = -D RELEASE
lib_flags = -lc -lm
extra_flags = ${sysenv.__PIO_TEST_CNF_EXTRA_FLAGS}
lib_ignore = LibIgnoreCustom

[env:base]
build_flags = ${custom.debug_flags} ${custom.extra_flags}
lib_compat_mode = ${strict_ldf.lib_compat_mode}
targets =

[env:test_extends]
extends = strict_settings

[env:inject_base_env]
debug_build_flags =
    ${env.debug_build_flags}
    -D CUSTOM_DEBUG_FLAG

";

const EXTRA_ENVS_CONFIG: &str = "
[env:extra_1]
build_flags =
    -fdata-sections
    -Wl,--gc-sections
    ${custom.lib_flags}
    ${custom.debug_flags}
    -D SERIAL_BAUD_RATE=${this.monitor_speed}
lib_install = 574

[env:extra_2]
build_flags = ${custom.debug_flags} ${custom.extra_flags}
lib_ignore = ${env.lib_ignore}, Lib3
upload_port = /dev/extra_2/port
debug_server = ${custom.debug_server}
";

const EXTRA_DEBUG_CONFIG: &str = "
# Override original \"custom.debug_flags\"
[custom]
debug_flags = -D DEBUG=1
debug_server =
    ${platformio.packages_dir}/tool-openocd/openocd
    --help
src_filter = -<*>
    +<a>
    +<b>

[env:extra_2]
build_flags = -Og
src_filter = ${custom.src_filter} +<c>
";

// ---------------------------------------------------------------------------
// Test harness
// ---------------------------------------------------------------------------

fn lock() -> MutexGuard<'static, ()> {
    // Shared with every other module's env/cwd-mutating tests.
    crate::test_lock::guard()
}

/// Restores the working directory on drop.
struct CwdGuard(PathBuf);

impl CwdGuard {
    fn enter(dir: &Path) -> Self {
        let orig = std::env::current_dir().expect("cwd");
        std::env::set_current_dir(dir).expect("set cwd");
        Self(orig)
    }
}

impl Drop for CwdGuard {
    fn drop(&mut self) {
        let _ = std::env::set_current_dir(&self.0);
    }
}

fn write(dir: &Path, name: &str, contents: &str) {
    std::fs::write(dir.join(name), contents).expect("write fixture");
}

fn ini_path(dir: &Path) -> String {
    dir.join("platformio.ini").to_string_lossy().into_owned()
}

/// Build the shared `config` fixture (the three-file project) under `dir`.
fn build_main_config(dir: &Path) -> ProjectConfig {
    write(dir, "platformio.ini", BASE_CONFIG);
    write(dir, "extra_envs.ini", EXTRA_ENVS_CONFIG);
    write(dir, "extra_debug.ini", EXTRA_DEBUG_CONFIG);
    ProjectConfig::new(&ini_path(dir)).expect("build config")
}

// Value constructors.
fn s(x: &str) -> Value {
    Value::Str(x.to_string())
}
fn l(xs: &[&str]) -> Value {
    Value::list_of_str(xs.iter().copied())
}

// Getter helpers.
fn g(cfg: &ProjectConfig, section: &str, option: &str) -> Value {
    cfg.get(section, option, Defaulted::Missing).expect("get")
}
fn gd(cfg: &ProjectConfig, section: &str, option: &str, default: Value) -> Value {
    cfg.get(section, option, Defaulted::Provided(default)).expect("get")
}
fn gr(cfg: &ProjectConfig, section: &str, option: &str) -> Value {
    cfg.getraw(section, option, Defaulted::Missing).expect("getraw")
}

fn default_core_dir() -> String {
    let home = dirs::home_dir().expect("home").to_string_lossy().into_owned();
    format!("{home}{MAIN_SEPARATOR}.platformio")
}
fn packages_dir() -> String {
    format!("{}{MAIN_SEPARATOR}packages", default_core_dir())
}

// ---------------------------------------------------------------------------
// Ported tests
// ---------------------------------------------------------------------------

#[test]
fn test_empty_config() {
    let _lock = lock();
    let cfg = ProjectConfig::new("/non/existing/platformio.ini").unwrap();
    // unknown section
    assert!(matches!(
        cfg.get("unknown_section", "unknown_option", Defaulted::Missing),
        Err(ConfigError::InvalidProjectConf { .. })
    ));
    assert_eq!(cfg.sections(), Vec::<String>::new());
    assert_eq!(gd(&cfg, "section", "option", Value::Int(13)), Value::Int(13));
}

#[test]
fn test_warnings() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    let cfg = build_main_config(tmp.path());

    cfg.validate(Some(&["extra_2".into(), "base".into()]), true).unwrap();
    assert_eq!(cfg.warnings().len(), 3);
    assert!(cfg.warnings()[1].contains("lib_install"));

    assert!(matches!(
        cfg.validate(Some(&["non-existing-env".into()]), false),
        Err(ConfigError::UnknownEnvNames { .. })
    ));
}

#[test]
fn test_defaults() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    let cfg = build_main_config(tmp.path());

    assert_eq!(g(&cfg, "platformio", "core_dir"), s(&default_core_dir()));
    assert_eq!(gd(&cfg, "strict_ldf", "lib_deps", l(&["Empty"])), l(&["Empty"]));
    assert_eq!(g(&cfg, "env:extra_2", "lib_compat_mode"), s("soft"));
    assert_eq!(g(&cfg, "env:extra_2", "build_type"), s("release"));
    assert_eq!(gd(&cfg, "env:extra_2", "build_type", Value::None), Value::None);
    assert_eq!(gd(&cfg, "env:extra_2", "lib_archive", s("no")), Value::Bool(false));

    cfg.set_expand_interpolations(false);
    let err = cfg
        .get("strict_ldf", "lib_deps", Defaulted::Provided(l(&["Empty"])))
        .unwrap_err();
    match err {
        ConfigError::InvalidProjectConf { detail, .. } => {
            assert!(detail.contains("No option 'lib_deps' in section: 'strict_ldf'"), "{detail}");
        }
        other => panic!("expected InvalidProjectConf, got {other:?}"),
    }
    cfg.set_expand_interpolations(true);
}

#[test]
fn test_sections() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    let cfg = build_main_config(tmp.path());

    assert!(matches!(
        cfg.getraw("unknown_section", "unknown_option", Defaulted::Missing),
        Err(ConfigError::NoSection { .. })
    ));
    assert_eq!(
        cfg.sections(),
        vec![
            "platformio",
            "env",
            "strict_ldf",
            "monitor_custom",
            "strict_settings",
            "custom",
            "env:base",
            "env:test_extends",
            "env:inject_base_env",
            "env:extra_1",
            "env:extra_2",
        ]
    );
}

#[test]
fn test_envs() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    let cfg = build_main_config(tmp.path());

    assert_eq!(cfg.envs(), vec!["base", "test_extends", "inject_base_env", "extra_1", "extra_2"]);
    assert_eq!(cfg.default_envs(), vec!["base", "extra_2"]);
    assert_eq!(cfg.get_default_env(), Some("base".to_string()));
}

#[test]
fn test_options() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    let cfg = build_main_config(tmp.path());

    assert_eq!(
        cfg.options_env("base"),
        vec![
            "build_flags",
            "lib_compat_mode",
            "targets",
            "monitor_speed",
            "custom_monitor_speed",
            "lib_deps",
            "lib_ignore",
            "custom_builtin_option",
        ]
    );
    assert_eq!(
        cfg.options_env("test_extends"),
        vec![
            "extends",
            "build_flags",
            "monitor_speed",
            "lib_ldf_mode",
            "lib_compat_mode",
            "custom_monitor_speed",
            "lib_deps",
            "lib_ignore",
            "custom_builtin_option",
        ]
    );
}

#[test]
fn test_has_option() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    let cfg = build_main_config(tmp.path());

    assert!(cfg.has_option("env:base", "monitor_speed"));
    assert!(!cfg.has_option("custom", "monitor_speed"));
    assert!(cfg.has_option("env:extra_1", "lib_install"));
    assert!(cfg.has_option("env:test_extends", "lib_compat_mode"));
    assert!(cfg.has_option("env:extra_2", "src_filter"));
}

#[test]
fn test_sysenv_options() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    let cfg = build_main_config(tmp.path());

    assert_eq!(gr(&cfg, "custom", "extra_flags"), s(""));
    assert_eq!(g(&cfg, "env:base", "build_flags"), l(&["-D DEBUG=1"]));
    assert_eq!(g(&cfg, "env:base", "upload_port"), Value::None);
    assert_eq!(g(&cfg, "env:extra_2", "upload_port"), s("/dev/extra_2/port"));

    std::env::set_var("PLATFORMIO_BUILD_FLAGS", "-DSYSENVDEPS1 -DSYSENVDEPS2");
    std::env::set_var("PLATFORMIO_BUILD_UNFLAGS", "-DREMOVE_MACRO");
    std::env::set_var("PLATFORMIO_UPLOAD_PORT", "/dev/sysenv/port");
    std::env::set_var("__PIO_TEST_CNF_EXTRA_FLAGS", "-L /usr/local/lib");

    assert_eq!(g(&cfg, "custom", "extra_flags"), s("-L /usr/local/lib"));
    assert_eq!(
        g(&cfg, "env:base", "build_flags"),
        l(&["-D DEBUG=1 -L /usr/local/lib", "-DSYSENVDEPS1 -DSYSENVDEPS2"])
    );
    assert_eq!(g(&cfg, "env:base", "upload_port"), s("/dev/sysenv/port"));
    assert_eq!(g(&cfg, "env:extra_2", "upload_port"), s("/dev/sysenv/port"));
    assert_eq!(g(&cfg, "env:base", "build_unflags"), l(&["-DREMOVE_MACRO"]));

    assert_eq!(
        cfg.options_env("test_extends"),
        vec![
            "extends",
            "build_flags",
            "monitor_speed",
            "lib_ldf_mode",
            "lib_compat_mode",
            "custom_monitor_speed",
            "lib_deps",
            "lib_ignore",
            "custom_builtin_option",
            "build_unflags",
            "upload_port",
        ]
    );

    let cwd = std::env::current_dir().unwrap();
    let custom_core_dir = cwd.join("custom-core").to_string_lossy().into_owned();
    let custom_src_dir = cwd.join("custom-src").to_string_lossy().into_owned();
    let custom_build_dir = cwd.join("custom-build").to_string_lossy().into_owned();
    std::env::set_var("PLATFORMIO_HOME_DIR", &custom_core_dir);
    std::env::set_var("PLATFORMIO_SRC_DIR", &custom_src_dir);
    std::env::set_var("PLATFORMIO_BUILD_DIR", &custom_build_dir);

    assert_eq!(g(&cfg, "platformio", "core_dir"), s(&super::options::abspath(&custom_core_dir)));
    assert_eq!(g(&cfg, "platformio", "src_dir"), s(&super::options::abspath(&custom_src_dir)));
    assert_eq!(g(&cfg, "platformio", "build_dir"), s(&super::options::abspath(&custom_build_dir)));

    for var in [
        "PLATFORMIO_BUILD_FLAGS",
        "PLATFORMIO_BUILD_UNFLAGS",
        "PLATFORMIO_UPLOAD_PORT",
        "__PIO_TEST_CNF_EXTRA_FLAGS",
        "PLATFORMIO_HOME_DIR",
        "PLATFORMIO_SRC_DIR",
        "PLATFORMIO_BUILD_DIR",
    ] {
        std::env::remove_var(var);
    }
}

#[test]
fn test_getraw_value() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    let cfg = build_main_config(tmp.path());

    assert!(matches!(
        cfg.getraw("custom", "unknown_option", Defaulted::Missing),
        Err(ConfigError::NoOption { .. })
    ));
    assert!(matches!(
        cfg.getraw("platformio", "monitor_speed", Defaulted::Missing),
        Err(ConfigError::NoOption { .. })
    ));

    assert_eq!(
        cfg.getraw("unknown", "option", Defaulted::Provided(s("default"))).unwrap(),
        s("default")
    );
    assert_eq!(gr(&cfg, "env:base", "custom_builtin_option"), s("release"));

    assert_eq!(gr(&cfg, "env:base", "targets"), s(""));
    assert_eq!(gr(&cfg, "env:extra_1", "lib_deps"), s("574"));
    assert_eq!(
        gr(&cfg, "env:extra_1", "build_flags"),
        s("\n-fdata-sections\n-Wl,--gc-sections\n-lc -lm\n-D DEBUG=1\n-D SERIAL_BAUD_RATE=9600")
    );

    assert_eq!(gr(&cfg, "env:test_extends", "lib_ldf_mode"), s("chain+"));
    assert_eq!(gr(&cfg, "env", "monitor_speed"), s("9600"));
    assert_eq!(gr(&cfg, "env:test_extends", "monitor_speed"), s("115200"));

    assert_eq!(g(&cfg, "platformio", "packages_dir"), s(&packages_dir()));
    assert_eq!(
        gr(&cfg, "custom", "debug_server"),
        s(&format!("\n{}/tool-openocd/openocd\n--help", packages_dir()))
    );

    assert_eq!(gr(&cfg, "env:extra_1", "lib_install"), s("574"));
    assert_eq!(gr(&cfg, "env:extra_1", "lib_deps"), s("574"));
    assert_eq!(gr(&cfg, "env:base", "debug_load_cmd"), l(&["load"]));
}

#[test]
fn test_get_value() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    let cfg = build_main_config(tmp.path());

    assert_eq!(g(&cfg, "custom", "debug_flags"), s("-D DEBUG=1"));
    assert_eq!(
        g(&cfg, "env:extra_1", "build_flags"),
        l(&["-fdata-sections", "-Wl,--gc-sections", "-lc -lm", "-D DEBUG=1", "-D SERIAL_BAUD_RATE=9600"])
    );
    assert_eq!(g(&cfg, "env:extra_2", "build_flags"), l(&["-Og"]));
    assert_eq!(g(&cfg, "env:extra_2", "monitor_speed"), Value::Int(9600));
    assert_eq!(g(&cfg, "env:base", "build_flags"), l(&["-D DEBUG=1"]));

    assert_eq!(
        g(&cfg, "env:inject_base_env", "debug_build_flags"),
        l(&["-Og", "-g2", "-ggdb2", "-D CUSTOM_DEBUG_FLAG"])
    );

    assert_eq!(g(&cfg, "platformio", "packages_dir"), s(&packages_dir()));
    assert_eq!(
        g(&cfg, "env:extra_2", "debug_server"),
        l(&[&format!("{}/tool-openocd/openocd", packages_dir()), "--help"])
    );
    assert_eq!(
        g(&cfg, "platformio", "src_dir"),
        s(&super::options::abspath(
            &std::env::current_dir().unwrap().join("source").to_string_lossy()
        ))
    );

    assert_eq!(g(&cfg, "env:extra_1", "lib_install"), l(&["574"]));
    assert_eq!(g(&cfg, "env:extra_1", "lib_deps"), l(&["574"]));
    assert_eq!(g(&cfg, "env:base", "debug_load_cmd"), l(&["load"]));
}

#[test]
fn test_items() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    let cfg = build_main_config(tmp.path());

    let items = |section: &str| cfg.items(section).unwrap();
    let items_env = |env: &str| cfg.items_env(env).unwrap();

    assert_eq!(
        items("custom"),
        vec![
            ("src_dir".to_string(), s("source")),
            ("debug_flags".to_string(), s("-D DEBUG=1")),
            ("lib_flags".to_string(), s("-lc -lm")),
            ("extra_flags".to_string(), s("")),
            ("lib_ignore".to_string(), s("LibIgnoreCustom")),
            (
                "debug_server".to_string(),
                s(&format!("\n{}/tool-openocd/openocd\n--help", packages_dir())),
            ),
            ("src_filter".to_string(), s("-<*>\n+<a>\n+<b>")),
        ]
    );
    assert_eq!(
        items_env("base"),
        vec![
            ("build_flags".to_string(), l(&["-D DEBUG=1"])),
            ("lib_compat_mode".to_string(), s("strict")),
            ("targets".to_string(), Value::List(vec![])),
            ("monitor_speed".to_string(), Value::Int(9600)),
            ("custom_monitor_speed".to_string(), s("115200")),
            ("lib_deps".to_string(), l(&["Lib1", "Lib2"])),
            ("lib_ignore".to_string(), l(&["LibIgnoreCustom"])),
            ("custom_builtin_option".to_string(), s("release")),
        ]
    );
    assert_eq!(
        items_env("extra_1"),
        vec![
            (
                "build_flags".to_string(),
                l(&["-fdata-sections", "-Wl,--gc-sections", "-lc -lm", "-D DEBUG=1", "-D SERIAL_BAUD_RATE=9600"]),
            ),
            ("lib_install".to_string(), l(&["574"])),
            ("monitor_speed".to_string(), Value::Int(9600)),
            ("custom_monitor_speed".to_string(), s("115200")),
            ("lib_deps".to_string(), l(&["574"])),
            ("lib_ignore".to_string(), l(&["LibIgnoreCustom"])),
            ("custom_builtin_option".to_string(), s("release")),
        ]
    );
    assert_eq!(
        items_env("extra_2"),
        vec![
            ("build_flags".to_string(), l(&["-Og"])),
            ("lib_ignore".to_string(), l(&["LibIgnoreCustom", "Lib3"])),
            ("upload_port".to_string(), s("/dev/extra_2/port")),
            (
                "debug_server".to_string(),
                l(&[&format!("{}/tool-openocd/openocd", packages_dir()), "--help"]),
            ),
            ("src_filter".to_string(), l(&["-<*>", "+<a>", "+<b> +<c>"])),
            ("monitor_speed".to_string(), Value::Int(9600)),
            ("custom_monitor_speed".to_string(), s("115200")),
            ("lib_deps".to_string(), l(&["Lib1", "Lib2"])),
            ("custom_builtin_option".to_string(), s("release")),
        ]
    );
    assert_eq!(
        items_env("test_extends"),
        vec![
            ("extends".to_string(), l(&["strict_settings"])),
            ("build_flags".to_string(), l(&["-D RELEASE"])),
            ("monitor_speed".to_string(), Value::Int(115200)),
            ("lib_ldf_mode".to_string(), s("chain+")),
            ("lib_compat_mode".to_string(), s("strict")),
            ("custom_monitor_speed".to_string(), s("115200")),
            ("lib_deps".to_string(), l(&["Lib1", "Lib2"])),
            ("lib_ignore".to_string(), l(&["LibIgnoreCustom"])),
            ("custom_builtin_option".to_string(), s("release")),
        ]
    );
}

#[test]
fn test_update_and_save() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    write(
        tmp.path(),
        "platformio.ini",
        "\n[platformio]\nextra_configs = a.ini, b.ini\n\n[env:myenv]\nboard = myboard\n    ",
    );
    let mut cfg = ProjectConfig::new(&ini_path(tmp.path())).unwrap();
    assert_eq!(cfg.envs(), vec!["myenv"]);
    let tuple = cfg.as_tuple().unwrap();
    assert_eq!(tuple[0].1[0].1, l(&["a.ini", "b.ini"]));

    cfg.update(
        vec![
            ("platformio".into(), vec![("extra_configs".into(), SetValue::List(vec!["extra.ini".into()]))]),
            ("env:myenv".into(), vec![("framework".into(), SetValue::List(vec!["espidf".into(), "arduino".into()]))]),
            (
                "check_types".into(),
                vec![
                    ("float_option".into(), SetValue::Float(13.99)),
                    ("bool_option".into(), SetValue::Bool(true)),
                ],
            ),
        ],
        false,
    );
    assert_eq!(g(&cfg, "platformio", "extra_configs"), l(&["extra.ini"]));
    cfg.remove_section("platformio");
    assert_eq!(
        cfg.as_tuple().unwrap(),
        vec![
            (
                "env:myenv".to_string(),
                vec![("board".to_string(), s("myboard")), ("framework".to_string(), l(&["espidf", "arduino"]))],
            ),
            (
                "check_types".to_string(),
                vec![("float_option".to_string(), s("13.99")), ("bool_option".to_string(), s("yes"))],
            ),
        ]
    );

    cfg.save(None).unwrap();
    let contents = std::fs::read_to_string(ini_path(tmp.path())).unwrap();
    assert_eq!(&contents[contents.len() - 4..], "yes\n");
    let lines: Vec<String> = contents
        .split('\n')
        .map(str::trim)
        .filter(|line| !line.is_empty() && !line.starts_with(';') && !line.starts_with('#'))
        .map(str::to_string)
        .collect();
    assert_eq!(
        lines,
        vec![
            "[env:myenv]",
            "board = myboard",
            "framework =",
            "espidf",
            "arduino",
            "[check_types]",
            "float_option = 13.99",
            "bool_option = yes",
        ]
    );
}

#[test]
fn test_update_and_clear() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    write(
        tmp.path(),
        "platformio.ini",
        "\n[platformio]\nextra_configs = a.ini, b.ini\n\n[env:myenv]\nboard = myboard\n    ",
    );
    let mut cfg = ProjectConfig::new(&ini_path(tmp.path())).unwrap();
    assert_eq!(cfg.sections(), vec!["platformio", "env:myenv"]);
    cfg.update(
        vec![(
            "mysection".into(),
            vec![("opt1".into(), SetValue::Str("value1".into())), ("opt2".into(), SetValue::Str("value2".into()))],
        )],
        true,
    );
    assert_eq!(
        cfg.as_tuple().unwrap(),
        vec![(
            "mysection".to_string(),
            vec![("opt1".to_string(), s("value1")), ("opt2".to_string(), s("value2"))]
        )]
    );
}

#[test]
fn test_dump() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    write(tmp.path(), "platformio.ini", BASE_CONFIG);
    write(tmp.path(), "extra_envs.ini", EXTRA_ENVS_CONFIG);
    write(tmp.path(), "extra_debug.ini", EXTRA_DEBUG_CONFIG);
    let cfg = ProjectConfig::with_options(&ini_path(tmp.path()), false, false).unwrap();

    assert_eq!(
        cfg.as_tuple().unwrap(),
        vec![
            (
                "platformio".to_string(),
                vec![
                    ("env_default".to_string(), l(&["base", "extra_2"])),
                    ("src_dir".to_string(), s("${custom.src_dir}")),
                    ("extra_configs".to_string(), l(&["extra_envs.ini", "extra_debug.ini"])),
                ],
            ),
            (
                "env".to_string(),
                vec![
                    ("monitor_speed".to_string(), Value::Int(9600)),
                    ("custom_monitor_speed".to_string(), s("115200")),
                    ("lib_deps".to_string(), l(&["Lib1", "Lib2"])),
                    ("lib_ignore".to_string(), l(&["${custom.lib_ignore}"])),
                    ("custom_builtin_option".to_string(), s("${env.build_type}")),
                ],
            ),
            (
                "strict_ldf".to_string(),
                vec![("lib_ldf_mode".to_string(), s("chain+")), ("lib_compat_mode".to_string(), s("strict"))],
            ),
            ("monitor_custom".to_string(), vec![("monitor_speed".to_string(), s("${env.custom_monitor_speed}"))]),
            (
                "strict_settings".to_string(),
                vec![("extends".to_string(), s("strict_ldf, monitor_custom")), ("build_flags".to_string(), s("-D RELEASE"))],
            ),
            (
                "custom".to_string(),
                vec![
                    ("src_dir".to_string(), s("source")),
                    ("debug_flags".to_string(), s("-D RELEASE")),
                    ("lib_flags".to_string(), s("-lc -lm")),
                    ("extra_flags".to_string(), s("${sysenv.__PIO_TEST_CNF_EXTRA_FLAGS}")),
                    ("lib_ignore".to_string(), s("LibIgnoreCustom")),
                ],
            ),
            (
                "env:base".to_string(),
                vec![
                    ("build_flags".to_string(), l(&["${custom.debug_flags} ${custom.extra_flags}"])),
                    ("lib_compat_mode".to_string(), s("${strict_ldf.lib_compat_mode}")),
                    ("targets".to_string(), Value::List(vec![])),
                ],
            ),
            ("env:test_extends".to_string(), vec![("extends".to_string(), l(&["strict_settings"]))]),
            (
                "env:inject_base_env".to_string(),
                vec![("debug_build_flags".to_string(), l(&["${env.debug_build_flags}", "-D CUSTOM_DEBUG_FLAG"]))],
            ),
        ]
    );
}

#[test]
fn test_this() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    write(
        tmp.path(),
        "platformio.ini",
        "\n[common]\nboard = uno\n\n[env:myenv]\nextends = common\nbuild_flags = -D${this.__env__}\ncustom_option = ${this.board}\n    ",
    );
    let cfg = ProjectConfig::new(&ini_path(tmp.path())).unwrap();
    assert_eq!(g(&cfg, "env:myenv", "custom_option"), s("uno"));
    assert_eq!(g(&cfg, "env:myenv", "build_flags"), l(&["-Dmyenv"]));
}

#[test]
fn test_project_name() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let project_dir = tmp.path().join("my-project-name");
    std::fs::create_dir(&project_dir).unwrap();
    let _cwd = CwdGuard::enter(&project_dir);
    let conf = project_dir.join("platformio.ini").to_string_lossy().into_owned();

    std::fs::write(&conf, "\n[env:myenv]\n    ").unwrap();
    let cfg = ProjectConfig::new(&conf).unwrap();
    assert_eq!(g(&cfg, "platformio", "name"), s("my-project-name"));

    std::fs::write(&conf, "\n[platformio]\nname = custom-project-name\n    ").unwrap();
    let cfg = ProjectConfig::new(&conf).unwrap();
    assert_eq!(g(&cfg, "platformio", "name"), s("custom-project-name"));
}

#[test]
fn test_nested_interpolation() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    write(
        tmp.path(),
        "platformio.ini",
        "\n[platformio]\nbuild_dir = /tmp/pio-$PROJECT_HASH\ndata_dir = $PROJECT_DIR/assets\n\n[env:myenv]\nbuild_flags =\n    -D UTIME=${UNIX_TIME}\n    -I ${PROJECTSRC_DIR}/hal\n    -Wl,-Map,${BUILD_DIR}/${PROGNAME}.map\ntest_testing_command =\n    ${platformio.packages_dir}/tool-simavr/bin/simavr\n     -m\n     atmega328p\n     -f\n     16000000L\n     ${UPLOAD_PORT and \"-p \"+UPLOAD_PORT}\n     ${platformio.build_dir}/${this.__env__}/firmware.elf\n    ",
    );
    let cfg = ProjectConfig::new(&ini_path(tmp.path())).unwrap();

    let data_dir = g(&cfg, "platformio", "data_dir");
    assert!(data_dir.as_str().unwrap().ends_with(&format!("$PROJECT_DIR{MAIN_SEPARATOR}assets")));

    let build_flags = g(&cfg, "env:myenv", "build_flags");
    let Value::List(flags) = &build_flags else { panic!("expected list") };
    let f0 = flags[0].as_str().unwrap();
    assert!(f0.len() >= 10 && f0[f0.len() - 10..].chars().all(|c| c.is_ascii_digit()), "{f0}");
    assert_eq!(flags[1], s("-I ${PROJECTSRC_DIR}/hal"));
    assert_eq!(flags[2], s("-Wl,-Map,${BUILD_DIR}/${PROGNAME}.map"));

    let cmd = g(&cfg, "env:myenv", "test_testing_command");
    let Value::List(items) = &cmd else { panic!("expected list") };
    assert!(!items[0].as_str().unwrap().contains('$'));
    assert_eq!(items[5], s("${UPLOAD_PORT and \"-p \"+UPLOAD_PORT}"));
}

#[test]
fn test_extends_order() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    write(
        tmp.path(),
        "platformio.ini",
        "\n[a]\nboard = test\n\n[b]\nupload_tool = two\n\n[c]\nupload_tool = three\n\n[env:na_ti-ve13]\nextends = a, b, c\n    ",
    );
    let cfg = ProjectConfig::new(&ini_path(tmp.path())).unwrap();
    assert_eq!(g(&cfg, "env:na_ti-ve13", "upload_tool"), s("three"));
}

#[test]
fn test_invalid_env_names() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    write(tmp.path(), "platformio.ini", "\n[env:app:1]\n    ");
    let cfg = ProjectConfig::new(&ini_path(tmp.path())).unwrap();
    match cfg.validate(None, false) {
        Err(ConfigError::InvalidEnvName { name }) => assert_eq!(name, "app:1"),
        other => panic!("expected InvalidEnvName, got {other:?}"),
    }
}

#[test]
fn test_linting_errors() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    write(tmp.path(), "platformio.ini", "\n[env:app1]\nlib_use = 1\nbroken_line\n    ");
    let result = ProjectConfig::lint(&ini_path(tmp.path()));
    assert!(result.warnings.is_empty());
    assert_eq!(result.errors.len(), 1);
    assert_eq!(result.errors[0].type_name, "ParsingError");
    assert_eq!(result.errors[0].lineno, Some(4));
}

#[test]
fn test_linting_warnings() {
    let _lock = lock();
    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());
    write(
        tmp.path(),
        "platformio.ini",
        "\n[platformio]\nbuild_dir = /tmp/pio-$PROJECT_HASH\n\n[env:app1]\nlib_use = 1\ntest_testing_command = /usr/bin/flash-tool -p $UPLOAD_PORT -b $UPLOAD_SPEED\n    ",
    );
    let result = ProjectConfig::lint(&ini_path(tmp.path()));
    assert!(result.errors.is_empty());
    assert_eq!(result.warnings.len(), 2);
    assert!(result.warnings[0].contains("deprecated"));
    assert!(result.warnings[1].contains("Invalid variable declaration"));
}

#[cfg(windows)]
#[test]
fn test_win_core_root_dir() {
    let _lock = lock();
    let home = dirs::home_dir().unwrap().to_string_lossy().into_owned();
    let drive = &home[..2]; // "C:"
    let win_core_root_dir = format!("{drive}\\.platformio");

    let created = if !Path::new(&win_core_root_dir).is_dir() {
        if std::fs::create_dir_all(&win_core_root_dir).is_err() {
            return; // PermissionError equivalent — skip.
        }
        true
    } else {
        false
    };

    let tmp = tempfile::tempdir().unwrap();
    let _cwd = CwdGuard::enter(tmp.path());

    // Default config (no platformio.ini in cwd → empty).
    let cfg = ProjectConfig::new(&ini_path(tmp.path())).unwrap();
    assert_eq!(g(&cfg, "platformio", "core_dir"), s(&win_core_root_dir));
    assert_eq!(g(&cfg, "platformio", "packages_dir"), s(&format!("{win_core_root_dir}\\packages")));

    // Override in config.
    let proj = tempfile::tempdir().unwrap();
    write(proj.path(), "platformio.ini", "\n[platformio]\ncore_dir = ~/.pio\n        ");
    let cfg = ProjectConfig::new(&ini_path(proj.path())).unwrap();
    assert_ne!(g(&cfg, "platformio", "core_dir"), s(&win_core_root_dir));
    assert_eq!(
        g(&cfg, "platformio", "core_dir"),
        s(&super::options::abspath(&super::options::expanduser("~/.pio")))
    );

    if created {
        let _ = std::fs::remove_dir(&win_core_root_dir);
    }
}
