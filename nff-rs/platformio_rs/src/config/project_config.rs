//! Port of `platformio/project/config.py` (`ProjectConfigBase` + the Lint/Dirs/
//! `ProjectConfig` mixins) into a single [`ProjectConfig`] type.

use std::cell::{Cell, RefCell};
use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::OnceLock;

use regex::Regex;
use sha1::{Digest, Sha1};

use super::error::{ConfigError, Result};
use super::ini::Parser;
use super::options::{project_options, ConfigOption};
use super::value::{parse_multi_values, Defaulted, Value};

/// `CONFIG_HEADER` from `config.py` (written by `save()`).
const CONFIG_HEADER: &str = "\n; PlatformIO Project Configuration File\n;\n;   Build options: build flags, source filter\n;   Upload options: custom upload port, speed and extra flags\n;   Library options: dependencies, extra library storages\n;   Advanced options: extra scripting\n;\n; Please visit documentation for the other options and examples\n; https://docs.platformio.org/page/projectconf.html\n";

/// Maximum interpolation recursion depth before raising `ProjectOptionValueError`
/// (Python relies on Python's own `RecursionError`).
const MAX_INTERP_DEPTH: usize = 60;

fn envname_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?i)^[a-z\d_\-]+$").expect("valid envname regex"))
}

fn vartpl_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\$\{(?:([^.}()]+)\.)?([^}]+)\}").expect("valid vartpl regex"))
}

/// A value passed to [`ProjectConfig::update`] / `set` (Python accepts a wider
/// set of types than [`Value`], including floats).
#[derive(Debug, Clone)]
pub enum SetValue {
    Str(String),
    Int(i64),
    Float(f64),
    Bool(bool),
    List(Vec<String>),
    None,
}

/// One lint finding (`{type, message, lineno}`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LintItem {
    pub type_name: String,
    pub message: String,
    pub lineno: Option<usize>,
}

/// The `(option, value)` pairs for one section (`items()` result).
pub type SectionItems = Vec<(String, Value)>;
/// The full `(section, items)` view (`as_tuple()` result).
pub type ConfigTuple = Vec<(String, SectionItems)>;

/// Result of [`ProjectConfig::lint`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LintResult {
    pub errors: Vec<LintItem>,
    pub warnings: Vec<String>,
}

pub struct ProjectConfig {
    pub path: String,
    parser: Parser,
    expand: Cell<bool>,
    warnings: RefCell<Vec<String>>,
    parsed: Vec<String>,
}

impl ProjectConfig {
    /// `ProjectConfig(path)` — `parse_extra=True`, `expand_interpolations=True`.
    pub fn new(path: &str) -> Result<Self> {
        Self::with_options(path, true, true)
    }

    /// `ProjectConfig(path, parse_extra, expand_interpolations)`.
    pub fn with_options(path: &str, parse_extra: bool, expand: bool) -> Result<Self> {
        let mut cfg = Self {
            path: path.to_string(),
            parser: Parser::new(),
            expand: Cell::new(expand),
            warnings: RefCell::new(Vec::new()),
            parsed: Vec::new(),
        };
        if !path.is_empty() && Path::new(path).is_file() {
            cfg.read(path, parse_extra)?;
        }
        cfg.maintain_renamed_options();
        Ok(cfg)
    }

    pub fn set_expand_interpolations(&self, value: bool) {
        self.expand.set(value);
    }

    #[must_use]
    pub fn warnings(&self) -> Vec<String> {
        self.warnings.borrow().clone()
    }

    fn push_warning(&self, msg: String) {
        self.warnings.borrow_mut().push(msg);
    }

    // ---- parser delegation (Python `__getattr__`) ----

    #[must_use]
    pub fn sections(&self) -> Vec<String> {
        self.parser.sections()
    }

    #[must_use]
    pub fn has_section(&self, name: &str) -> bool {
        self.parser.has_section(name)
    }

    pub fn remove_section(&mut self, name: &str) -> bool {
        self.parser.remove_section(name)
    }

    // ---- reading ----

