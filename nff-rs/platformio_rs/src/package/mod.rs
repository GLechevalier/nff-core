//! Package subsystem: spec parsing, semver, metadata, and (in later M2 steps)
//! the manifest parser, packer, registry client, and download/extract/cache.
//!
//! Port of `platformio/package/*` + the `semantic_version` slice PlatformIO
//! depends on. M2a covers `meta.py` + `version.py`; the behavioural spec is the
//! vendored `tests/package/test_meta.py`, mirrored as Rust unit tests in
//! [`tests`].

pub mod error;
pub mod manifest;
pub mod meta;
pub mod version;

pub use error::{PackageError, Result};
pub use meta::{
    PackageCompatibility, PackageItem, PackageMetadata, PackageOutdatedResult, PackageSpec,
    PackageType, Qualifier,
};
pub use version::{
    cast_version_to_semver, cast_version_to_semver_default, get_original_version, pepver_to_semver,
    SimpleSpec, Version,
};

#[cfg(test)]
mod tests;
