//! Port of `platformio/package/version.py` plus the slice of the
//! `python-semanticversion` library (`semantic_version/base.py`) that PlatformIO
//! relies on: the `Version` type and the `SimpleSpec` range grammar.
//!
//! We hand-roll both rather than leaning on the Rust `semver` crate because
//! `SimpleSpec`'s range semantics (the `^`/`~`/`~=`/`!=` expansions, the
//! prerelease/build match policies, and `Version.coerce`) differ from Cargo's
//! `VersionReq`. Reproducing them exactly is the parity requirement behind
//! `tests/package/test_meta.py::test_spec_requirements`.
//!
//! Scope note: PlatformIO only ever builds *non-partial* `Version`s (the
//! `SimpleSpec` parser constructs full `major.minor.patch` targets), so the
//! `partial=True` code path from upstream is intentionally omitted.

use std::cmp::Ordering;
use std::fmt;
use std::sync::OnceLock;

use regex::Regex;

use crate::package::error::{PackageError, Result};

// ---------------------------------------------------------------------------
// Version
// ---------------------------------------------------------------------------

/// A semantic version — `semantic_version.Version` (non-partial).
///
/// Equality mirrors Python's `Version.__eq__`: raw comparison of every field,
/// *including* `build`. Ordering for *matching* (`<`, `>`, …) uses semver
/// precedence and ignores `build` — see [`Version::precedence_cmp`]. The derived
/// [`Ord`] here is a separate, total, canonical order used only to keep
/// [`SimpleSpec`] clauses in a deterministic shape for structural equality; it is
/// **not** semver precedence.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Version {
    pub major: u64,
    pub minor: u64,
    pub patch: u64,
    pub prerelease: Vec<String>,
    pub build: Vec<String>,
}

/// A prerelease/build identifier, ordered `Numeric < Alpha < Max` exactly as
/// `semantic_version`'s `NumericIdentifier`/`AlphaIdentifier`/`MaxIdentifier`.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
enum Identifier {
    Num(u64),
    Alpha(String),
    /// Sentinel that sorts above everything: a version with *no* prerelease has
    /// higher precedence than one that has any (`1.0.0 > 1.0.0-alpha`).
    Max,
}

fn full_version_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9a-zA-Z.-]+))?(?:\+([0-9a-zA-Z.-]+))?$").unwrap()
    })
}

fn coerce_base_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^\d+(?:\.\d+(?:\.\d+)?)?").unwrap())
}

fn commit_hash_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?i)^[\da-f]+$").unwrap())
}

/// `semantic_version.base._has_leading_zero`.
fn has_leading_zero(value: &str) -> bool {
    value.len() > 1 && value.starts_with('0') && value.bytes().all(|b| b.is_ascii_digit())
}

impl Version {
    /// `Version.parse` (non-partial) — rejects leading zeros and validates the
    /// prerelease/build identifiers.
    pub fn parse(s: &str) -> Result<Self> {
        let caps = full_version_re()
            .captures(s)
            .ok_or_else(|| PackageError::SemanticVersion {
                message: format!("Invalid version string: '{s}'"),
            })?;
        let major_s = caps.get(1).unwrap().as_str();
        let minor_s = caps.get(2).unwrap().as_str();
        let patch_s = caps.get(3).unwrap().as_str();
        for part in [major_s, minor_s, patch_s] {
            if has_leading_zero(part) {
                return Err(PackageError::SemanticVersion {
                    message: format!("Invalid leading zero in '{s}'"),
                });
            }
        }
        let to_u64 = |v: &str| -> Result<u64> {
            v.parse::<u64>().map_err(|_| PackageError::SemanticVersion {
                message: format!("Invalid version string: '{s}'"),
            })
        };
        let prerelease = match caps.get(4) {
            Some(m) => split_identifiers(m.as_str(), false)?,
            None => Vec::new(),
        };
        let build = match caps.get(5) {
            Some(m) => split_identifiers(m.as_str(), true)?,
            None => Vec::new(),
        };
        Ok(Self {
            major: to_u64(major_s)?,
            minor: to_u64(minor_s)?,
            patch: to_u64(patch_s)?,
            prerelease,
            build,
        })
    }