    fn read(&mut self, path: &str, parse_extra: bool) -> Result<()> {
        if self.parsed.iter().any(|p| p == path) {
            return Ok(());
        }
        self.parsed.push(path.to_string());

        let content = std::fs::read_to_string(path).map_err(|e| ConfigError::InvalidProjectConf {
            path: path.to_string(),
            detail: e.to_string(),
            parse_errors: None,
        })?;
        self.parser.read_str(&content).map_err(|e| match e {
            ConfigError::Parsing { source, errors } => ConfigError::InvalidProjectConf {
                path: path.to_string(),
                detail: format!("Source contains parsing errors: '{source}'"),
                parse_errors: Some(errors),
            },
            other => ConfigError::InvalidProjectConf {
                path: path.to_string(),
                detail: other.to_string(),
                parse_errors: None,
            },
        })?;

        if !parse_extra {
            return Ok(());
        }

        // Load extra configs (immutable borrow released before the recursive read).
        let patterns = self.get_str_list("platformio", "extra_configs");
        for pattern in patterns {
            let pat = if pattern.starts_with('~') {
                super::options::expanduser(&pattern)
            } else {
                pattern
            };
            if let Ok(paths) = glob::glob(&pat) {
                for entry in paths.flatten() {
                    let item = entry.to_string_lossy().into_owned();
                    self.read(&item, true)?;
                }
            }
        }
        Ok(())
    }

    fn maintain_renamed_options(&self) {
        let mut renamed: HashMap<&'static str, &'static str> = HashMap::new();
        for opt in project_options().iter() {
            for old in opt.oldnames {
                renamed.insert(old, opt.name);
            }
        }
        for section in self.parser.sections() {
            let scope = get_section_scope(&section);
            if scope != "platformio" && scope != "env" {
                continue;
            }
            for option in self.parser.options(&section) {
                if let Some(newname) = renamed.get(option.as_str()) {
                    self.push_warning(format!(
                        "`{option}` configuration option in section [{section}] is \
                         deprecated and will be removed in the next release! \
                         Please use `{newname}` instead"
                    ));
                    continue;
                }
                let is_custom = scope == "env"
                    && (option.starts_with("custom_") || option.starts_with("board_"));
                if !project_options().contains(scope, &option) && !is_custom {
                    self.push_warning(format!(
                        "Ignore unknown configuration option `{option}` in section [{section}]"
                    ));
                }
            }
        }
    }

    // ---- option resolution ----

    fn walk_options(&self, root_section: &str) -> Vec<(String, String)> {
        let mut queue: Vec<String> = if root_section.starts_with("env:") {
            vec!["env".to_string(), root_section.to_string()]
        } else {
            vec![root_section.to_string()]
        };
        let mut out = Vec::new();
        while let Some(section) = queue.pop() {
            if !self.parser.has_section(&section) {
                continue;
            }
            for option in self.parser.options(&section) {
                out.push((section.clone(), option));
            }
            if self.parser.has_option(&section, "extends") {
                if let Ok(raw) = self.parser.get(&section, "extends") {
                    queue.extend(parse_multi_values(&Value::Str(raw)));
                }
            }
        }
        out
    }

