//! Package manifest parsing + schema validation.
//!
//! Port of `platformio/package/manifest/{parser,schema}.py`. The behavioural spec
//! is the vendored `tests/package/test_manifest.py`, mirrored as Rust unit tests.

pub mod parser;
pub mod schema;

pub use parser::{ManifestFileType, ManifestParser, ManifestParserFactory};
pub use schema::ManifestSchema;

#[cfg(test)]
mod tests;
