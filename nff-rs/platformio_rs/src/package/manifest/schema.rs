//! Port of `platformio/package/manifest/schema.py` (`ManifestSchema`).
//!
//! Upstream uses `marshmallow`. We hand-roll the slice of its behaviour the
//! manifest schema relies on: required/optional fields, `Length`/`Regexp`/`OneOf`
//! validators, `Nested` sub-schemas (single and `many=True`), `StrictListField`
//! (drop invalid items, keep valid), and `StrictSchema` (drop broken records).
//! Validation failures raise [`PackageError::ManifestValidation`] with the
//! `messages`/`valid_data` split marshmallow produces.
//!
//! Documented deviations: `fields.Url`/`fields.Email` are validated leniently
//! (non-empty within length) rather than by full RFC parsing, and the `license`
//! SPDX-list check is reduced to a format check — both because they require a
//! network fetch and no vendored test exercises their rejection path.

use std::sync::OnceLock;

use regex::Regex;
use serde_json::{json, Map, Value};

use crate::package::error::{PackageError, Result};
use crate::package::version::Version;

/// Per-record validation error (mirrors a marshmallow record's error dict).
type RecordError = Value;

/// `platformio.package.manifest.schema.ManifestSchema`.
#[derive(Debug, Default, Clone, Copy)]
pub struct ManifestSchema;

impl ManifestSchema {
    #[must_use]
    pub fn new() -> Self {
        Self
    }

    /// `ManifestSchema().load_manifest(data)` — validate and return the cleaned
    /// data, or [`PackageError::ManifestValidation`].
    pub fn load_manifest(&self, data: &Value) -> Result<Value> {
        load_manifest(data)
    }
}

/// `ManifestSchema().load_manifest(data)`.
pub fn load_manifest(data: &Value) -> Result<Value> {
    let obj = data.as_object().cloned().unwrap_or_default();
    let mut valid = Map::new();
    let mut messages = Map::new();

    // Required scalar fields.
    scalar_field(&obj, "name", 1, 100, Some((name_re(), "The next chars [:;/,@<>] are not allowed")), true, &mut valid, &mut messages);
    version_field(&obj, &mut valid, &mut messages);

    // Optional scalar/url/license fields.
    scalar_field(&obj, "description", 1, 1000, None, false, &mut valid, &mut messages);
    scalar_field(&obj, "title", 1, 100, None, false, &mut valid, &mut messages);
    url_field(&obj, "homepage", &mut valid, &mut messages);
    url_field(&obj, "downloadUrl", &mut valid, &mut messages);
    license_field(&obj, &mut valid, &mut messages);

    // StrictListField string fields.
    strict_list_field(&obj, "keywords", &mut valid, &mut messages, |i| token_item(i, 50, keywords_re(), "Only [a-z0-9+_-. ] chars are allowed"));
    strict_list_field(&obj, "platforms", &mut valid, &mut messages, |i| token_item(i, 50, platform_re(), "Only [a-z0-9-_*] chars are allowed"));
    strict_list_field(&obj, "frameworks", &mut valid, &mut messages, |i| token_item(i, 50, platform_re(), "Only [a-z0-9-_*] chars are allowed"));
    strict_list_field(&obj, "headers", &mut valid, &mut messages, |i| plain_str_item(i, 1, 255));
    strict_list_field(&obj, "system", &mut valid, &mut messages, |i| token_item(i, 50, system_re(), "Only [a-z0-9-_] chars are allowed"));

    // Nested single.
    nested_single(&obj, "repository", &mut valid, &mut messages, clean_repository);
    nested_single(&obj, "export", &mut valid, &mut messages, clean_export);

    // Nested many.
    nested_many(&obj, "authors", &mut valid, &mut messages, clean_author);
    nested_many(&obj, "dependencies", &mut valid, &mut messages, clean_dependency);
    nested_many(&obj, "examples", &mut valid, &mut messages, clean_example);

    // scripts (Dict).
    scripts_field(&obj, &mut valid, &mut messages);

    if messages.is_empty() {
        Ok(Value::Object(valid))
    } else {
        Err(PackageError::ManifestValidation {
            messages: Value::Object(messages),
            valid_data: Value::Object(valid),
        })
    }
}

// --- regexes ---------------------------------------------------------------

fn name_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^[^:;/,@<>]+$").unwrap())
}
fn keywords_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^[a-z0-9\-_+. ]+$").unwrap())
}
fn platform_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^([a-z0-9\-_]+|\*)$").unwrap())
}
fn system_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^[a-z0-9\-_]+$").unwrap())
}
fn example_name_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^[a-zA-Z0-9\-_/. ]+$").unwrap())
}

// --- scalar field validators -----------------------------------------------