    /// Construct directly from parts (used by the [`SimpleSpec`] parser).
    fn from_parts(major: u64, minor: u64, patch: u64) -> Self {
        Self { major, minor, patch, prerelease: Vec::new(), build: Vec::new() }
    }

    /// `Version.coerce` — best-effort mapping of an arbitrary string into a
    /// valid non-partial version (pad missing components, spill extras into
    /// build metadata).
    pub fn coerce(s: &str) -> Result<Self> {
        let m = coerce_base_re()
            .find(s)
            .ok_or_else(|| PackageError::SemanticVersion {
                message: format!("Version string lacks a numerical component: '{s}'"),
            })?;
        let end = m.end();
        let mut version: String = s[..end].to_string();
        while version.matches('.').count() < 2 {
            version.push_str(".0");
        }
        // Strip leading zeros in each numeric component.
        version = version
            .split('.')
            .map(|part| {
                let stripped = part.trim_start_matches('0');
                if stripped.is_empty() { "0".to_string() } else { stripped.to_string() }
            })
            .collect::<Vec<_>>()
            .join(".");

        if end == s.len() {
            return Version::parse(&version);
        }

        let raw_rest = &s[end..];
        // Cleanup: replace anything outside [A-Za-z0-9+.-] with '-'.
        let rest: String = raw_rest
            .chars()
            .map(|c| if c.is_ascii_alphanumeric() || matches!(c, '+' | '.' | '-') { c } else { '-' })
            .collect();

        let first = rest.chars().next().unwrap();
        // A leading '+' is build metadata; a leading '.' is an extra numeric
        // component treated as build — both spill into `build`.
        let (prerelease, build): (String, String) = if first == '+' || first == '.' {
            (String::new(), rest[1..].to_string())
        } else if first == '-' {
            let tail = &rest[1..];
            match tail.split_once('+') {
                Some((pre, bld)) => (pre.to_string(), bld.to_string()),
                None => (tail.to_string(), String::new()),
            }
        } else if let Some((pre, bld)) = rest.split_once('+') {
            (pre.to_string(), bld.to_string())
        } else {
            (rest.clone(), String::new())
        };
        let build = build.replace('+', ".");

        if !prerelease.is_empty() {
            version = format!("{version}-{prerelease}");
        }
        if !build.is_empty() {
            version = format!("{version}+{build}");
        }
        Version::parse(&version)
    }

    /// `Version.next_major`.
    fn next_major(&self) -> Version {
        if !self.prerelease.is_empty() && self.minor == 0 && self.patch == 0 {
            Version::from_parts(self.major, 0, 0)
        } else {
            Version::from_parts(self.major + 1, 0, 0)
        }
    }

    /// `Version.next_minor`.
    fn next_minor(&self) -> Version {
        if !self.prerelease.is_empty() && self.patch == 0 {
            Version::from_parts(self.major, self.minor, 0)
        } else {
            Version::from_parts(self.major, self.minor + 1, 0)
        }
    }

    /// `Version.next_patch`.
    fn next_patch(&self) -> Version {
        if !self.prerelease.is_empty() {
            Version::from_parts(self.major, self.minor, self.patch)
        } else {
            Version::from_parts(self.major, self.minor, self.patch + 1)
        }
    }

    /// `Version.truncate('prerelease')` — keep prerelease, drop build.
    fn truncate_prerelease(&self) -> Version {
        Version {
            major: self.major,
            minor: self.minor,
            patch: self.patch,
            prerelease: self.prerelease.clone(),
            build: Vec::new(),
        }
    }

