//! Errors for the project-configuration subsystem.
//!
//! Ports `platformio/project/exception.py` (the `ProjectError` hierarchy) plus the
//! `configparser`-level errors (`NoSectionError`, `NoOptionError`, `ParsingError`)
//! that `config.py` lets propagate and the lint path inspects. The `Display`
//! impls reproduce the exact upstream message strings the tests match on.

use std::fmt;

/// A single parse-failure line, mirroring `configparser.ParsingError.errors`
/// entries of `(lineno, line)`.
pub type ParseErrors = Vec<(usize, String)>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConfigError {
    /// `configparser.NoSectionError`.
    NoSection { section: String },
    /// `configparser.NoOptionError`.
    NoOption { section: String, option: String },
    /// `configparser.ParsingError` — carries the per-line `(lineno, line)` pairs.
    Parsing { source: String, errors: ParseErrors },

    /// `InvalidProjectConfError` — `"Invalid '{0}' (project configuration file): '{1}'"`.
    /// When it wraps a parsing failure, `parse_errors` carries the underlying
    /// `(lineno, line)` pairs so [`crate::config::ProjectConfig::lint`] can emit
    /// one error per line (mirrors Python unwrapping `exc.__cause__`).
    InvalidProjectConf {
        path: String,
        detail: String,
        parse_errors: Option<ParseErrors>,
    },
    /// `NotPlatformIOProjectError`.
    NotPlatformIOProject { cwd: String },
    /// `ProjectEnvsNotAvailableError`.
    ProjectEnvsNotAvailable,
    /// `UnknownEnvNamesError` — `"Unknown environment names '{0}'. Valid names are '{1}'"`.
    UnknownEnvNames { unknown: String, valid: String },
    /// `InvalidEnvNameError`.
    InvalidEnvName { name: String },
    /// `ProjectOptionValueError` — raised with an explicit message (no template).
    ProjectOptionValue { message: String },
}

impl ConfigError {
    /// The class name Python's `lint()` records as `error["type"]`.
    #[must_use]
    pub fn type_name(&self) -> &'static str {
        match self {
            Self::NoSection { .. } => "NoSectionError",
            Self::NoOption { .. } => "NoOptionError",
            Self::Parsing { .. } => "ParsingError",
            Self::InvalidProjectConf { .. } => "InvalidProjectConfError",
            Self::NotPlatformIOProject { .. } => "NotPlatformIOProjectError",
            Self::ProjectEnvsNotAvailable => "ProjectEnvsNotAvailableError",
            Self::UnknownEnvNames { .. } => "UnknownEnvNamesError",
            Self::InvalidEnvName { .. } => "InvalidEnvNameError",
            Self::ProjectOptionValue { .. } => "ProjectOptionValueError",
        }
    }

    /// True for the low-level parser errors that `config.py` catches as
    /// `configparser.Error` (so `get()` rewraps them as `InvalidProjectConf`).
    #[must_use]
    pub fn is_configparser_error(&self) -> bool {
        matches!(self, Self::NoSection { .. } | Self::NoOption { .. } | Self::Parsing { .. })
    }
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoSection { section } => {
                write!(f, "No section: '{section}'")
            }
            Self::NoOption { section, option } => {
                write!(f, "No option '{option}' in section: '{section}'")
            }
            Self::Parsing { source, .. } => {
                write!(f, "Source contains parsing errors: '{source}'")
            }
            Self::InvalidProjectConf { path, detail, .. } => {
                write!(f, "Invalid '{path}' (project configuration file): '{detail}'")
            }
            Self::NotPlatformIOProject { cwd } => write!(
                f,
                "Not a PlatformIO project. `platformio.ini` file has not been \
                 found in current working directory ({cwd}). To initialize new project \
                 please use `platformio project init` command"
            ),
            Self::ProjectEnvsNotAvailable => {
                write!(f, "Please setup environments in `platformio.ini` file")
            }
            Self::UnknownEnvNames { unknown, valid } => {
                write!(f, "Unknown environment names '{unknown}'. Valid names are '{valid}'")
            }
            Self::InvalidEnvName { name } => write!(
                f,
                "Invalid environment name '{name}'. The name can contain \
                 alphanumeric, underscore, and hyphen characters (a-z, 0-9, -, _)"
            ),
            Self::ProjectOptionValue { message } => write!(f, "{message}"),
        }
    }
}

impl std::error::Error for ConfigError {}

pub type Result<T> = std::result::Result<T, ConfigError>;