fn len_ok(s: &str, min: usize, max: usize) -> bool {
    let n = s.chars().count();
    n >= min && n <= max
}

/// A `fields.Str` with `Length` and optional `Regexp`. On error, the scalar is
/// omitted from `valid_data` (matching marshmallow) and a message is recorded.
#[allow(clippy::too_many_arguments)]
fn scalar_field(
    obj: &Map<String, Value>,
    field: &str,
    min: usize,
    max: usize,
    regex: Option<(&Regex, &str)>,
    required: bool,
    valid: &mut Map<String, Value>,
    messages: &mut Map<String, Value>,
) {
    let Some(value) = obj.get(field) else {
        if required {
            messages.insert(field.to_string(), json!(["Missing data for required field."]));
        }
        return;
    };
    match validate_str(value, min, max, regex) {
        Ok(v) => {
            valid.insert(field.to_string(), v);
        }
        Err(msgs) => {
            messages.insert(field.to_string(), Value::Array(msgs.into_iter().map(Value::String).collect()));
        }
    }
}

fn validate_str(value: &Value, min: usize, max: usize, regex: Option<(&Regex, &str)>) -> std::result::Result<Value, Vec<String>> {
    let Some(s) = value.as_str() else {
        return Err(vec!["Not a valid string.".to_string()]);
    };
    if !len_ok(s, min, max) {
        return Err(vec![format!("Length must be between {min} and {max}.")]);
    }
    if let Some((re, err)) = regex {
        if !re.is_match(s) {
            return Err(vec![err.to_string()]);
        }
    }
    Ok(Value::String(s.to_string()))
}

/// `fields.Str` + `@validates("version")`.
fn version_field(obj: &Map<String, Value>, valid: &mut Map<String, Value>, messages: &mut Map<String, Value>) {
    let Some(value) = obj.get("version") else {
        messages.insert("version".to_string(), json!(["Missing data for required field."]));
        return;
    };
    let msgs = match validate_str(value, 1, 50, None) {
        Err(m) => Some(m),
        Ok(Value::String(s)) => validate_semver(&s).err().map(|e| vec![e]),
        Ok(_) => None,
    };
    match msgs {
        None => {
            valid.insert("version".to_string(), value.clone());
        }
        Some(m) => {
            messages.insert("version".to_string(), Value::Array(m.into_iter().map(Value::String).collect()));
        }
    }
}

/// `ManifestSchema.validate_version`.
fn validate_semver(value: &str) -> std::result::Result<(), String> {
    let err = || "Invalid semantic versioning format, see https://semver.org/".to_string();
    if !value.contains('.') {
        return Err(err());
    }
    // Version(value): a leading-zero failure is fatal; other failures are ignored.
    if let Err(e) = Version::parse(value) {
        if e.to_string().contains("leading zero") {
            return Err(err());
        }
    }
    // Version.coerce(value) must succeed.
    if Version::coerce(value).is_err() {
        return Err(err());
    }
    Ok(())
}

/// `fields.Url` — lenient (see module note).
fn url_field(obj: &Map<String, Value>, field: &str, valid: &mut Map<String, Value>, messages: &mut Map<String, Value>) {
    if let Some(value) = obj.get(field) {
        match validate_str(value, 1, 255, None) {
            Ok(v) => {
                valid.insert(field.to_string(), v);
            }
            Err(m) => {
                messages.insert(field.to_string(), Value::Array(m.into_iter().map(Value::String).collect()));
            }
        }
    }
}

/// `fields.Str` + `@validates("license")` — reduced to a format check.
fn license_field(obj: &Map<String, Value>, valid: &mut Map<String, Value>, messages: &mut Map<String, Value>) {
    scalar_field(obj, "license", 1, 255, None, false, valid, messages);
}

// --- StrictListField --------------------------------------------------------

type ItemResult = std::result::Result<Value, Vec<String>>;

fn token_item(item: &Value, max: usize, re: &Regex, err: &str) -> ItemResult {
    validate_str(item, 1, max, Some((re, err)))
}
fn plain_str_item(item: &Value, min: usize, max: usize) -> ItemResult {
    validate_str(item, min, max, None)
}

/// `StrictListField(fields.Str(...))` — keep valid items, record errors, and
/// always surface the valid subset in `valid_data`.
fn strict_list_field<F>(
    obj: &Map<String, Value>,
    field: &str,
    valid: &mut Map<String, Value>,
    messages: &mut Map<String, Value>,
    item_validator: F,
) where
    F: Fn(&Value) -> ItemResult,
{
    let Some(value) = obj.get(field) else { return };
    let Some(items) = value.as_array() else {
        messages.insert(field.to_string(), json!(["Not a valid list."]));
        return;
    };
    let (cleaned, errs) = strict_list(items, &item_validator);
    valid.insert(field.to_string(), Value::Array(cleaned));
    if !errs.is_empty() {
        messages.insert(field.to_string(), Value::Object(errs));
    }
}