    /// `Version.truncate('patch')` (the default) — drop prerelease and build.
    fn truncate_patch(&self) -> Version {
        Version::from_parts(self.major, self.minor, self.patch)
    }

    fn identifier_key(parts: &[String]) -> Vec<Identifier> {
        parts
            .iter()
            .map(|p| {
                if !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()) {
                    Identifier::Num(p.parse().unwrap_or(0))
                } else {
                    Identifier::Alpha(p.clone())
                }
            })
            .collect()
    }

    fn prerelease_key(&self) -> Vec<Identifier> {
        if self.prerelease.is_empty() {
            vec![Identifier::Max]
        } else {
            Self::identifier_key(&self.prerelease)
        }
    }

    /// Semver *precedence* comparison (`Version.__lt__`/`__gt__`/…). `with_build`
    /// mirrors `_build_precedence_key(with_build=...)`: matching (`<`, `>`) uses
    /// `with_build = false`; sorting uses `true`.
    #[must_use]
    pub fn precedence_cmp(&self, other: &Version, with_build: bool) -> Ordering {
        (self.major, self.minor, self.patch)
            .cmp(&(other.major, other.minor, other.patch))
            .then_with(|| self.prerelease_key().cmp(&other.prerelease_key()))
            .then_with(|| {
                if with_build {
                    Self::identifier_key(&self.build).cmp(&Self::identifier_key(&other.build))
                } else {
                    Ordering::Equal
                }
            })
    }

    /// A deterministic *total* order for canonicalising clauses (raw lexicographic
    /// over all fields). Distinct from [`Version::precedence_cmp`].
    fn canonical_cmp(&self, other: &Version) -> Ordering {
        (self.major, self.minor, self.patch)
            .cmp(&(other.major, other.minor, other.patch))
            .then_with(|| self.prerelease.cmp(&other.prerelease))
            .then_with(|| self.build.cmp(&other.build))
    }
}

impl fmt::Display for Version {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}.{}.{}", self.major, self.minor, self.patch)?;
        if !self.prerelease.is_empty() {
            write!(f, "-{}", self.prerelease.join("."))?;
        }
        if !self.build.is_empty() {
            write!(f, "+{}", self.build.join("."))?;
        }
        Ok(())
    }
}

/// `Version._validate_identifiers` combined with the `.split('.')` in `parse`.
fn split_identifiers(s: &str, allow_leading_zeroes: bool) -> Result<Vec<String>> {
    // An empty capture (`1.2.3-`) means "no identifiers" (Python maps `''` to `()`).
    if s.is_empty() {
        return Ok(Vec::new());
    }
    let parts: Vec<String> = s.split('.').map(str::to_string).collect();
    for item in &parts {
        if item.is_empty() {
            return Err(PackageError::SemanticVersion {
                message: format!("Invalid empty identifier '{item}' in '{s}'"),
            });
        }
        if !allow_leading_zeroes && has_leading_zero(item) {
            return Err(PackageError::SemanticVersion {
                message: format!("Invalid leading zero in identifier '{item}'"),
            });
        }
    }
    Ok(parts)
}

// ---------------------------------------------------------------------------
// cast_version_to_semver / helpers (version.py)
// ---------------------------------------------------------------------------

/// `platformio.package.version.cast_version_to_semver`.
pub fn cast_version_to_semver(value: &str, force: bool, raise_exception: bool) -> Result<Version> {
    debug_assert!(!value.is_empty());
    if let Ok(v) = Version::parse(value) {
        return Ok(v);
    }
    if force {
        if let Ok(v) = Version::coerce(value) {
            return Ok(v);
        }
    }
    if raise_exception {
        return Err(PackageError::SemanticVersion {
            message: format!("Invalid SemVer version {value}"),
        });
    }
    if commit_hash_re().is_match(value) {
        return Version::parse(&format!("0.0.0+sha.{value}"));
    }
    Version::parse(&format!("0.0.0+{value}"))
}