    fn find_option_meta(&self, section: &str, option: &str) -> Option<&'static ConfigOption> {
        let scope = get_section_scope(section);
        if scope != "platformio" && scope != "env" {
            return None;
        }
        project_options().find(scope, option)
    }

    /// `_traverse_for_value` — first matching raw value, or `None` for `MISSING`.
    fn traverse_for_value(
        &self,
        section: &str,
        option: &str,
        meta: Option<&ConfigOption>,
    ) -> Option<String> {
        for (sec, opt) in self.walk_options(section) {
            let matches = opt == option
                || meta.is_some_and(|m| m.name == opt || m.oldnames.contains(&opt.as_str()));
            if matches {
                return self.parser.get(&sec, &opt).ok();
            }
        }
        None
    }

    // ---- getraw / interpolation ----

    pub fn getraw(&self, section: &str, option: &str, default: Defaulted) -> Result<Value> {
        self.getraw_inner(section, option, default, 0)
    }

    fn getraw_inner(
        &self,
        section: &str,
        option: &str,
        default: Defaulted,
        depth: usize,
    ) -> Result<Value> {
        if !self.expand.get() {
            return self.parser.get(section, option).map(Value::Str);
        }

        let meta = self.find_option_meta(section, option);
        // `None` represents the `MISSING` sentinel.
        let mut value: Option<Value> = self.traverse_for_value(section, option, meta).map(Value::Str);

        let Some(meta) = meta else {
            let resolved = match value {
                Some(v) => v,
                None => match default {
                    Defaulted::Provided(d) => d,
                    Defaulted::Missing => Value::Str(self.parser.get(section, option)?),
                },
            };
            return self.expand_interpolations(section, option, resolved, depth);
        };

        if let Some(envvar) = meta.sysenvvar {
            let mut envval = std::env::var(envvar).ok().filter(|s| !s.is_empty());
            if envval.is_none() {
                for old in meta.oldnames {
                    if let Ok(v) = std::env::var(format!("PLATFORMIO_{}", old.to_uppercase())) {
                        if !v.is_empty() {
                            envval = Some(v);
                            break;
                        }
                    }
                }
            }
            if let Some(ev) = envval {
                if meta.multiple {
                    let base = match &value {
                        Some(Value::Str(s)) => s.clone(),
                        _ => String::new(),
                    };
                    let sep = if base.is_empty() { "" } else { "\n" };
                    value = Some(Value::Str(format!("{base}{sep}{ev}")));
                } else {
                    value = Some(Value::Str(ev));
                }
            }
        }

        let resolved = match value {
            Some(v) => v,
            None => match default {
                Defaulted::Provided(d) => d,
                Defaulted::Missing => meta.default.resolve(),
            },
        };
        self.expand_interpolations(section, option, resolved, depth)
    }

    fn expand_interpolations(
        &self,
        section: &str,
        option: &str,
        value: Value,
        depth: usize,
    ) -> Result<Value> {
        let Value::Str(s) = &value else {
            return Ok(value);
        };
        if s.is_empty() || !s.contains('$') {
            return Ok(value);
        }
        let mut s = s.clone();

        // Legacy support for variables declared without "${}" (only PROJECT_HASH).
        const LEGACY: [&str; 1] = ["PROJECT_HASH"];
        loop {
            let mut changed = false;
            for name in LEGACY {
                let needle = format!("${name}");
                if let Some(x) = s.find(&needle) {
                    let bytes = s.as_bytes();
                    let prev = if x == 0 {
                        bytes.last().copied()
                    } else {
                        Some(bytes[x - 1])
                    };
                    if prev == Some(b'$') {
                        continue;
                    }
                    let end = x + needle.len();
                    s = format!("{}${{{}}}{}", &s[..x], name, &s[end..]);
                    let warn = format!(
                        "Invalid variable declaration. Please use `${{{name}}}` instead of `${name}`"
                    );
                    if !self.warnings.borrow().iter().any(|w| w == &warn) {
                        self.push_warning(warn);
                    }
                    changed = true;
                }
            }
            if !changed {
                break;
            }
        }

        if !(s.contains("${") && s.contains('}')) {
            return Ok(Value::Str(s));
        }
        let expanded = self.interp_substitute(section, option, &s, depth)?;
        Ok(Value::Str(expanded))
    }

    fn interp_substitute(
        &self,
        parent_section: &str,
        parent_option: &str,
        s: &str,
        depth: usize,
    ) -> Result<String> {
        let re = vartpl_re();
        let mut out = String::new();
        let mut last = 0;
        for caps in re.captures_iter(s) {
            let whole = caps.get(0).unwrap();
            out.push_str(&s[last..whole.start()]);
            let sec = caps.get(1).map(|m| m.as_str());
            let opt = caps.get(2).unwrap().as_str();
            let repl = self.interp_handler(parent_section, parent_option, sec, opt, depth)?;
            out.push_str(&repl);
            last = whole.end();
        }
        out.push_str(&s[last..]);
        Ok(out)
    }

    fn interp_handler(
        &self,
        parent_section: &str,
        parent_option: &str,
        section: Option<&str>,
        option: &str,
        depth: usize,
    ) -> Result<String> {
        // Built-in variables / SCons passthrough.
        let Some(section) = section else {
            if let Some(v) = builtin_var(option) {
                return Ok(v);
            }
            return Ok(format!("${{{option}}}"));
        };

        // System environment variables.
        if section == "sysenv" {
            return Ok(std::env::var(option).unwrap_or_default());
        }

        // ${this.*}
        let target_section: String = if section == "this" {
            if option == "__env__" {
                if !parent_section.starts_with("env:") {
                    return Err(ConfigError::ProjectOptionValue {
                        message: format!(
                            "`${{this.__env__}}` is called from the `{parent_section}` \
                             section that is not valid PlatformIO environment. Please \
                             check `{parent_option}` option in the `{parent_section}` section"
                        ),
                    });
                }
                return Ok(parent_section[4..].to_string());
            }
            parent_section.to_string()
        } else {
            section.to_string()
        };

        if depth >= MAX_INTERP_DEPTH {
            return Err(ConfigError::ProjectOptionValue {
                message: format!(
                    "Infinite recursion has been detected for `{option}` \
                     option in the `{target_section}` section"
                ),
            });
        }
        let value = self.get_inner(&target_section, option, Defaulted::Missing, depth + 1)?;
        Ok(join_value(&value))
    }

    // ---- get / cast ----

    pub fn get(&self, section: &str, option: &str, default: Defaulted) -> Result<Value> {
        self.get_inner(section, option, default, 0)
    }

    fn get_inner(
        &self,
        section: &str,
        option: &str,
        default: Defaulted,
        depth: usize,
    ) -> Result<Value> {
        let value = match self.getraw_inner(section, option, default, depth) {
            Ok(v) => v,
            Err(e) if e.is_configparser_error() => {
                return Err(ConfigError::InvalidProjectConf {
                    path: self.path.clone(),
                    detail: e.to_string(),
                    parse_errors: None,
                });
            }
            Err(e) => return Err(e),
        };

        let Some(meta) = self.find_option_meta(section, option) else {
            return Ok(value);
        };

        let mut value = value;
        if let Some(validate) = meta.validate {
            value = validate.apply(value);
        }
        if meta.multiple {
            value = Value::List(
                parse_multi_values(&value).into_iter().map(Value::Str).collect(),
            );
        }
        let pre_cast = value.clone();
        match meta.ty.cast_to(value) {
            Ok(v) => Ok(v),
            Err(msg) => {
                if !self.expand.get() {
                    return Ok(pre_cast);
                }
                Err(ConfigError::ProjectOptionValue {
                    message: format!(
                        "{msg} for `{option}` option in the `{section}` section"
                    ),
                })
            }
        }
    }

    // ---- options / items / envs ----

    pub fn options(&self, section: &str) -> Vec<String> {
        if !self.expand.get() {
            return self.parser.options(section);
        }
        let mut result: Vec<String> = Vec::new();
        for (_, option) in self.walk_options(section) {
            if !result.iter().any(|o| o == &option) {
                result.push(option);
            }
        }
        let scope = get_section_scope(section);
        for meta in project_options().iter() {
            if meta.scope != scope || result.iter().any(|o| o == meta.name) {
                continue;
            }
            if let Some(envvar) = meta.sysenvvar {
                if std::env::var_os(envvar).is_some() {
                    result.push(meta.name.to_string());
                }
            }
        }
        result
    }

    /// `options(env=...)` convenience.
    pub fn options_env(&self, env: &str) -> Vec<String> {
        self.options(&format!("env:{env}"))
    }

    pub fn has_option(&self, section: &str, option: &str) -> bool {
        if self.parser.has_option(section, option) {
            return true;
        }
        self.options(section).iter().any(|o| o == option)
    }

    pub fn items(&self, section: &str) -> Result<SectionItems> {
        let mut out = Vec::new();
        for option in self.options(section) {
            let value = self.get(section, &option, Defaulted::Missing)?;
            out.push((option, value));
        }
        Ok(out)
    }

    pub fn items_env(&self, env: &str) -> Result<SectionItems> {
        self.items(&format!("env:{env}"))
    }

    #[must_use]
    pub fn envs(&self) -> Vec<String> {
        self.parser
            .sections()
            .into_iter()
            .filter(|s| s.starts_with("env:"))
            .map(|s| s[4..].to_string())
            .collect()
    }

    pub fn default_envs(&self) -> Vec<String> {
        self.get_str_list("platformio", "default_envs")
    }

    #[must_use]
    pub fn get_default_env(&self) -> Option<String> {
        let defaults = self.default_envs();
        if let Some(first) = defaults.into_iter().next() {
            return Some(first);
        }
        self.envs().into_iter().next()
    }

    /// `get(section, option, [])` flattened to a `Vec<String>` (swallows errors → `[]`).
    fn get_str_list(&self, section: &str, option: &str) -> Vec<String> {
        match self.get(section, option, Defaulted::Provided(Value::List(Vec::new()))) {
            Ok(Value::List(items)) => items.iter().map(Value::to_plain_string).collect(),
            Ok(Value::Str(s)) if !s.is_empty() => vec![s],
            _ => Vec::new(),
        }
    }

    pub fn validate(&self, envs: Option<&[String]>, silent: bool) -> Result<()> {
        if !Path::new(&self.path).is_file() {
            let cwd = Path::new(&self.path)
                .parent()
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or_default();
            return Err(ConfigError::NotPlatformIOProject { cwd });
        }
        let known = self.envs();
        if known.is_empty() {
            return Err(ConfigError::ProjectEnvsNotAvailable);
        }
        let known_set: HashSet<&str> = known.iter().map(String::as_str).collect();
        let mut to_check: Vec<String> = envs.unwrap_or(&[]).to_vec();
        to_check.extend(self.default_envs());
        let mut unknown: Vec<String> = Vec::new();
        for e in to_check {
            if !known_set.contains(e.as_str()) && !unknown.contains(&e) {
                unknown.push(e);
            }
        }
        if !unknown.is_empty() {
            return Err(ConfigError::UnknownEnvNames {
                unknown: unknown.join(", "),
                valid: known.join(", "),
            });
        }

        for env in &known {
            if !envname_re().is_match(env) {
                return Err(ConfigError::InvalidEnvName { name: env.clone() });
            }
            let section = format!("env:{env}");
            let raw = self.get(&section, "monitor_raw", Defaulted::Provided(Value::Bool(false)))?;
            let filters =
                self.get(&section, "monitor_filters", Defaulted::Provided(Value::None))?;
            if !raw.is_falsy() && !filters.is_falsy() {
                self.push_warning(format!(
                    "The `monitor_raw` and `monitor_filters` options cannot be used \
                     simultaneously for the `{env}` environment in the `platformio.ini` \
                     file. The `monitor_filters` option will be disabled to avoid conflicts."
                ));
            }
        }

        if !silent {
            for warning in self.warnings.borrow().iter() {
                eprintln!("Warning! {warning}");
            }
        }
        Ok(())
    }

    // ---- write / json / lint ----

    pub fn as_tuple(&self) -> Result<ConfigTuple> {
        let mut out = Vec::new();
        for section in self.parser.sections() {
            out.push((section.clone(), self.items(&section)?));
        }
        Ok(out)
    }

    pub fn to_json(&self) -> Result<String> {
        let tuple = self.as_tuple()?;
        let json: Vec<serde_json::Value> = tuple
            .into_iter()
            .map(|(section, items)| {
                let opts: Vec<serde_json::Value> = items
                    .into_iter()
                    .map(|(k, v)| serde_json::json!([k, value_to_json(&v)]))
                    .collect();
                serde_json::json!([section, opts])
            })
            .collect();
        Ok(serde_json::Value::Array(json).to_string())
    }

    pub fn set(&mut self, section: &str, option: &str, value: SetValue) {
        let mut s = match value {
            SetValue::None => String::new(),
            SetValue::List(items) => items.join("\n"),
            SetValue::Bool(b) => if b { "yes" } else { "no" }.to_string(),
            SetValue::Int(n) => n.to_string(),
            SetValue::Float(x) => x.to_string(),
            SetValue::Str(s) => s,
        };
        if s.contains('\n') && !s.starts_with('\n') {
            s = format!("\n{s}");
        }
        // The section is added by `update`/callers before `set`.
        let _ = self.parser.set(section, option, &s);
    }

    pub fn update(&mut self, data: Vec<(String, Vec<(String, SetValue)>)>, clear: bool) {
        if clear {
            self.parser = Parser::new();
        }
        for (section, options) in data {
            self.parser.add_section(&section);
            for (option, value) in options {
                self.set(&section, &option, value);
            }
        }
    }

    pub fn save(&self, path: Option<&str>) -> Result<()> {
        let target = path.unwrap_or(&self.path);
        let body = format!("{}\n\n{}", CONFIG_HEADER.trim(), self.parser.write_to_string());
        let final_contents = format!("{}\n", body.trim());
        std::fs::write(target, final_contents).map_err(|e| ConfigError::InvalidProjectConf {
            path: target.to_string(),
            detail: e.to_string(),
            parse_errors: None,
        })
    }

    /// `ProjectConfig.lint(path)` — `{errors, warnings}`.
    #[must_use]
    pub fn lint(path: &str) -> LintResult {
        let cfg = match Self::new(path) {
            Ok(cfg) => cfg,
            Err(e) => return lint_from_error(e),
        };
        if let Err(e) = cfg.validate(None, true) {
            let mut res = lint_from_error(e);
            // Warnings accumulated before the failure are still surfaced.
            res.warnings = cfg.warnings();
            return res;
        }
        if let Err(e) = cfg.as_tuple() {
            let mut res = lint_from_error(e);
            res.warnings = cfg.warnings();
            return res;
        }
        LintResult { errors: Vec::new(), warnings: cfg.warnings() }
    }
}

