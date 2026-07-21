//! Project configuration: `platformio.ini` parsing, `${...}` interpolation, the
//! option registry, typed casting, validation, and the write/lint path.
//!
//! Port of `platformio/project/config.py` + `options.py` (M1). The behavioural
//! spec is the vendored `tests/project/test_config.py`, mirrored as Rust unit
//! tests in [`tests`].

pub mod error;
pub mod ini;
pub mod options;
pub mod project_config;
pub mod value;

pub use error::{ConfigError, Result};
pub use project_config::{LintItem, LintResult, ProjectConfig, SetValue};
pub use value::{Defaulted, Value};

#[cfg(test)]
mod tests;