/// `platformio.package.version.cast_version_to_semver` with the default
/// arguments (`force=True, raise_exception=False`) — the common call site.
pub fn cast_version_to_semver_default(value: &str) -> Result<Version> {
    cast_version_to_semver(value, true, false)
}

/// `platformio.package.version.pepver_to_semver`.
pub fn pepver_to_semver(pepver: &str) -> Result<Version> {
    static RE: OnceLock<Regex> = OnceLock::new();
    let re = RE.get_or_init(|| Regex::new(r"(\.\d+)\.?(dev|a|b|rc|post)").unwrap());
    // `count=1`: only the first occurrence is rewritten.
    let replaced = re.replace(pepver, "$1-$2.");
    cast_version_to_semver_default(&replaced)
}

/// `platformio.package.version.get_original_version`.
#[must_use]
pub fn get_original_version(version: &str) -> Option<String> {
    if version.matches('.').count() != 2 {
        return None;
    }
    let raw = version.split('.').nth(1)?;
    let n: i64 = raw.parse().ok()?;
    if n <= 99 {
        return None;
    }
    if n <= 9999 {
        let head = &raw[..raw.len() - 2];
        let tail: i64 = raw[raw.len() - 2..].parse().ok()?;
        return Some(format!("{head}.{tail}"));
    }
    let head = &raw[..raw.len() - 4];
    let mid: i64 = raw[raw.len() - 4..raw.len() - 2].parse().ok()?;
    let tail: i64 = raw[raw.len() - 2..].parse().ok()?;
    Some(format!("{head}.{mid}.{tail}"))
}

// ---------------------------------------------------------------------------
// SimpleSpec (semantic_version.base.SimpleSpec, 'simple' syntax)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RangeOp {
    Eq,
    Neq,
    Lt,
    Lte,
    Gt,
    Gte,
}

