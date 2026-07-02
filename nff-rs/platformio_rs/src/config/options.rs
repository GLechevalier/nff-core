//! Port of `platformio/project/options.py`: the `ConfigOption` schema and the
//! ordered `ProjectOptions` registry, plus the directory validators.

use std::collections::HashMap;
use std::path::{Component, Path, MAIN_SEPARATOR, MAIN_SEPARATOR_STR};
use std::sync::OnceLock;

use super::value::Value;

/// A `click`-style option type. Casting is a no-op for [`OptionType::Str`]
/// (Python `str` is not a `ParamType`); the rest map to `click` `ParamType`s.
#[derive(Debug, Clone)]
pub enum OptionType {
    Str,
    Int,
    Bool,
    IntRange(i64, i64),
    Choice(&'static [&'static str]),
    /// `click.Path` — implemented as identity (existence checks are untested and
    /// would spuriously fail in unit tests); documented deviation.
    Path,
}

impl OptionType {
    /// Whether this maps to a real `click.ParamType` (so `cast_to` applies it).
    fn is_param_type(&self) -> bool {
        !matches!(self, Self::Str)
    }

    /// Port of `ProjectConfigBase.cast_to`: wrap a scalar in a list, cast each
    /// item (only when this is a `ParamType`), and unwrap if the input was scalar.
    /// `Err` carries a `click`-style message for the caller to wrap.
    pub fn cast_to(&self, value: Value) -> Result<Value, String> {
        let was_list = matches!(value, Value::List(_));
        let items: Vec<Value> = match value {
            Value::List(items) => items,
            other => vec![other],
        };
        let mut cast = Vec::with_capacity(items.len());
        for item in items {
            cast.push(self.cast_item(item)?);
        }
        if was_list {
            Ok(Value::List(cast))
        } else {
            Ok(cast.into_iter().next().unwrap_or(Value::None))
        }
    }

    fn cast_item(&self, item: Value) -> Result<Value, String> {
        // `click.ParamType.__call__` returns None untouched for a None value.
        if item.is_none() || !self.is_param_type() {
            return Ok(item);
        }
        let s = item.to_plain_string();
        match self {
            Self::Str | Self::Path => Ok(Value::Str(s)),
            Self::Int => s
                .trim()
                .parse::<i64>()
                .map(Value::Int)
                .map_err(|_| format!("{s:?} is not a valid integer.")),
            Self::Bool => parse_bool(&s)
                .map(Value::Bool)
                .ok_or_else(|| format!("{s:?} is not a valid boolean.")),
            Self::IntRange(min, max) => match s.trim().parse::<i64>() {
                Ok(n) if n >= *min && n <= *max => Ok(Value::Int(n)),
                Ok(n) => Err(format!("{n} is not in the range {min}<=x<={max}.")),
                Err(_) => Err(format!("{s:?} is not a valid integer.")),
            },
            Self::Choice(choices) => {
                if choices.contains(&s.as_str()) {
                    Ok(Value::Str(s))
                } else {
                    Err(format!("invalid choice: {s}. (choose from {})", choices.join(", ")))
                }
            }
        }
    }
}

/// `click.BOOL` value parsing.
fn parse_bool(s: &str) -> Option<bool> {
    match s.trim().to_lowercase().as_str() {
        "1" | "true" | "t" | "yes" | "y" | "on" => Some(true),
        "0" | "false" | "f" | "no" | "n" | "off" => Some(false),
        _ => None,
    }
}

/// A per-option validator (only `validate_dir` exists upstream).
#[derive(Debug, Clone, Copy)]
pub enum Validate {
    Dir,
}

impl Validate {
    pub fn apply(self, value: Value) -> Value {
        match self {
            Self::Dir => match value {
                Value::Str(s) => Value::Str(validate_dir(&s)),
                other => other,
            },
        }
    }
}

/// An option's default. Callables (`name`, `core_dir`) and the per-OS `os.path.join`
/// dir defaults are resolved lazily against the live cwd/home.
#[derive(Debug, Clone)]
pub enum DefaultVal {
    /// Python `default=None` (no default).
    None,
    Str(&'static str),
    Int(i64),
    Bool(bool),
    StrList(&'static [&'static str]),
    /// `os.path.join(a, b)` with the platform separator (left un-interpolated).
    JoinDir(&'static str, &'static str),
    /// `lambda: os.path.basename(os.getcwd())`.
    NameBasename,
    /// `get_default_core_dir()`.
    CoreDir,
}