fn lint_from_error(e: ConfigError) -> LintResult {
    if let ConfigError::InvalidProjectConf { parse_errors: Some(perrs), .. } = e {
        let errors = perrs
            .into_iter()
            .map(|(lineno, line)| LintItem {
                type_name: "ParsingError".to_string(),
                message: format!("Parsing error: {line}"),
                lineno: Some(lineno),
            })
            .collect();
        return LintResult { errors, warnings: Vec::new() };
    }
    LintResult {
        errors: vec![LintItem {
            type_name: e.type_name().to_string(),
            message: e.to_string(),
            lineno: None,
        }],
        warnings: Vec::new(),
    }
}

/// `get_section_scope` — the part before the first `:`.
fn get_section_scope(section: &str) -> &str {
    section.split_once(':').map_or(section, |(scope, _)| scope)
}

/// `_re_interpolation_handler` list/`str()` conversion of a nested value.
fn join_value(value: &Value) -> String {
    match value {
        Value::List(items) => {
            items.iter().map(Value::to_plain_string).collect::<Vec<_>>().join("\n")
        }
        Value::None => "None".to_string(),
        Value::Int(n) => n.to_string(),
        Value::Bool(b) => if *b { "True" } else { "False" }.to_string(),
        Value::Str(s) => s.clone(),
    }
}