impl RangeOp {
    fn rank(self) -> u8 {
        match self {
            Self::Eq => 0,
            Self::Neq => 1,
            Self::Lt => 2,
            Self::Lte => 3,
            Self::Gt => 4,
            Self::Gte => 5,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PrereleasePolicy {
    Natural,
    Always,
    SamePatch,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum BuildPolicy {
    Implicit,
    Strict,
}

/// `semantic_version.base.Range`. `build_policy` is excluded from equality and
/// ordering to match Python's `Range.__eq__`/`__hash__` (which key on operator,
/// target, and `prerelease_policy` only).
#[derive(Debug, Clone)]
struct Range {
    op: RangeOp,
    target: Version,
    prerelease_policy: PrereleasePolicy,
    build_policy: BuildPolicy,
}

impl Range {
    fn new(op: RangeOp, target: Version) -> Self {
        Self::with_policies(op, target, PrereleasePolicy::Natural, BuildPolicy::Implicit)
    }

    fn with_policies(
        op: RangeOp,
        target: Version,
        prerelease_policy: PrereleasePolicy,
        build_policy: BuildPolicy,
    ) -> Self {
        // Range.__init__: build numbers force strict build matching.
        let build_policy = if !target.build.is_empty() { BuildPolicy::Strict } else { build_policy };
        Self { op, target, prerelease_policy, build_policy }
    }

    fn matches(&self, version: &Version) -> bool {
        let mut v = version.clone();
        if self.build_policy != BuildPolicy::Strict {
            v = v.truncate_prerelease();
        }
        if !v.prerelease.is_empty() {
            let same_patch = self.target.truncate_patch() == v.truncate_patch();
            if self.prerelease_policy == PrereleasePolicy::SamePatch && !same_patch {
                return false;
            }
        }
        match self.op {
            RangeOp::Eq => {
                if self.build_policy == BuildPolicy::Strict {
                    self.target.truncate_prerelease() == v.truncate_prerelease()
                        && v.build == self.target.build
                } else {
                    v == self.target
                }
            }
            RangeOp::Gt => v.precedence_cmp(&self.target, false) == Ordering::Greater,
            RangeOp::Gte => v.precedence_cmp(&self.target, false) != Ordering::Less,
            RangeOp::Lt => {
                if !v.prerelease.is_empty()
                    && self.prerelease_policy == PrereleasePolicy::Natural
                    && v.truncate_patch() == self.target.truncate_patch()
                    && self.target.prerelease.is_empty()
                {
                    return false;
                }
                v.precedence_cmp(&self.target, false) == Ordering::Less
            }
            RangeOp::Lte => v.precedence_cmp(&self.target, false) != Ordering::Greater,
            RangeOp::Neq => {
                if self.build_policy == BuildPolicy::Strict {
                    !(self.target.truncate_prerelease() == v.truncate_prerelease()
                        && v.build == self.target.build)
                } else {
                    if !v.prerelease.is_empty()
                        && self.prerelease_policy == PrereleasePolicy::Natural
                        && v.truncate_patch() == self.target.truncate_patch()
                        && self.target.prerelease.is_empty()
                    {
                        return false;
                    }
                    v != self.target
                }
            }
        }
    }

    fn eq_key(&self) -> impl Ord + '_ {
        // (operator, target-canonical, prerelease_policy) — build_policy excluded.
        (self.op.rank(), VersionKey(&self.target), self.prerelease_policy as u8)
    }
}

/// Wraps a `&Version` so it can be compared with the canonical (non-precedence)
/// order inside clause keys.
struct VersionKey<'a>(&'a Version);

impl PartialEq for VersionKey<'_> {
    fn eq(&self, other: &Self) -> bool {
        self.0 == other.0
    }
}
impl Eq for VersionKey<'_> {}
impl PartialOrd for VersionKey<'_> {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for VersionKey<'_> {
    fn cmp(&self, other: &Self) -> Ordering {
        self.0.canonical_cmp(other.0)
    }
}

impl PartialEq for Range {
    fn eq(&self, other: &Self) -> bool {
        self.op == other.op
            && self.target == other.target
            && self.prerelease_policy == other.prerelease_policy
    }
}
impl Eq for Range {}
impl PartialOrd for Range {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for Range {
    fn cmp(&self, other: &Self) -> Ordering {
        self.eq_key().cmp(&other.eq_key())
    }
}

/// `semantic_version.base.Clause` (the `AnyOf(AllOf(...))` matcher tree). `AllOf`
/// and `AnyOf` children are kept sorted+deduped so structural equality matches
/// Python's `frozenset`-based `Clause.__eq__`.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Clause {
    Always,
    Never,
    Range(Range),
    AllOf(Vec<Clause>),
    AnyOf(Vec<Clause>),
}

impl Clause {
    fn discriminant(&self) -> u8 {
        match self {
            Self::Always => 0,
            Self::Never => 1,
            Self::Range(_) => 2,
            Self::AllOf(_) => 3,
            Self::AnyOf(_) => 4,
        }
    }

    fn all_of(mut clauses: Vec<Clause>) -> Clause {
        clauses.sort();
        clauses.dedup();
        if clauses.len() == 1 {
            clauses.pop().unwrap()
        } else {
            Clause::AllOf(clauses)
        }
    }

    fn any_of(mut clauses: Vec<Clause>) -> Clause {
        clauses.sort();
        clauses.dedup();
        if clauses.len() == 1 {
            clauses.pop().unwrap()
        } else {
            Clause::AnyOf(clauses)
        }
    }

    /// `Clause.__and__` (only the combinations the `SimpleSpec` parser produces).
    fn and(self, other: Clause) -> Clause {
        match (self, other) {
            (Clause::Always, x) | (x, Clause::Always) => x,
            (Clause::Never, _) | (_, Clause::Never) => Clause::Never,
            (a, b) => {
                let mut v = Vec::new();
                match a {
                    Clause::AllOf(xs) => v.extend(xs),
                    x => v.push(x),
                }
                match b {
                    Clause::AllOf(xs) => v.extend(xs),
                    x => v.push(x),
                }
                Clause::all_of(v)
            }
        }
    }