impl DefaultVal {
    pub fn resolve(&self) -> Value {
        match self {
            Self::None => Value::None,
            Self::Str(s) => Value::Str((*s).to_string()),
            Self::Int(n) => Value::Int(*n),
            Self::Bool(b) => Value::Bool(*b),
            Self::StrList(xs) => Value::List(xs.iter().map(|s| Value::Str((*s).to_string())).collect()),
            Self::JoinDir(a, b) => Value::Str(format!("{a}{MAIN_SEPARATOR}{b}")),
            Self::NameBasename => Value::Str(cwd_basename()),
            Self::CoreDir => Value::Str(get_default_core_dir()),
        }
    }
}

/// A single configuration option (`ConfigOption`).
#[derive(Debug, Clone)]
pub struct ConfigOption {
    pub scope: &'static str,
    pub name: &'static str,
    pub ty: OptionType,
    pub multiple: bool,
    pub sysenvvar: Option<&'static str>,
    pub oldnames: &'static [&'static str],
    pub default: DefaultVal,
    pub validate: Option<Validate>,
}

/// The ordered `ProjectOptions` registry.
pub struct ProjectOptions {
    options: Vec<ConfigOption>,
    by_key: HashMap<String, usize>,
}

impl ProjectOptions {
    pub fn iter(&self) -> impl Iterator<Item = &ConfigOption> {
        self.options.iter()
    }

    /// Exact `"scope.name"` lookup (`ProjectOptions.get(...)`).
    #[must_use]
    pub fn exact(&self, scope: &str, name: &str) -> Option<&ConfigOption> {
        self.by_key.get(&format!("{scope}.{name}")).map(|&i| &self.options[i])
    }

    #[must_use]
    pub fn contains(&self, scope: &str, name: &str) -> bool {
        self.by_key.contains_key(&format!("{scope}.{name}"))
    }

    /// Find by exact name then by `oldnames` within `scope` (`find_option_meta`).
    #[must_use]
    pub fn find(&self, scope: &str, option: &str) -> Option<&ConfigOption> {
        if let Some(meta) = self.exact(scope, option) {
            return Some(meta);
        }
        self.options
            .iter()
            .find(|o| o.scope == scope && o.oldnames.contains(&option))
    }
}

/// `validate_dir`: passthrough on empty / unexpanded `${...}`; expanduser `~`;
/// otherwise `os.path.abspath`.
#[must_use]
pub fn validate_dir(path: &str) -> String {
    if path.is_empty() {
        return path.to_string();
    }
    if path.contains("${") && path.contains('}') {
        return path.to_string();
    }
    let expanded = if path.starts_with('~') { expanduser(path) } else { path.to_string() };
    abspath(&expanded)
}

/// The effective PlatformIO core directory: `$PLATFORMIO_CORE_DIR` when set,
/// else [`get_default_core_dir`]. This is the value `ProjectConfig.get(
/// "platformio", "core_dir")` resolves to for a default (project-less) config.
#[must_use]
pub fn get_core_dir() -> String {
    match std::env::var("PLATFORMIO_CORE_DIR") {
        Ok(v) if !v.is_empty() => v,
        _ => get_default_core_dir(),
    }
}

/// `get_default_core_dir`: `~/.platformio`, preferring `<drive>:\.platformio` on
/// Windows when it exists.
#[must_use]
pub fn get_default_core_dir() -> String {
    let home = expanduser("~");
    let path = format!("{home}{MAIN_SEPARATOR}.platformio");
    #[cfg(windows)]
    {
        if let Some(drive) = splitdrive(&home) {
            let win_core_dir = format!("{drive}\\.platformio");
            if Path::new(&win_core_dir).is_dir() {
                return win_core_dir;
            }
        }
    }
    path
}

#[cfg(windows)]
fn splitdrive(path: &str) -> Option<String> {
    // Mirror os.path.splitdrive[0]: the "C:" prefix.
    let bytes = path.as_bytes();
    if bytes.len() >= 2 && bytes[1] == b':' {
        Some(path[..2].to_string())
    } else {
        None
    }
}

/// `os.path.expanduser` for the `~`/`~/...` forms PlatformIO uses.
#[must_use]
pub fn expanduser(path: &str) -> String {
    if path == "~" {
        return home_dir();
    }
    if let Some(rest) = path.strip_prefix("~/").or_else(|| path.strip_prefix("~\\")) {
        return format!("{}{MAIN_SEPARATOR}{}", home_dir(), rest);
    }
    path.to_string()
}

fn home_dir() -> String {
    dirs::home_dir().map(|p| p.to_string_lossy().into_owned()).unwrap_or_else(|| "~".to_string())
}