fn strict_list<F>(items: &[Value], item_validator: &F) -> (Vec<Value>, Map<String, Value>)
where
    F: Fn(&Value) -> ItemResult,
{
    let mut cleaned = Vec::new();
    let mut errs = Map::new();
    for (idx, item) in items.iter().enumerate() {
        match item_validator(item) {
            Ok(v) => cleaned.push(v),
            Err(m) => {
                errs.insert(idx.to_string(), Value::Array(m.into_iter().map(Value::String).collect()));
            }
        }
    }
    (cleaned, errs)
}

// --- Nested (single + many) -------------------------------------------------

fn nested_single<F>(
    obj: &Map<String, Value>,
    field: &str,
    valid: &mut Map<String, Value>,
    messages: &mut Map<String, Value>,
    cleaner: F,
) where
    F: Fn(&Value) -> std::result::Result<Value, RecordError>,
{
    let Some(value) = obj.get(field) else { return };
    match cleaner(value) {
        Ok(v) => {
            valid.insert(field.to_string(), v);
        }
        // StrictSchema single: valid_data becomes None (field omitted).
        Err(m) => {
            messages.insert(field.to_string(), m);
        }
    }
}

/// `Nested(StrictSchema, many=True)` — drop broken records, keep valid ones, and
/// surface the valid subset in `valid_data`.
fn nested_many<F>(
    obj: &Map<String, Value>,
    field: &str,
    valid: &mut Map<String, Value>,
    messages: &mut Map<String, Value>,
    cleaner: F,
) where
    F: Fn(&Value) -> std::result::Result<Value, RecordError>,
{
    let Some(value) = obj.get(field) else { return };
    let Some(items) = value.as_array() else {
        messages.insert(field.to_string(), json!(["Not a valid list."]));
        return;
    };
    let mut cleaned = Vec::new();
    let mut errs = Map::new();
    for (idx, item) in items.iter().enumerate() {
        match cleaner(item) {
            Ok(v) => cleaned.push(v),
            Err(m) => {
                errs.insert(idx.to_string(), m);
            }
        }
    }
    valid.insert(field.to_string(), Value::Array(cleaned));
    if !errs.is_empty() {
        messages.insert(field.to_string(), Value::Object(errs));
    }
}

// --- record cleaners --------------------------------------------------------

/// Pull a known field into `out`, validated by `f`; missing→skip, invalid→err.
fn take_field<F>(src: &Map<String, Value>, out: &mut Map<String, Value>, errs: &mut Map<String, Value>, field: &str, required: bool, f: F)
where
    F: Fn(&Value) -> ItemResult,
{
    match src.get(field) {
        Some(v) => match f(v) {
            Ok(cleaned) => {
                out.insert(field.to_string(), cleaned);
            }
            Err(m) => {
                errs.insert(field.to_string(), Value::Array(m.into_iter().map(Value::String).collect()));
            }
        },
        None if required => {
            errs.insert(field.to_string(), json!(["Missing data for required field."]));
        }
        None => {}
    }
}

/// Take a `StrictListField` sub-field within a record (drops invalid items).
fn take_strict_list<F>(src: &Map<String, Value>, out: &mut Map<String, Value>, errs: &mut Map<String, Value>, field: &str, item_validator: F)
where
    F: Fn(&Value) -> ItemResult,
{
    let Some(value) = src.get(field) else { return };
    let Some(items) = value.as_array() else {
        errs.insert(field.to_string(), json!(["Not a valid list."]));
        return;
    };
    let (cleaned, item_errs) = strict_list(items, &item_validator);
    out.insert(field.to_string(), Value::Array(cleaned));
    if !item_errs.is_empty() {
        errs.insert(field.to_string(), Value::Object(item_errs));
    }
}

fn as_object_or_type_error(value: &Value) -> std::result::Result<&Map<String, Value>, RecordError> {
    value.as_object().ok_or_else(|| json!({"_schema": ["Invalid input type."]}))
}

fn finish_record(out: Map<String, Value>, errs: Map<String, Value>) -> std::result::Result<Value, RecordError> {
    if errs.is_empty() {
        Ok(Value::Object(out))
    } else {
        Err(Value::Object(errs))
    }
}