    /// `Clause.__or__` (only the combinations the `SimpleSpec` parser produces).
    fn or(self, other: Clause) -> Clause {
        match (self, other) {
            (Clause::Always, _) | (_, Clause::Always) => Clause::Always,
            (Clause::Never, x) | (x, Clause::Never) => x,
            (a, b) => {
                let mut v = Vec::new();
                match a {
                    Clause::AnyOf(xs) => v.extend(xs),
                    x => v.push(x),
                }
                match b {
                    Clause::AnyOf(xs) => v.extend(xs),
                    x => v.push(x),
                }
                Clause::any_of(v)
            }
        }
    }

    fn matches(&self, version: &Version) -> bool {
        match self {
            Clause::Always => true,
            Clause::Never => false,
            Clause::Range(r) => r.matches(version),
            Clause::AllOf(cs) => cs.iter().all(|c| c.matches(version)),
            Clause::AnyOf(cs) => cs.iter().any(|c| c.matches(version)),
        }
    }
}

impl PartialOrd for Clause {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for Clause {
    fn cmp(&self, other: &Self) -> Ordering {
        match (self, other) {
            (Clause::Range(a), Clause::Range(b)) => a.cmp(b),
            (Clause::AllOf(a), Clause::AllOf(b)) | (Clause::AnyOf(a), Clause::AnyOf(b)) => a.cmp(b),
            _ => self.discriminant().cmp(&other.discriminant()),
        }
    }
}

/// `semantic_version.SimpleSpec` — a version range with the `simple` syntax.
///
/// [`Display`] returns the original expression (mirrors `BaseSpec.__str__`, which
/// returns `self.expression`), so `PackageSpec.as_dict()` round-trips exactly.
/// Equality is *structural* over the parsed clause (`BaseSpec.__eq__`).
#[derive(Debug, Clone)]
pub struct SimpleSpec {
    expression: String,
    clause: Clause,
}

impl SimpleSpec {
    /// `SimpleSpec(expression)` — parse and validate.
    pub fn parse(expression: &str) -> Result<Self> {
        let clause = SimpleSpec::parse_to_clause(expression)?;
        Ok(Self { expression: expression.to_string(), clause })
    }

    /// `SimpleSpec.__contains__` — whether `version` satisfies the spec.
    #[must_use]
    pub fn contains(&self, version: &Version) -> bool {
        self.clause.matches(version)
    }

    fn parse_to_clause(expression: &str) -> Result<Clause> {
        // Parser.parse: split on ',', AND the blocks together.
        let mut clause = Clause::Always;
        for block in expression.split(',') {
            clause = clause.and(SimpleSpec::parse_block(block)?);
        }
        Ok(clause)
    }

    fn naive_spec_re() -> &'static Regex {
        static RE: OnceLock<Regex> = OnceLock::new();
        RE.get_or_init(|| {
            // NUMBER = \*|0|[1-9][0-9]* ; op longest-first with a trailing empty
            // alternative (matches Python's `<|<=||=|==|>=|>|!=|\^|~|~=`).
            Regex::new(concat!(
                r"^(<=|>=|==|!=|~=|<|>|\^|~|=|)",
                r"(\*|0|[1-9][0-9]*)",
                r"(?:\.(\*|0|[1-9][0-9]*)(?:\.(\*|0|[1-9][0-9]*))?)?",
                r"(?:-([0-9a-zA-Z.-]*))?",
                r"(?:\+([0-9a-zA-Z.-]*))?$"
            ))
            .unwrap()
        })
    }