fn cwd() -> String {
    std::env::current_dir().map(|p| p.to_string_lossy().into_owned()).unwrap_or_default()
}

fn cwd_basename() -> String {
    std::env::current_dir()
        .ok()
        .and_then(|p| p.file_name().map(|f| f.to_string_lossy().into_owned()))
        .unwrap_or_default()
}

/// `os.path.abspath`: join cwd if relative, then normalize (`normpath`).
#[must_use]
pub fn abspath(path: &str) -> String {
    let p = Path::new(path);
    let joined = if p.is_absolute() { p.to_path_buf() } else { Path::new(&cwd()).join(p) };
    normpath(&joined)
}

/// `os.path.normpath`: collapse `.`/`..`, normalize separators.
fn normpath(path: &Path) -> String {
    let mut prefix = String::new();
    let mut has_root = false;
    let mut parts: Vec<String> = Vec::new();
    for comp in path.components() {
        match comp {
            Component::Prefix(p) => prefix = p.as_os_str().to_string_lossy().into_owned(),
            Component::RootDir => has_root = true,
            Component::CurDir => {}
            Component::ParentDir => {
                if matches!(parts.last().map(String::as_str), Some(p) if p != "..") {
                    parts.pop();
                } else if !has_root {
                    parts.push("..".to_string());
                }
            }
            Component::Normal(s) => parts.push(s.to_string_lossy().into_owned()),
        }
    }
    let mut out = prefix;
    if has_root {
        out.push(MAIN_SEPARATOR);
    }
    out.push_str(&parts.join(MAIN_SEPARATOR_STR));
    if out.is_empty() {
        ".".to_string()
    } else {
        out
    }
}

/// The lazily-built singleton registry (`ProjectOptions`).
pub fn project_options() -> &'static ProjectOptions {
    static REGISTRY: OnceLock<ProjectOptions> = OnceLock::new();
    REGISTRY.get_or_init(build_registry)
}

