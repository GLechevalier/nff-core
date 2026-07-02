//! Errors for the package subsystem.
//!
//! Ports `platformio/package/exception.py` plus `platformio/package/version.py`'s
//! `SemanticVersionError`. As in [`crate::config::error`], we hand-roll the enum
//! and reproduce the exact upstream message strings rather than pulling in
//! `thiserror`, so parity tests can match on them.

use std::fmt;

// Note: no `Eq` — the `ManifestValidation` variant carries a `serde_json::Value`,
// which is only `PartialEq` (it can hold floats).
#[derive(Debug, Clone, PartialEq)]
pub enum PackageError {
    /// `platformio.package.version.SemanticVersionError` — raised as
    /// `"Invalid SemVer version %s"` by `cast_version_to_semver(raise_exception=True)`,
    /// and wrapping the low-level `ValueError` from `semantic_version` in the
    /// `PackageSpec.requirements` setter (there the message is the wrapped error).
    SemanticVersion { message: String },

    /// The `ValueError` `semantic_version.SimpleSpec` raises on a malformed
    /// requirement block — `"Invalid simple block %r"`. Surfaced through the
    /// `PackageSpec.requirements` setter as a [`Self::SemanticVersion`].
    InvalidSimpleSpec { block: String },

    /// The `ValueError` raised for an unknown [`crate::package::meta::PackageCompatibility`]
    /// qualifier — `"Unknown package compatibility qualifier -> `%s`"`.
    UnknownCompatibilityQualifier { qualifier: String },

    /// `platformio.package.exception.ManifestParserError`.
    ManifestParser { message: String },

    /// `platformio.package.exception.UnknownManifestError`.
    UnknownManifest { message: String },

    /// `platformio.package.exception.ManifestValidationError`. `messages` is the
    /// marshmallow-style field→errors structure; `valid_data` is the subset that
    /// validated. `Display` reproduces the upstream `__str__`.
    ManifestValidation { messages: serde_json::Value, valid_data: serde_json::Value },

    /// `platformio.package.exception.MissingPackageManifestError` — carries the
    /// comma-joined manifest names the manager looked for.
    MissingPackageManifest { names: String },

    /// `platformio.package.exception.UnknownPackageError`.
    UnknownPackage { spec: String },

    /// `platformio.package.exception.IncompatiblePackageError`.
    IncompatiblePackage { spec: String, system: String },

    /// `platformio.http.HTTPClientError` — carries the HTTP status when known
    /// (so the registry client can turn a 404 into "no such package").
    Http { message: String, status: Option<u16> },

    /// `platformio.package.exception.ManifestException` / generic package error.
    Package { message: String },
}

impl PackageError {
    /// The upstream exception class name (useful when a caller needs to branch on
    /// the Python type, mirroring `crate::config::error::ConfigError::type_name`).
    #[must_use]
    pub fn type_name(&self) -> &'static str {
        match self {
            Self::SemanticVersion { .. } | Self::InvalidSimpleSpec { .. } => "SemanticVersionError",
            Self::UnknownCompatibilityQualifier { .. } => "ValueError",
            Self::ManifestParser { .. } => "ManifestParserError",
            Self::UnknownManifest { .. } => "UnknownManifestError",
            Self::ManifestValidation { .. } => "ManifestValidationError",
            Self::MissingPackageManifest { .. } => "MissingPackageManifestError",
            Self::UnknownPackage { .. } => "UnknownPackageError",
            Self::IncompatiblePackage { .. } => "IncompatiblePackageError",
            Self::Http { .. } => "HTTPClientError",
            Self::Package { .. } => "PackageException",
        }
    }
}

impl fmt::Display for PackageError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            // `SemanticVersionError("Invalid SemVer version %s" % value)`.
            Self::SemanticVersion { message } => write!(f, "{message}"),
            // `semantic_version` renders the offending block with `%r` (repr),
            // i.e. single-quoted; we reproduce that.
            Self::InvalidSimpleSpec { block } => write!(f, "Invalid simple block '{block}'"),
            Self::UnknownCompatibilityQualifier { qualifier } => {
                write!(f, "Unknown package compatibility qualifier -> `{qualifier}`")
            }
            Self::ManifestParser { message } => write!(f, "{message}"),
            Self::UnknownManifest { message } => write!(f, "{message}"),
            // `ManifestValidationError.__str__`.
            Self::ManifestValidation { messages, .. } => write!(
                f,
                "Invalid manifest fields: {messages}. \nPlease check specification -> \
                 https://docs.platformio.org/page/librarymanager/config.html"
            ),
            Self::MissingPackageManifest { names } => {
                write!(f, "Could not find one of '{names}' manifest files in the package")
            }
            Self::UnknownPackage { spec } => {
                write!(f, "Could not find the package with '{spec}' requirements")
            }
            Self::IncompatiblePackage { spec, system } => write!(
                f,
                "Could not find a version of the package with '{spec}' requirements \
                 compatible with the '{system}' system"
            ),
            Self::Http { message, .. } => write!(f, "{message}"),
            Self::Package { message } => write!(f, "{message}"),
        }
    }
}

impl std::error::Error for PackageError {}

/// Crate-local result alias, mirroring [`crate::config::error::Result`].
pub type Result<T> = std::result::Result<T, PackageError>;