    #[allow(clippy::too_many_lines)]
    fn parse_block(expr: &str) -> Result<Clause> {
        let caps =
            SimpleSpec::naive_spec_re()
                .captures(expr)
                .ok_or_else(|| PackageError::InvalidSimpleSpec { block: expr.to_string() })?;

        // PREFIX_ALIASES: '=' and '' both mean '=='.
        let raw_op = caps.get(1).map_or("", |m| m.as_str());
        let prefix = match raw_op {
            "" | "=" => "==",
            other => other,
        };

        let is_empty_value = |m: Option<regex::Match<'_>>| -> Option<u64> {
            match m.map(|x| x.as_str()) {
                None | Some("*") => None,
                Some(n) => Some(n.parse().unwrap()),
            }
        };
        let major = is_empty_value(caps.get(2));
        let minor = is_empty_value(caps.get(3));
        let patch = is_empty_value(caps.get(4));
        // Distinguish "absent" (None) from "present but empty" (Some("")).
        let prerel: Option<&str> = caps.get(5).map(|m| m.as_str());
        let build: Option<&str> = caps.get(6).map(|m| m.as_str());

        let invalid = || PackageError::InvalidSimpleSpec { block: expr.to_string() };

        let target = match (major, minor, patch) {
            // '*' — only valid with `==`/`>=`.
            (None, _, _) => {
                if prefix != "==" && prefix != ">=" {
                    return Err(invalid());
                }
                Version::from_parts(0, 0, 0)
            }
            (Some(maj), None, _) => Version::from_parts(maj, 0, 0),
            (Some(maj), Some(min), None) => Version::from_parts(maj, min, 0),
            (Some(maj), Some(min), Some(pat)) => Version {
                major: maj,
                minor: min,
                patch: pat,
                prerelease: match prerel {
                    Some(p) if !p.is_empty() => p.split('.').map(str::to_string).collect(),
                    _ => Vec::new(),
                },
                build: match build {
                    Some(b) if !b.is_empty() => b.split('.').map(str::to_string).collect(),
                    _ => Vec::new(),
                },
            },
        };

        let has_prerel = prerel.is_some_and(|p| !p.is_empty());
        let has_build = build.is_some_and(|b| !b.is_empty());
        if (major.is_none() || minor.is_none() || patch.is_none()) && (has_prerel || has_build) {
            return Err(invalid());
        }
        if build.is_some() && prefix != "==" && prefix != "!=" {
            return Err(invalid());
        }