fn build_registry() -> ProjectOptions {
    use DefaultVal as D;
    use OptionType as T;

    // Concise entry helper mirroring the upstream `ConfigOption(...)` calls.
    #[allow(clippy::too_many_arguments)]
    fn o(
        scope: &'static str,
        name: &'static str,
        ty: OptionType,
        multiple: bool,
        sysenvvar: Option<&'static str>,
        oldnames: &'static [&'static str],
        default: DefaultVal,
        validate: Option<Validate>,
    ) -> ConfigOption {
        ConfigOption { scope, name, ty, multiple, sysenvvar, oldnames, default, validate }
    }

    const NONE: &[&str] = &[];
    let pio = "platformio";
    let env = "env";
    let dir = Some(Validate::Dir);

    let options = vec![
        // ---- [platformio] : generic ----
        o(pio, "name", T::Str, false, None, NONE, D::NameBasename, None),
        o(pio, "description", T::Str, false, None, NONE, D::None, None),
        o(pio, "default_envs", T::Str, true, Some("PLATFORMIO_DEFAULT_ENVS"), &["env_default"], D::None, None),
        o(pio, "extra_configs", T::Str, true, None, NONE, D::None, None),
        // ---- [platformio] : directory ----
        o(pio, "core_dir", T::Str, false, Some("PLATFORMIO_CORE_DIR"), &["home_dir"], D::CoreDir, dir),
        o(pio, "globallib_dir", T::Str, false, Some("PLATFORMIO_GLOBALLIB_DIR"), NONE, D::JoinDir("${platformio.core_dir}", "lib"), dir),
        o(pio, "platforms_dir", T::Str, false, Some("PLATFORMIO_PLATFORMS_DIR"), NONE, D::JoinDir("${platformio.core_dir}", "platforms"), dir),
        o(pio, "packages_dir", T::Str, false, Some("PLATFORMIO_PACKAGES_DIR"), NONE, D::JoinDir("${platformio.core_dir}", "packages"), dir),
        o(pio, "cache_dir", T::Str, false, Some("PLATFORMIO_CACHE_DIR"), NONE, D::JoinDir("${platformio.core_dir}", ".cache"), dir),
        o(pio, "build_cache_dir", T::Str, false, Some("PLATFORMIO_BUILD_CACHE_DIR"), NONE, D::None, dir),
        o(pio, "workspace_dir", T::Str, false, Some("PLATFORMIO_WORKSPACE_DIR"), NONE, D::JoinDir("${PROJECT_DIR}", ".pio"), dir),
        o(pio, "build_dir", T::Str, false, Some("PLATFORMIO_BUILD_DIR"), NONE, D::JoinDir("${platformio.workspace_dir}", "build"), dir),
        o(pio, "libdeps_dir", T::Str, false, Some("PLATFORMIO_LIBDEPS_DIR"), NONE, D::JoinDir("${platformio.workspace_dir}", "libdeps"), dir),
        o(pio, "include_dir", T::Str, false, Some("PLATFORMIO_INCLUDE_DIR"), NONE, D::JoinDir("${PROJECT_DIR}", "include"), dir),
        o(pio, "src_dir", T::Str, false, Some("PLATFORMIO_SRC_DIR"), NONE, D::JoinDir("${PROJECT_DIR}", "src"), dir),
        o(pio, "lib_dir", T::Str, false, Some("PLATFORMIO_LIB_DIR"), NONE, D::JoinDir("${PROJECT_DIR}", "lib"), dir),
        o(pio, "data_dir", T::Str, false, Some("PLATFORMIO_DATA_DIR"), NONE, D::JoinDir("${PROJECT_DIR}", "data"), dir),
        o(pio, "test_dir", T::Str, false, Some("PLATFORMIO_TEST_DIR"), NONE, D::JoinDir("${PROJECT_DIR}", "test"), dir),
        o(pio, "boards_dir", T::Str, false, Some("PLATFORMIO_BOARDS_DIR"), NONE, D::JoinDir("${PROJECT_DIR}", "boards"), dir),
        o(pio, "monitor_dir", T::Str, false, Some("PLATFORMIO_MONITOR_DIR"), NONE, D::JoinDir("${PROJECT_DIR}", "monitor"), dir),
        o(pio, "shared_dir", T::Str, false, Some("PLATFORMIO_SHARED_DIR"), NONE, D::JoinDir("${PROJECT_DIR}", "shared"), dir),
        // ---- [env] : platform ----
        o(env, "platform", T::Str, false, None, NONE, D::None, None),
        o(env, "platform_packages", T::Str, true, None, NONE, D::None, None),
        o(env, "board", T::Str, false, None, NONE, D::None, None),
        o(env, "framework", T::Str, true, None, NONE, D::None, None),
        o(env, "board_build.mcu", T::Str, false, None, &["board_mcu"], D::None, None),
        o(env, "board_build.f_cpu", T::Str, false, None, &["board_f_cpu"], D::None, None),
        o(env, "board_build.f_flash", T::Str, false, None, &["board_f_flash"], D::None, None),
        o(env, "board_build.flash_mode", T::Str, false, None, &["board_flash_mode"], D::None, None),
        // ---- [env] : build ----
        o(env, "build_type", T::Choice(&["release", "test", "debug"]), false, None, NONE, D::Str("release"), None),
        o(env, "build_flags", T::Str, true, Some("PLATFORMIO_BUILD_FLAGS"), NONE, D::None, None),
        o(env, "build_src_flags", T::Str, true, Some("PLATFORMIO_BUILD_SRC_FLAGS"), &["src_build_flags"], D::None, None),
        o(env, "build_unflags", T::Str, true, Some("PLATFORMIO_BUILD_UNFLAGS"), NONE, D::None, None),
        o(env, "build_src_filter", T::Str, true, Some("PLATFORMIO_BUILD_SRC_FILTER"), &["src_filter"], D::Str("+<*> -<.git/> -<.svn/>"), None),
        o(env, "targets", T::Str, true, None, NONE, D::None, None),
        // ---- [env] : upload ----
        o(env, "upload_port", T::Str, false, Some("PLATFORMIO_UPLOAD_PORT"), NONE, D::None, None),
        o(env, "upload_protocol", T::Str, false, None, NONE, D::None, None),
        o(env, "upload_speed", T::Int, false, None, NONE, D::None, None),
        o(env, "upload_flags", T::Str, true, Some("PLATFORMIO_UPLOAD_FLAGS"), NONE, D::None, None),
        o(env, "upload_resetmethod", T::Str, false, None, NONE, D::None, None),
        o(env, "upload_command", T::Str, false, None, NONE, D::None, None),
        // ---- [env] : monitor ----
        o(env, "monitor_port", T::Str, false, None, NONE, D::None, None),
        o(env, "monitor_speed", T::Int, false, None, &["monitor_baud"], D::Int(9600), None),
        o(env, "monitor_parity", T::Choice(&["N", "E", "O", "S", "M"]), false, None, NONE, D::Str("N"), None),
        o(env, "monitor_filters", T::Str, true, None, NONE, D::None, None),
        o(env, "monitor_rts", T::IntRange(0, 1), false, None, NONE, D::None, None),
        o(env, "monitor_dtr", T::IntRange(0, 1), false, None, NONE, D::None, None),
        o(env, "monitor_eol", T::Choice(&["CR", "LF", "CRLF"]), false, None, NONE, D::Str("CRLF"), None),
        o(env, "monitor_raw", T::Bool, false, None, NONE, D::Bool(false), None),
        o(env, "monitor_echo", T::Bool, false, None, NONE, D::Bool(false), None),
        o(env, "monitor_encoding", T::Str, false, None, NONE, D::Str("UTF-8"), None),
        // ---- [env] : library ----
        o(env, "lib_deps", T::Str, true, None, &["lib_use", "lib_force", "lib_install"], D::None, None),
        o(env, "lib_ignore", T::Str, true, None, NONE, D::None, None),
        o(env, "lib_extra_dirs", T::Str, true, Some("PLATFORMIO_LIB_EXTRA_DIRS"), NONE, D::None, None),
        o(env, "lib_ldf_mode", T::Choice(&["off", "chain", "deep", "chain+", "deep+"]), false, None, NONE, D::Str("chain"), None),
        o(env, "lib_compat_mode", T::Choice(&["off", "soft", "strict"]), false, None, NONE, D::Str("soft"), None),
        o(env, "lib_archive", T::Bool, false, None, NONE, D::Bool(true), None),
        // ---- [env] : check ----
        o(env, "check_tool", T::Choice(&["cppcheck", "clangtidy", "pvs-studio"]), true, None, NONE, D::StrList(&["cppcheck"]), None),
        o(env, "check_src_filters", T::Str, true, None, &["check_patterns"], D::None, None),
        o(env, "check_flags", T::Str, true, None, NONE, D::None, None),
        o(env, "check_severity", T::Choice(&["low", "medium", "high"]), true, None, NONE, D::StrList(&["low", "medium", "high"]), None),
        o(env, "check_skip_packages", T::Bool, false, None, NONE, D::Bool(false), None),
        // ---- [env] : test ----
        o(env, "test_framework", T::Choice(&["doctest", "googletest", "unity", "custom"]), false, None, NONE, D::Str("unity"), None),
        o(env, "test_filter", T::Str, true, None, NONE, D::None, None),
        o(env, "test_ignore", T::Str, true, None, NONE, D::None, None),
        o(env, "test_port", T::Str, false, None, NONE, D::None, None),
        o(env, "test_speed", T::Int, false, None, NONE, D::Int(115200), None),
        o(env, "test_build_src", T::Bool, false, None, &["test_build_project_src"], D::Bool(false), None),
        o(env, "test_testing_command", T::Str, true, None, NONE, D::None, None),
        // ---- [env] : debug ----
        o(env, "debug_tool", T::Str, false, None, NONE, D::None, None),
        o(env, "debug_build_flags", T::Str, true, None, NONE, D::StrList(&["-Og", "-g2", "-ggdb2"]), None),
        o(env, "debug_init_break", T::Str, false, None, NONE, D::Str("tbreak main"), None),
        o(env, "debug_init_cmds", T::Str, true, None, NONE, D::None, None),
        o(env, "debug_extra_cmds", T::Str, true, None, NONE, D::None, None),
        o(env, "debug_load_cmds", T::Str, true, None, &["debug_load_cmd"], D::StrList(&["load"]), None),
        o(env, "debug_load_mode", T::Choice(&["always", "modified", "manual"]), false, None, NONE, D::Str("always"), None),
        o(env, "debug_server", T::Str, true, None, NONE, D::None, None),
        o(env, "debug_port", T::Str, false, None, NONE, D::None, None),
        o(env, "debug_speed", T::Str, false, None, NONE, D::None, None),
        o(env, "debug_svd_path", T::Path, false, None, NONE, D::None, None),
        o(env, "debug_server_ready_pattern", T::Str, false, None, NONE, D::None, None),
        o(env, "debug_test", T::Str, false, None, NONE, D::None, None),
        // ---- [env] : advanced ----
        o(env, "extends", T::Str, true, None, NONE, D::None, None),
        o(env, "extra_scripts", T::Str, true, Some("PLATFORMIO_EXTRA_SCRIPTS"), &["extra_script"], D::None, None),
    ];

    let by_key = options
        .iter()
        .enumerate()
        .map(|(i, opt)| (format!("{}.{}", opt.scope, opt.name), i))
        .collect();
    ProjectOptions { options, by_key }
}