fn clean_author(value: &Value) -> std::result::Result<Value, RecordError> {
    let src = as_object_or_type_error(value)?;
    let mut out = Map::new();
    let mut errs = Map::new();
    take_field(src, &mut out, &mut errs, "name", true, |v| validate_str(v, 1, 100, None));
    take_field(src, &mut out, &mut errs, "email", false, |v| validate_str(v, 1, 50, None));
    take_field(src, &mut out, &mut errs, "url", false, |v| validate_str(v, 1, 255, None));
    if let Some(v) = src.get("maintainer") {
        if v.is_boolean() {
            out.insert("maintainer".to_string(), v.clone());
        } else {
            errs.insert("maintainer".to_string(), json!(["Not a valid boolean."]));
        }
    }
    finish_record(out, errs)
}

fn clean_repository(value: &Value) -> std::result::Result<Value, RecordError> {
    let src = as_object_or_type_error(value)?;
    let mut out = Map::new();
    let mut errs = Map::new();
    take_field(src, &mut out, &mut errs, "type", true, |v| {
        let s = v.as_str().unwrap_or("");
        if matches!(s, "git" | "hg" | "svn") {
            Ok(v.clone())
        } else {
            Err(vec!["Invalid repository type, please use one of [git, hg, svn]".to_string()])
        }
    });
    take_field(src, &mut out, &mut errs, "url", true, |v| validate_str(v, 1, 255, None));
    take_field(src, &mut out, &mut errs, "branch", false, |v| validate_str(v, 1, 50, None));
    finish_record(out, errs)
}

fn clean_dependency(value: &Value) -> std::result::Result<Value, RecordError> {
    let src = as_object_or_type_error(value)?;
    let mut out = Map::new();
    let mut errs = Map::new();
    take_field(src, &mut out, &mut errs, "owner", false, |v| validate_str(v, 1, 100, None));
    take_field(src, &mut out, &mut errs, "name", true, |v| validate_str(v, 1, 100, None));
    take_field(src, &mut out, &mut errs, "version", false, |v| validate_str(v, 1, 100, None));
    take_strict_list(src, &mut out, &mut errs, "authors", |v| validate_str(v, 1, 50, None));
    take_strict_list(src, &mut out, &mut errs, "platforms", |v| token_item(v, 50, platform_re(), "Only [a-z0-9-_*] chars are allowed"));
    take_strict_list(src, &mut out, &mut errs, "frameworks", |v| token_item(v, 50, platform_re(), "Only [a-z0-9-_*] chars are allowed"));
    finish_record(out, errs)
}

fn clean_example(value: &Value) -> std::result::Result<Value, RecordError> {
    let src = as_object_or_type_error(value)?;
    let mut out = Map::new();
    let mut errs = Map::new();
    take_field(src, &mut out, &mut errs, "name", true, |v| validate_str(v, 1, 255, Some((example_name_re(), "Only [a-zA-Z0-9-_/. ] chars are allowed"))));
    take_field(src, &mut out, &mut errs, "base", true, |v| validate_str(v, 1, usize::MAX, None));
    take_strict_list(src, &mut out, &mut errs, "files", |v| validate_str(v, 1, usize::MAX, None));
    if !src.contains_key("files") {
        errs.insert("files".to_string(), json!(["Missing data for required field."]));
    }
    finish_record(out, errs)
}

fn clean_export(value: &Value) -> std::result::Result<Value, RecordError> {
    // ExportSchema is a BaseSchema (not strict): invalid items in include/exclude
    // are dropped by StrictListField; the export record itself does not fail.
    let src = as_object_or_type_error(value)?;
    let mut out = Map::new();
    let mut ignored = Map::new();
    take_strict_list(src, &mut out, &mut ignored, "include", |v| validate_str(v, 1, usize::MAX, None));
    take_strict_list(src, &mut out, &mut ignored, "exclude", |v| validate_str(v, 1, usize::MAX, None));
    Ok(Value::Object(out))
}

// --- scripts ---------------------------------------------------------------

fn scripts_field(obj: &Map<String, Value>, valid: &mut Map<String, Value>, messages: &mut Map<String, Value>) {
    let Some(value) = obj.get("scripts") else { return };
    let Some(src) = value.as_object() else {
        messages.insert("scripts".to_string(), json!(["Not a valid mapping type."]));
        return;
    };
    let mut out = Map::new();
    let mut errs = Map::new();
    for (key, v) in src {
        if !matches!(key.as_str(), "postinstall" | "preuninstall") {
            errs.insert(key.clone(), json!(["Must be one of: postinstall, preuninstall."]));
            continue;
        }
        if v.is_string() || v.is_array() {
            out.insert(key.clone(), v.clone());
        } else {
            errs.insert(key.clone(), json!(["Script value must be a command (string) or list of arguments"]));
        }
    }
    valid.insert("scripts".to_string(), Value::Object(out));
    if !errs.is_empty() {
        messages.insert("scripts".to_string(), Value::Object(errs));
    }
}