        let clause = match prefix {
            "^" => {
                let high = if target.major != 0 {
                    target.next_major()
                } else if target.minor != 0 {
                    target.next_minor()
                } else {
                    target.next_patch()
                };
                Clause::Range(Range::new(RangeOp::Gte, target))
                    .and(Clause::Range(Range::new(RangeOp::Lt, high)))
            }
            "~" => {
                let high = if minor.is_none() { target.next_major() } else { target.next_minor() };
                Clause::Range(Range::new(RangeOp::Gte, target))
                    .and(Clause::Range(Range::new(RangeOp::Lt, high)))
            }
            "~=" => {
                let high = if minor.is_none() || patch.is_none() {
                    target.next_major()
                } else {
                    target.next_minor()
                };
                Clause::Range(Range::new(RangeOp::Gte, target))
                    .and(Clause::Range(Range::new(RangeOp::Lt, high)))
            }
            "==" => {
                if major.is_none() {
                    Clause::Range(Range::new(RangeOp::Gte, target))
                } else if minor.is_none() {
                    let high = target.next_major();
                    Clause::Range(Range::new(RangeOp::Gte, target))
                        .and(Clause::Range(Range::new(RangeOp::Lt, high)))
                } else if patch.is_none() {
                    let high = target.next_minor();
                    Clause::Range(Range::new(RangeOp::Gte, target))
                        .and(Clause::Range(Range::new(RangeOp::Lt, high)))
                } else if build == Some("") {
                    Clause::Range(Range::with_policies(
                        RangeOp::Eq,
                        target,
                        PrereleasePolicy::Natural,
                        BuildPolicy::Strict,
                    ))
                } else {
                    Clause::Range(Range::new(RangeOp::Eq, target))
                }
            }
            "!=" => {
                if minor.is_none() {
                    let high = target.next_major();
                    Clause::Range(Range::new(RangeOp::Lt, target))
                        .or(Clause::Range(Range::new(RangeOp::Gte, high)))
                } else if patch.is_none() {
                    let high = target.next_minor();
                    Clause::Range(Range::new(RangeOp::Lt, target))
                        .or(Clause::Range(Range::new(RangeOp::Gte, high)))
                } else if prerel == Some("") {
                    Clause::Range(Range::with_policies(
                        RangeOp::Neq,
                        target,
                        PrereleasePolicy::Always,
                        BuildPolicy::Implicit,
                    ))
                } else if build == Some("") {
                    Clause::Range(Range::with_policies(
                        RangeOp::Neq,
                        target,
                        PrereleasePolicy::Natural,
                        BuildPolicy::Strict,
                    ))
                } else {
                    Clause::Range(Range::new(RangeOp::Neq, target))
                }
            }
            ">" => {
                if minor.is_none() {
                    Clause::Range(Range::new(RangeOp::Gte, target.next_major()))
                } else if patch.is_none() {
                    Clause::Range(Range::new(RangeOp::Gte, target.next_minor()))
                } else {
                    Clause::Range(Range::new(RangeOp::Gt, target))
                }
            }
            ">=" => Clause::Range(Range::new(RangeOp::Gte, target)),
            "<" => {
                if prerel == Some("") {
                    Clause::Range(Range::with_policies(
                        RangeOp::Lt,
                        target,
                        PrereleasePolicy::Always,
                        BuildPolicy::Implicit,
                    ))
                } else {
                    Clause::Range(Range::new(RangeOp::Lt, target))
                }
            }
            "<=" => {
                if minor.is_none() {
                    Clause::Range(Range::new(RangeOp::Lt, target.next_major()))
                } else if patch.is_none() {
                    Clause::Range(Range::new(RangeOp::Lt, target.next_minor()))
                } else {
                    Clause::Range(Range::new(RangeOp::Lte, target))
                }
            }
            _ => return Err(invalid()),
        };
        Ok(clause)
    }
}

impl fmt::Display for SimpleSpec {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.expression)
    }
}

impl PartialEq for SimpleSpec {
    fn eq(&self, other: &Self) -> bool {
        self.clause == other.clause
    }
}
impl Eq for SimpleSpec {}

#[cfg(test)]
mod version_tests {
    use super::*;

    #[test]
    fn coerce_pads_and_spills_build() {
        assert_eq!(Version::coerce("0.1").unwrap(), Version::parse("0.1.0").unwrap());
        assert_eq!(Version::coerce("0.1.2.3").unwrap(), Version::parse("0.1.2+3").unwrap());
    }

    #[test]
    fn cast_commit_hash_and_fallback() {
        assert_eq!(
            cast_version_to_semver_default("abcdef").unwrap(),
            Version::parse("0.0.0+sha.abcdef").unwrap()
        );
        // Non-hex, non-semver → 0.0.0+<value>.
        assert_eq!(
            cast_version_to_semver_default("zzz").unwrap(),
            Version::parse("0.0.0+zzz").unwrap()
        );
    }

    #[test]
    fn simplespec_membership_and_display() {
        let s = SimpleSpec::parse("!=1.2.3,<2.0").unwrap();
        assert!(s.contains(&Version::parse("1.3.0-beta.1").unwrap()));
        assert_eq!(s.to_string(), "!=1.2.3,<2.0");
        // Structural equality is independent of the source expression object.
        assert_eq!(SimpleSpec::parse("~1.2.3").unwrap(), SimpleSpec::parse("~1.2.3").unwrap());
    }
}