fn builtin_var(name: &str) -> Option<String> {
    match name {
        "PROJECT_DIR" => Some(cwd()),
        "PROJECT_HASH" => {
            let dir = cwd();
            let mut hasher = Sha1::new();
            hasher.update(dir.as_bytes());
            let hex = hasher.finalize();
            let hex_str: String = hex.iter().map(|b| format!("{b:02x}")).collect();
            let base = Path::new(&dir)
                .file_name()
                .map(|f| f.to_string_lossy().into_owned())
                .unwrap_or_default();
            Some(format!("{base}-{}", &hex_str[..10]))
        }
        "UNIX_TIME" => {
            let secs = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs())
                .unwrap_or(0);
            Some(secs.to_string())
        }
        _ => None,
    }
}

fn cwd() -> String {
    std::env::current_dir().map(|p| p.to_string_lossy().into_owned()).unwrap_or_default()
}

fn value_to_json(value: &Value) -> serde_json::Value {
    match value {
        Value::Str(s) => serde_json::Value::String(s.clone()),
        Value::Int(n) => serde_json::Value::Number((*n).into()),
        Value::Bool(b) => serde_json::Value::Bool(*b),
        Value::None => serde_json::Value::Null,
        Value::List(items) => serde_json::Value::Array(items.iter().map(value_to_json).collect()),
    }
}
