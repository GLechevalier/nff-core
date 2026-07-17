//! The dynamic value system the heterogeneous getters need.
//!
//! PlatformIO's `get()`/`items()`/`as_tuple()` return a Python-dynamic mix of
//! `str`, `int`, `bool`, `list`, and `None`. [`Value`] models that closed set.
//! [`Defaulted`] reproduces the three-state `MISSING` sentinel semantics
//! (`not-passed` vs `default=None` vs option-default).

use std::sync::OnceLock;

use regex::Regex;

/// A configuration value, mirroring the Python types `get()` can return.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Value {
    Str(String),
    Int(i64),
    Bool(bool),
    List(Vec<Value>),
    /// Python `None`.
    None,
}

impl Value {
    #[must_use]
    pub fn str(s: impl Into<String>) -> Self {
        Self::Str(s.into())
    }

    #[must_use]
    pub fn list_of_str<I, S>(items: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self::List(items.into_iter().map(|s| Self::Str(s.into())).collect())
    }

    /// Python truthiness, as used by `parse_multi_values`/`_expand_interpolations`
    /// (`if not value`).
    #[must_use]
    pub fn is_falsy(&self) -> bool {
        match self {
            Self::None => true,
            Self::Str(s) => s.is_empty(),
            Self::Int(n) => *n == 0,
            Self::Bool(b) => !*b,
            Self::List(items) => items.is_empty(),
        }
    }

    #[must_use]
    pub fn is_none(&self) -> bool {
        matches!(self, Self::None)
    }

    #[must_use]
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::Str(s) => Some(s),
            _ => None,
        }
    }

    /// The string form used when an item flows into `parse_multi_values` (where
    /// list items are already strings in practice).
    #[must_use]
    pub fn to_plain_string(&self) -> String {
        match self {
            Self::Str(s) => s.clone(),
            Self::Int(n) => n.to_string(),
            Self::Bool(b) => if *b { "True" } else { "False" }.to_string(),
            Self::None => String::new(),
            Self::List(_) => String::new(),
        }
    }
}

/// The three-state default argument that reproduces Python's `MISSING` sentinel.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Defaulted {
    /// `default` was not passed (`MISSING`).
    Missing,
    /// `default` was passed explicitly (possibly `None`).
    Provided(Value),
}

fn inline_comment_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\s+;.*$").expect("valid inline-comment regex"))
}

/// Port of `ProjectConfigBase.parse_multi_values`.
///
/// Splits a multi-value option into cleaned items: split on `"\n"` (if present)
/// else `", "`, strip each, drop blank / `;` / `#` lines, and strip inline
/// `;...` comments via `INLINE_COMMENT_RE`.
#[must_use]
pub fn parse_multi_values(value: &Value) -> Vec<String> {
    if value.is_falsy() {
        return Vec::new();
    }
    let raw_items: Vec<String> = match value {
        Value::List(items) => items.iter().map(Value::to_plain_string).collect(),
        other => {
            let s = other.to_plain_string();
            let sep = if s.contains('\n') { "\n" } else { ", " };
            s.split(sep).map(str::to_string).collect()
        }
    };

    let mut result = Vec::new();
    for item in raw_items {
        let item = item.trim();
        if item.is_empty() || item.starts_with(';') || item.starts_with('#') {
            continue;
        }
        let cleaned = if item.contains(';') {
            inline_comment_re().replace(item, "").trim().to_string()
        } else {
            item.to_string()
        };
        result.push(cleaned);
    }
    result
}
